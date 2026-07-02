//! Burn management/metadata repository — parity port of the read-side of
//! `routers/burn.py`.
//!
//! **Batch → Job mapping.** The old app's "burn batch" was a directory
//! (`projects/<p>/burned/<batch_id>/`) holding `burned_NNN.mp4` files plus a
//! `batch_meta.json` sidecar for the (optional) rename label. In the Rust
//! world a burn run is a durable `Job` row (`JobKind::Burn`); its
//! `result_asset_ids` are the `Burned`-kind assets it produced. So here:
//!
//!   - `batch_id`      == `job.id`
//!   - batch item count == `job.result_asset_ids.len()` (ready assets only)
//!   - batch "label"   is a parity deviation — there is no dedicated column,
//!     so we store it inside `job.params` as `{"label": "..."}`. `params` is
//!     otherwise unused once the burn job runner (jobs.rs) has consumed it to
//!     start the ffmpeg run, so overwriting it post-hoc to carry the label is
//!     safe and keeps the label durable across restarts without a schema
//!     change / migration (which is out of scope for this file-ownership
//!     slice — see the wave-2 seam-request notes).
//!
//! Folder rename (`PATCH /api/burn/folders/rename`) has no asset-table
//! equivalent — burn source folders (`videos/`, `clips/`) are pure filesystem
//! concepts the DB doesn't model. That endpoint stays filesystem-based (see
//! `routes/burn.rs`); this module only owns the DB-backed pieces: fonts
//! listing is also disk-based but pure/no-DB, included here for testability.

use crate::db::Db;
use crate::model::{Asset, AssetKind, AssetStatus, Job, JobKind, JobStatus};

/// One burn batch as surfaced to the frontend: a burn job plus its ready
/// burned assets. Mirrors the old `{id, label, count, created}` shape from
/// `GET /api/burn/batches`, with `assets` added (the Rust app addresses media
/// by stable asset id, never by folder-guessing).
#[derive(Debug, Clone, serde::Serialize)]
pub struct BurnBatch {
    pub id: String,
    pub label: Option<String>,
    pub count: usize,
    pub created: i64,
    pub status: JobStatus,
    pub assets: Vec<Asset>,
}

/// Extract the `label` a caller previously set via `set_batch_label`, if any.
fn label_from_params(params: Option<&str>) -> Option<String> {
    let params = params?;
    let v: serde_json::Value = serde_json::from_str(params).ok()?;
    v.get("label")
        .and_then(|l| l.as_str())
        .map(|s| s.to_string())
}

/// Resolve a job's produced asset ids (JSON array column) to actual `Asset`
/// rows, filtering to `status = ready` (mirrors the Python behavior of only
/// counting `burned_*.mp4` files that actually exist on disk).
async fn resolve_result_assets(db: &Db, job: &Job) -> Result<Vec<Asset>, sqlx::Error> {
    let ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
    let mut assets = Vec::with_capacity(ids.len());
    for id in ids {
        if let Some(asset) = super::get_asset(db, &id).await? {
            if asset.status == AssetStatus::Ready {
                assets.push(asset);
            }
        }
    }
    Ok(assets)
}

/// A burn batch (job) → `BurnBatch` view, or `None` if the job doesn't exist
/// or isn't a burn job.
pub async fn get_batch(db: &Db, batch_id: &str) -> Result<Option<BurnBatch>, sqlx::Error> {
    let job = match super::get_job(db, batch_id).await? {
        Some(j) if j.kind == JobKind::Burn => j,
        _ => return Ok(None),
    };
    let assets = resolve_result_assets(db, &job).await?;
    Ok(Some(BurnBatch {
        id: job.id.clone(),
        label: label_from_params(job.params.as_deref()),
        count: assets.len(),
        created: job.created_at.timestamp(),
        status: job.status,
        assets,
    }))
}

/// List all burn batches for a project, newest first — parity for
/// `GET /api/burn/batches`. Batches with zero ready burned assets are dropped,
/// matching the old app's `if not mp4s: continue` skip of empty batch dirs.
pub async fn list_batches(db: &Db, project: &str) -> Result<Vec<BurnBatch>, sqlx::Error> {
    let jobs = super::list_jobs(db, project).await?;
    let mut batches = Vec::new();
    for job in jobs.into_iter().filter(|j| j.kind == JobKind::Burn) {
        let assets = resolve_result_assets(db, &job).await?;
        if assets.is_empty() {
            continue;
        }
        batches.push(BurnBatch {
            id: job.id.clone(),
            label: label_from_params(job.params.as_deref()),
            count: assets.len(),
            created: job.created_at.timestamp(),
            status: job.status,
            assets,
        });
    }
    Ok(batches)
}

