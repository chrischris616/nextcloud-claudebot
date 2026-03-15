# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [2.0.0] - 2026-03-15

### Added
- **Multi-room support** — Bot works in 1:1 chats, group conversations, and public rooms
- **Voice message support** — Transcribes audio messages using faster-whisper (GPU/CUDA)
- **File attachment support** — Images, PDFs, text files are downloaded and passed to Claude for analysis
- **Message queue** — Messages sent while Claude is busy are queued and processed sequentially
- **Status message editing** — "Thinking..." messages are edited in-place instead of sending new ones
- **`/effort [level]`** command — Control response quality (low, medium, high, max)
- **`/cost`** command — Show token usage and costs for the current session
- **`/compact [focus]`** command — Compress session context (summarize + restart with context)
- **Cost tracking** — Tracks input/output tokens and USD cost per session via JSON output
- **Mention detection** — In groups with multiple users, bot only responds to @mentions or /commands
- **Participant count tracking** — Periodically updated for group response logic
- **Whisper GPU auto-unload** — Model unloads from GPU after 5 minutes of inactivity

### Changed
- Sessions are now per (room, user) instead of per user
- `send_message()` now returns the message ID (int) instead of boolean
- Claude CLI called with `--output-format json` for cost tracking
- Claude CLI called with `--effort` flag (configurable per session)
- Status updates edit existing message instead of sending new ones
- Room types 4 (changelog), 5 (notes), 6 (note-to-self) are skipped

### Fixed
- URL encoding for file downloads with spaces in filenames
- Missing leading slash in WebDAV file paths

## [1.1.0] - 2026-03-14

### Changed
- Admin UI and all strings translated to English
- Bot user is now configurable via `occ config:app:set claudebot bot_user`
- Admin check uses Nextcloud's built-in `isAdmin()` instead of hardcoded group list
- Extended Nextcloud compatibility to versions 28-32
- License identifier updated to SPDX format `AGPL-3.0-or-later`
- Date formatting now uses browser locale instead of hardcoded `de-DE`

### Added
- README with setup instructions and API documentation
- CHANGELOG file
- LICENSE file (AGPL-3.0-or-later)
- Screenshot for App Store listing
- Bug tracker and repository URLs in app metadata

## [1.0.2] - 2026-02-27

### Fixed
- OCS API URL generation (removed `/index.php/` prefix)
- `QBMapper::findEntity()` is protected in Nextcloud 32 — added public `findById()` wrapper

## [1.0.1] - 2026-02-26

### Fixed
- Admin UI autocomplete now correctly calls OCS API with proper headers

## [1.0.0] - 2026-02-25

### Added
- Initial release
- Permission management for users and groups
- Admin settings page with autocomplete search
- OCS REST API for permission CRUD and checks
- Database migration for `claudebot_permissions` table
