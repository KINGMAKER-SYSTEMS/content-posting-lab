//! Telegram client wrapper (teloxide).
//!
//! Covers the send/forward/forum-topic hot path — 100% of what the old aiogram
//! code did for actually moving content. Two design rules carried over:
//!   - Bot token priority: TELEGRAM_BOT_TOKEN env wins (lets ops override
//!     without a redeploy). v1 reads env only; a stored fallback can be added.
//!   - APPEND-ONLY sends. We never delete-and-repost and never brute-force
//!     message-id ranges — inventory rows in SQLite are the source of truth for
//!     what's been delivered, so distribution is safe by construction.

use std::path::Path;

use anyhow::{Context, Result};
use teloxide::prelude::*;
use teloxide::types::{ChatId, InputFile, MessageId, ThreadId};

/// Thin handle around a teloxide `Bot`. Cloneable.
#[derive(Clone)]
pub struct Telegram {
    bot: Bot,
}

impl Telegram {
    /// Build from the TELEGRAM_BOT_TOKEN env var. Returns None if unset, so the
    /// app boots fine without a bot configured (distribution endpoints then
    /// report "not configured" rather than crashing).
    pub fn from_env() -> Option<Self> {
        let token = std::env::var("TELEGRAM_BOT_TOKEN").ok()?;
        let token = token.trim();
        if token.is_empty() {
            return None;
        }
        Some(Self {
            bot: Bot::new(token),
        })
    }

    /// Send a local video file into a chat, optionally targeting a forum topic.
    /// Returns the new message id (to store in inventory).
    pub async fn send_video(
        &self,
        chat_id: i64,
        thread_id: Option<i64>,
        path: &Path,
        caption: Option<&str>,
    ) -> Result<i64> {
        let mut req = self
            .bot
            .send_video(ChatId(chat_id), InputFile::file(path));
        if let Some(t) = thread_id {
            req = req.message_thread_id(ThreadId(MessageId(t as i32)));
        }
        if let Some(c) = caption {
            req = req.caption(c);
        }
        let msg = req.await.context("send_video failed")?;
        Ok(msg.id.0 as i64)
    }

    /// Forward an existing message from one chat into another, optionally into a
    /// forum topic. Returns the forwarded message id. Append-only: we forward,
    /// we never delete the original or the copy.
    pub async fn forward(
        &self,
        to_chat: i64,
        to_thread: Option<i64>,
        from_chat: i64,
        message_id: i64,
    ) -> Result<i64> {
        let mut req = self.bot.forward_message(
            ChatId(to_chat),
            ChatId(from_chat),
            MessageId(message_id as i32),
        );
        if let Some(t) = to_thread {
            req = req.message_thread_id(ThreadId(MessageId(t as i32)));
        }
        let msg = req.await.context("forward_message failed")?;
        Ok(msg.id.0 as i64)
    }

    /// Create a forum topic in a group and return its thread id. Stored in the
    /// `topics` table — we never discover topics by brute-forcing thread ids.
    // Wired into a topic-setup endpoint next; kept as deliberate API surface.
    #[allow(dead_code)]
    pub async fn create_topic(&self, chat_id: i64, name: &str) -> Result<i64> {
        let topic = self
            .bot
            .create_forum_topic(ChatId(chat_id), name)
            .await
            .context("create_forum_topic failed")?;
        Ok(topic.thread_id.0 .0 as i64)
    }

    /// Send a text message (used for sound-assignment broadcasts), optionally
    /// into a topic, with HTML parse mode for clickable links.
    // Wired into the sound-assignment broadcast next; deliberate API surface.
    #[allow(dead_code)]
    pub async fn send_html(
        &self,
        chat_id: i64,
        thread_id: Option<i64>,
        html: &str,
    ) -> Result<i64> {
        use teloxide::types::ParseMode;
        let mut req = self.bot.send_message(ChatId(chat_id), html);
        req = req.parse_mode(ParseMode::Html);
        if let Some(t) = thread_id {
            req = req.message_thread_id(ThreadId(MessageId(t as i32)));
        }
        let msg = req.await.context("send_message failed")?;
        Ok(msg.id.0 as i64)
    }
}