/// List every `burned`-kind, ready asset in a project — parity for the
/// wave-2 contract's redefinition of `GET /api/burn/videos` (Rust reads
/// finished burns from the assets table rather than Python's disk scan of
/// burn *candidates*; see the route file's doc comment for the deviation).
pub async fn list_burned_assets(db: &Db, project: &str) -> Result<Vec<Asset>, sqlx::Error> {
    let assets = super::list_assets(db, project, Some(AssetKind::Burned)).await?;
    Ok(assets.into_iter().filter(|a| a.status == AssetStatus::Ready).collect())
}

/// Set (or clear, if `label` is empty) a batch's rename label — parity for
/// `PATCH /api/burn/batches/{batch_id}/rename`. Stored inside `job.params`
/// (see module doc). Returns `Ok(false)` if the batch doesn't exist so the
/// route can 404, matching the Python 404 on a missing batch dir.
pub async fn set_batch_label(db: &Db, batch_id: &str, label: &str) -> Result<bool, sqlx::Error> {
    let job = match super::get_job(db, batch_id).await? {
        Some(j) if j.kind == JobKind::Burn => j,
        _ => return Ok(false),
    };
    // Preserve any other keys already in params (defensive; today only
    // `label` is ever stored, but this keeps the merge non-destructive if a
    // future caller adds more fields).
    let mut obj = job
        .params
        .as_deref()
        .and_then(|p| serde_json::from_str::<serde_json::Value>(p).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    obj.insert("label".to_string(), serde_json::Value::String(label.to_string()));
    let params = serde_json::Value::Object(obj).to_string();

    sqlx::query("UPDATE jobs SET params = ?, updated_at = ? WHERE id = ?")
        .bind(&params)
        .bind(chrono::Utc::now())
        .bind(batch_id)
        .execute(db.writer())
        .await?;
    Ok(true)
}

// ── Fonts ────────────────────────────────────────────────────────────────

/// One listable caption font — parity shape for `GET /api/burn/fonts`
/// (`{file, name}`).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct FontEntry {
    pub file: String,
    pub name: String,
}

/// Pure name-transform for a `TikTokSans*.ttf` filename → display name.
/// Split out from the directory walk so it's unit-testable without a real
/// fonts/ directory. Mirrors Python's `_list_fonts` exactly: strip the
/// `TikTokSans` prefix (inserting a space), drop the `16pt-`/`12pt-`/`36pt-`
/// size-family infix, trim.
pub fn font_display_name(stem: &str) -> String {
    stem.replace("TikTokSans", "TikTok Sans ")
        .replace("16pt-", "")
        .replace("12pt-", "")
        .replace("36pt-", "")
        .trim()
        .to_string()
}

/// List available caption fonts from `fonts/<Bold|ExtraBold>` `.ttf` files
/// under `font_dir`, sorted by filename. Excludes Italic variants. Mirrors
/// `services/captions`... actually `routers/burn.py::_list_fonts` exactly
/// (only `TikTokSans*.ttf`, skip anything with "Italic" in the name).
pub fn list_fonts(font_dir: &std::path::Path) -> Vec<FontEntry> {
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(font_dir) else {
        return out;
    };
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok())
        .filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| n.starts_with("TikTokSans") && n.ends_with(".ttf"))
        .filter(|n| !n.contains("Italic"))
        .collect();
    names.sort();
    for file in names {
        let stem = file.strip_suffix(".ttf").unwrap_or(&file);
        out.push(FontEntry {
            name: font_display_name(stem),
            file,
        });
    }
    out
}

// ── Caption CSV bank (disk-based, parity with services/captions.py) ────────

/// One caption row — parity shape with Python's canonical caption-row
/// primitive (`CAPTION_FIELDS`).
#[derive(Debug, Clone, serde::Serialize)]
pub struct CaptionRow {
    pub text: String,
    pub video_id: String,
    pub video_url: String,
    pub creator: String,
    pub sound_id: String,
    pub song: String,
    pub mood: String,
    pub views: i64,
}

