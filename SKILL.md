---
name: nextcloud
description: Full-featured Nextcloud integration for Hermes — manage files, notes, tasks, calendar events, and contacts via WebDAV, Nextcloud Notes API, and CalDAV/CardDAV. Works with self-hosted Nextcloud instances.
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [Nextcloud, WebDAV, Notes, Calendar, Tasks, Contacts, CalDAV, CardDAV, Files, Self-hosted]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [google-workspace]
---

# Nextcloud

Full-featured integration with self-hosted Nextcloud. Manage files, notes, tasks, calendar events, and contacts using WebDAV, the Nextcloud Notes REST API, and CalDAV/CardDAV protocols.

Requires: **Nextcloud Notes app** installed on the instance (for Notes features). All other features work with a standard Nextcloud installation.

## Setup

Run the guided setup to configure credentials:

```bash
python3 ~/.hermes/skills/productivity/nextcloud/scripts/setup.py
```

The setup script will:
1. Ask for your Nextcloud URL, username, and App Password
2. Validate connectivity against your instance
3. Save credentials to `~/.hermes/nextcloud.env`

Alternatively, set environment variables manually:

```bash
export NEXTCLOUD_URL="https://your-nextcloud.example.com"
export NEXTCLOUD_USER="your_username"
export NEXTCLOUD_TOKEN="***"
export NEXTCLOUD_TIMEZONE="Europe/Prague"  # optional, defaults to UTC
```

### Creating an App Password

1. Log in to your Nextcloud instance
2. Go to **Settings → Security → App passwords**
3. Click **Add new app password**
4. Enter a name (e.g. `hermes-agent`) and click **Create**
5. Copy the generated password — it is shown only once

### Verifying the Setup

```bash
NC="python3 ~/.hermes/skills/productivity/nextcloud/scripts/nextcloud_api.py"
$NC check
```

Expected output: `{"status": "success", "data": {"authenticated": true, "user": "your_username"}}`

## Architecture

| Feature | Protocol | Endpoint |
|---------|----------|----------|
| Files | WebDAV | `/remote.php/dav/files/<user>/` |
| Notes | REST API | `/index.php/apps/notes/api/v1/notes` |
| Tasks | CalDAV (VTODO) | `/remote.php/dav/calendars/<user>/` |
| Calendar Events | CalDAV (VEVENT) | `/remote.php/dav/calendars/<user>/` |
| Contacts | CardDAV | `/remote.php/dav/addressbooks/<user>/` |

Authentication: HTTP Basic Auth with App Password (`user:token`).

## Usage

Define the convenience alias:

```bash
NC="python3 ~/.hermes/skills/productivity/nextcloud/scripts/nextcloud_api.py"
```

---

## Files (WebDAV)

### List directory contents

```bash
$NC files list --path "/Documents"
$NC files list  # root folder
```

Returns: `{"status": "success", "data": [{"path": "...", "type": "file|dir", "size": N, "last_modified": "HTTP-date", "etag": "..."}]}`

### Get file content

```bash
$NC files get --path "/Documents/notes.md"
```

### Upload file

```bash
$NC files upload --path "/Documents/new-file.txt" --content "File content here"
```

For large or sensitive content, pass it via stdin or a file instead of argv
(argv content is visible in the process list and capped at ~128 KiB):

```bash
$NC files upload --path "/Documents/report.pdf.txt" --content-file - < report.txt
$NC files upload --path "/Documents/notes.md" --content-file notes.md
```

### Create directory

```bash
$NC files mkdir --path "/Documents/projects"
```

### Delete file or directory

```bash
$NC files delete --path "/Documents/old-file.txt"
```

> Warning: Deletion is permanent and recursive for directories.

### Move / rename

```bash
$NC files move --src "/Documents/old-name.txt" --dst "/Documents/new-name.txt"
```

### Search files

```bash
$NC files search --query "budget"
```

> Note: search uses a `Depth: infinity` PROPFIND, which Nextcloud rejects with
> HTTP 400 unless `dav.propfind.depth_infinity` is enabled in `config.php`.
> On default instances, browse directories with `files list` instead.

---

## Notes (Nextcloud Notes API)

Requires the **Notes** app installed in Nextcloud.

### List notes

```bash
$NC notes list
$NC notes list --category "work"
```

### Get note by ID

```bash
$NC notes get --id 42
```

### Create note

```bash
$NC notes create --title "Meeting Notes" --content "Discussion points..."
$NC notes create --title "Work Note" --content "..." --category "work"
```

### Update note

