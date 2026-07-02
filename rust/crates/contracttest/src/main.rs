//! contracttest — API contract-parity harness for the content-posting-lab
//! Rust rewrite.
//!
//! This binary replays the frozen API contract (`rust/contract/openapi.json`,
//! 195 paths, classified by `rust/contract/CUTLIST.md`) against a running
//! server, and — for the wave-4 shadow run — against two servers at once
//! (Rust candidate vs Python incumbent), reporting any structural drift.
//!
//! ## Subcommands
//!
//! ```text
//! contracttest single --base http://127.0.0.1:8000 [--include-mutations]
//! contracttest diff --rust http://127.0.0.1:8000 --python http://127.0.0.1:8001 [--fixtures fixtures.json]
//! ```
//!
//! ### `single`
//!
//! Hits every KEEP-bucket GET endpoint (from CUTLIST.md's three KEEP
//! sections) against one server. Each is reported as:
//!   - `OK` — 2xx, and (if openapi declared a response schema) all expected
//!     top-level fields were present.
//!   - `MISSING` — server returned 404 where the contract expects a route.
//!   - `SHAPE-MISMATCH` — 2xx but missing declared top-level fields.
//!   - `ERROR` — any other non-2xx status, or a transport failure.
//!   - `SKIPPED` — path has `{param}` segments and no fixture value was
//!     supplied (see `--fixtures` below).
//!
//! GET-only by default — this tool will never POST/DELETE/PATCH against a
//! server unless `--include-mutations` is passed, and even then only against
//! whatever `--base` you point it at (make that a throwaway instance).
//!
//! ### `diff`
//!
//! For each KEEP GET path, requests both `--rust` and `--python` base URLs,
//! normalizes each JSON body (see `normalize.rs` — volatile fields like
//! timestamps/ids are stripped down to "present or not", array ordering
//! doesn't matter), and reports any path whose *structure* (key sets / JSON
//! types) differs between the two servers. This is the shadow-run workhorse
//! for wave 4.
//!
//! ### Fixtures
//!
//! Parameterized paths (`{name}` segments) are skipped by default — reported
//! as `SKIPPED (needs param)` with a clear count, per the v1 scope agreed for
//! this workstream. Pass `--fixtures path/to/fixtures.json` — a flat JSON
//! object mapping param name -> sample string value, e.g.
//! `{"batch_id": "demo-batch", "job_id": "demo-job"}` — to substitute values
//! and actually exercise those routes instead of skipping them.
//!
//! ## Exit code
//!
//! Non-zero if `single` finds any KEEP endpoint MISSING, or `diff` finds any
//! structural mismatch — so CI can gate on either.

mod check;
mod cutlist;
mod normalize;
mod openapi;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use serde_json::Value;

use check::{classify_response, Outcome};
use cutlist::{Cutlist, Endpoint};
use openapi::{has_path_params, substitute_path_params, OpenApiSpec};

#[derive(Parser)]
#[command(name = "contracttest", about = "API contract-parity harness")]
struct Cli {
    /// Path to openapi.json (defaults to rust/contract/openapi.json relative
    /// to the workspace root, resolved from CARGO_MANIFEST_DIR).
    #[arg(long, global = true)]
    openapi: Option<PathBuf>,

    /// Path to CUTLIST.md (defaults similarly).
    #[arg(long, global = true)]
    cutlist: Option<PathBuf>,

    /// JSON file mapping path-param name -> sample value, for exercising
    /// parameterized paths instead of skipping them.
    #[arg(long, global = true)]
    fixtures: Option<PathBuf>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Hit every KEEP GET endpoint against one server.
    Single {
        /// Base URL of the server under test.
        #[arg(long)]
        base: String,

        /// Also exercise POST/PATCH/DELETE KEEP endpoints with synthesized
        /// empty-ish bodies. Only use this against a THROWAWAY server — it
        /// performs real mutations (sends, deletes, syncs...).
        #[arg(long, default_value_t = false)]
        include_mutations: bool,
    },
    /// Diff every KEEP GET endpoint's response shape between two servers.
    Diff {
        /// Base URL of the Rust candidate server.
        #[arg(long)]
        rust: String,

        /// Base URL of the Python incumbent server.
        #[arg(long)]
        python: String,
    },
}

