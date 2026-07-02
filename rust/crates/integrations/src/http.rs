//! The one shared HTTP client. Connection pooling across every integration;
//! never build ad-hoc `reqwest::Client`s in service modules.

use std::sync::OnceLock;
use std::time::Duration;

static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();

/// Process-wide reqwest client (30s timeout, rustls).
pub fn client() -> &'static reqwest::Client {
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .expect("reqwest client")
    })
}