```bash
$NC notes edit --id 42 --title "Updated Title" --content "New content"
$NC notes edit --id 42 --category "personal"
```

### Delete note

```bash
$NC notes delete --id 42
```

---

## Tasks (CalDAV VTODO)

### List task lists (calendars)

```bash
$NC calendars list --type tasks
```

### List tasks from a calendar

```bash
$NC tasks list
$NC tasks list --calendar "Personal"
```

### Create task

```bash
$NC tasks create --title "Finish quarterly report" --due "2026-06-30T17:00:00Z" --priority 5
$NC tasks create --title "Call with client" --calendar "Work" --due "2026-06-15T10:00:00Z" --priority 3
```

Priority: 0 = none, 1 = highest, 9 = lowest.

### Update task

```bash
$NC tasks edit --uid "abc123def" --title "New title" --priority 9
$NC tasks edit --uid "abc123def" --due "2026-07-01T12:00:00Z"
```

### Mark task complete

```bash
$NC tasks complete --uid "abc123def"
```

### Delete task

```bash
$NC tasks delete --uid "abc123def"
```

---

## Calendar Events (CalDAV VEVENT)

### List event calendars

```bash
$NC calendars list --type events
```

### List events

```bash
$NC calendar list
$NC calendar list --from "2026-07-01T00:00:00Z" --to "2026-07-07T23:59:59Z"
$NC calendar list --calendar "Work"
```

### Create event

```bash
$NC calendar create --summary "Team Standup" --start "2026-07-01T09:00:00Z" --end "2026-07-01T09:30:00Z"
$NC calendar create --summary "Conference" --start "2026-09-15T08:00:00Z" --end "2026-09-17T18:00:00Z" --location "Prague" --calendar "Work"
```

### Update event

```bash
$NC calendar edit --uid "xyz789" --summary "New Title"
$NC calendar edit --uid "xyz789" --start "2026-07-02T10:00:00Z" --end "2026-07-02T11:00:00Z"
```

### Delete event

```bash
$NC calendar delete --uid "xyz789"
```

---

## Contacts (CardDAV)

### List address books

```bash
$NC addressbooks list
```

### List contacts

```bash
$NC contacts list
$NC contacts list --addressbook "Personal"
```

### Search contacts

```bash
$NC contacts search --query "John"
```

### Get contact by UID

```bash
$NC contacts get --uid "contact-uid-string"
```

### Create contact

```bash
$NC contacts create --name "John Doe" --email "john@example.com" --phone "+420123456789"
$NC contacts create --name "Jane Smith" --email "jane@example.com" --organization "Acme Corp" --title "Engineer"
```

### Update contact

```bash
$NC contacts edit --uid "contact-uid-string" --phone "+420987654321" --email "newemail@example.com"
```

### Delete contact

```bash
$NC contacts delete --uid "contact-uid-string"
```

---

## Output Format

All commands return JSON:

```json
{"status": "success", "data": {...}}
{"status": "error", "message": "Descriptive error message"}
```

For list operations, `data` is always an array even if empty: `[]`.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXTCLOUD_URL` | Yes | Base URL of your Nextcloud instance (no trailing slash) |
| `NEXTCLOUD_USER` | Yes | Your Nextcloud username |
| `NEXTCLOUD_TOKEN` | Yes | App Password (from Settings → Security → App passwords) |

Credentials are stored in `~/.hermes/nextcloud.env` by the setup script. The skill reads them automatically if present.

---

## Error Handling

| HTTP Code | Meaning | Common Cause |
|-----------|---------|--------------|
| 401 | Unauthorized | Invalid or expired App Password |
| 403 | Forbidden | User lacks permission for this resource |
| 404 | Not Found | Wrong path, or Notes app not installed |
| 405 | Method Not Allowed | Resource already exists (e.g. MKCOL on an existing directory) |
| 409 | Conflict | Parent collection missing (e.g. upload into a non-existent directory) |
| 412 | Precondition Failed | ETag mismatch (concurrent edit) or destination exists (MOVE) |
| 424 | Failed Dependency | Feature not supported by server (e.g. Notes API) |

---

## Rules for Hermes Agent

1. **Confirm before destructive operations** — always show what will be deleted before running a delete command
2. **Use App Password, never primary password** — App Password can be revoked without affecting primary credentials
3. **Respect rate limits** — avoid rapid sequential API calls; batch reads where possible
4. **Verify Notes app availability** — if Notes API returns 404, inform the user the Notes app may not be installed
5. **Handle missing credentials gracefully** — if env vars are not set, prompt user to run `setup.py`