fn default_contract_path(file: &str) -> PathBuf {
    // CARGO_MANIFEST_DIR = rust/crates/contracttest -> ../../contract/<file>
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../contract")
        .join(file)
}

fn load_cutlist(path: &Option<PathBuf>) -> Result<Cutlist> {
    let path = path
        .clone()
        .unwrap_or_else(|| default_contract_path("CUTLIST.md"));
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("reading cutlist at {}", path.display()))?;
    Ok(Cutlist::parse(&text))
}

fn load_openapi(path: &Option<PathBuf>) -> Result<OpenApiSpec> {
    let path = path
        .clone()
        .unwrap_or_else(|| default_contract_path("openapi.json"));
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("reading openapi spec at {}", path.display()))?;
    let value: Value =
        serde_json::from_str(&text).with_context(|| "openapi.json is not valid JSON")?;
    Ok(OpenApiSpec::parse(&value))
}

fn load_fixtures(path: &Option<PathBuf>) -> Result<BTreeMap<String, String>> {
    let Some(path) = path else {
        return Ok(BTreeMap::new());
    };
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("reading fixtures at {}", path.display()))?;
    let map: BTreeMap<String, String> =
        serde_json::from_str(&text).with_context(|| "fixtures file is not a flat JSON object")?;
    Ok(map)
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let cutlist = load_cutlist(&cli.cutlist)?;
    let spec = load_openapi(&cli.openapi)?;
    let fixtures = load_fixtures(&cli.fixtures)?;

    let exit_ok = match cli.command {
        Command::Single {
            base,
            include_mutations,
        } => run_single(&cutlist, &spec, &fixtures, &base, include_mutations).await?,
        Command::Diff { rust, python } => run_diff(&cutlist, &fixtures, &rust, &python).await?,
    };

    if !exit_ok {
        std::process::exit(1);
    }
    Ok(())
}

fn resolve_target(endpoint: &Endpoint, fixtures: &BTreeMap<String, String>) -> Result<String, ()> {
    if has_path_params(&endpoint.path) {
        substitute_path_params(&endpoint.path, fixtures).ok_or(())
    } else {
        Ok(endpoint.path.clone())
    }
}

/// Returns Ok(true) if no MISSING endpoints were found (success for CI gating).
async fn run_single(
    cutlist: &Cutlist,
    spec: &OpenApiSpec,
    fixtures: &BTreeMap<String, String>,
    base: &str,
    include_mutations: bool,
) -> Result<bool> {
    let client = reqwest::Client::new();
    let base = base.trim_end_matches('/');

    let endpoints: Vec<&Endpoint> = if include_mutations {
        cutlist.keep_endpoints()
    } else {
        cutlist.keep_get_paths()
    };

    println!(
        "contracttest single — base={base} endpoints={} distinct-keep-paths={} (include_mutations={include_mutations})",
        endpoints.len(),
        cutlist.keep_paths_distinct().len()
    );
    println!("{:-<90}", "");

    let mut counts: BTreeMap<&'static str, usize> = BTreeMap::new();
    let mut any_missing = false;

    for endpoint in &endpoints {
        let resolved = resolve_target(endpoint, fixtures);
        let Ok(path) = resolved else {
            let outcome = Outcome::SkippedNeedsParam;
            *counts.entry(outcome.label()).or_default() += 1;
            println!(
                "{:<8} {:<60} {} (needs param, no fixture)",
                endpoint.method,
                endpoint.path,
                outcome.label()
            );
            continue;
        };

        let url = format!("{base}{path}");
        let req = client.request(endpoint.method.parse().unwrap_or(reqwest::Method::GET), &url);

        let outcome = match req.send().await {
            Ok(resp) => {
                let status = resp.status().as_u16();
                let body_bytes = resp.bytes().await.ok();
                let body: Option<Value> = body_bytes
                    .as_deref()
                    .and_then(|b| serde_json::from_slice(b).ok());
                let info = spec.get(&endpoint.path, &endpoint.method).cloned();
                classify_response(status, body.as_ref(), &info.unwrap_or_default())
            }
            Err(e) => Outcome::Error {
                detail: e.to_string(),
            },
        };

        if matches!(outcome, Outcome::Missing) {
            any_missing = true;
        }
        *counts.entry(outcome.label()).or_default() += 1;

        let detail = match &outcome {
            Outcome::ShapeMismatch { missing_fields } => {
                format!(" (missing: {})", missing_fields.join(", "))
            }
            Outcome::Error { detail } => format!(" ({detail})"),
            _ => String::new(),
        };
        println!(
            "{:<8} {:<60} {}{}",
            endpoint.method,
            endpoint.path,
            outcome.label(),
            detail
        );
    }

    println!("{:-<90}", "");
    println!("Summary:");
    for label in ["OK", "MISSING", "SHAPE-MISMATCH", "ERROR", "SKIPPED"] {
        println!("  {label:<16} {}", counts.get(label).copied().unwrap_or(0));
    }

    Ok(!any_missing)
}

