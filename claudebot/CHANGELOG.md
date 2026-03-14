# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
