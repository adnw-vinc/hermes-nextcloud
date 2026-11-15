#!/usr/bin/env python3
"""
Nextcloud setup script for Hermes Agent.
Guided (interactive) or non-interactive configuration of Nextcloud credentials.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

ENV_FILE = os.path.expanduser("~/.hermes/nextcloud.env")


def load_env():
    """Load credentials from nextcloud.env or environment variables."""
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    env.setdefault("NEXTCLOUD_URL", os.environ.get("NEXTCLOUD_URL", ""))
    env.setdefault("NEXTCLOUD_USER", os.environ.get("NEXTCLOUD_USER", ""))
    env.setdefault("NEXTCLOUD_TOKEN", os.environ.get("NEXTCLOUD_TOKEN", ""))
    return env


def save_env(url, user, token):
    """Save credentials to nextcloud.env."""
    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    with open(ENV_FILE, "w") as f:
        f.write(f'NEXTCLOUD_URL="{url}"\n')
        f.write(f'NEXTCLOUD_USER="{user}"\n')
        f.write(f'NEXTCLOUD_TOKEN="{token}"\n')
    # Restrict permissions
    os.chmod(ENV_FILE, 0o600)
    print(f"Credentials saved to {ENV_FILE}")


def _auth_args(user, token):
    """Return curl args carrying Basic auth credentials via a temporary
    0600 config file, so the secret never appears in the process list."""
    fd = tempfile.NamedTemporaryFile(
        "w", prefix="hermes-nc-auth-", suffix=".cfg", delete=False
    )
    value = f"{user}:{token}".replace("\\", "\\\\").replace('"', '\\"')
    fd.write(f'user = "{value}"\n')
    fd.close()
    os.chmod(fd.name, 0o600)
    return ["-K", fd.name], fd.name


def test_connection(url, user, token):
    """Test connection to Nextcloud and return user info."""
    auth_args, auth_file = _auth_args(user, token)
    try:
        # Try a simple PROPFIND on the root
        result = subprocess.run(
            [
                "curl", "--silent", "--show-error",
                *auth_args,
                "-X", "PROPFIND",
                "-H", "Depth: 0",
                f"{url.rstrip('/')}/remote.php/dav/files/{user}/",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

        if result.returncode != 0:
            return False, f"Curl error: {result.stderr}"

        if "<?xml" in result.stdout or "<d:href>" in result.stdout:
            return True, "connected"
        return False, f"Authentication failed. Response: {result.stdout[:200]}"
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.unlink(auth_file)
        except OSError:
            pass


def interactive_setup():
    """Interactive setup: ask for URL, user, token."""
    print("=" * 50)
    print("  Nextcloud Setup for Hermes Agent")
    print("=" * 50)
    print()

    # Load existing values for pre-filling
    existing = load_env()

    # Step 1: URL
    default_url = existing.get("NEXTCLOUD_URL", "")
    url = input(f"Nextcloud URL (e.g. https://cloud.example.com): ").strip()
    if not url:
        url = default_url
    while not url:
        url = input("Nextcloud URL (required): ").strip()

    # Step 2: Username
    default_user = existing.get("NEXTCLOUD_USER", "")
    user = input(f"Username [{default_user}]: ").strip()
    if not user:
        user = default_user
    while not user:
        user = input("Username (required): ").strip()

    # Step 3: App Password
    token = input("App Password (from Nextcloud → Settings → Security → App passwords): ").strip()
    while not token:
        token = input("App Password (required): ").strip()

    print()
    print("Testing connection...")
    ok, info = test_connection(url, user, token)

    if ok:
        print(f"✓ Connection successful!")
        print()
        save_env(url, user, token)
        print("Setup complete.")
    else:
        print(f"✗ Connection failed: {info}")
        print()
        retry = input("Retry? [Y/n]: ").strip().lower()
        if retry in ("", "y", "yes"):
            interactive_setup()
        else:
            print("Setup aborted.")
            sys.exit(1)


def non_interactive_setup(url, user, token):
    """Non-interactive setup for scripting."""
    print(f"Testing connection to {url}...")
    ok, info = test_connection(url, user, token)
    if ok:
        print(f"✓ Connection successful!")
        save_env(url, user, token)
        print("Setup complete.")
    else:
        print(f"✗ Connection failed: {info}")
        sys.exit(1)


def check():
    """Check if credentials are configured and valid."""
    env = load_env()
    url = env.get("NEXTCLOUD_URL", "")
    user = env.get("NEXTCLOUD_USER", "")
    token = env.get("NEXTCLOUD_TOKEN", "")

    if not url or not user or not token:
        print("NOT_CONFIGURED")
        print("Run: python3 setup.py to configure credentials.")
        sys.exit(1)

    print("Credentials found. Testing connection...")
    ok, info = test_connection(url, user, token)
    if ok:
        print(f"AUTHENTICATED")
        print(f"Version: {info}")
    else:
        print("NOT_AUTHENTICATED")
        print(f"Error: {info}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Nextcloud setup for Hermes Agent")
    parser.add_argument("--url", help="Nextcloud URL (non-interactive)")
    parser.add_argument("--user", help="Username (non-interactive)")
    parser.add_argument("--token", help="App Password (non-interactive)")
    parser.add_argument("--check", action="store_true", help="Check if configured and valid")
    parser.add_argument("--test", action="store_true", help="Test saved credentials")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing credentials")

    args = parser.parse_args()

    if args.check:
        check()
        return

    if args.test:
        env = load_env()
        if not all(env.get(k) for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_TOKEN")):
            print("NOT_CONFIGURED")
            sys.exit(1)
        ok, info = test_connection(env["NEXTCLOUD_URL"], env["NEXTCLOUD_USER"], env["NEXTCLOUD_TOKEN"])
        if ok:
            print(f"OK — {info}")
        else:
            print(f"FAILED — {info}")
            sys.exit(1)
        return

    # Check if already configured (unless --overwrite)
    existing = load_env()
    if all(existing.get(k) for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_TOKEN")) and not args.overwrite:
        print("Credentials already configured.")
        print(f"URL: {existing['NEXTCLOUD_URL']}")
        print(f"User: {existing['NEXTCLOUD_USER']}")
        print()
        overwrite = input("Overwrite? [y/N]: ").strip().lower()
        if overwrite not in ("y", "yes"):
            print("Aborted.")
            return

    # Run setup
    if args.url and args.user and args.token:
        non_interactive_setup(args.url, args.user, args.token)
    else:
        interactive_setup()


if __name__ == "__main__":
    main()
