"""
Nextcloud Talk Client - Shared library for sending/receiving messages via NC Talk OCS API.
Uses Basic Auth with real NC user accounts.
Supports long-polling for receiving messages.
"""

import json
import logging
import threading
import time
import uuid
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

    def _request_raw(self, method, path, body=None, timeout=15):
        """Low-level request. Returns (status_code, parsed_json_or_None).
        status_code is None on connection errors.
        """
        url = f'{self.base_path}{path}'
        try:
            conn = HTTPSConnection(self.host, self.port, timeout=timeout)
            conn.request(method, url, body=body, headers=self._headers)
            resp = conn.getresponse()
            raw = resp.read().decode()
            conn.close()
            try:
                data = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                data = None
            return resp.status, data
        except Exception as e:
            log.error(f'NC Talk API error: {e}')
            return None, None

    def _request(self, method, path, body=None, timeout=15):
        status, data = self._request_raw(method, path, body=body, timeout=timeout)
        if status in (200, 201):
            return data
        if status == 304:
            return None  # No new messages (expected for long-polling)
        if status is not None:
            log.warning(f'NC Talk API {method} {path}: HTTP {status}')
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

    def send_message(self, room_token, message, timeout=15):
        """Send a message to a specific room. Returns message ID (int) on success, None on failure.

        Workaround for NC Talk 22.x bug: POST sometimes returns HTTP 400 with
        data.error="message" even though the message was delivered. We always
        attach a referenceId we generate; on a 400 we re-fetch and recover the
        message ID by referenceId so callers can still edit the message.
        """
        ref_id = uuid.uuid4().hex
        body = f'message={quote(message)}&referenceId={ref_id}'
        status, data = self._request_raw(
            'POST',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}',
            body=body,
            timeout=timeout,
        )
        if status in (200, 201) and data:
            msg_id = data.get('ocs', {}).get('data', {}).get('id')
            if msg_id:
                return msg_id
        # Recover from spurious 400: server rejected the parsed-message echo,
        # but the message itself usually got through. Look it up by referenceId.
        if status == 400 and data and data.get('ocs', {}).get('data', {}).get('error') == 'message':
            recovered = self._find_message_by_reference(room_token, ref_id, timeout=timeout)
            if recovered:
                log.info(f'NC Talk: send returned 400 but message was delivered (room {room_token}, id {recovered})')
                return recovered
            log.warning(f'NC Talk POST chat/{room_token}: HTTP 400, message NOT recovered (ref {ref_id[:8]})')
        elif status is not None and status not in (200, 201):
            log.warning(f'NC Talk POST chat/{room_token}: HTTP {status}')
        return None

    def _find_message_by_reference(self, room_token, reference_id, timeout=10):
        """Look up a recently sent message by referenceId. Returns int id or None."""
        status, data = self._request_raw(
            'GET',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}?lookIntoFuture=0&limit=15',
            timeout=timeout,
        )
        if status == 200 and data:
            for m in data.get('ocs', {}).get('data', []):
                if m.get('referenceId') == reference_id:
                    return m.get('id')
        return None

    def create_poll(self, room_token, question, options, max_votes=1):
        """Create a poll in a room. Returns poll ID on success, None on failure.
        Does not work in 1:1 conversations (NC Talk limitation).
        Uses resultMode=1 so votes are immediately visible for monitoring.
        """
        from urllib.parse import urlencode
        body = urlencode({
            'question': question,
            'options[]': options,
            'resultMode': 1,
            'maxVotes': max_votes,
        }, doseq=True)
        result = self._request(
            'POST',
            f'/ocs/v2.php/apps/spreed/api/v1/poll/{room_token}',
            body=body,
        )
        if result:
            return result.get('ocs', {}).get('data', {}).get('id')
        return None

    def get_poll(self, room_token, poll_id):
        """Get poll results. Returns poll data dict or None."""
        result = self._request(
            'GET',
            f'/ocs/v2.php/apps/spreed/api/v1/poll/{room_token}/{poll_id}',
        )
        if result:
            return result.get('ocs', {}).get('data')
        return None

    def close_poll(self, room_token, poll_id):
        """Close a poll. Returns poll data with final results or None."""
        result = self._request(
            'DELETE',
            f'/ocs/v2.php/apps/spreed/api/v1/poll/{room_token}/{poll_id}',
        )
        if result:
            return result.get('ocs', {}).get('data')
        return None

    def edit_message(self, room_token, message_id, new_message, timeout=15):
        """Edit an existing message. Returns True on success."""
        body = f'message={quote(new_message)}'
        result = self._request(
            'PUT',
            f'/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}/{message_id}',
            body=body,
            timeout=timeout,
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

    def get_participant_count(self, token):
        """Get the number of participants in a room.
        Returns int count, or 2 as fallback on error.
        """
        result = self._request(
            'GET',
            f'/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants',
        )
        if result:
            participants = result.get('ocs', {}).get('data', [])
            return len(participants)
        return 2  # Safe fallback: assume bot + 1 user

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