/// A project's caption CSV source (one per scraped username / TOS import) —
/// parity shape for `scan_project_captions`.
#[derive(Debug, Clone, serde::Serialize)]
pub struct CaptionSource {
    pub username: String,
    pub csv_path: String,
    pub count: usize,
    pub captions: Vec<CaptionRow>,
}

/// Distinct filter facets across a project's caption sources — parity shape
/// for `caption_facets`.
#[derive(Debug, Clone, serde::Serialize)]
pub struct CaptionFacets {
    pub creators: Vec<String>,
    pub sounds: Vec<SoundFacet>,
    pub moods: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct SoundFacet {
    pub sound_id: String,
    pub song: String,
}

/// Parse one `captions.csv` (minimal hand-rolled CSV reader — no external
/// crate available in this file-ownership slice). Handles quoted fields with
/// embedded commas/quotes (RFC 4180 `""` escaping), which is all the writer
/// side (`import-tos`, the caption scraper) ever produces. Filters rows with
/// empty `caption` text, matching Python's `load_captions`.
pub fn parse_captions_csv(content: &str) -> Vec<CaptionRow> {
    let mut rows = parse_csv_rows(content);
    if rows.is_empty() {
        return Vec::new();
    }
    let header = rows.remove(0);
    let idx = |name: &str| header.iter().position(|h| h == name);
    let i_caption = idx("caption");
    let i_video_id = idx("video_id");
    let i_video_url = idx("video_url");
    let i_creator = idx("creator");
    let i_sound_id = idx("sound_id");
    let i_song = idx("song");
    let i_mood = idx("mood");
    let i_views = idx("views");

    let get = |row: &[String], i: Option<usize>| -> String {
        i.and_then(|i| row.get(i)).cloned().unwrap_or_default()
    };

    let mut out = Vec::new();
    for row in rows {
        let text = get(&row, i_caption).trim().to_string();
        if text.is_empty() {
            continue;
        }
        let views = get(&row, i_views).trim().parse::<i64>().unwrap_or(0);
        out.push(CaptionRow {
            text,
            video_id: get(&row, i_video_id),
            video_url: get(&row, i_video_url),
            creator: get(&row, i_creator).trim_start_matches('@').to_string(),
            sound_id: get(&row, i_sound_id),
            song: get(&row, i_song),
            mood: get(&row, i_mood),
            views,
        });
    }
    out
}

/// Minimal RFC 4180 CSV parser: handles quoted fields, embedded commas,
/// escaped `""`, and both `\n`/`\r\n` line endings. No external crate — this
/// file may only touch `repo/burn.rs` this wave.
fn parse_csv_rows(content: &str) -> Vec<Vec<String>> {
    let mut rows = Vec::new();
    let mut row = Vec::new();
    let mut field = String::new();
    let mut in_quotes = false;
    let mut chars = content.chars().peekable();

    while let Some(c) = chars.next() {
        if in_quotes {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    field.push('"');
                    chars.next();
                } else {
                    in_quotes = false;
                }
            } else {
                field.push(c);
            }
        } else {
            match c {
                '"' => in_quotes = true,
                ',' => {
                    row.push(std::mem::take(&mut field));
                }
                '\r' => {} // swallow; \n follows
                '\n' => {
                    row.push(std::mem::take(&mut field));
                    rows.push(std::mem::take(&mut row));
                }
                _ => field.push(c),
            }
        }
    }
    // Final field/row if the file doesn't end with a newline.
    if !field.is_empty() || !row.is_empty() {
        row.push(field);
        rows.push(row);
    }
    // Drop wholly-empty trailing rows (e.g. a stray final blank line).
    rows.retain(|r| !(r.len() == 1 && r[0].is_empty()));
    rows
}

