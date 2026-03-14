#!/usr/bin/env python3
"""
Claude NC Talk Bot - Multi-User Edition.
Polls all 1:1 conversations, checks permissions via NC claudebot app,
maintains per-user Claude sessions.
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
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

from nextcloud_talk import NextcloudTalkClient

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
    """Per-user Claude session state."""

    def __init__(self, user_id, model='sonnet'):
        self.user_id = user_id
        self.session_id = str(uuid.uuid4())
        self.model = model
        self.message_count = 0
        self.created_at = datetime.now()
        self.last_active = datetime.now()
        self.busy = False
        self.session_created = False
        self.process = None

    def reset(self):
        old_id = self.session_id[:8]
        self.session_id = str(uuid.uuid4())
        self.message_count = 0
        self.session_created = False
        self.created_at = datetime.now()
        return old_id


class ClaudeBot:
    def __init__(self):
        self.cfg = load_config()
        nc_cfg = self.cfg['nextcloud']

        self.nc = NextcloudTalkClient(
            nc_cfg['base_url'], nc_cfg['username'],
            nc_cfg['password'], nc_cfg.get('notify_user', '')
        )

        claude_cfg = self.cfg.get('claude', {})
        self.default_model = claude_cfg.get('model', 'sonnet')
        self.max_response_length = claude_cfg.get('max_response_length', 3500)
        self.working_directory = claude_cfg.get('working_directory', str(Path.home()))
        self.max_turns = claude_cfg.get('max_turns', 0)

        cache_ttl = self.cfg.get('permission_cache_ttl', 300)
        self.permissions = PermissionChecker(self.nc, cache_ttl)
        self.admin_users = set(self.cfg.get('admin_users', []))

        self.sessions = {}
        self.rooms = {}

        self.start_time = datetime.now()
        self.total_messages = 0
        self.running = True
        self.poll_timeout = 30
        self.room_threads = {}

        log.info(f'Multi-user mode. Default model: {self.default_model}')

    def _get_session(self, user_id):
        if user_id not in self.sessions:
            self.sessions[user_id] = UserSession(user_id, self.default_model)
            log.info(f'New session for {user_id}: {self.sessions[user_id].session_id[:8]}...')
        return self.sessions[user_id]

    def _call_claude(self, message, session, room_token=None):
        """Call Claude Code CLI. Uses Popen — runs until finished or killed via /stop."""
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)
        env.pop('CLAUDE_CODE_SESSION', None)

        cmd = [
            'claude',
            '-p', message,
            '--model', session.model,
            '--dangerously-skip-permissions',
        ]

        if self.max_turns > 0:
            cmd.extend(['--max-turns', str(self.max_turns)])

        if session.session_created:
            cmd.extend(['--resume', session.session_id])
        else:
            cmd.extend(['--session-id', session.session_id])

        log.info(f'Calling Claude CLI ({session.model}) for {session.user_id}...')
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
            update_interval = 60
            next_update = start_time + update_interval
            update_count = 0
            status_msgs = ['Still working...', 'Taking a bit longer...', 'Still on it...']

            while True:
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    if time.time() >= next_update and room_token:
                        elapsed = int((time.time() - start_time) / 60)
                        msg = status_msgs[min(update_count, len(status_msgs) - 1)]
                        try:
                            self.nc.send_message(room_token, f'{msg} ({elapsed} min)')
                        except Exception:
                            pass
                        update_count += 1
                        next_update = time.time() + update_interval

            session.process = None

            if proc.returncode and proc.returncode < 0:
                return None

            output = proc.stdout.read().strip()
            stderr = proc.stderr.read().strip()

            if not output and stderr:
                output = f'Error: {stderr}'
            if not output:
                output = '(No response)'

            if proc.returncode == 0:
                session.session_created = True

            return output
        except FileNotFoundError:
            return 'Error: Claude CLI not found. Is claude installed and in PATH?'
        except Exception as e:
            return f'Error: {e}'

    def _truncate(self, text):
        if len(text) <= self.max_response_length:
            return text
        return text[:self.max_response_length - 20] + '\n...(truncated)'

    def cmd_clear(self, session):
        old_id = session.reset()
        return f'Session reset.\nOld session: {old_id}...\nNew session: {session.session_id[:8]}...'

    def cmd_model(self, session, args):
        valid_models = ['sonnet', 'opus', 'haiku']
        if not args:
            return f'Current model: {session.model}\nAvailable: {", ".join(valid_models)}'
        new_model = args[0].lower()
        if new_model not in valid_models:
            return f'Unknown model: {new_model}\nAvailable: {", ".join(valid_models)}'
        old_model = session.model
        session.model = new_model
        return f'Model changed: {old_model} -> {session.model}'

    def cmd_status(self, session, user_id):
        uptime = datetime.now() - self.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f'{hours}h {minutes}m {seconds}s'

        session_age = datetime.now() - session.created_at
        sh, sr = divmod(int(session_age.total_seconds()), 3600)
        sm, ss = divmod(sr, 60)

        lines = [
            f'Claude Bot Status',
            f'Model: {session.model}',
            f'Session: {session.session_id[:8]}...',
            f'Messages: {session.message_count}',
            f'Session age: {sh}h {sm}m {ss}s',
        ]

        if user_id in self.admin_users:
            lines.extend([
                f'---',
                f'Bot uptime: {uptime_str}',
                f'Active users: {len(self.sessions)}',
                f'Total messages: {self.total_messages}',
                f'Monitored rooms: {len(self.rooms)}',
                f'Working directory: {self.working_directory}',
            ])

        return '\n'.join(lines)

    def cmd_stop(self, session):
        if not session.busy or not session.process:
            return 'No running request to cancel.'
        try:
            pid = session.process.pid
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                session.process.kill()
            session.process.wait(timeout=5)
        except Exception:
            pass
        session.process = None
        session.busy = False
        return 'Request cancelled.'

    def cmd_help(self):
        return (
            'Claude Bot Commands:\n\n'
            '/clear - Start a new session\n'
            '/stop - Cancel running request\n'
            '/model [name] - Show/change model (sonnet, opus, haiku)\n'
            '/status - Session info\n'
            '/help - This help\n\n'
            'All other messages are forwarded to Claude Code.'
        )

    def process_command(self, text, session, user_id):
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
        elif cmd == '/status':
            return self.cmd_status(session, user_id)
        elif cmd == '/help':
            return self.cmd_help()

        return None

    def handle_message(self, text, actor_id, room_token):
        text = re.sub(r'<[^>]+>', '', text).strip()
        if not text:
            return None

        log.info(f'[{actor_id}] {text[:80]}{"..." if len(text) > 80 else ""}')

        if not self.permissions.is_allowed(actor_id):
            log.info(f'User {actor_id} not permitted, ignoring')
            return 'You do not have permission to use Claude Bot. Please contact an admin.'

        session = self._get_session(actor_id)
        session.last_active = datetime.now()

        response = self.process_command(text, session, actor_id)
        if response is not None:
            return response

        if session.busy:
            log.info(f'[{actor_id}] Session busy, queuing message')
            return 'Your previous request is still running... Please wait until it finishes.'

        session.busy = True
        session.message_count += 1
        self.total_messages += 1

        try:
            self.nc.send_message(room_token, 'Thinking...')
        except Exception:
            pass

        def _run_claude():
            try:
                response = self._call_claude(text, session, room_token)
                if response is None:
                    return
                response = self._truncate(response)
                log.info(f'[{actor_id}] Response: {len(response)} chars')
                self.nc.send_message(room_token, response)
            except Exception as e:
                log.error(f'[{actor_id}] Claude thread error: {e}')
                try:
                    self.nc.send_message(room_token, f'Error: {e}')
                except Exception:
                    pass
            finally:
                session.busy = False

        thread = threading.Thread(target=_run_claude, daemon=True)
        thread.start()
        return None

    def _start_room_thread(self, token):
        if token in self.room_threads and self.room_threads[token].is_alive():
            return
        thread = threading.Thread(
            target=self._poll_room_loop, args=(token,), daemon=True
        )
        thread.start()
        self.room_threads[token] = thread

    def _discover_rooms(self):
        conversations = self.nc.list_conversations()
        for conv in conversations:
            token = conv.get('token')
            conv_type = conv.get('type')
            if not token or conv_type != 1:
                continue

            if token not in self.rooms:
                last_id = self.nc.init_last_known_id_for_room(token)
                self.rooms[token] = {
                    'last_known_id': last_id,
                    'name': conv.get('displayName', '?'),
                }
                log.info(f'Discovered room {token} ({conv.get("displayName", "?")}), last_id={last_id}')

            self._start_room_thread(token)

    def _poll_room_loop(self, token):
        room_name = self.rooms[token].get('name', '?')
        log.info(f'Long-poll thread started for room {token} ({room_name})')
        backoff = 5

        while self.running:
            try:
                room_state = self.rooms.get(token)
                if not room_state:
                    break

                messages, new_last_id = self.nc.get_messages_for_room(
                    token,
                    last_known_id=room_state['last_known_id'],
                    limit=20,
                    look_into_future=True,
                    timeout=self.poll_timeout,
                )
                room_state['last_known_id'] = new_last_id
                backoff = 5

                for msg in messages:
                    if msg.get('actorId') == self.nc.username:
                        continue
                    if msg.get('actorType') != 'users':
                        continue

                    text = msg.get('message', '').strip()
                    actor_id = msg.get('actorId', '')
                    if not text or not actor_id:
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

        self._discover_rooms()
        log.info(f'Found {len(self.rooms)} 1:1 conversations')

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

            time.sleep(30)


if __name__ == '__main__':
    bot = ClaudeBot()
    bot.run()
