//! Telegram client wrapper (teloxide).
//!
//! Covers the send/forward/forum-topic hot path — 100% of what the old aiogram
//! code did for actually moving content. Design rules carried over:
//!   - Bot token priority: TELEGRAM_BOT_TOKEN env wins over the stored
//!     settings value (lets ops override without a redeploy).
//!   - APPEND-ONLY sends. We never delete-and-repost and never brute-force
//!     message-id ranges — inventory rows in SQLite are the source of truth for
//!     what's been delivered, so distribution is safe by construction.
//!   - The bot can be hot-swapped at runtime (PUT /api/telegram/bot-token),
//!     so the handle holds `Arc<RwLock<Option<Bot>>>` instead of a bare Bot.

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use serde::Serialize;
use teloxide::prelude::*;
use teloxide::types::{ChatId, InputFile, MessageId, ThreadId};
use tokio::sync::RwLock;

/// Hot-swappable handle around a teloxide `Bot`. Cloneable; all clones share
/// the same inner bot, so a token swap is visible everywhere at once.
#[derive(Clone)]
pub struct Telegram {
    inner: Arc<RwLock<Option<Bot>>>,
}

/// What `validate_group` reports about a candidate staging/poster group.
/// Field names match the Python payload exactly.
#[derive(Debug, Clone, Serialize)]
pub struct GroupInfo {
    pub is_admin: bool,
    pub is_forum: bool,
    pub title: String,
}

fn env_token() -> Option<String> {
    let t = std::env::var("TELEGRAM_BOT_TOKEN").ok()?;
    let t = t.trim().to_string();
    if t.is_empty() {
        None
    } else {
        Some(t)
    }
}

impl Telegram {
    fn with_bot(bot: Option<Bot>) -> Self {
        Self {
            inner: Arc::new(RwLock::new(bot)),
        }
    }

    /// Build from the TELEGRAM_BOT_TOKEN env var. Returns None if unset, so the
    /// app boots fine without a bot configured (distribution endpoints then
    /// report "not configured" rather than crashing).
    pub fn from_env() -> Option<Self> {
        env_token().map(|t| Self::with_bot(Some(Bot::new(t))))
    }

    /// Preferred boot path (seam: wire into main.rs in place of `from_env`):
    /// env token first (hard rule — env beats stored), then the stored
    /// settings token. Returns a handle even when NO token is available yet,
    /// so PUT /api/telegram/bot-token can install one at runtime.
    #[allow(dead_code)] // awaiting the main.rs seam wiring at integration
    pub async fn from_env_or_settings(db: &clab_core::Db) -> Self {
        if let Some(t) = env_token() {
            return Self::with_bot(Some(Bot::new(t)));
        }
        let stored = clab_core::repo::telegram_cfg::get_setting(db, "bot_token")
            .await
            .ok()
            .flatten()
            .map(|t| t.trim().to_string())
            .filter(|t| !t.is_empty());
        Self::with_bot(stored.map(Bot::new))
    }

    /// Is a bot currently installed (i.e. can we send right now)?
    pub async fn is_running(&self) -> bool {
        self.inner.read().await.is_some()
    }

    /// Hot-swap the bot to a new token (PUT /api/telegram/bot-token). Matches
    /// the Python behavior: the runtime bot uses whatever token was installed
    /// last; the env-beats-stored priority applies at boot.
    pub async fn install(&self, token: &str) {
        *self.inner.write().await = Some(Bot::new(token.trim()));
    }

    /// Stop the bot (DELETE /api/telegram/bot-token). Sends fail until a new
    /// token is installed or the process restarts.
    pub async fn shutdown(&self) {
        *self.inner.write().await = None;
    }

    /// Validate a token by asking Telegram who it is. Returns the bot username.
    pub async fn validate_token(token: &str) -> Result<String> {
        let bot = Bot::new(token.trim());
        let me = bot.get_me().await.context("get_me failed")?;
        Ok(me.username().to_string())
    }

    async fn bot(&self) -> Result<Bot> {
        self.inner
            .read()
            .await
            .clone()
            .context("Telegram bot is not running")
    }