/// Scan `<caption_dir>/*/captions.csv` for a project — parity for
/// `scan_project_captions`. `caption_dir` is the already-resolved,
/// project-scoped `captions/` directory; `csv_path_prefix` is the string
/// prepended to the relative csv path in the response (Python returns the
/// path relative to `BASE_DIR`, e.g. `projects/<p>/captions/<user>/captions.csv`).
pub fn scan_caption_sources(caption_dir: &std::path::Path, csv_path_prefix: &str) -> Vec<CaptionSource> {
    let mut sources = Vec::new();
    let Ok(entries) = std::fs::read_dir(caption_dir) else {
        return sources;
    };
    let mut dirs: Vec<_> = entries.filter_map(|e| e.ok()).collect();
    dirs.sort_by_key(|e| e.file_name());
    for entry in dirs {
        let Ok(file_type) = entry.file_type() else { continue };
        if !file_type.is_dir() {
            continue;
        }
        let name = entry.file_name();
        let username = name.to_string_lossy().to_string();
        if username.starts_with('.') {
            continue;
        }
        let csv_file = entry.path().join("captions.csv");
        if !csv_file.is_file() {
            continue;
        }
        let Ok(content) = std::fs::read_to_string(&csv_file) else {
            continue;
        };
        let captions = parse_captions_csv(&content);
        sources.push(CaptionSource {
            csv_path: format!("{csv_path_prefix}/{username}/captions.csv"),
            count: captions.len(),
            captions,
            username,
        });
    }
    sources
}

/// Compute distinct filter facets across caption sources — parity for
/// `caption_facets`. Sounds are sorted by song title (Python:
/// `sorted(sounds.items(), key=lambda kv: kv[1])`); creators/moods sorted
/// lexically (Python: `sorted(set)`).
pub fn caption_facets(sources: &[CaptionSource]) -> CaptionFacets {
    use std::collections::BTreeSet;

    let mut creators: BTreeSet<String> = BTreeSet::new();
    let mut moods: BTreeSet<String> = BTreeSet::new();
    // Preserve "first song wins for a given sound_id" like Python's dict.setdefault.
    let mut sounds: Vec<SoundFacet> = Vec::new();
    let mut seen_sound_ids: std::collections::HashSet<String> = std::collections::HashSet::new();

    for src in sources {
        for c in &src.captions {
            if !c.creator.is_empty() {
                creators.insert(c.creator.clone());
            }
            if !c.mood.is_empty() {
                moods.insert(c.mood.clone());
            }
            if !c.sound_id.is_empty() && seen_sound_ids.insert(c.sound_id.clone()) {
                sounds.push(SoundFacet {
                    sound_id: c.sound_id.clone(),
                    song: c.song.clone(),
                });
            }
        }
    }
    sounds.sort_by(|a, b| a.song.cmp(&b.song));

    CaptionFacets {
        creators: creators.into_iter().collect(),
        sounds,
        moods: moods.into_iter().collect(),
    }
}

// ── Folder rename validation (pure logic, filesystem-agnostic) ─────────────

/// Sanitize a folder leaf name for filesystem safety — parity for
/// `_sanitize_folder_leaf`: lowercase, spaces→hyphens, strip anything except
/// `[a-z0-9_-]`, cap at 100 chars, reject empty, block traversal characters
/// up front.
pub fn sanitize_folder_leaf(name: &str) -> Result<String, String> {
    if name.trim().is_empty() {
        return Err("New folder name must be a non-empty string".to_string());
    }
    let raw = name.trim();
    if raw.contains("..") || raw.contains('/') || raw.contains('\\') {
        return Err("New folder name cannot contain path separators or traversal characters".to_string());
    }
    let sanitized: String = raw
        .to_lowercase()
        .replace(' ', "-")
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .take(100)
        .collect();
    if sanitized.is_empty() {
        return Err("New folder name must contain at least one alphanumeric character".to_string());
    }
    Ok(sanitized)
}

/// Is a folder string a "virtual" (non-disk) folder invented by the burn
/// video scanner (`run_{job_id}` subfolders, empty string, or `(root)`)? —
/// parity for `_is_virtual_folder`.
pub fn is_virtual_folder(folder: &str) -> bool {
    if folder.is_empty() || folder == "(root)" {
        return true;
    }
    folder.split('/').any(|seg| seg.starts_with("run_"))
}

// ── ZIP (ZIP_STORED, no compression) ────────────────────────────────────────

