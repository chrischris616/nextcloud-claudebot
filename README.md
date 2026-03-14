# Claude Bot — Nextcloud App

Permission management for a Claude AI assistant in Nextcloud Talk.

Control which users and groups are allowed to interact with a Claude AI bot through Nextcloud Talk conversations.

## Features

- **User permissions** — Allow individual users to chat with the Claude bot
- **Group permissions** — Allow entire Nextcloud groups at once
- **Admin UI** — Manage permissions from Settings → Administration → Claude Bot
- **REST API** — Bot queries the OCS API to verify permissions before responding
- **Autocomplete** — Search users and groups with Nextcloud's built-in autocomplete

## Requirements

- Nextcloud 28 — 32
- PHP 8.0+

## Installation

### From the App Store
Search for "Claude Bot" in your Nextcloud app management.

### Manual
1. Download the latest release archive
2. Extract to your Nextcloud `apps/` directory:
   ```bash
   tar xzf claudebot-*.tar.gz -C /path/to/nextcloud/apps/
   ```
3. Enable the app:
   ```bash
   occ app:enable claudebot
   ```

## Setup

1. **Create a bot user** in Nextcloud (default username: `bot-claude`)
2. **Configure the bot username** (if different from default):
   ```bash
   occ config:app:set claudebot bot_user --value="your-bot-user"
   ```
3. **Add permissions** — Go to Settings → Administration → Claude Bot and add users/groups
4. **Set up your bot service** to poll Nextcloud Talk and call the permission check API

## API

All endpoints use OCS and require the header `OCS-APIRequest: true`.

### Check permission (used by the bot)

```
GET /ocs/v2.php/apps/claudebot/api/v1/check/{userId}
```

Accessible by the configured bot user and admins. Returns:

```json
{"ocs": {"data": {"allowed": true, "reason": "user"}}}
```

### List permissions (admin only)

```
GET /ocs/v2.php/apps/claudebot/api/v1/permissions
```

### Add permission (admin only)

```
POST /ocs/v2.php/apps/claudebot/api/v1/permissions
Content-Type: application/json

{"type": "user", "target": "username"}
```

Type can be `user` or `group`.

### Remove permission (admin only)

```
DELETE /ocs/v2.php/apps/claudebot/api/v1/permissions/{id}
```

## Screenshots

![Admin UI](screenshots/admin-ui.png)

## License

AGPL-3.0-or-later — see [LICENSE](claudebot/LICENSE)
