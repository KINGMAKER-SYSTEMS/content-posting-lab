//! Parser for `rust/contract/CUTLIST.md`.
//!
//! The cutlist buckets every path from the frozen `openapi.json` into one of:
//! `KEEP`, `KEEP (mini app)`, `KEEP (external client)`, `PROXY`, or one of
//! several `CUT (...)` reasons. Entries look like:
//!
//! ```md
//! ## KEEP — 72
//!
//! - `GET /api/burn/batch-status/{batch_id}`
//! - `GET,POST /api/projects/`
//! - `POST,DELETE /api/clipper/cookies`
//! ```
//!
//! A single bullet can carry a comma-separated method list sharing one path.
//! We only care about the three KEEP buckets for the harness — everything
//! else (PROXY / CUT) is parsed too (for completeness / future use) but is
//! not exercised by `single` or `diff`.

use std::collections::BTreeSet;

/// Which bucket a path/method entry fell into.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Bucket {
    Keep,
    KeepMiniApp,
    KeepExternalClient,
    Proxy,
    Cut,
}

impl Bucket {
    /// True for any of the three KEEP buckets — the Rust parity target.
    pub fn is_keep(self) -> bool {
        matches!(
            self,
            Bucket::Keep | Bucket::KeepMiniApp | Bucket::KeepExternalClient
        )
    }
}

/// One (method, path) endpoint extracted from a cutlist bullet.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Endpoint {
    pub method: String,
    pub path: String,
    pub bucket: Bucket,
}

/// Parsed cutlist: every endpoint with its bucket.
#[derive(Debug, Default)]
pub struct Cutlist {
    pub endpoints: Vec<Endpoint>,
}

impl Cutlist {
    /// Parse the markdown text of CUTLIST.md.
    pub fn parse(text: &str) -> Self {
        let mut endpoints = Vec::new();
        let mut current_bucket: Option<Bucket> = None;

        for raw_line in text.lines() {
            let line = raw_line.trim();

            if let Some(heading) = line.strip_prefix("## ") {
                current_bucket = classify_heading(heading);
                continue;
            }

            if !line.starts_with('-') {
                continue;
            }
            let Some(bucket) = current_bucket else {
                continue;
            };

            // Bullet format: `- \`METHOD[,METHOD...] /path\``
            let Some(entry) = extract_backticked(line) else {
                continue;
            };
            let Some((methods_part, path)) = entry.split_once(' ') else {
                continue;
            };
            let path = path.trim();
            if path.is_empty() {
                continue;
            }

            for method in methods_part.split(',') {
                let method = method.trim();
                if method.is_empty() {
                    continue;
                }
                endpoints.push(Endpoint {
                    method: method.to_ascii_uppercase(),
                    path: path.to_string(),
                    bucket,
                });
            }
        }

        Cutlist { endpoints }
    }

    /// All KEEP-bucket GET endpoints (the safely-idempotent `single` subset).
    pub fn keep_get_paths(&self) -> Vec<&Endpoint> {
        self.endpoints
            .iter()
            .filter(|e| e.bucket.is_keep() && e.method == "GET")
            .collect()
    }

    /// All KEEP-bucket endpoints of any method (used by `--include-mutations`).
    pub fn keep_endpoints(&self) -> Vec<&Endpoint> {
        self.endpoints.iter().filter(|e| e.bucket.is_keep()).collect()
    }

    /// Distinct KEEP paths (methods collapsed) — useful for counting.
    pub fn keep_paths_distinct(&self) -> BTreeSet<&str> {
        self.endpoints
            .iter()
            .filter(|e| e.bucket.is_keep())
            .map(|e| e.path.as_str())
            .collect()
    }
}

