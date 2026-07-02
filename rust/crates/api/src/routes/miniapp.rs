//! Stub — implemented by the wave-1 owner (see ownership map in mod.rs).

use axum::Router;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
}
