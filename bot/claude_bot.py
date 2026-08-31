#!/usr/bin/env python3
"""
Claude NC Talk Bot - Multi-User, Multi-Room Edition.
Polls all conversations where bot-claude is a participant (1:1, groups, public).
Checks permissions via NC claudebot app, maintains per-(room, user) Claude sessions.

Behavior:
- 1:1 chats: responds to all messages
- Groups with only bot + 1 user: responds to all messages
- Groups with multiple users: responds only to @bot-claude mentions or /commands
"""

import json
import subprocess
import re
import time
import logging
import os
import shutil
import signal
import sys
import uuid
import threading
import tempfile
import queue
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.nextcloud_talk import NextcloudTalkClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger('claude-bot')

CONFIG_PATH = Path(__file__).parent / 'config.json'


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


class PermissionChecker:
    """Check user permissions via NC claudebot app API with TTL cache."""

    def __init__(self, nc_client, cache_ttl=300):
        self.nc = nc_client
        self.cache_ttl = cache_ttl
        self._cache = {}  # {user_id: (allowed: bool, timestamp)}

    def is_allowed(self, user_id):
        """Check if user is allowed. Returns True/False. Defaults to DENY on error."""
        now = time.time()
        cached = self._cache.get(user_id)
        if cached and (now - cached[1]) < self.cache_ttl:
            return cached[0]

        allowed = self._check_api(user_id)
        self._cache[user_id] = (allowed, now)
        return allowed

    def _check_api(self, user_id):
        """Call NC claudebot check API."""
        result = self.nc._request(
            'GET',
            f'/ocs/v2.php/apps/claudebot/api/v1/check/{quote(user_id)}',
        )
        if result:
            data = result.get('ocs', {}).get('data', {})
            allowed = data.get('allowed', False)
            log.info(f'Permission check for {user_id}: {allowed} ({data.get("reason", "?")})')
            return allowed
        log.warning(f'Permission check failed for {user_id}, defaulting to DENY')
        return False

    def invalidate(self, user_id=None):
        """Clear cache for one user or all."""
        if user_id:
            self._cache.pop(user_id, None)
        else:
            self._cache.clear()


class UserSession:
    """Per-user Claude session state with message queue."""

    def __init__(self, user_id, model='sonnet'):
        self.user_id = user_id
        self.session_id = str(uuid.uuid4())
        self.model = model
        self.effort = 'high'  # low, medium, high, max
        self.message_count = 0
        self.created_at = datetime.now()
        self.last_active = datetime.now()
        self.busy = False  # True while Claude CLI is running
        self.session_created = False  # True after first successful CLI call
        self.process = None  # Active Popen process (for /stop)
        self.queue = queue.Queue()  # Message queue: (text, room_token, temp_files)
        self._worker_running = False
        self.status_msg_id = None  # ID of the current status message (for editing)
        self.status_room_token = None  # Room of the current status message
        self.status_send_failed = False  # True after a status send_message returned None — don't retry
        self.active_poll = None  # {poll_id, room_token, question, options} — active poll awaiting vote
        self.transcribe_pending = False  # /transcribe: next voice → .txt instead of Claude
        # Cost tracking
        self.total_cost = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def reset(self):
        old_id = self.session_id[:8]
        self.session_id = str(uuid.uuid4())
        self.message_count = 0
        self.session_created = False
        self.created_at = datetime.now()
        self.total_cost = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        return old_id


