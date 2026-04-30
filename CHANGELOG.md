# Changelog

## v2.1.6 (2026-04-30)

### New Features
- **Persistent token-usage logging + `/usage` command:** Every Claude CLI call is now appended as one JSON line to `bot/data/usage.jsonl` with timestamp, user, room, model, effort, input/output tokens, cache-read/cache-creation tokens, cost in USD, tool count, and call duration. The new `/usage` command renders any time window directly into the NC Talk chat:
  - `/usage` — today
  - `/usage gestern` — yesterday
  - `/usage 7d`, `/usage 30d` — last N days
  - `/usage 2026-04-30` — specific date
  - `/usage all` — entire history
  - Suffix `me` (e.g. `/usage 7d me`) restricts to the caller
  - Output: total calls / cost / tools, token totals with cache-hit-rate, per-day breakdown for multi-day ranges, per-model split, and per-user split for admins
  - **Privacy:** non-admin callers automatically see only their own entries; only `admin_users` from `config.json` see the global view

### Bundled bot-side improvements (previously unreleased)
- **stream-json output parsing:** the Claude CLI is now invoked with `--output-format stream-json --verbose`. The bot tails events live, displays the active tool (Read/Edit/Bash/Glob/Grep/etc.) and a rolling history of icons in the editable status message, and counts tool invocations.
- **`/transcribe`:** mark the next voice message as transcribe-only — the bot uploads the `.txt` to NC instead of forwarding it to Claude.
- **`/cancel`:** drop queued (not-yet-running) messages from the worker queue without killing the active call.
- **Voice-message TTS reply (when triggered by a voice message):** Claude's response is converted to speech and posted as an audio file alongside the editable status indicator.
- **Status-message hardening:** when a status `send_message` call genuinely fails (after the v2.1.5 NC Talk 400 workaround already recovers spurious failures), the worker now sets a `status_send_failed` flag and stops retrying inside the same call to avoid spam. Final-response delivery falls back through progressively longer edit timeouts (30s → 45s → 60s) before reverting to a fresh send.
- **Voice transcription status reuse with longer cleanup grace period.**

### Internal
- New helpers: `_log_usage`, `_fmt_num`, `_tool_detail`, `TOOL_STATUS` mapping.

## v2.1.5 (2026-04-30)

### Bug Fixes
- **Duplicate messages / missing final response (NC Talk 22.x):** NC Talk 22.0.10 has a server-side bug where `POST /ocs/v2.php/apps/spreed/api/v1/chat/{token}` returns HTTP 400 with `data.error="message"` even though the message is actually delivered. The bot interpreted the 400 as a failure, so `status_msg_id` stayed empty — every status update created a new message instead of editing the placeholder, and the final answer was sent as a fresh message too (sometimes failing entirely). Visible symptom: many duplicate "thinking…" / status messages per request, and occasionally no final reply.
- **Fix:** `send_message()` now attaches a self-generated `referenceId` on every send. On a 400 with `error:"message"`, it re-fetches recent messages and recovers the message ID by referenceId, so callers can `edit_message()` the placeholder as designed. The workaround is silent when the bug is fixed upstream — the 200-path returns the ID directly and the recovery branch is never entered.

### Internal
- New `_request_raw(method, path, body, timeout)` helper returning `(status, data)` so callers can branch on specific status codes; existing `_request()` keeps its `dict | None` contract.
- New `_find_message_by_reference(room_token, reference_id)` helper.
- `send_message()` and `edit_message()` now accept an optional `timeout` parameter (default 15s).

## v2.1.0 (2026-03-17)

### New Features
- **Polls:** Claude can create NC Talk polls for multiple-choice questions
  - `[POLL]...[/POLL]` format in system prompt
  - Automatic fallback to numbered list in 1:1 chats (NC limitation)
  - Poll tracking with vote monitoring and auto-close
  - Voted option forwarded to Claude as context
- **Voice message status:** Transcription progress shown as editable status message
  - `🎤 Transkribiere Sprachnachricht...` → response (single message, no spam)

### Bug Fixes
- **Voice message double-message bug:** Previously, after transcribing a voice message, both a "Transkribiere..." and a separate "Claude denkt nach..." message were shown. Now the transcription status message is directly reused for the Claude response — no duplicate messages.

### API Additions (nextcloud_talk.py)
- `create_poll(room_token, question, options)` — Create NC Talk poll
- `get_poll(room_token, poll_id)` — Get poll results
- `close_poll(room_token, poll_id)` — Close active poll

## v2.0.0 (2026-03-15)

- Multi-room, multi-user architecture
- Voice messages (faster-whisper GPU)
- File attachments (images, PDFs)
- Message queue with status editing
- Per-user model and effort settings
- Cost tracking
- Permission checking via NC app API
