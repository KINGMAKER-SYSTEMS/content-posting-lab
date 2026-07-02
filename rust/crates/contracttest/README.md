# contracttest

API contract-parity harness for the content-posting-lab Rust rewrite. Standalone
CLI — not wired into the server. It reads the frozen API contract
(`rust/contract/openapi.json`, 195 paths) and its classification
(`rust/contract/CUTLIST.md`: KEEP / KEEP (mini app) / KEEP (external client) /
PROXY / CUT) and replays the KEEP subset against one or two running servers.

This is the tool the wave-4 shadow run drives: point `diff` at the Rust
candidate and the Python incumbent side by side and it reports every response
whose *structure* differs.

## Subcommands

### `single` — smoke-test one server

```sh
cargo run -p contracttest -- single --base http://127.0.0.1:8000
```

Hits every KEEP-bucket **GET** endpoint (safely idempotent — this is the
default and only mode unless you opt in below) and classifies each response:

- `OK` — 2xx, and (if openapi.json declared a response schema for this
  operation) all expected top-level fields were present in the body.
- `MISSING` — server returned 404 where the contract expects a route to exist.
- `SHAPE-MISMATCH` — 2xx but missing one or more declared top-level fields.
- `ERROR` — any other non-2xx status, or a request/transport failure.
- `SKIPPED` — the path has `{param}` segments and no fixture value was
  supplied for them (see Fixtures below).

Prints a per-endpoint line plus a summary table. **Exits non-zero if any KEEP
endpoint is MISSING** — wire this into CI as a gate.

To also exercise mutating KEEP endpoints (POST/PUT/PATCH/DELETE) with
synthesized empty-ish JSON bodies:

```sh
cargo run -p contracttest -- single --base http://127.0.0.1:8000 --include-mutations
```

Only ever point `--include-mutations` at a **throwaway** server — it performs
real sends/deletes/syncs against whatever it's plugged into.

### `diff` — shadow-run two servers

```sh
cargo run -p contracttest -- diff --rust http://127.0.0.1:8000 --python http://127.0.0.1:8001
```

For each KEEP GET path, requests both servers, normalizes each JSON body
(volatile fields collapsed — see Normalization below), and reports any path
whose *structure* (key sets / JSON value types) differs. Outcomes:

- `MATCH` — same shape.
- `STRUCTURAL-MISMATCH` — one or more field diffs, printed with a dotted JSON
  path (e.g. `$.data.items[].sound_id`) and whether the key is missing on the
  Rust or Python side, or has a type mismatch.
- `ERROR` — one side didn't return 2xx JSON.
- `SKIPPED` — parameterized path, no fixture supplied.

**Exits non-zero if any structural mismatch is found.**

## How expected fields are derived from openapi.json

For each `(path, method)`, the harness reads
`paths.<path>.<method>.responses.200.content.application/json.schema`:

- If the schema is `{}` (no `response_model` was declared in FastAPI — true
  for the vast majority of routes in this contract), there are no expected
  fields and the check degrades to **status-only** (2xx / 404 / other).
- If the schema is a `$ref` to `components.schemas.<Name>`, it's resolved one
  level and the target's `properties` keys become the expected field set.
- If the schema is `type: array` with an `items.$ref`, the same resolution
  happens against the item schema, and array responses are checked against
  their **first element** (empty arrays are vacuously OK — nothing to check).

This logic lives in `src/openapi.rs` (`OpenApiSpec::parse`,
`resolve_schema_fields`) and is unit-tested with inline fixtures modeling both
the empty-schema and `$ref`-resolved cases actually present in the real spec
(confirmed against `/api/health` and `/api/pipeline/mint-alias` while
building this).

## How `diff` normalization works

`src/normalize.rs` turns a JSON value into a `Shape` tree: objects become
sorted `(key, Shape)` pairs, arrays collapse to the shape of their first
element (ignoring order/count), and any key that looks like an id or a
timestamp (`*_id`, `*_at`, `id`, `uuid`, `timestamp`, `created_at`, ...) has
its *value* collapsed to `Volatile` while its *presence* still counts. Two
shapes are then diffed key-by-key, recursively, producing a flat list of
`ShapeDiff` (missing key on one side, or a type mismatch) with a dotted path
breadcrumb. This means: different ids, different timestamps, and different
array ordering never trigger a mismatch — but a genuinely missing/renamed
field, or a field that changed JSON type, does.

## What gets skipped

Any path with a `{param}` path-template segment is skipped by default in both
`single` and `diff`, reported as `SKIPPED (needs param, no fixture)` with a
clear count in the summary — this was the agreed v1 scope (parameterized-path
coverage was explicitly optional). To exercise them anyway, pass
`--fixtures path/to/fixtures.json`: a flat JSON object mapping param name to a
sample string value, e.g.

```json
{ "batch_id": "demo-batch", "job_id": "demo-job", "poster_id": "demo-poster" }
```

Every `{name}` segment in a path is substituted from this map; if any segment
has no entry the path is still skipped.

## CUTLIST.md parsing

`src/cutlist.rs` parses the `## <BUCKET> — N` headings and `` - `METHOD[,METHOD] /path` `` bullets. A bullet with a comma-separated method list
(e.g. `` `GET,POST /api/projects/` ``) expands into one `Endpoint` per method,
all sharing the bucket from the enclosing heading. Only the three KEEP
buckets (`KEEP`, `KEEP (mini app)`, `KEEP (external client)`) are exercised by
`single`/`diff`; PROXY and CUT entries are parsed (for completeness / future
tooling) but never requested.

## Running the tests

```sh
cargo test -p contracttest
```

29 unit tests, all against inline fixtures — **no live server required**.
Covers: cutlist parsing (single + comma-method bullets, bucket
classification, KEEP-GET filtering), openapi schema-field extraction (empty
schema, `$ref` resolution, array-of-`$ref` resolution, path-template
parsing/substitution), response classification (`single`'s OK / MISSING /
SHAPE-MISMATCH / ERROR logic), and normalization/diff (volatile-key handling,
missing keys on either side, type mismatches, nested/array paths, key-order
independence).

## Verifying

```sh
cargo build -p contracttest
cargo clippy -p contracttest --all-targets   # zero warnings
cargo test -p contracttest
```

## Pointing it at the real Rust server

Once the Rust Axum server is up (wave 3+):

```sh
# Smoke-test the Rust server alone
cargo run -p contracttest -- single --base http://127.0.0.1:8000

# Shadow-run: Rust candidate vs Python incumbent, same contract
cargo run -p contracttest -- diff \
  --rust   http://127.0.0.1:8000 \
  --python http://127.0.0.1:8001
```

Both subcommands default `--openapi`/`--cutlist` to
`rust/contract/openapi.json` / `rust/contract/CUTLIST.md` (resolved relative
to this crate's manifest dir, so they work from any `cwd`) — override with
`--openapi`/`--cutlist` if you're testing against a different contract
snapshot.