class ClaudeBot:
    def __init__(self):
        self.cfg = load_config()
        nc_cfg = self.cfg['nextcloud']

        # NC client without notify_user (multi-user mode)
        self.nc = NextcloudTalkClient(
            nc_cfg['base_url'], nc_cfg['username'],
            nc_cfg['password'], nc_cfg.get('notify_user', '')
        )

        claude_cfg = self.cfg.get('claude', {})
        self.default_model = claude_cfg.get('model', 'sonnet')
        self.max_response_length = claude_cfg.get('max_response_length', 3500)
        self.working_directory = claude_cfg.get('working_directory', '/home/depp')
        self.max_turns = claude_cfg.get('max_turns', 0)

        cache_ttl = self.cfg.get('permission_cache_ttl', 300)
        self.permissions = PermissionChecker(self.nc, cache_ttl)
        self.admin_users = set(self.cfg.get('admin_users', []))

        # Per-(room, user) sessions: {(room_token, user_id): UserSession}
        self.sessions = {}
        # Per-room state: {room_token: {'last_known_id': int, 'name': str, 'type': int, 'participants': int}}
        self.rooms = {}

        self.start_time = datetime.now()
        self.total_messages = 0
        self.running = True
        self.poll_timeout = 30  # seconds long-poll timeout per room
        self.room_threads = {}  # {token: Thread}
        self._participant_update_interval = 300  # Update participant counts every 5 min
        self._last_participant_update = 0

        # Whisper model: lazy-loaded, auto-unloaded after inactivity
        self._whisper_model = None
        self._whisper_last_used = 0
        self._whisper_unload_delay = 300  # 5 min inactivity → unload from GPU
        self._whisper_lock = threading.Lock()

        # Persistent usage log: one JSON line per Claude CLI call
        self.usage_log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'data', 'usage.jsonl'
        )
        os.makedirs(os.path.dirname(self.usage_log_path), exist_ok=True)
        self._usage_log_lock = threading.Lock()

        log.info(f'Multi-user mode. Default model: {self.default_model}')

    def _get_session(self, user_id, room_token):
        """Get or create a session for a user in a specific room."""
        key = (room_token, user_id)
        if key not in self.sessions:
            self.sessions[key] = UserSession(user_id, self.default_model)
            log.info(f'New session for {user_id} in room {room_token}: {self.sessions[key].session_id[:8]}...')
        return self.sessions[key]

    # Tool name to user-friendly status mapping
    TOOL_STATUS = {
        'Read': ('📖', 'Liest'),
        'Write': ('📝', 'Schreibt'),
        'Edit': ('✏️', 'Bearbeitet'),
        'Bash': ('💻', 'Fuehrt aus'),
        'Glob': ('🔍', 'Sucht Dateien'),
        'Grep': ('🔍', 'Durchsucht Code'),
        'Agent': ('🤖', 'Sub-Agent'),
        'WebFetch': ('🌐', 'Ruft Webseite ab'),
        'WebSearch': ('🌐', 'Sucht im Web'),
        'Skill': ('⚡', 'Fuehrt Skill aus'),
        'NotebookEdit': ('📓', 'Bearbeitet Notebook'),
    }

    @staticmethod
    def _tool_detail(name, tool_input):
        """Extract a short detail string from tool input for status display."""
        if not isinstance(tool_input, dict):
            return ''
        if name in ('Read', 'Write', 'Edit'):
            fp = tool_input.get('file_path', '')
            if fp:
                # Show last 2 path components
                parts = fp.rstrip('/').split('/')
                return '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
        elif name == 'Bash':
            cmd = tool_input.get('command', '')
            # First line, max 50 chars
            first_line = cmd.split('\n')[0][:50]
            return first_line + ('...' if len(cmd) > 50 else '')
        elif name in ('Grep', 'Glob'):
            return tool_input.get('pattern', '')[:40]
        elif name == 'WebSearch':
            return tool_input.get('query', '')[:40]
        elif name == 'WebFetch':
            url = tool_input.get('url', '')
            # Show domain only
            if '://' in url:
                url = url.split('://')[1].split('/')[0]
            return url[:40]
        elif name == 'Skill':
            return tool_input.get('skill', '')
        elif name == 'Agent':
            return tool_input.get('description', '')[:40]
        return ''

    def _call_claude(self, message, session, room_token=None, is_voice=False):
        """Call Claude Code CLI with the given message using user's session.
        Uses Popen with stream-json output for live tool status updates.
        Runs until finished or killed via /stop.
        """
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)
        env.pop('CLAUDE_CODE_SESSION', None)

        poll_instruction = (
            'Wenn du dem User eine Rückfrage mit konkreten Auswahlmöglichkeiten stellen willst, '
            'verwende dieses Format am ENDE deiner Antwort:\n'
            '[POLL]\n'
            'Frage: Deine Frage hier?\n'
            'Option: Erste Option\n'
            'Option: Zweite Option\n'
            'Option: Dritte Option\n'
            '[/POLL]\n'
            'Nutze das nur bei echten Rückfragen mit 2-6 klaren Optionen, nicht bei offenen Fragen.'
        )

        voice_instruction = (
            'Diese Anfrage kam als Sprachnachricht. '
            'Antworte ausschließlich in natürlichem, fließendem gesprochenem Deutsch — '
            'kein Markdown, keine Bullet-Points, keine Überschriften, keine Code-Blöcke, keine Listen. '
            'Schreib so, wie du sprechen würdest.'
        )

        system_prompt = poll_instruction
        if is_voice:
            system_prompt = voice_instruction + '\n\n' + poll_instruction

        cmd = [
            'claude',
            '-p', message,
            '--model', session.model,
            '--effort', session.effort,
            '--output-format', 'stream-json',
            '--verbose',
            '--dangerously-skip-permissions',
            '--append-system-prompt', system_prompt,
        ]

        if self.max_turns > 0:
            cmd.extend(['--max-turns', str(self.max_turns)])

        if session.session_created:
            cmd.extend(['--resume', session.session_id])
        else:
            cmd.extend(['--session-id', session.session_id])

        log.info(f'Calling Claude CLI ({session.model}, effort={session.effort}) for {session.user_id} ({"resume" if session.session_created else "new"})...')
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.working_directory,
                env=env,
                start_new_session=True
            )
            session.process = proc

            # Read stdout lines in a background thread to avoid pipe buffer deadlock
            stdout_lines = []
            def _read_stdout():
                try:
                    for line in proc.stdout:
                        stdout_lines.append(line.rstrip('\n'))
                except Exception:
                    pass
            reader_thread = threading.Thread(target=_read_stdout, daemon=True)
            reader_thread.start()

            start_time = time.time()
            last_status_update = 0
            min_status_interval = 5  # Minimum seconds between status edits
            lines_seen = 0
            tool_count = 0
            tool_history = []  # List of emojis for completed tools
            current_tool_text = None  # Currently active tool display text
            prev_tool_emoji = None  # Emoji of the previous tool (moves to history)

            while proc.poll() is None:
                time.sleep(2)

                # Parse new stdout lines for tool_use events
                new_lines = stdout_lines[lines_seen:]
                lines_seen = len(stdout_lines)

                for line in new_lines:
                    try:
                        event = json.loads(line)
                        etype = event.get('type', '')

                        # Look for tool_use in assistant content blocks
                        if etype == 'assistant':
                            content = event.get('message', {}).get('content', [])
                            for block in content:
                                if block.get('type') == 'tool_use':
                                    tool_name = block.get('name', '')
                                    tool_input = block.get('input', {})
                                    emoji, label = self.TOOL_STATUS.get(tool_name, ('🔧', tool_name))
                                    detail = self._tool_detail(tool_name, tool_input)
                                    # Move previous tool to history
                                    if prev_tool_emoji is not None:
                                        tool_history.append(prev_tool_emoji)
                                    prev_tool_emoji = emoji
                                    current_tool_text = f'{emoji} {label}'
                                    if detail:
                                        current_tool_text += f': {detail}'
                                    tool_count += 1
                    except (json.JSONDecodeError, TypeError, KeyError):
                        pass

                # Update status message — always edit the existing status message
                now = time.time()
                elapsed = now - start_time

                if room_token and (now - last_status_update) >= min_status_interval:
                    # Build status: header line + current tool + history
                    elapsed_str = f'{int(elapsed)}s' if elapsed < 60 else f'{int(elapsed / 60)} Min'

                    if current_tool_text:
                        # Show: tool count | elapsed | current tool | history trail
                        history_str = ' '.join(tool_history[-15:]) if tool_history else ''
                        lines = [f'⚙️ Tool {tool_count} | {elapsed_str}']
                        lines.append(current_tool_text)
                        if history_str:
                            lines.append(history_str)
                        status_text = '\n'.join(lines)
                    elif elapsed >= 10:
                        status_text = f'💭 Claude denkt nach... ({elapsed_str})'
                    else:
                        status_text = None

                    if status_text:
                        try:
                            if session.status_msg_id:
                                self.nc.edit_message(session.status_room_token, session.status_msg_id, status_text)
                            elif not session.status_send_failed:
                                mid = self.nc.send_message(room_token, status_text)
                                if mid:
                                    session.status_msg_id = mid
                                    session.status_room_token = room_token
                                else:
                                    session.status_send_failed = True
                        except Exception:
                            pass
                        last_status_update = now

            # Process finished — wait for reader thread
            reader_thread.join(timeout=5)
            session.process = None

            # Check if killed by /stop
            if proc.returncode and proc.returncode < 0:
                return None  # Signal kill — no response needed, /stop already sent message

            stderr = proc.stderr.read().strip()

            # Parse stream-json output: find the result event
            output = None
            for line in reversed(stdout_lines):
                try:
                    event = json.loads(line)
                    if event.get('type') == 'result':
                        output = event.get('result', '')
                        # Track costs
                        cost = event.get('total_cost_usd', 0)
                        if cost:
                            session.total_cost += cost
                        usage = event.get('usage', {})
                        session.total_input_tokens += usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
                        session.total_output_tokens += usage.get('output_tokens', 0)
                        log.info(f'Cost: ${cost:.4f} (session total: ${session.total_cost:.4f}), tools used: {tool_count}')
                        self._log_usage(session, room_token, cost, usage, tool_count, time.time() - start_time)
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

            # Fallback: join all non-JSON lines as raw output
            if output is None:
                raw = '\n'.join(stdout_lines).strip()
                # Try parsing as single JSON (old format fallback)
                try:
                    result_json = json.loads(raw)
                    output = result_json.get('result', raw)
                    cost = result_json.get('total_cost_usd', 0)
                    if cost:
                        session.total_cost += cost
                    usage = result_json.get('usage', {})
                    session.total_input_tokens += usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
                    session.total_output_tokens += usage.get('output_tokens', 0)
                    self._log_usage(session, room_token, cost, usage, tool_count, time.time() - start_time)
                except (json.JSONDecodeError, TypeError):
                    output = raw

            if not output and stderr:
                output = f'Fehler: {stderr}'
            if not output:
                output = '(Keine Antwort)'

            if proc.returncode == 0:
                session.session_created = True

            return output
        except FileNotFoundError:
            return 'Fehler: Claude CLI nicht gefunden. Ist claude installiert?'
        except Exception as e:
            return f'Fehler: {e}'

    def _log_usage(self, session, room_token, cost, usage, tool_count, duration_s):
        """Append one Claude CLI call's metrics to the persistent usage log (JSONL).
        One line per call so /usage can replay any time window without parsing logs.
        """
        try:
            room_state = self.rooms.get(room_token, {}) if room_token else {}
            now = datetime.now()
            entry = {
                'ts': now.isoformat(timespec='seconds'),
                'date': now.strftime('%Y-%m-%d'),
                'user': session.user_id,
                'room': room_token or '',
                'room_name': room_state.get('name', ''),
                'model': session.model,
                'effort': session.effort,
                'input_tokens': int(usage.get('input_tokens', 0) or 0),
                'output_tokens': int(usage.get('output_tokens', 0) or 0),
                'cache_read': int(usage.get('cache_read_input_tokens', 0) or 0),
                'cache_creation': int(usage.get('cache_creation_input_tokens', 0) or 0),
                'cost_usd': round(float(cost or 0), 6),
                'tools': int(tool_count or 0),
                'duration_s': round(float(duration_s or 0), 2),
            }
            line = json.dumps(entry, ensure_ascii=False) + '\n'
            with self._usage_log_lock:
                with open(self.usage_log_path, 'a', encoding='utf-8') as f:
                    f.write(line)
        except Exception as e:
            log.warning(f'Failed to log usage: {e}')

    @staticmethod
    def _fmt_num(n):
        """Compact number formatting for token counts."""
        n = int(n or 0)
        if n >= 1_000_000:
            return f'{n / 1_000_000:.2f}M'
        if n >= 1_000:
            return f'{n / 1_000:.1f}k'
        return str(n)

    def _extract_poll(self, text):
        """Extract [POLL]...[/POLL] block from Claude output.
        Returns (text_without_poll, question, options) or (text, None, None) if no poll.
        """
        match = re.search(r'\[POLL\]\s*\n(.*?)\[/POLL\]', text, re.DOTALL)
        if not match:
            return text, None, None

        poll_block = match.group(1)
        text_without_poll = text[:match.start()].rstrip()

        question = None
        options = []
        for line in poll_block.strip().split('\n'):
            line = line.strip()
            if line.lower().startswith('frage:'):
                question = line[6:].strip()
            elif line.lower().startswith('option:'):
                opt = line[7:].strip()
                if opt:
                    options.append(opt)

        if question and len(options) >= 2:
            return text_without_poll, question, options
        return text, None, None

    def _send_poll_or_fallback(self, room_token, question, options, session):
        """Try to create a NC Talk poll. Falls back to numbered list in 1:1 chats."""
        room_state = self.rooms.get(room_token, {})
        room_type = room_state.get('type', 1)

        # Polls don't work in 1:1 chats
        if room_type != 1:
            poll_id = self.nc.create_poll(room_token, question, options)
            if poll_id:
                log.info(f'Created poll {poll_id} in {room_token}: {question}')
                session.active_poll = {
                    'poll_id': poll_id,
                    'room_token': room_token,
                    'question': question,
                    'options': options,
                }
                return

        # Fallback: numbered list
        lines = [f'📊 {question}']
        for i, opt in enumerate(options, 1):
            lines.append(f'{i}. {opt}')
        lines.append('\nAntworte mit der Nummer deiner Wahl.')
        self.nc.send_message(room_token, '\n'.join(lines))

    def _truncate(self, text):
        if len(text) <= self.max_response_length:
            return text
        return text[:self.max_response_length - 20] + '\n...(abgeschnitten)'

    def _download_file(self, file_path):
        """Download a file from NC via WebDAV.
        file_path: path from messageParameters.file.path (e.g. '/Talk/recording.ogg')
        Returns: path to temporary file or None on error.
        """
        try:
            # Ensure leading slash and URL-encode path segments (spaces etc.)
            if not file_path.startswith('/'):
                file_path = '/' + file_path
            encoded_path = quote(f'/remote.php/dav/files/{self.nc.username}{file_path}', safe='/')
            conn = __import__('http.client', fromlist=['HTTPSConnection']).HTTPSConnection(
                self.nc.host, self.nc.port, timeout=60
            )
            conn.request('GET', f'{self.nc.base_path}{encoded_path}', headers=self.nc._headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()

            if resp.status != 200:
                log.error(f'File download failed: HTTP {resp.status} for {encoded_path}')
                return None

            suffix = Path(file_path).suffix or '.bin'
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(data)
            tmp.close()
            log.info(f'File downloaded: {len(data)} bytes -> {tmp.name}')
            return tmp.name
        except Exception as e:
            log.error(f'File download error: {e}')
            return None

    def _send_voice_message(self, text: str, token: str) -> bool:
        """Generate TTS audio and send it as a voice message to the NC Talk room.
        Returns True on success."""
        try:
            import asyncio, edge_tts, requests as req
            # Generate MP3 with edge-tts
            fname = f'claude_voice_{uuid.uuid4().hex[:8]}.mp3'
            tmp_mp3 = Path(tempfile.gettempdir()) / fname
            async def _tts():
                tts = edge_tts.Communicate(text, voice='de-DE-FlorianMultilingualNeural')
                await tts.save(str(tmp_mp3))
            asyncio.run(_tts())
            if not tmp_mp3.exists():
                log.error('TTS: keine Ausgabedatei')
                return False
            audio_data = tmp_mp3.read_bytes()
            log.info(f'TTS: {len(audio_data)} bytes generiert ({fname})')

            # Upload to WebDAV /Talk/
            base = f'https://{self.nc.host}'
            dav_path = f'/remote.php/dav/files/{self.nc.username}/Talk/{fname}'
            r = req.put(f'{base}{dav_path}', data=audio_data,
                auth=(self.nc.username, self.nc.password),
                headers={'Content-Type': 'audio/mpeg'}, timeout=30)
            if r.status_code not in (200, 201, 204):
                log.error(f'TTS upload failed: HTTP {r.status_code}')
                return False

            # Share as voice-message to Talk room
            r2 = req.post(f'{base}/ocs/v2.php/apps/files_sharing/api/v1/shares',
                auth=(self.nc.username, self.nc.password),
                headers={'OCS-APIREQUEST': 'true', 'Accept': 'application/json'},
                json={
                    'shareType': 10,
                    'path': f'/Talk/{fname}',
                    'shareWith': token,
                    'talkMetaData': json.dumps({'messageType': 'voice-message'}),
                }, timeout=15)
            if r2.status_code in (200, 201):
                log.info(f'TTS voice message sent to room {token}')
                tmp_mp3.unlink(missing_ok=True)
                return True
            else:
                log.error(f'TTS share failed: HTTP {r2.status_code} {r2.text[:100]}')
                return False
        except Exception as e:
            log.error(f'TTS error: {e}')
            return False

    def _get_whisper_model(self):
        """Get or lazy-load the Whisper model. Thread-safe."""
        with self._whisper_lock:
            if self._whisper_model is None:
                from faster_whisper import WhisperModel
                log.info('Loading Whisper model (small, CUDA)...')
                self._whisper_model = WhisperModel('small', device='cuda', compute_type='int8')
                log.info('Whisper model loaded on GPU')
            self._whisper_last_used = time.time()
            return self._whisper_model

    def _unload_whisper_model(self):
        """Unload Whisper model from GPU after inactivity."""
        with self._whisper_lock:
            if self._whisper_model is None:
                return
            # Check again if still inactive (another transcription may have happened)
            if time.time() - self._whisper_last_used < self._whisper_unload_delay:
                return
            log.info('Unloading Whisper model from GPU (inactivity)')
            del self._whisper_model
            self._whisper_model = None
            import gc
            gc.collect()

    def _schedule_whisper_unload(self):
        """Schedule a check to unload the Whisper model after the delay."""
        def _check_unload():
            time.sleep(self._whisper_unload_delay + 5)
            self._unload_whisper_model()
        thread = threading.Thread(target=_check_unload, daemon=True)
        thread.start()

    def _transcribe_audio(self, audio_path, delete_after=True):
        """Transcribe an audio file using faster-whisper. Returns transcribed text or None.
        Deletes the source file after transcription unless delete_after=False."""
        try:
            model = self._get_whisper_model()
            segments, info = model.transcribe(audio_path, language='de')
            text = ' '.join(seg.text.strip() for seg in segments)
            log.info(f'Transcription ({info.language}, {info.duration:.1f}s): {text[:80]}...')
            self._schedule_whisper_unload()
            return text if text.strip() else None
        except Exception as e:
            log.error(f'Transcription error: {e}')
            return None
        finally:
            if delete_after:
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass

    def _send_transcript_file(self, text: str, token: str, source_name: str = '') -> bool:
        """Upload transcript as .txt to NC /Talk/ and share to the room.
        Returns True on success."""
        try:
            import requests as req
            stem = Path(source_name).stem if source_name else 'transcript'
            stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', stem)[:60] or 'transcript'
            fname = f'{stem}_{uuid.uuid4().hex[:6]}.txt'
            base = f'https://{self.nc.host}'
            dav_path = f'/remote.php/dav/files/{self.nc.username}/Talk/{fname}'
            r = req.put(f'{base}{dav_path}', data=text.encode('utf-8'),
                auth=(self.nc.username, self.nc.password),
                headers={'Content-Type': 'text/plain; charset=utf-8'}, timeout=30)
            if r.status_code not in (200, 201, 204):
                log.error(f'Transcript upload failed: HTTP {r.status_code}')
                return False
            r2 = req.post(f'{base}/ocs/v2.php/apps/files_sharing/api/v1/shares',
                auth=(self.nc.username, self.nc.password),
                headers={'OCS-APIREQUEST': 'true', 'Accept': 'application/json'},
                json={
                    'shareType': 10,
                    'path': f'/Talk/{fname}',
                    'shareWith': token,
                }, timeout=15)
            if r2.status_code in (200, 201):
                log.info(f'Transcript shared to room {token}: {fname}')
                return True
            log.error(f'Transcript share failed: HTTP {r2.status_code} {r2.text[:100]}')
            return False
        except Exception as e:
            log.error(f'Transcript send error: {e}')
            return False

    def cmd_transcribe(self, session):
        session.transcribe_pending = True
        return ('🎤 Transkribier-Modus aktiv. Sende jetzt eine Sprachnachricht — '
                'sie wird ohne Claude-Call als .txt im Chat geteilt.')

    def cmd_clear(self, session):
        old_id = session.reset()
        return f'Session zurueckgesetzt.\nAlte Session: {old_id}...\nNeue Session: {session.session_id[:8]}...'

    def cmd_model(self, session, args):
        valid_models = ['sonnet', 'opus', 'opus4', 'opus47', 'haiku']
        model_aliases = {
            'opus4': 'claude-opus-4-6',
            'opus47': 'claude-opus-4-7',
        }
        model_display = {
            'sonnet': 'Sonnet 4.6 (Standard)',
            'opus': 'Opus (Standard)',
            'opus4': 'Opus 4.6 (claude-opus-4-6)',
            'opus47': 'Opus 4.7 (claude-opus-4-7, neu!)',
            'haiku': 'Haiku 4.5 (schnell/guenstig)',
        }
        if not args:
            display = '\n'.join(f'  {k}: {v}' for k, v in model_display.items())
            return f'Dein Modell: {session.model}\nVerfuegbar:\n{display}'
        new_model = args[0].lower()
        if new_model not in valid_models:
            return f'Unbekanntes Modell: {new_model}\nVerfuegbar: {", ".join(valid_models)}'
        old_model = session.model
        session.model = model_aliases.get(new_model, new_model)
        return f'Modell gewechselt: {old_model} -> {session.model}'

    @staticmethod
    def _format_bytes(num_bytes):
        size = float(num_bytes)
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if size < 1024 or unit == 'TB':
                return f'{size:.1f} {unit}'
            size /= 1024

    def cmd_status(self, session, user_id, room_token=None):
        uptime = datetime.now() - self.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f'{hours}h {minutes}m {seconds}s'

        session_age = datetime.now() - session.created_at
        sh, sr = divmod(int(session_age.total_seconds()), 3600)
        sm, ss = divmod(sr, 60)

        # Count how many rooms this user has sessions in
        user_rooms = sum(1 for (rt, uid) in self.sessions if uid == user_id)

        room_info = ''
        if room_token and room_token in self.rooms:
            r = self.rooms[room_token]
            room_type_name = {1: '1:1', 2: 'Gruppe', 3: 'Oeffentlich'}.get(r.get('type', 1), '?')
            room_info = f' ({r.get("name", "?")}, {room_type_name})'

        lines = [
            f'Claude Bot Status',
            f'Dein Modell: {session.model} (effort: {session.effort})',
            f'Deine Session: {session.session_id[:8]}...{room_info}',
            f'Deine Nachrichten: {session.message_count}',
            f'Session-Kosten: ${session.total_cost:.4f}',
            f'Session-Alter: {sh}h {sm}m {ss}s',
            f'Deine aktiven Raeume: {user_rooms}',
        ]

        if user_id in self.admin_users:
            unique_users = len(set(uid for (rt, uid) in self.sessions))
            disk = shutil.disk_usage(self.working_directory)
            lines.extend([
                f'---',
                f'Bot-Uptime: {uptime_str}',
                f'Aktive Sessions: {len(self.sessions)}',
                f'Aktive User: {unique_users}',
                f'Gesamt-Nachrichten: {self.total_messages}',
                f'Ueberwachte Raeume: {len(self.rooms)}',
                f'Arbeitsverzeichnis: {self.working_directory}',
                f'Freier Speicherplatz: {self._format_bytes(disk.free)} von {self._format_bytes(disk.total)}',
            ])

        return '\n'.join(lines)

    def cmd_cancel(self, session):
        """Clear the message queue (remove waiting messages, keep current running)."""
        cleared = 0
        while not session.queue.empty():
            try:
                _text, _room, temp_files, _is_voice = session.queue.get_nowait()
                self._cleanup_temp_files(temp_files)
                cleared += 1
            except queue.Empty:
                break
        if cleared == 0:
            return 'Keine wartenden Nachrichten in der Warteschlange.'
        return f'🗑️ {cleared} wartende Nachricht{"en" if cleared != 1 else ""} aus der Warteschlange entfernt.'

    def cmd_stop(self, session):
        """Kill the running Claude CLI process and all its children."""
        if not session.busy or not session.process:
            return 'Keine laufende Anfrage zum Abbrechen.'
        try:
            import signal
            pid = session.process.pid
            # Kill entire process group to clean up SSH/subprocess children
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                session.process.kill()
            session.process.wait(timeout=5)
        except Exception:
            pass
        session.process = None
        session.busy = False
        return '🛑 Anfrage abgebrochen.'

    def cmd_effort(self, session, args):
        valid_levels = ['low', 'medium', 'high', 'max']
        if not args:
            return f'Effort-Level: {session.effort}\nVerfuegbar: {", ".join(valid_levels)}'
        new_effort = args[0].lower()
        if new_effort not in valid_levels:
            return f'Unbekanntes Level: {new_effort}\nVerfuegbar: {", ".join(valid_levels)}'
        old_effort = session.effort
        session.effort = new_effort
        return f'Effort gewechselt: {old_effort} -> {session.effort}'

    def cmd_cost(self, session):
        if session.total_cost == 0 and session.message_count == 0:
            return 'Noch keine Kosten in dieser Session.'
        lines = [
            'Kosten dieser Session:',
            f'Gesamt: ${session.total_cost:.4f}',
            f'Input-Tokens: {session.total_input_tokens:,}',
            f'Output-Tokens: {session.total_output_tokens:,}',
            f'Nachrichten: {session.message_count}',
        ]
        if session.message_count > 0:
            avg = session.total_cost / session.message_count
            lines.append(f'Durchschnitt/Nachricht: ${avg:.4f}')
        return '\n'.join(lines)

    def cmd_usage(self, args, user_id):
        """Show token usage from the persistent log. Args:
        - none / 'heute' / 'today' → today
        - 'gestern' / 'yesterday' → yesterday
        - 'Nd' (e.g. '7d', '30d') → last N days
        - 'YYYY-MM-DD' → specific date
        - 'all' / 'alle' → entire history
        - 'me' / 'mir' → restrict to caller; otherwise admins see global
        """
        args_lower = [a.lower() for a in args]
        only_me = 'me' in args_lower or 'mir' in args_lower
        range_arg = next((a for a in args_lower if a not in ('me', 'mir')), None)

        today = datetime.now().date()
        date_filter = None
        title = ''

        if range_arg is None or range_arg in ('today', 'heute'):
            d = today.isoformat()
            date_filter = lambda x: x == d
            title = f'heute ({today.strftime("%d.%m.%Y")})'
        elif range_arg in ('gestern', 'yesterday'):
            yest = today - timedelta(days=1)
            ds = yest.isoformat()
            date_filter = lambda x: x == ds
            title = f'gestern ({yest.strftime("%d.%m.%Y")})'
        elif range_arg in ('all', 'alle', 'gesamt'):
            date_filter = lambda x: True
            title = 'gesamt'
        elif range_arg.endswith('d') and range_arg[:-1].isdigit():
            n = max(1, int(range_arg[:-1]))
            cutoff = (today - timedelta(days=n - 1)).isoformat()
            date_filter = lambda x: x >= cutoff
            title = f'letzte {n} Tage'
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', range_arg):
            ds = range_arg
            date_filter = lambda x: x == ds
            try:
                title = datetime.strptime(ds, '%Y-%m-%d').strftime('%d.%m.%Y')
            except ValueError:
                title = ds
        else:
            return (
                'Token-Verbrauch anzeigen. Beispiele:\n'
                '/usage          (heute)\n'
                '/usage gestern\n'
                '/usage 7d       (letzte 7 Tage)\n'
                '/usage 30d\n'
                '/usage 2026-04-30\n'
                '/usage all      (alle Daten)\n\n'
                'Suffix "me" filtert auf eigene Calls, z.B. /usage 7d me'
            )

        if not os.path.exists(self.usage_log_path):
            return f'📊 Keine Nutzungsdaten ({title}).'

        is_admin = user_id in self.admin_users
        # Non-admins always see only their own entries (privacy)
        restrict_to_self = only_me or not is_admin

        entries = []
        try:
            with open(self.usage_log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not date_filter(e.get('date', '')):
                        continue
                    if restrict_to_self and e.get('user') != user_id:
                        continue
                    entries.append(e)
        except Exception as ex:
            return f'Fehler beim Lesen des Logs: {ex}'

        if restrict_to_self and is_admin and only_me:
            title += ' (eigene)'
        elif restrict_to_self and not is_admin:
            pass  # implicit self-scope, no annotation needed

        if not entries:
            return f'📊 Keine Nutzungsdaten ({title}).'

        total_calls = len(entries)
        total_cost = sum(e.get('cost_usd', 0) for e in entries)
        total_in_new = sum(e.get('input_tokens', 0) for e in entries)
        total_out = sum(e.get('output_tokens', 0) for e in entries)
        total_cache_read = sum(e.get('cache_read', 0) for e in entries)
        total_cache_creation = sum(e.get('cache_creation', 0) for e in entries)
        total_tools = sum(e.get('tools', 0) for e in entries)
        total_in_full = total_in_new + total_cache_read + total_cache_creation
        cache_hit_pct = (total_cache_read / total_in_full * 100) if total_in_full > 0 else 0

        lines = [f'📊 Token-Verbrauch — {title}']
        lines.append(f'{total_calls} Calls   ${total_cost:.4f}   {total_tools} Tools')
        lines.append(f'Tokens: {self._fmt_num(total_in_full)} in / {self._fmt_num(total_out)} out')
        if total_in_full > 0:
            lines.append(f'Cache: {cache_hit_pct:.0f}% hit ({self._fmt_num(total_cache_read)} cached, {self._fmt_num(total_in_new)} fresh)')

        # Per-day breakdown when range > 1 day
        distinct_dates = sorted({e.get('date', '') for e in entries})
        if len(distinct_dates) > 1:
            lines.append('')
            lines.append('Pro Tag:')
            per_day = {}
            for e in entries:
                d = e.get('date', '')
                slot = per_day.setdefault(d, {'calls': 0, 'cost': 0.0})
                slot['calls'] += 1
                slot['cost'] += e.get('cost_usd', 0)
            for d in distinct_dates:
                try:
                    day_str = datetime.strptime(d, '%Y-%m-%d').strftime('%a %d.%m')
                except ValueError:
                    day_str = d
                v = per_day[d]
                lines.append(f'  {day_str}  {v["calls"]:>3} Calls  ${v["cost"]:.4f}')

        # Per-model breakdown
        per_model = {}
        for e in entries:
            m = e.get('model', '?') or '?'
            slot = per_model.setdefault(m, {'calls': 0, 'cost': 0.0})
            slot['calls'] += 1
            slot['cost'] += e.get('cost_usd', 0)
        if len(per_model) > 1:
            lines.append('')
            lines.append('Pro Modell:')
            for m, v in sorted(per_model.items(), key=lambda x: -x[1]['cost']):
                lines.append(f'  {m:<20} {v["calls"]:>3} Calls  ${v["cost"]:.4f}')

        # Per-user breakdown only when admin sees global view
        if is_admin and not only_me:
            per_user = {}
            for e in entries:
                u = e.get('user', '?') or '?'
                slot = per_user.setdefault(u, {'calls': 0, 'cost': 0.0})
                slot['calls'] += 1
                slot['cost'] += e.get('cost_usd', 0)
            if len(per_user) > 1:
                lines.append('')
                lines.append('Pro User:')
                for u, v in sorted(per_user.items(), key=lambda x: -x[1]['cost']):
                    lines.append(f'  {u:<15} {v["calls"]:>3} Calls  ${v["cost"]:.4f}')

        return '\n'.join(lines)

    def cmd_compact(self, session, args, room_token):
        """Compact session: ask Claude to summarize, then start fresh with summary as context."""
        if session.busy:
            return 'Kann nicht komprimieren waehrend eine Anfrage laeuft.'
        if not session.session_created:
            return 'Noch keine Session zum Komprimieren vorhanden.'

        focus = ' '.join(args) if args else ''
        summary_prompt = (
            'Fasse unsere bisherige Unterhaltung in einer kompakten Zusammenfassung zusammen. '
            'Behalte: wichtige Entscheidungen, offene Aufgaben, relevante Dateien/Pfade, Kontext. '
            'Antworte NUR mit der Zusammenfassung, ohne Einleitung.'
        )
        if focus:
            summary_prompt += f' Fokus auf: {focus}'

        # Step 1: Get summary from current session
        # Step 2: Reset session, inject summary as first message
        def _do_compact():
            session.busy = True
            try:
                summary = self._call_claude(summary_prompt, session, room_token)
                if not summary:
                    self.nc.send_message(room_token, 'Komprimierung fehlgeschlagen.')
                    return
                # Reset session
                old_id = session.reset()
                # Start new session with summary as context
                context_msg = f'[Kontext aus vorheriger Session {old_id}...]\n\n{summary}'
                result = self._call_claude(context_msg, session, room_token)
                self.nc.send_message(room_token, f'🗜️ Session komprimiert.\nNeue Session: {session.session_id[:8]}...')
            except Exception as e:
                log.error(f'Compact error: {e}')
                self.nc.send_message(room_token, f'Komprimierung fehlgeschlagen: {e}')
            finally:
                session.busy = False

        thread = threading.Thread(target=_do_compact, daemon=True)
        thread.start()
        return None

    def cmd_help(self):
        return (
            'Claude Bot Befehle:\n\n'
            '/clear - Neue Session starten\n'
            '/stop - Laufende Anfrage abbrechen\n'
            '/cancel - Wartende Nachrichten aus der Queue entfernen\n'
            '/model [name] - Modell anzeigen/wechseln (sonnet, opus, opus4, opus47, haiku)\n'
            '/effort [level] - Effort-Level (low, medium, high, max)\n'
            '/cost - Token-Verbrauch & Kosten anzeigen (aktuelle Session)\n'
            '/usage [heute|gestern|Nd|YYYY-MM-DD|all] [me] - Token-Statistik aus dem Log\n'
            '/compact [fokus] - Kontext komprimieren\n'
            '/status - Session-Info\n'
            '/transcribe - Naechste Sprachnachricht nur transkribieren (kein Claude, .txt im Chat)\n'
            '/help - Diese Hilfe\n\n'
            'Alle anderen Nachrichten werden an Claude Code weitergeleitet.\n'
            'Dateien & Sprachnachrichten werden ebenfalls verarbeitet.\n\n'
            'In Gruppen: @bot-claude erwaehnen oder /befehl nutzen.\n'
            'Jede Unterhaltung hat eine eigene Session.'
        )

    def process_command(self, text, session, user_id, room_token=None):
        """Process slash commands. Returns response string or None."""
        if not text.startswith('/'):
            return None

        parts = text.split()
        cmd = parts[0].lower()

        if cmd == '/clear':
            return self.cmd_clear(session)
        elif cmd == '/cancel':
            return self.cmd_cancel(session)
        elif cmd == '/stop':
            return self.cmd_stop(session)
        elif cmd == '/model':
            return self.cmd_model(session, parts[1:])
        elif cmd == '/effort':
            return self.cmd_effort(session, parts[1:])
        elif cmd == '/cost':
            return self.cmd_cost(session)
        elif cmd == '/usage':
            return self.cmd_usage(parts[1:], user_id)
        elif cmd == '/compact':
            return self.cmd_compact(session, parts[1:], room_token)
        elif cmd == '/status':
            return self.cmd_status(session, user_id, room_token)
        elif cmd == '/transcribe':
            return self.cmd_transcribe(session)
        elif cmd == '/help':
            return self.cmd_help()

        return None

    def handle_message(self, text, actor_id, room_token, temp_files=None, is_voice=False):
        """Handle incoming NC Talk message from a specific room.
        temp_files: optional list of temp file paths to clean up after Claude is done.
        Returns response string for immediate replies (commands),
        or None if handled async (Claude CLI calls run in background thread).
        """
        # Strip HTML tags and mention placeholders like {mention-user1}
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\{mention-[^}]+\}', '', text).strip()
        if not text:
            self._cleanup_temp_files(temp_files)
            return None

        log.info(f'[{room_token}:{actor_id}] {text[:80]}{"..." if len(text) > 80 else ""}')

        # Permission check
        if not self.permissions.is_allowed(actor_id):
            log.info(f'User {actor_id} not permitted, ignoring')
            self._cleanup_temp_files(temp_files)
            return 'Du hast keine Berechtigung fuer den Claude Bot. Wende dich an einen Admin.'

        session = self._get_session(actor_id, room_token)
        session.last_active = datetime.now()

        # Check for slash commands (always immediate)
        response = self.process_command(text, session, actor_id, room_token)
        if response is not None:
            self._cleanup_temp_files(temp_files)
            return response

        # Queue the message for sequential processing
        session.message_count += 1
        self.total_messages += 1
        queued = session.busy  # Already processing something?
        session.queue.put((text, room_token, temp_files, is_voice))

        if queued:
            qsize = session.queue.qsize()
            log.info(f'[{room_token}:{actor_id}] Queued (queue size: {qsize})')
            # Notify user that their message is queued
            try:
                self.nc.send_message(
                    room_token,
                    f'⏳ Vorherige Anfrage laeuft noch... deine Nachricht kommt danach dran (Position {qsize} in der Warteschlange).\n'
                    f'Mit /cancel kannst du wartende Nachrichten entfernen.'
                )
            except Exception:
                pass
        elif not session.status_msg_id:
            # Send immediate "thinking" feedback and track message ID for editing
            # Skip if status_msg_id already set (e.g. from voice transcription)
            try:
                msg_id = self.nc.send_message(room_token, '💭 Claude denkt nach...')
                session.status_msg_id = msg_id
                session.status_room_token = room_token
            except Exception:
                pass

        # Start worker thread if not already running
        self._ensure_session_worker(session, actor_id)
        return None  # Response sent async from worker

    def _ensure_session_worker(self, session, actor_id):
        """Start a worker thread for a session if not already running."""
        if session._worker_running:
            return
        session._worker_running = True

        def _worker():
            while True:
                try:
                    text, room_token, temp_files, is_voice = session.queue.get(timeout=1)
                except queue.Empty:
                    session._worker_running = False
                    session.busy = False
                    return

                session.busy = True
                # Send status message if not already present (queued messages)
                if not session.status_msg_id:
                    try:
                        mid = self.nc.send_message(room_token, '💭 Claude denkt nach...')
                        if mid:
                            session.status_msg_id = mid
                            session.status_room_token = room_token
                        else:
                            session.status_send_failed = True
                    except Exception:
                        session.status_send_failed = True
                try:
                    response = self._call_claude(text, session, room_token, is_voice=is_voice)
                    if response is None:
                        # Process killed (by /stop or crash) — clear status message
                        if session.status_msg_id:
                            try:
                                self.nc.edit_message(session.status_room_token, session.status_msg_id, '🛑 Abgebrochen.')
                            except Exception:
                                pass
                    if response is not None:
                        # Check for poll in response
                        response, poll_question, poll_options = self._extract_poll(response)
                        response = self._truncate(response)
                        log.info(f'[{room_token}:{actor_id}] Response: {len(response)} chars')
                        # Edit status message with the response, or send new if edit fails
                        sent = False
                        if response.strip():
                            if is_voice:
                                tts_ok = self._send_voice_message(response, room_token)
                                if tts_ok:
                                    # Edit status indicator away (can't delete in NC Talk)
                                    if session.status_msg_id:
                                        try:
                                            self.nc.edit_message(session.status_room_token, session.status_msg_id, '🔊')
                                        except Exception:
                                            pass
                                    sent = True
                            if not sent:
                                if session.status_msg_id:
                                    # Retry edit with progressively longer timeouts before falling back
                                    for attempt_timeout in (30, 45, 60):
                                        try:
                                            sent = self.nc.edit_message(
                                                session.status_room_token,
                                                session.status_msg_id,
                                                response,
                                                timeout=attempt_timeout,
                                            )
                                        except Exception:
                                            sent = False
                                        if sent:
                                            break
                                if not sent:
                                    self.nc.send_message(room_token, response, timeout=45)
                        elif session.status_msg_id:
                            # No text, only poll — delete status message by editing to minimal
                            try:
                                self.nc.edit_message(session.status_room_token, session.status_msg_id, '...')
                            except Exception:
                                pass
                        # Create poll if detected
                        if poll_question and poll_options:
                            self._send_poll_or_fallback(room_token, poll_question, poll_options, session)
                except Exception as e:
                    log.error(f'[{room_token}:{actor_id}] Claude worker error: {e}')
                    try:
                        if session.status_msg_id:
                            self.nc.edit_message(session.status_room_token, session.status_msg_id, f'Fehler: {e}')
                        else:
                            self.nc.send_message(room_token, f'Fehler: {e}')
                    except Exception:
                        pass
                finally:
                    session.status_msg_id = None
                    session.status_room_token = None
                    session.status_send_failed = False
                    self._cleanup_temp_files(temp_files)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    @staticmethod
    def _cleanup_temp_files(temp_files):
        """Remove temporary files after processing."""
        if not temp_files:
            return
        for f in temp_files:
            try:
                os.unlink(f)
                log.debug(f'Cleaned up temp file: {f}')
            except Exception:
                pass

    def _check_polls_for_room(self, room_token):
        """Check all sessions in this room for active polls with votes."""
        for (rt, uid), session in list(self.sessions.items()):
            if rt != room_token or not session.active_poll:
                continue
            poll = session.active_poll
            if poll['room_token'] != room_token:
                continue

            try:
                poll_data = self.nc.get_poll(room_token, poll['poll_id'])
                if not poll_data or poll_data.get('numVoters', 0) == 0:
                    continue

                # Someone voted — close poll and get results
                result_data = self.nc.close_poll(room_token, poll['poll_id'])
                session.active_poll = None

                if not result_data:
                    continue

                # Find who voted and what they chose
                details = result_data.get('details', [])
                if not details:
                    # Fallback: use votes dict
                    votes = result_data.get('votes', {})
                    if votes:
                        # Find option with most votes
                        top_option_key = max(votes, key=votes.get)
                        option_idx = int(top_option_key.replace('option-', ''))
                        chosen = poll['options'][option_idx] if option_idx < len(poll['options']) else '?'
                        voter_id = uid  # Assume session owner voted
                    else:
                        continue
                else:
                    # Use details for voter info
                    voter = details[0]
                    voter_id = voter.get('actorId', '')
                    option_idx = voter.get('optionId', 0)
                    chosen = poll['options'][option_idx] if option_idx < len(poll['options']) else '?'

                # Permission check on voter
                if not self.permissions.is_allowed(voter_id):
                    log.info(f'Poll vote from unauthorized user {voter_id}, ignoring')
                    continue

                log.info(f'[{room_token}:{voter_id}] Poll answer: {chosen}')

                # Forward the chosen option to Claude as a message
                answer_text = f'[Umfrage-Antwort auf "{poll["question"]}"]: {chosen}'
                response = self.handle_message(answer_text, voter_id, room_token)
                if response:
                    self.nc.send_message(room_token, response)

            except Exception as e:
                log.error(f'Poll check error in {room_token}: {e}')

    def _start_room_thread(self, token):
        """Start a long-poll thread for a room if not already running."""
        if token in self.room_threads and self.room_threads[token].is_alive():
            return
        thread = threading.Thread(
            target=self._poll_room_loop, args=(token,), daemon=True
        )
        thread.start()
        self.room_threads[token] = thread

    def _discover_rooms(self):
        """Discover all conversations where bot is a participant.
        Accepts type 1 (1:1), 2 (group), 3 (public). Skips type 4 (changelog), 5 (notes), 6 (note-to-self).
        Fetches participant count for new rooms and periodically for groups.
        """
        conversations = self.nc.list_conversations()
        now = time.time()
        update_participants = (now - self._last_participant_update) >= self._participant_update_interval

        for conv in conversations:
            token = conv.get('token')
            conv_type = conv.get('type')
            if not token or conv_type in (4, 5, 6):
                continue

            type_name = {1: '1:1', 2: 'Gruppe', 3: 'Oeffentlich'}.get(conv_type, f'Typ {conv_type}')

            if token not in self.rooms:
                # New room — initialize last known ID + fetch participant count
                last_id = self.nc.init_last_known_id_for_room(token)
                participants = 2 if conv_type == 1 else self.nc.get_participant_count(token)
                self.rooms[token] = {
                    'last_known_id': last_id,
                    'name': conv.get('displayName', '?'),
                    'type': conv_type,
                    'participants': participants,
                }
                log.info(f'Discovered room {token} ({conv.get("displayName", "?")}, {type_name}, {participants} Teilnehmer), last_id={last_id}')
            else:
                self.rooms[token]['name'] = conv.get('displayName', self.rooms[token].get('name', '?'))
                # Periodically update participant count for groups (users may join/leave)
                if update_participants and conv_type in (2, 3):
                    self.rooms[token]['participants'] = self.nc.get_participant_count(token)

            # Ensure polling thread is running
            self._start_room_thread(token)

        if update_participants:
            self._last_participant_update = now

    def _should_respond(self, msg, room_state):
        """Determine if the bot should respond to this message based on room context.

        Rules:
        - 1:1 chat (type 1): always respond
        - Group with only bot + 1 user (participants <= 2): always respond
        - Group with multiple users (participants > 2): only if @mentioned or /command
        """
        room_type = room_state.get('type', 1)
        participants = room_state.get('participants', 2)

        # 1:1 or solo group: respond to everything
        if room_type == 1 or participants <= 2:
            return True

        text = msg.get('message', '').strip()

        # Slash commands always get through
        if text.startswith('/'):
            return True

        # Check for @bot-claude mention in messageParameters
        msg_params = msg.get('messageParameters', {})
        for param in msg_params.values():
            if isinstance(param, dict) and param.get('type') == 'user' and param.get('id') == self.nc.username:
                return True

        return False

    def _poll_room_loop(self, token):
        """Long-poll a single room. Runs as a blocking loop in its own thread."""
        room_name = self.rooms[token].get('name', '?')
        log.info(f'Long-poll thread started for room {token} ({room_name})')
        backoff = 5

        while self.running:
            try:
                room_state = self.rooms.get(token)
                if not room_state:
                    log.info(f'Room {token} removed, stopping thread')
                    break

                messages, new_last_id = self.nc.get_messages_for_room(
                    token,
                    last_known_id=room_state['last_known_id'],
                    limit=20,
                    look_into_future=True,
                    timeout=self.poll_timeout,
                )
                room_state['last_known_id'] = new_last_id
                backoff = 5  # reset on success

                for msg in messages:
                    if msg.get('actorId') == self.nc.username:
                        continue
                    if msg.get('actorType') != 'users':
                        continue

                    actor_id = msg.get('actorId', '')
                    if not actor_id:
                        continue

                    # Check if bot should respond (mention/solo logic)
                    if not self._should_respond(msg, room_state):
                        continue

                    # Detect file attachments
                    msg_type = msg.get('messageType', '')
                    msg_params = msg.get('messageParameters', {})
                    file_info = None
                    if isinstance(msg_params, dict):
                        fi = msg_params.get('file', {})
                        if isinstance(fi, dict) and fi.get('path'):
                            file_info = fi

                    if file_info:
                        mimetype = file_info.get('mimetype', '')
                        file_path = file_info['path']
                        file_name = file_info.get('name', Path(file_path).name)
                        is_voice = msg_type == 'voice-message' or mimetype.startswith('audio/')

                        log.info(f'[{token}:{actor_id}] File: {file_name} ({mimetype})')
                        try:
                            # Send initial status for voice messages
                            status_mid = None
                            if is_voice:
                                status_mid = self.nc.send_message(token, '🎤 Transkribiere Sprachnachricht...')

                            local_path = self._download_file(file_path)
                            if not local_path:
                                if status_mid:
                                    self.nc.edit_message(token, status_mid, f'Datei konnte nicht heruntergeladen werden: {file_name}')
                                else:
                                    self.nc.send_message(token, f'Datei konnte nicht heruntergeladen werden: {file_name}')
                                continue

                            if is_voice:
                                session = self._get_session(actor_id, token)
                                transcribe_only = session.transcribe_pending
                                if transcribe_only:
                                    session.transcribe_pending = False
                                # Voice/audio → transcribe, then either share .txt (transcribe-only)
                                # or pass to Claude as prompt.
                                transcription = self._transcribe_audio(local_path, delete_after=False)
                                try:
                                    os.unlink(local_path)
                                except Exception:
                                    pass
                                if not transcription:
                                    if status_mid:
                                        self.nc.edit_message(token, status_mid, 'Sprachnachricht konnte nicht transkribiert werden.')
                                    else:
                                        self.nc.send_message(token, 'Sprachnachricht konnte nicht transkribiert werden.')
                                    continue
                                if transcribe_only:
                                    ok = self._send_transcript_file(transcription, token, source_name=file_name)
                                    if status_mid:
                                        self.nc.edit_message(token, status_mid,
                                            '📝 Transkript geteilt.' if ok else 'Transkript-Upload fehlgeschlagen.')
                                    elif not ok:
                                        self.nc.send_message(token, 'Transkript-Upload fehlgeschlagen.')
                                    continue
                                # Pass transcription status message to session so worker reuses it
                                if status_mid:
                                    session.status_msg_id = status_mid
                                    session.status_room_token = token
                                prompt = f'[Sprachnachricht]: {transcription}'
                                self.handle_message(prompt, actor_id, token, is_voice=True)
                            else:
                                # Image/PDF/other → download, tell Claude the path
                                user_text = msg.get('message', '').strip()
                                user_text = re.sub(r'\{file\}', '', user_text).strip()
                                user_text = re.sub(r'\{mention-[^}]+\}', '', user_text).strip()
                                if user_text:
                                    prompt = f'Der User hat eine Datei gesendet ({file_name}). Die Datei liegt unter: {local_path}\n\nNachricht des Users: {user_text}'
                                else:
                                    prompt = f'Der User hat eine Datei gesendet ({file_name}). Bitte lies und analysiere die Datei: {local_path}'
                                self.handle_message(prompt, actor_id, token, temp_files=[local_path])
                        except Exception as e:
                            log.error(f'File handling error from {actor_id} in {token}: {e}')
                        continue

                    text = msg.get('message', '').strip()
                    if not text:
                        continue

                    try:
                        response = self.handle_message(text, actor_id, token)
                        if response:
                            self.nc.send_message(token, response)
                    except Exception as e:
                        log.error(f'Error handling message from {actor_id} in {token}: {e}')

                # Check active polls for votes in this room
                self._check_polls_for_room(token)

            except Exception as e:
                log.error(f'Poll error room {token}: {e}, retry in {backoff}s')
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

        self.room_threads.pop(token, None)
        log.info(f'Long-poll thread ended for room {token}')

    def run(self):
        log.info('Claude Bot starting (multi-user)...')

        # Initial room discovery
        self._discover_rooms()
        log.info(f'Found {len(self.rooms)} conversations (1:1 + groups)')

        def shutdown(signum, frame):
            log.info('Shutting down...')
            self.running = False
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        while self.running:
            try:
                self._discover_rooms()
            except Exception as e:
                log.error(f'Room discovery error: {e}')

            # Sleep 30s between discovery cycles (polling runs in threads)
            time.sleep(30)


if __name__ == '__main__':
    bot = ClaudeBot()
    bot.run()