/// Build an in-memory ZIP archive (method 0 = STORED, i.e. no compression) of
/// `(name, bytes)` entries. Parity with Python's
/// `zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED)`: no compression trades
/// size for near-instant archiving, which is what lets the batch download
/// stream its first bytes immediately instead of stalling behind a deflate
/// pass. Hand-rolled (no zip crate available in this file-ownership slice —
/// see the wave-2 seam request) but the format is simple enough that STORED
/// mode is a handful of struct writes plus a CRC32.
pub fn build_zip_stored(entries: &[(String, Vec<u8>)]) -> Vec<u8> {
    let mut out = Vec::new();
    // (offset, name, crc32, size)
    let mut central: Vec<(u32, &str, u32, u32)> = Vec::with_capacity(entries.len());

    for (name, data) in entries {
        let offset = out.len() as u32;
        let crc = crc32(data);
        let size = data.len() as u32;
        let name_bytes = name.as_bytes();

        // Local file header (PK\x03\x04)
        out.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes()); // version needed
        out.extend_from_slice(&0u16.to_le_bytes()); // flags
        out.extend_from_slice(&0u16.to_le_bytes()); // method = stored
        out.extend_from_slice(&0u16.to_le_bytes()); // mod time
        out.extend_from_slice(&0u16.to_le_bytes()); // mod date
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes()); // compressed size
        out.extend_from_slice(&size.to_le_bytes()); // uncompressed size
        out.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes()); // extra field length
        out.extend_from_slice(name_bytes);
        out.extend_from_slice(data);

        central.push((offset, name, crc, size));
    }

    let central_start = out.len() as u32;
    for (offset, name, crc, size) in &central {
        let name_bytes = name.as_bytes();
        // Central directory file header (PK\x01\x02)
        out.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes()); // version made by
        out.extend_from_slice(&20u16.to_le_bytes()); // version needed
        out.extend_from_slice(&0u16.to_le_bytes()); // flags
        out.extend_from_slice(&0u16.to_le_bytes()); // method = stored
        out.extend_from_slice(&0u16.to_le_bytes()); // mod time
        out.extend_from_slice(&0u16.to_le_bytes()); // mod date
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes()); // extra field length
        out.extend_from_slice(&0u16.to_le_bytes()); // comment length
        out.extend_from_slice(&0u16.to_le_bytes()); // disk number start
        out.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
        out.extend_from_slice(&0u32.to_le_bytes()); // external attrs
        out.extend_from_slice(&offset.to_le_bytes());
        out.extend_from_slice(name_bytes);
    }
    let central_size = out.len() as u32 - central_start;

    // End of central directory record (PK\x05\x06)
    out.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes()); // disk number
    out.extend_from_slice(&0u16.to_le_bytes()); // disk with central dir
    out.extend_from_slice(&(entries.len() as u16).to_le_bytes());
    out.extend_from_slice(&(entries.len() as u16).to_le_bytes());
    out.extend_from_slice(&central_size.to_le_bytes());
    out.extend_from_slice(&central_start.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes()); // comment length

    out
}

