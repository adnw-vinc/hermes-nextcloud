# hermes-nextcloud

<p align="center">
  <img src="NC_Hermes.png" alt="hermes-nextcloud" width="400"/>
</p>

Connect your self-hosted Nextcloud instance to Hermes Agent. Manage files, notes, calendar events, tasks, and contacts directly from any conversation. No plugins, no external services, just your data.

## Status

![Nextcloud](https://img.shields.io/badge/Nextcloud-yes-green)
![Python](https://img.shields.io/badge/Python-3.8+-blue)
![License](https://img.shields.io/badge/License-MIT-purple)

## What is this?

hermes-nextcloud is a skill for Hermes Agent that wraps the Nextcloud WebDAV, Notes API, CalDAV, and CardDAV protocols into a command-line interface. If you run Nextcloud on your own VPS or home server, you can read and write your data without opening a browser.

The skill communicates over HTTPS only. No desktop client or third-party sync service is required.

## Features

**Files:** Upload, download, list, delete, and move files through Nextcloud WebDAV.

**Notes:** Create, list, edit, and delete notes using the Nextcloud Notes API (requires the Notes app on your Nextcloud instance).

**Calendar:** List calendars, create and edit events via CalDAV. Times are handled in your configured timezone.

**Tasks:** Manage Nextcloud Tasks calendar items (tasks stored in a Tasks calendar).

**Contacts:** Browse and search your address books, view contact cards, export as vCard.

**Setup:** Guided setup walks you through configuring your URL, username, and app password. Credentials are stored in `~/.hermes/nextcloud.env` with owner-only permissions (0600), and are never passed on the command line of subprocesses (a 0600 curl config file is used instead, so the app password does not leak via the process list).

## Requirements

- A running Nextcloud instance (any recent version)
- An **App Password** created in Nextcloud Settings → Security → App passwords
- Python 3.8 or newer
- Hermes Agent

## Installation

If Hermes Agent is running on the same machine where this repo is cloned:

```bash
cd ~/.hermes/skills/productivity
git clone https://github.com/adnw-vinc/hermes-nextcloud.git nextcloud
```

Alternatively, copy the `nextcloud/` directory into your skills folder manually.

## Setup

Run the guided setup:

```bash
python3 ~/.hermes/skills/productivity/nextcloud/scripts/setup.py
```

The script will ask for:
1. Your Nextcloud URL (e.g. `https://nc.example.com`)
2. Your Nextcloud username
3. An App Password (not your login password)

It validates the credentials and saves them to `~/.hermes/nextcloud.env`.

### Manual configuration

```bash
export NEXTCLOUD_URL="https://your-nextcloud.example.com"
export NEXTCLOUD_USER="your_username"
export NEXTCLOUD_TOKEN="your_app_password"
export NEXTCLOUD_TIMEZONE="Europe/Prague"   # optional, defaults to UTC
```

### Creating an App Password

1. Log in to your Nextcloud instance
2. Go to **Settings → Security → App passwords**
3. Click **Add new app password**
4. Give it a name (e.g. `hermes-agent`) and click **Create**
5. Copy the password. It is shown only once.

## Usage

All commands are called through the Python script:

```bash
NC="python3 ~/.hermes/skills/productivity/nextcloud/scripts/nextcloud_api.py"
```

### Files

```bash
# List files in a directory
$NC files list --path /Documents

# Upload a file
$NC files upload --local ./report.pdf --remote /Documents/report.pdf

# Download a file
$NC files download --remote /Documents/report.pdf --local ./report.pdf

# Delete a file
$NC files delete --path /Documents/old.txt
```

### Notes (requires Nextcloud Notes app)

```bash
# List all notes
$NC notes list

# Create a note
$NC notes create --title "Meeting notes" --content "Discussed the Q3 roadmap."

# Edit a note
$NC notes edit --id 941 --title "Updated title" --content "New content here."

# Delete a note
$NC notes delete --id 941
```

### Calendar

```bash
# List calendars
$NC calendar list

# List events in a calendar
$NC calendar list --calendar "Personal"

# Create an event
$NC calendar create \
    --summary "Team standup" \
    --start "2026-07-01T09:00:00Z" \
    --end "2026-07-01T09:30:00Z" \
    --calendar "Work"

# Edit an event
$NC calendar edit --uid <event-uid> --summary "New title"

# Delete an event
$NC calendar delete --uid <event-uid>
```

### Tasks

```bash
# List all tasks
$NC tasks list

# Create a task
$NC tasks create --summary "Review pull request" --due "2026-07-05"

# Complete a task
$NC tasks complete --uid <task-uid>

# Edit a task
$NC tasks edit --uid <task-uid> --summary "Updated summary"
```

### Contacts

```bash
# List address books
$NC contacts list

# List contacts in an address book
$NC contacts list --addressbook "Personal"

# Get a contact by UID
$NC contacts get --uid <contact-uid>

# Export a contact as vCard
$NC contacts export --uid <contact-uid> --local ./contact.vcf
```

### Verify the setup

```bash
$NC check
```

## Project structure

```
hermes-nextcloud/
├── README.md
├── LICENSE
├── SKILL.md                          # Skill manifest and documentation
└── scripts/
    ├── nextcloud_api.py              # Main CLI — all commands
    ├── setup.py                      # Guided credential setup
    └── requirements.txt              # Python dependencies (python-caldav)
```

The `SKILL.md` file is the skill manifest. Hermes Agent loads it automatically when the skill is installed.

## Security

Credentials are stored in `~/.hermes/nextcloud.env` with restricted file permissions (mode 0600). This file is never committed to git or shared anywhere.

The script only accepts an **App Password**, not your main Nextcloud password. App passwords can be revoked individually from Nextcloud Settings without affecting your main login.

## Contributing

Contributions are welcome. If you find a bug or want a new feature, open an issue or a pull request.

1. Fork the repository
2. Create a branch (`git checkout -b fix/something`)
3. Make your changes
4. Run a functional test against your own Nextcloud instance
5. Open a pull request

## License

MIT License. See [LICENSE](LICENSE) for the full text.

---

## Sponsored by

This project is proudly sponsored by [Adventure Does Not Wait](https://adventuredoesnotwait.com) - Sustainable outdoor apparel & accessories for adventure seekers. Organic cotton clothing designed for those who embrace exploration and protect our planet. Real photos no AI Slop! Don't wait for the perfect moment -- start your adventure today!
