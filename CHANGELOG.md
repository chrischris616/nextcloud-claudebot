# Changelog

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
