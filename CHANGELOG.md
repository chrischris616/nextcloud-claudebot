# Changelog

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
