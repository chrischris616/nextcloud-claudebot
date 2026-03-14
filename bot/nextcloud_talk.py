"""
Nextcloud Talk Client - Shared library for sending/receiving messages via NC Talk OCS API.
Uses Basic Auth with real NC user accounts.
Supports long-polling for receiving messages.
"""

import json
import logging
import threading
import time
from base64 import b64encode
from http.client import HTTPSConnection
from urllib.parse import urlparse, quote

log = logging.getLogger(__name__)


class NextcloudTalkClient:
    """Send and receive messages via Nextcloud Talk OCS Chat API."""

    def __init__(self, base_url, username, password, notify_user):
        parsed = urlparse(base_url)
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.base_path = parsed.path.rstrip('/')
        self.username = username
        self.password = password
        self.notify_user = notify_user
        self._room_token = None
        self._last_known_id = 0

        auth_str = b64encode(f'{username}:{password}'.encode()).decode()
        self._headers = {
            'Authorization': f'Basic {auth_str}',
            'OCS-APIRequest': 'true',
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        }

    def _request(self, method, path, body=None, timeout=15):
        url = f'{self.base_path}{path}'
        try:
            conn = HTTPSConnection(self.host, self.port, timeout=timeout)
            conn.request(method, url, body=body, headers=self._headers)
            resp = conn.getresponse()
            data = resp.read().decode()
            conn.close()
            if resp.status in (200, 201):
                return json.loads(data)
            if resp.status == 304:
                return None  # No new messages (expected for long-polling)
            log.warning(f'NC Talk API {method} {path}: HTTP {resp.status}')
            return None
        except Exception as e:
            log.error(f'NC Talk API error: {e}')
            return None

    def get_or_create_conversation(self):
        """Get or create a 1:1 conversation with the notify_user. Returns room token."""
        if self._room_token:
            return self._room_token
        result = self._request(
            'POST',
            '/ocs/v2.php/apps/spreed/api/v4/room',
            body=f'roomType=1&invite={self.notify_user}',
        )
        if result and result.get('ocs', {}).get('data', {}).get('token'):
            self._room_token = result['ocs']['data']['token']
            return self._room_token
        log.error(f'Failed to create conversation with {self.notify_user}')
        return None

    def send_message(self, room_token, message):
        """Send a message to a specific room. Returns True on success."""
        body = f'message={quote(message)}'
        result = self._request(
            'POST',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}',
            body=body,
        )
        return result is not None

    def send(self, message):
        """Convenience: auto-resolve room token and send message. Returns True on success."""
        token = self.get_or_create_conversation()
        if not token:
            return False
        return self.send_message(token, message)

    def get_messages(self, limit=50, look_into_future=False, timeout=30):
        """Get messages from the conversation.

        If look_into_future=True, long-polls for new messages (blocks until
        new message arrives or timeout).
        Returns list of message dicts or empty list.
        """
        token = self.get_or_create_conversation()
        if not token:
            return []

        future = 1 if look_into_future else 0
        params = f'lookIntoFuture={future}&limit={limit}&timeout={timeout}'
        if self._last_known_id > 0:
            params += f'&lastKnownMessageId={self._last_known_id}'

        result = self._request(
            'GET',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{token}?{params}',
            timeout=timeout + 10,
        )
        if not result:
            return []

        messages = result.get('ocs', {}).get('data', [])
        if messages:
            # Update last known ID to the newest message
            max_id = max(m['id'] for m in messages)
            if max_id > self._last_known_id:
                self._last_known_id = max_id

        return messages

    def _init_last_known_id(self):
        """Set last_known_id to the latest message so we only get NEW messages."""
        token = self.get_or_create_conversation()
        if not token:
            return
        result = self._request(
            'GET',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{token}?lookIntoFuture=0&limit=1',
        )
        if result:
            messages = result.get('ocs', {}).get('data', [])
            if messages:
                self._last_known_id = max(m['id'] for m in messages)
                log.info(f'NC Talk: initialized at message ID {self._last_known_id}')

    def poll(self, callback, timeout=30):
        """Long-polling loop. Calls callback(message_text, actor_id) for each
        new message from other users (ignores own messages).

        callback should return a response string or None.
        Blocks indefinitely - run in a thread.
        """
        self._init_last_known_id()
        log.info('NC Talk polling started')

        while True:
            try:
                messages = self.get_messages(
                    limit=20, look_into_future=True, timeout=timeout
                )
                for msg in messages:
                    # Skip own messages and system messages
                    if msg.get('actorId') == self.username:
                        continue
                    if msg.get('actorType') != 'users':
                        continue

                    text = msg.get('message', '').strip()
                    actor = msg.get('actorId', '')
                    if not text:
                        continue

                    log.info(f'NC Talk message from {actor}: {text[:50]}')
                    try:
                        response = callback(text, actor)
                        if response:
                            self.send(response)
                    except Exception as e:
                        log.error(f'NC Talk callback error: {e}')

            except Exception as e:
                log.error(f'NC Talk poll error: {e}')
                time.sleep(5)

    def start_polling(self, callback, timeout=30):
        """Start polling in a background thread. Returns the thread."""
        thread = threading.Thread(
            target=self.poll, args=(callback, timeout), daemon=True
        )
        thread.start()
        return thread

    # --- Multi-room methods (for multi-user bots) ---

    def list_conversations(self):
        """List all conversations the bot user participates in.
        Returns list of conversation dicts with 'token', 'type', 'name', etc.
        """
        result = self._request(
            'GET',
            '/ocs/v2.php/apps/spreed/api/v4/room',
        )
        if not result:
            return []
        return result.get('ocs', {}).get('data', [])

    def get_messages_for_room(self, token, last_known_id=0, limit=20, look_into_future=True, timeout=5):
        """Get messages for a specific room token.
        Short-poll variant (low timeout) for multi-room iteration.
        Returns (messages_list, new_last_known_id).
        """
        future = 1 if look_into_future else 0
        params = f'lookIntoFuture={future}&limit={limit}&timeout={timeout}'
        if last_known_id > 0:
            params += f'&lastKnownMessageId={last_known_id}'

        result = self._request(
            'GET',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{token}?{params}',
            timeout=timeout + 10,
        )
        if not result:
            return [], last_known_id

        messages = result.get('ocs', {}).get('data', [])
        new_last_id = last_known_id
        if messages:
            max_id = max(m['id'] for m in messages)
            if max_id > new_last_id:
                new_last_id = max_id

        return messages, new_last_id

    def init_last_known_id_for_room(self, token):
        """Get the latest message ID for a room so we only process NEW messages.
        Returns the last known message ID (int).
        """
        result = self._request(
            'GET',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{token}?lookIntoFuture=0&limit=1',
        )
        if result:
            messages = result.get('ocs', {}).get('data', [])
            if messages:
                last_id = max(m['id'] for m in messages)
                log.info(f'NC Talk: room {token} initialized at message ID {last_id}')
                return last_id
        return 0