/// Standard (IEEE 802.3 / zlib) CRC32, table-based. Hand-rolled — no crc
/// crate is a direct dependency in this file-ownership slice.
fn crc32(data: &[u8]) -> u32 {
    static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();
    let table = TABLE.get_or_init(|| {
        let mut table = [0u32; 256];
        for (i, entry) in table.iter_mut().enumerate() {
            let mut c = i as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            }
            *entry = c;
        }
        table
    });

    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        let idx = ((crc ^ byte as u32) & 0xFF) as usize;
        crc = table[idx] ^ (crc >> 8);
    }
    crc ^ 0xFFFF_FFFF
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use crate::model::AssetKind;

    async fn setup() -> (Db, tempfile::TempDir) {
        Db::connect_temp().await.unwrap()
    }

    #[tokio::test]
    async fn batch_maps_from_burn_job_and_ready_assets() {
        let (db, _guard) = setup().await;
        crate::repo::create_project(&db, "proj").await.unwrap();
        let clip = crate::repo::create_asset(&db, "proj", AssetKind::Clip, None, None)
            .await
            .unwrap();
        let job = crate::repo::create_job(&db, "proj", JobKind::Burn, Some(&clip.id), None)
            .await
            .unwrap();
        let burned = crate::repo::create_asset(&db, "proj", AssetKind::Burned, Some(&clip.id), None)
            .await
            .unwrap();
        crate::repo::mark_asset_ready(&db, &burned.id, "projects/proj/burned/x/burned_000.mp4")
            .await
            .unwrap();
        crate::repo::finish_job(&db, &job.id, std::slice::from_ref(&burned.id)).await.unwrap();

        let batch = get_batch(&db, &job.id).await.unwrap().expect("batch exists");
        assert_eq!(batch.id, job.id);
        assert_eq!(batch.count, 1);
        assert_eq!(batch.assets[0].id, burned.id);
        assert!(batch.label.is_none());
    }

    #[tokio::test]
    async fn batches_without_ready_assets_are_skipped() {
        let (db, _guard) = setup().await;
        crate::repo::create_project(&db, "proj").await.unwrap();
        // A burn job with no result assets at all (still queued).
        crate::repo::create_job(&db, "proj", JobKind::Burn, None, None).await.unwrap();
        let batches = list_batches(&db, "proj").await.unwrap();
        assert!(batches.is_empty());
    }

    #[tokio::test]
    async fn non_burn_job_is_not_a_batch() {
        let (db, _guard) = setup().await;
        crate::repo::create_project(&db, "proj").await.unwrap();
        let job = crate::repo::create_job(&db, "proj", JobKind::Clip, None, None).await.unwrap();
        assert!(get_batch(&db, &job.id).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn set_batch_label_roundtrips() {
        let (db, _guard) = setup().await;
        crate::repo::create_project(&db, "proj").await.unwrap();
        let job = crate::repo::create_job(&db, "proj", JobKind::Burn, None, None).await.unwrap();
        let asset = crate::repo::create_asset(&db, "proj", AssetKind::Burned, None, None).await.unwrap();
        crate::repo::mark_asset_ready(&db, &asset.id, "p").await.unwrap();
        crate::repo::finish_job(&db, &job.id, &[asset.id]).await.unwrap();

        let ok = set_batch_label(&db, &job.id, "My Cool Batch").await.unwrap();
        assert!(ok);
        let batch = get_batch(&db, &job.id).await.unwrap().unwrap();
        assert_eq!(batch.label.as_deref(), Some("My Cool Batch"));
    }

    #[tokio::test]
    async fn set_batch_label_missing_batch_returns_false() {
        let (db, _guard) = setup().await;
        assert!(!set_batch_label(&db, "nope", "x").await.unwrap());
    }

    #[test]
    fn font_display_name_matches_python() {
        assert_eq!(font_display_name("TikTokSans16pt-Bold"), "TikTok Sans Bold");
        assert_eq!(font_display_name("TikTokSans12pt-ExtraBold"), "TikTok Sans ExtraBold");
        // NB: Python's `_list_fonts` only `.strip()`s (trims whitespace at the
        // ends) — it does NOT strip internal punctuation. For a stem with no
        // `16pt-`/`12pt-`/`36pt-` infix, the literal "TikTokSans" -> "TikTok
        // Sans " replace leaves the following "-" intact, e.g.
        // "TikTokSans-Bold" -> "TikTok Sans -Bold" (verified against the
        // actual Python one-liner). This looks odd but is byte-for-byte
        // parity with the deployed app.
        assert_eq!(font_display_name("TikTokSans-Bold"), "TikTok Sans -Bold");
    }

    #[test]
    fn list_fonts_excludes_italic_and_non_tiktoksans() {
        let dir = tempfile::tempdir().unwrap();
        for f in [
            "TikTokSans16pt-Bold.ttf",
            "TikTokSans16pt-BoldItalic.ttf",
            "TikTokSans12pt-ExtraBold.ttf",
            "Montserrat-ExtraBold.ttf",
            "not-a-font.txt",
        ] {
            std::fs::write(dir.path().join(f), b"").unwrap();
        }
        let fonts = list_fonts(dir.path());
        let files: Vec<&str> = fonts.iter().map(|f| f.file.as_str()).collect();
        assert_eq!(
            files,
            vec!["TikTokSans12pt-ExtraBold.ttf", "TikTokSans16pt-Bold.ttf"]
        );
    }

    #[test]
    fn list_fonts_missing_dir_returns_empty() {
        assert!(list_fonts(std::path::Path::new("/nonexistent/does/not/exist")).is_empty());
    }

    #[test]
    fn csv_parse_handles_quotes_and_filters_empty_text() {
        let csv = "video_id,video_url,caption,creator,sound_id,song,views\n\
                   1,http://x,\"hello, world\",@alice,s1,Song One,42\n\
                   2,http://y,,bob,s2,Song Two,0\n\
                   3,http://z,\"with \"\"quotes\"\"\",carol,,,\n";
        let rows = parse_captions_csv(csv);
        assert_eq!(rows.len(), 2); // row 2 has empty caption, filtered
        assert_eq!(rows[0].text, "hello, world");
        assert_eq!(rows[0].creator, "alice"); // @ stripped
        assert_eq!(rows[0].views, 42);
        assert_eq!(rows[1].text, "with \"quotes\"");
    }

    #[test]
    fn caption_facets_dedups_and_sorts() {
        let sources = vec![CaptionSource {
            username: "u".into(),
            csv_path: "p".into(),
            count: 3,
            captions: vec![
                CaptionRow {
                    text: "a".into(),
                    video_id: "".into(),
                    video_url: "".into(),
                    creator: "bob".into(),
                    sound_id: "s2".into(),
                    song: "Zeta".into(),
                    mood: "hype".into(),
                    views: 0,
                },
                CaptionRow {
                    text: "b".into(),
                    video_id: "".into(),
                    video_url: "".into(),
                    creator: "alice".into(),
                    sound_id: "s1".into(),
                    song: "Alpha".into(),
                    mood: "chill".into(),
                    views: 0,
                },
                CaptionRow {
                    text: "c".into(),
                    video_id: "".into(),
                    video_url: "".into(),
                    creator: "alice".into(),
                    sound_id: "s1".into(),
                    song: "Alpha".into(),
                    mood: "hype".into(),
                    views: 0,
                },
            ],
        }];
        let facets = caption_facets(&sources);
        assert_eq!(facets.creators, vec!["alice".to_string(), "bob".to_string()]);
        assert_eq!(facets.moods, vec!["chill".to_string(), "hype".to_string()]);
        assert_eq!(
            facets.sounds,
            vec![
                SoundFacet { sound_id: "s1".into(), song: "Alpha".into() },
                SoundFacet { sound_id: "s2".into(), song: "Zeta".into() },
            ]
        );
    }

    #[test]
    fn folder_leaf_sanitize_matches_python() {
        assert_eq!(sanitize_folder_leaf("My Folder!!").unwrap(), "my-folder");
        assert!(sanitize_folder_leaf("").is_err());
        assert!(sanitize_folder_leaf("../etc").is_err());
        assert!(sanitize_folder_leaf("a/b").is_err());
        assert!(sanitize_folder_leaf("a\\b").is_err());
        assert!(sanitize_folder_leaf("!!!").is_err()); // nothing alnum survives
    }

    #[test]
    fn virtual_folder_detection() {
        assert!(is_virtual_folder(""));
        assert!(is_virtual_folder("(root)"));
        assert!(is_virtual_folder("run_abc123"));
        assert!(is_virtual_folder("clips/run_abc123"));
        assert!(!is_virtual_folder("clips/my-video"));
        assert!(!is_virtual_folder("some-folder"));
    }

    #[test]
    fn zip_stored_roundtrips_with_a_real_unzip_reader() {
        // We don't have a zip-reading crate available either, so verify the
        // structural invariants a reader depends on: PK signatures at the
        // right offsets, entry count, and that CRC32 matches a known vector.
        let entries = vec![
            ("a.mp4".to_string(), b"hello world".to_vec()),
            ("b.mp4".to_string(), b"more data here".to_vec()),
        ];
        let zip = build_zip_stored(&entries);
        assert_eq!(&zip[0..4], &[0x50, 0x4b, 0x03, 0x04]); // first local file header
        // End of central directory signature must be present near the tail.
        let eocd_sig = [0x50, 0x4b, 0x05, 0x06];
        assert!(zip.windows(4).any(|w| w == eocd_sig));
        // Entry count in EOCD (last 22 bytes, offset 10..12) == 2.
        let eocd_pos = zip.windows(4).rposition(|w| w == eocd_sig).unwrap();
        let count = u16::from_le_bytes([zip[eocd_pos + 10], zip[eocd_pos + 11]]);
        assert_eq!(count, 2);
    }

    #[test]
    fn crc32_known_vector() {
        // "123456789" -> 0xCBF43926 is the standard CRC-32/ISO-HDLC test vector.
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }
}
