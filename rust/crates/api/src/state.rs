//! Shared application state (cheap to clone — the DB is two `Arc` pools).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use clab_core::Db;
use tokio::sync::Mutex as AsyncMutex;

use crate::telegram::Telegram;

/// Per-page async locks, created on demand. Serializes distribution actions
/// (send / forward) for a single page so concurrent requests can't double-send
/// or double-forward. Keyed by page_id; entries persist for process life
/// (bounded by the page count — small).
type PageLocks = Arc<Mutex<HashMap<String, Arc<AsyncMutex<()>>>>>;

#[derive(Clone)]
pub struct AppState {
    pub db: Db,
    /// None when no bot token is configured — distribution endpoints then
    /// report "not configured" instead of failing.
    pub telegram: Option<Telegram>,
    page_locks: PageLocks,
}

impl AppState {
    pub fn new(db: Db, telegram: Option<Telegram>) -> Self {
        Self {
            db,
            telegram,
            page_locks: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Get (or create) the async lock for a page. Hold the returned guard across
    /// a send/forward to serialize distribution for that page.
    pub fn page_lock(&self, page_id: &str) -> Arc<AsyncMutex<()>> {
        let mut map = self.page_locks.lock().unwrap();
        map.entry(page_id.to_string())
            .or_insert_with(|| Arc::new(AsyncMutex::new(())))
            .clone()
    }
}
