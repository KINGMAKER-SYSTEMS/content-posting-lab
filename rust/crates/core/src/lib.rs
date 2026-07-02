//! `core` — domain model + SQLite data layer for the Content Lab rewrite.
//!
//! Everything stateful lives behind [`Db`] (two-pool SQLite) and the typed
//! repositories. No JSON-blob "database", no in-memory job dicts that vanish on
//! restart.

pub mod db;
pub mod model;
pub mod repo;

pub use db::Db;
pub use model::{
    Asset, AssetKind, AssetStatus, Caption, InventoryItem, Job, JobKind, JobStatus, Page,
    Poster, Project,
};
