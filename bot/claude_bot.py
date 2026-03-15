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
import signal
import sys
import uuid
import threading
import tempfile
import queue
from pathlib import Path
from datetime import datetime
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

        log.info(f'Multi-user mode. Default model: {self.default_model}')

    def _get_session(self, user_id, room_token):
        """Get or create a session for a user in a specific room."""
        key = (room_token, user_id)
        if key not in self.sessions:
            self.sessions[key] = UserSession(user_id, self.default_model)
            log.info(f'New session for {user_id} in room {room_token}: {self.sessions[key].session_id[:8]}...')
        return self.sessions[key]

    def _call_claude(self, message, session, room_token=None):
        """Call Claude Code CLI with the given message using user's session.
        Uses Popen with no timeout — runs until finished or killed via /stop.
        Sends periodic status updates to keep user informed.
        """
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)
        env.pop('CLAUDE_CODE_SESSION', None)

        cmd = [
            'claude',
            '-p', message,
            '--model', session.model,
            '--effort', session.effort,
            '--output-format', 'json',
            '--dangerously-skip-permissions',
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

            start_time = time.time()
            update_interval = 60  # Send status update every 60s
            next_update = start_time + update_interval
            update_count = 0
            status_msgs = ['⏳ Claude arbeitet noch...', '🔧 Dauert etwas länger...', '⚙️ Immer noch dran...']

            while True:
                try:
                    proc.wait(timeout=5)
                    break  # Process finished
                except subprocess.TimeoutExpired:
                    # Periodic status update — edit existing status message
                    if time.time() >= next_update and room_token:
                        elapsed = time.time() - start_time
                        msg = status_msgs[min(update_count, len(status_msgs) - 1)]
                        elapsed_min = int(elapsed / 60)
                        status_text = f'{msg} ({elapsed_min} Min)'
                        try:
                            if session.status_msg_id:
                                self.nc.edit_message(session.status_room_token, session.status_msg_id, status_text)
                            else:
                                mid = self.nc.send_message(room_token, status_text)
                                session.status_msg_id = mid
                                session.status_room_token = room_token
                        except Exception:
                            pass
                        update_count += 1
                        next_update = time.time() + update_interval

            session.process = None

            # Check if killed by /stop
            if proc.returncode and proc.returncode < 0:
                return None  # Signal kill — no response needed, /stop already sent message

            raw_output = proc.stdout.read().strip()
            stderr = proc.stderr.read().strip()

            # Parse JSON output for cost tracking and result text
            output = raw_output
            try:
                result_json = json.loads(raw_output)
                output = result_json.get('result', raw_output)
                # Track costs
                cost = result_json.get('total_cost_usd', 0)
                if cost:
                    session.total_cost += cost
                usage = result_json.get('usage', {})
                session.total_input_tokens += usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
                session.total_output_tokens += usage.get('output_tokens', 0)
                log.info(f'Cost: ${cost:.4f} (session total: ${session.total_cost:.4f})')
            except (json.JSONDecodeError, TypeError):
                pass  # Fallback to raw output

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

    def _transcribe_audio(self, audio_path):
        """Transcribe an audio file using faster-whisper. Returns transcribed text or None."""
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
            try:
                os.unlink(audio_path)
            except Exception:
                pass

    def cmd_clear(self, session):
        old_id = session.reset()
        return f'Session zurueckgesetzt.\nAlte Session: {old_id}...\nNeue Session: {session.session_id[:8]}...'

    def cmd_model(self, session, args):
        valid_models = ['sonnet', 'opus', 'haiku']
        if not args:
            return f'Dein Modell: {session.model}\nVerfuegbar: {", ".join(valid_models)}'
        new_model = args[0].lower()
        if new_model not in valid_models:
            return f'Unbekanntes Modell: {new_model}\nVerfuegbar: {", ".join(valid_models)}'
        old_model = session.model
        session.model = new_model
        return f'Modell gewechselt: {old_model} -> {session.model}'

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
            lines.extend([
                f'---',
                f'Bot-Uptime: {uptime_str}',
                f'Aktive Sessions: {len(self.sessions)}',
                f'Aktive User: {unique_users}',
                f'Gesamt-Nachrichten: {self.total_messages}',
                f'Ueberwachte Raeume: {len(self.rooms)}',
                f'Arbeitsverzeichnis: {self.working_directory}',
            ])

        return '\n'.join(lines)

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
            '/model [name] - Modell anzeigen/wechseln (sonnet, opus, haiku)\n'
            '/effort [level] - Effort-Level (low, medium, high, max)\n'
            '/cost - Token-Verbrauch & Kosten anzeigen\n'
            '/compact [fokus] - Kontext komprimieren\n'
            '/status - Session-Info\n'
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
        elif cmd == '/stop':
            return self.cmd_stop(session)
        elif cmd == '/model':
            return self.cmd_model(session, parts[1:])
        elif cmd == '/effort':
            return self.cmd_effort(session, parts[1:])
        elif cmd == '/cost':
            return self.cmd_cost(session)
        elif cmd == '/compact':
            return self.cmd_compact(session, parts[1:], room_token)
        elif cmd == '/status':
            return self.cmd_status(session, user_id, room_token)
        elif cmd == '/help':
            return self.cmd_help()

        return None

    def handle_message(self, text, actor_id, room_token, temp_files=None):
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
        session.queue.put((text, room_token, temp_files))

        if queued:
            log.info(f'[{room_token}:{actor_id}] Queued (queue size: {session.queue.qsize()})')
        else:
            # Send immediate "thinking" feedback and track message ID for editing
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
                    text, room_token, temp_files = session.queue.get(timeout=1)
                except queue.Empty:
                    session._worker_running = False
                    session.busy = False
                    return

                session.busy = True
                # Send status message if not already present (queued messages)
                if not session.status_msg_id:
                    try:
                        mid = self.nc.send_message(room_token, '💭 Claude denkt nach...')
                        session.status_msg_id = mid
                        session.status_room_token = room_token
                    except Exception:
                        pass
                try:
                    response = self._call_claude(text, session, room_token)
                    if response is not None:
                        response = self._truncate(response)
                        log.info(f'[{room_token}:{actor_id}] Response: {len(response)} chars')
                        # Edit status message with the response, or send new if edit fails
                        sent = False
                        if session.status_msg_id:
                            try:
                                sent = self.nc.edit_message(session.status_room_token, session.status_msg_id, response)
                            except Exception:
                                pass
                        if not sent:
                            self.nc.send_message(room_token, response)
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
                            local_path = self._download_file(file_path)
                            if not local_path:
                                self.nc.send_message(token, f'Datei konnte nicht heruntergeladen werden: {file_name}')
                                continue

                            if is_voice:
                                # Voice/audio → transcribe, then send text to Claude
                                transcription = self._transcribe_audio(local_path)
                                if not transcription:
                                    self.nc.send_message(token, 'Sprachnachricht konnte nicht transkribiert werden.')
                                    continue
                                prompt = f'[Sprachnachricht]: {transcription}'
                                response = self.handle_message(prompt, actor_id, token)
                            else:
                                # Image/PDF/other → download, tell Claude the path
                                user_text = msg.get('message', '').strip()
                                # Clean mention placeholders from accompanying text
                                user_text = re.sub(r'\{file\}', '', user_text).strip()
                                user_text = re.sub(r'\{mention-[^}]+\}', '', user_text).strip()
                                if user_text:
                                    prompt = f'Der User hat eine Datei gesendet ({file_name}). Die Datei liegt unter: {local_path}\n\nNachricht des Users: {user_text}'
                                else:
                                    prompt = f'Der User hat eine Datei gesendet ({file_name}). Bitte lies und analysiere die Datei: {local_path}'
                                response = self.handle_message(prompt, actor_id, token, temp_files=[local_path])

                            if response:
                                self.nc.send_message(token, response)
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