fn classify_heading(heading: &str) -> Option<Bucket> {
    // Headings look like "KEEP — 72", "KEEP (mini app) — 3",
    // "KEEP (external client) — 11", "PROXY — 49", "CUT (feature dropped) — 28".
    let name = heading.split('—').next().unwrap_or(heading).trim();
    if name.starts_with("KEEP (mini app)") {
        Some(Bucket::KeepMiniApp)
    } else if name.starts_with("KEEP (external client)") {
        Some(Bucket::KeepExternalClient)
    } else if name.starts_with("KEEP") {
        Some(Bucket::Keep)
    } else if name.starts_with("PROXY") {
        Some(Bucket::Proxy)
    } else if name.starts_with("CUT") {
        Some(Bucket::Cut)
    } else {
        None
    }
}

/// Pull the contents of the first `\`...\`` span in a line.
fn extract_backticked(line: &str) -> Option<&str> {
    let start = line.find('`')? + 1;
    let rest = &line[start..];
    let end = rest.find('`')?;
    Some(&rest[..end])
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
# Endpoint Cut-List

## KEEP — 3

- `GET /api/health`
- `GET,POST /api/projects/`
- `POST,DELETE /api/clipper/cookies`

## KEEP (mini app) — 1

- `GET /api/miniapp/me`

## KEEP (external client) — 1

- `POST /api/burn/import-tos`

## PROXY — 1

- `GET /api/agenticnews/stats`

## CUT (feature dropped) — 1

- `GET /api/slideshow/audio`
"#;

    #[test]
    fn parses_single_method_bullets() {
        let cl = Cutlist::parse(SAMPLE);
        assert!(cl
            .endpoints
            .iter()
            .any(|e| e.method == "GET" && e.path == "/api/health" && e.bucket == Bucket::Keep));
    }

    #[test]
    fn expands_comma_method_bullets() {
        let cl = Cutlist::parse(SAMPLE);
        let projects: Vec<_> = cl
            .endpoints
            .iter()
            .filter(|e| e.path == "/api/projects/")
            .collect();
        assert_eq!(projects.len(), 2);
        assert!(projects.iter().any(|e| e.method == "GET"));
        assert!(projects.iter().any(|e| e.method == "POST"));

        let cookies: Vec<_> = cl
            .endpoints
            .iter()
            .filter(|e| e.path == "/api/clipper/cookies")
            .collect();
        assert_eq!(cookies.len(), 2);
        assert!(cookies.iter().any(|e| e.method == "POST"));
        assert!(cookies.iter().any(|e| e.method == "DELETE"));
    }

    #[test]
    fn classifies_buckets_correctly() {
        let cl = Cutlist::parse(SAMPLE);
        let bucket_of = |path: &str| {
            cl.endpoints
                .iter()
                .find(|e| e.path == path)
                .map(|e| e.bucket)
        };
        assert_eq!(bucket_of("/api/miniapp/me"), Some(Bucket::KeepMiniApp));
        assert_eq!(
            bucket_of("/api/burn/import-tos"),
            Some(Bucket::KeepExternalClient)
        );
        assert_eq!(bucket_of("/api/agenticnews/stats"), Some(Bucket::Proxy));
        assert_eq!(bucket_of("/api/slideshow/audio"), Some(Bucket::Cut));
    }

    #[test]
    fn keep_get_paths_excludes_non_get_and_non_keep() {
        let cl = Cutlist::parse(SAMPLE);
        let gets = cl.keep_get_paths();
        // health + projects GET + miniapp/me + import-tos is POST so excluded
        let paths: BTreeSet<&str> = gets.iter().map(|e| e.path.as_str()).collect();
        assert!(paths.contains("/api/health"));
        assert!(paths.contains("/api/projects/"));
        assert!(paths.contains("/api/miniapp/me"));
        assert!(!paths.contains("/api/burn/import-tos")); // POST only
        assert!(!paths.contains("/api/agenticnews/stats")); // PROXY, not KEEP
        assert!(!paths.contains("/api/slideshow/audio")); // CUT
    }

    #[test]
    fn is_keep_covers_all_three_keep_buckets() {
        assert!(Bucket::Keep.is_keep());
        assert!(Bucket::KeepMiniApp.is_keep());
        assert!(Bucket::KeepExternalClient.is_keep());
        assert!(!Bucket::Proxy.is_keep());
        assert!(!Bucket::Cut.is_keep());
    }
}