    /// Send a local media file into a chat, optionally targeting a forum topic.
    /// Picks video/photo/document by extension (mirrors the Python bot).
    /// Returns (message_id, file_id) — the file_id lets us re-send without
    /// re-uploading.
    pub async fn send_media(
        &self,
        chat_id: i64,
        thread_id: Option<i64>,
        path: &Path,
        caption: Option<&str>,
    ) -> Result<(i64, Option<String>)> {
        let bot = self.bot().await?;
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let input = InputFile::file(path);
        let chat = ChatId(chat_id);
        let thread = thread_id.map(|t| ThreadId(MessageId(t as i32)));

        let msg = match ext.as_str() {
            "mp4" | "mov" | "avi" | "mkv" => {
                let mut req = bot.send_video(chat, input);
                if let Some(t) = thread {
                    req = req.message_thread_id(t);
                }
                if let Some(c) = caption {
                    req = req.caption(c);
                }
                req.await.context("send_video failed")?
            }
            "jpg" | "jpeg" | "png" | "webp" => {
                let mut req = bot.send_photo(chat, input);
                if let Some(t) = thread {
                    req = req.message_thread_id(t);
                }
                if let Some(c) = caption {
                    req = req.caption(c);
                }
                req.await.context("send_photo failed")?
            }
            _ => {
                let mut req = bot.send_document(chat, input);
                if let Some(t) = thread {
                    req = req.message_thread_id(t);
                }
                if let Some(c) = caption {
                    req = req.caption(c);
                }
                req.await.context("send_document failed")?
            }
        };

        let file_id = msg
            .video()
            .map(|v| v.file.id.to_string())
            .or_else(|| msg.photo().and_then(|p| p.last()).map(|p| p.file.id.to_string()))
            .or_else(|| msg.document().map(|d| d.file.id.to_string()));
        Ok((msg.id.0 as i64, file_id))
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
        let bot = self.bot().await?;
        let mut req = bot.forward_message(
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
    pub async fn create_topic(&self, chat_id: i64, name: &str) -> Result<i64> {
        let bot = self.bot().await?;
        let topic = bot
            .create_forum_topic(ChatId(chat_id), name)
            .await
            .context("create_forum_topic failed")?;
        Ok(topic.thread_id.0 .0 as i64)
    }

    /// Delete a forum topic. Best-effort: returns false (not an error) when
    /// Telegram refuses, matching the Python helper.
    pub async fn delete_topic(&self, chat_id: i64, topic_id: i64) -> bool {
        let Ok(bot) = self.bot().await else {
            return false;
        };
        bot.delete_forum_topic(ChatId(chat_id), ThreadId(MessageId(topic_id as i32)))
            .await
            .is_ok()
    }

    /// Send a plain-text message (daily batch summaries), optionally into a
    /// topic. Returns the message id.
    pub async fn send_text(
        &self,
        chat_id: i64,
        thread_id: Option<i64>,
        text: &str,
    ) -> Result<i64> {
        let bot = self.bot().await?;
        let mut req = bot.send_message(ChatId(chat_id), text);
        if let Some(t) = thread_id {
            req = req.message_thread_id(ThreadId(MessageId(t as i32)));
        }
        let msg = req.await.context("send_message failed")?;
        Ok(msg.id.0 as i64)
    }

    /// Send a text message with HTML parse mode for clickable links (used for
    /// sound-assignment broadcasts).
    // Wired by the sounds workstream; deliberate API surface.
    #[allow(dead_code)]
    pub async fn send_html(
        &self,
        chat_id: i64,
        thread_id: Option<i64>,
        html: &str,
    ) -> Result<i64> {
        use teloxide::types::ParseMode;
        let bot = self.bot().await?;
        let mut req = bot.send_message(ChatId(chat_id), html);
        req = req.parse_mode(ParseMode::Html);
        if let Some(t) = thread_id {
            req = req.message_thread_id(ThreadId(MessageId(t as i32)));
        }
        let msg = req.await.context("send_message failed")?;
        Ok(msg.id.0 as i64)
    }

    /// Validate a group chat: is the bot an admin, is the group a forum
    /// (topics enabled), and what's its title.
    pub async fn validate_group(&self, chat_id: i64) -> Result<GroupInfo> {
        let bot = self.bot().await?;
        let chat = bot
            .get_chat(ChatId(chat_id))
            .await
            .context("get_chat failed")?;
        let me = bot.get_me().await.context("get_me failed")?;
        let member = bot
            .get_chat_member(ChatId(chat_id), me.id)
            .await
            .context("get_chat_member failed")?;
        // is_forum lives on the supergroup variant of the full-info kind tree.
        let is_forum = match &chat.kind {
            teloxide::types::ChatFullInfoKind::Public(p) => match &p.kind {
                teloxide::types::ChatFullInfoPublicKind::Supergroup(sg) => sg.is_forum,
                _ => false,
            },
            _ => false,
        };
        Ok(GroupInfo {
            is_admin: member.is_privileged(),
            is_forum,
            title: chat.title().unwrap_or_default().to_string(),
        })
    }
}