/// Returns Ok(true) if no structural mismatches were found.
async fn run_diff(
    cutlist: &Cutlist,
    fixtures: &BTreeMap<String, String>,
    rust_base: &str,
    python_base: &str,
) -> Result<bool> {
    let client = reqwest::Client::new();
    let rust_base = rust_base.trim_end_matches('/');
    let python_base = python_base.trim_end_matches('/');

    let endpoints = cutlist.keep_get_paths();
    println!(
        "contracttest diff — rust={rust_base} python={python_base} endpoints={}",
        endpoints.len()
    );
    println!("{:-<90}", "");

    let mut ok_count = 0;
    let mut mismatch_count = 0;
    let mut skipped_count = 0;
    let mut error_count = 0;
    let mut any_mismatch = false;

    for endpoint in &endpoints {
        let resolved = resolve_target(endpoint, fixtures);
        let Ok(path) = resolved else {
            skipped_count += 1;
            println!(
                "{:<8} {:<60} SKIPPED (needs param, no fixture)",
                endpoint.method, endpoint.path
            );
            continue;
        };

        let rust_url = format!("{rust_base}{path}");
        let python_url = format!("{python_base}{path}");

        let (rust_result, python_result) = tokio::join!(
            fetch_json(&client, &rust_url),
            fetch_json(&client, &python_url)
        );

        match (rust_result, python_result) {
            (Ok(r), Ok(p)) => {
                let diffs = normalize::diff_responses(&r, &p);
                if diffs.is_empty() {
                    ok_count += 1;
                    println!("{:<8} {:<60} MATCH", endpoint.method, endpoint.path);
                } else {
                    mismatch_count += 1;
                    any_mismatch = true;
                    println!(
                        "{:<8} {:<60} STRUCTURAL-MISMATCH ({} diffs)",
                        endpoint.method,
                        endpoint.path,
                        diffs.len()
                    );
                    for d in &diffs {
                        println!("           - {} : {:?}", d.path, d.kind);
                    }
                }
            }
            (Err(e), _) => {
                error_count += 1;
                println!(
                    "{:<8} {:<60} ERROR (rust: {e})",
                    endpoint.method, endpoint.path
                );
            }
            (_, Err(e)) => {
                error_count += 1;
                println!(
                    "{:<8} {:<60} ERROR (python: {e})",
                    endpoint.method, endpoint.path
                );
            }
        }
    }

    println!("{:-<90}", "");
    println!("Summary:");
    println!("  MATCH               {ok_count}");
    println!("  STRUCTURAL-MISMATCH {mismatch_count}");
    println!("  ERROR               {error_count}");
    println!("  SKIPPED             {skipped_count}");

    Ok(!any_mismatch)
}

async fn fetch_json(client: &reqwest::Client, url: &str) -> Result<Value> {
    let resp = client.get(url).send().await?;
    let status = resp.status();
    let bytes = resp.bytes().await?;
    if !status.is_success() {
        anyhow::bail!("HTTP {status}");
    }
    let value: Value = serde_json::from_slice(&bytes)
        .with_context(|| format!("response from {url} was not valid JSON"))?;
    Ok(value)
}
