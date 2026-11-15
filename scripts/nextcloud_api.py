#!/usr/bin/env python3
"""
Nextcloud API wrapper for Hermes Agent.
Provides unified access to Nextcloud via WebDAV, Notes API, and CalDAV/CardDAV.
"""

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_FILE = os.path.expanduser("~/.hermes/nextcloud.env")


def load_env(env_file=None):
    """Load credentials from nextcloud.env or environment variables."""
    env_file = env_file or ENV_FILE
    env = {}
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    # Environment variables take precedence
    env.setdefault("NEXTCLOUD_URL", os.environ.get("NEXTCLOUD_URL", ""))
    env.setdefault("NEXTCLOUD_USER", os.environ.get("NEXTCLOUD_USER", ""))
    env.setdefault("NEXTCLOUD_TOKEN", os.environ.get("NEXTCLOUD_TOKEN", ""))
    env.setdefault("NEXTCLOUD_TIMEZONE", os.environ.get("NEXTCLOUD_TIMEZONE", "UTC"))
    return env


def check_credentials(env):
    """Validate that required credentials are present."""
    missing = [k for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_TOKEN") if not env.get(k)]
    if missing:
        print(json.dumps({
            "status": "error",
            "message": f"Missing credentials: {', '.join(missing)}. Run: python3 setup.py"
        }))
        sys.exit(1)


# HTTP status codes treated as success by the API wrappers.
# 207 Multi-Status is the normal WebDAV/CalDAV success code for PROPFIND.
HTTP_OK = (200, 201, 204, 207)

_temp_auth_files = []


def _cleanup_temp_auth_files():
    for path in _temp_auth_files:
        try:
            os.unlink(path)
        except OSError:
            pass


atexit.register(_cleanup_temp_auth_files)


def _auth_args(env):
    """Return curl args carrying Basic auth credentials WITHOUT exposing them
    in the process list.

    The credentials are written to a temporary curl config file (chmod 0600)
    that curl reads via -K. Passing them as `-u user:token` would leak them to
    any local user via `ps` / /proc/<pid>/cmdline.
    """
    fd = tempfile.NamedTemporaryFile(
        "w", prefix="hermes-nc-auth-", suffix=".cfg", delete=False
    )
    value = f"{env['NEXTCLOUD_USER']}:{env['NEXTCLOUD_TOKEN']}"
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    fd.write(f'user = "{value}"\n')
    fd.close()
    os.chmod(fd.name, 0o600)
    _temp_auth_files.append(fd.name)
    return ["-K", fd.name]


def curl_base(env, accept="application/json"):
    """Return curl base args with basic auth (via 0600 config file, not argv)."""
    return [
        "curl", "--silent", "--show-error",
        *_auth_args(env),
        "-H", f"Accept: {accept}",
    ]


def _quote_path(path):
    """Percent-encode a user-supplied path for use in a URL (preserves '/')."""
    return urllib.parse.quote(path.lstrip("/"), safe="/")


def dav_url(env, path=""):
    """Build WebDAV URL for files endpoint."""
    base = env["NEXTCLOUD_URL"].rstrip("/")
    user = urllib.parse.quote(env["NEXTCLOUD_USER"], safe="")
    return f"{base}/remote.php/dav/files/{user}/{_quote_path(path)}"


def notes_url(env, path="", params=None):
    """Build Nextcloud Notes API URL."""
    base = env["NEXTCLOUD_URL"].rstrip("/")
    url = f"{base}/index.php/apps/notes/api/v1/{_quote_path(path)}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def caldav_url(env, path=""):
    """Build CalDAV URL."""
    base = env["NEXTCLOUD_URL"].rstrip("/")
    user = urllib.parse.quote(env["NEXTCLOUD_USER"], safe="")
    return f"{base}/remote.php/dav/calendars/{user}/{_quote_path(path)}"


def carddav_url(env, path=""):
    """Build CardDAV URL."""
    base = env["NEXTCLOUD_URL"].rstrip("/")
    user = urllib.parse.quote(env["NEXTCLOUD_USER"], safe="")
    path = path.lstrip("/")
    # Address books are under /addressbooks/users/<user>/
    if path.startswith("users/") or path.startswith("system/"):
        return f"{base}/remote.php/dav/addressbooks/{_quote_path(path)}"
    return f"{base}/remote.php/dav/addressbooks/users/{user}/{_quote_path(path)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def curl_request(args, input_data=None, timeout=30):
    """Execute curl and return (http_code, body, error_message).

    The HTTP status code is captured via curl's `-w` write-out and returned to
    callers: curl exits 0 even when the server answers 4xx/5xx, so the exit
    code alone cannot distinguish success from a server-side error.
    A non-None error_message means the request did not complete (network
    error, timeout, ...).
    """
    full_args = args + ["-w", "\n%{http_code}"]
    try:
        result = subprocess.run(
            full_args,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 0, "", f"Request timed out after {timeout} seconds"
    except Exception as e:  # defensive
        return 0, "", str(e)

    body = result.stdout or ""
    code = 0
    last_nl = body.rfind("\n")
    if last_nl != -1:
        tail = body[last_nl + 1:].strip()
        if tail.isdigit():
            code = int(tail)
            body = body[:last_nl]

    if result.returncode != 0:
        err = (result.stderr or "").strip() or f"curl exited with code {result.returncode}"
        return code, body, err
    if code == 0:
        return 0, body, "curl did not report an HTTP status code"
    return code, body, None


def http_error(code, err, action):
    """Build a uniform error response for a failed HTTP request."""
    if err:
        message = f"{action} failed: {err}"
    elif code == 401:
        message = f"{action} failed (HTTP 401): authentication rejected - check NEXTCLOUD_USER and app password"
    else:
        message = f"{action} failed (HTTP {code})"
    return {"status": "error", "message": message}


def xml_propfind(url, env, depth="1", props=None):
    """Send a PROPFIND request and return parsed results using XML parser."""
    if props is None:
        props = """
            <d:prop>
                <d:displayname/>
                <d:getcontenttype/>
                <d:getcontentlength/>
                <d:getlastmodified/>
                <d:resourcetype/>
                <d:etag/>
            </d:prop>
        """

    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns" xmlns:cal="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/" xmlns:card="urn:ietf:params:xml:ns:carddav">
{props}
</d:propfind>"""

    args = curl_base(env, accept="application/xml") + [
        "-X", "PROPFIND",
        "-H", f"Depth: {depth}",
        "-H", "Content-Type: application/xml",
        "--data-binary", "@-",
        url,
    ]
    code, body, err = curl_request(args, input_data=xml_body, timeout=60)
    if err:
        return {"status": "error", "message": f"PROPFIND failed: {err}", "http_code": code}
    if code != 207:
        return {"status": "error", "message": f"PROPFIND failed (HTTP {code})", "http_code": code}

    # Parse XML properly
    try:
        # Strip XML declaration if present for ET compatibility
        xml_text = body
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # Fallback: try stripping namespace prefixes that ET doesn't handle well
        try:
            # Remove namespace prefixes from tags but keep tag names
            cleaned = re.sub(r'<(\w+):(\w+)', r'<\2', xml_text)
            cleaned = re.sub(r'</(\w+):(\w+)>', r'</\2>', cleaned)
            root = ET.fromstring(cleaned)
        except Exception:
            return {"error": f"Failed to parse XML response: {xml_text[:200]}"}

    results = []
    # Find all d:response elements (handle both namespaced and non-namespaced)
    for resp in root.findall(".//{DAV:}response") + root.findall(".//response"):
        href_elem = resp.find("{DAV:}href")
        if href_elem is None:
            href_elem = resp.find("href")
        href = href_elem.text if href_elem is not None else ""

        # Skip root/parent entries
        user_path_prefix = f"/remote.php/dav/files/{env['NEXTCLOUD_USER']}"
        if href.rstrip("/") in (user_path_prefix, user_path_prefix + "/", ""):
            continue

        # Parse ALL props from ALL propstat elements
        # Each propstat contains a status and a set of properties
        all_props = {}
        for propstat in resp.findall("{DAV:}propstat") + resp.findall("propstat"):
            # Get status - only process successful props (HTTP 200/OK)
            status_elem = propstat.find("{DAV:}status")
            if status_elem is None:
                status_elem = propstat.find("status")
            if status_elem is not None and "200" not in status_elem.text:
                continue  # Skip properties that failed

            # Collect all property values in this propstat
            for prop in propstat.findall("{DAV:}prop") + propstat.findall("prop"):
                for child in prop:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    all_props[tag] = child.text if child.text else ""

        # Determine resource type
        res_type = "file"
        if "resourcetype" in all_props:
            if "collection" in all_props["resourcetype"]:
                res_type = "dir"
            if "calendar" in all_props["resourcetype"]:
                res_type = "calendar"

        # Also check for calendar tag directly
        for propstat in resp.findall("{DAV:}propstat") + resp.findall("propstat"):
            for prop in propstat.findall("{DAV:}prop") + propstat.findall("prop"):
                for child in prop:
                    if "calendar" in child.tag.lower():
                        res_type = "calendar"
                        break

        results.append({
            "href": href,
            "type": res_type,
            "displayname": all_props.get("displayname", ""),
            "etag": all_props.get("etag", "").strip('"'),
            "last_modified": all_props.get("getlastmodified", ""),
            "content_type": all_props.get("getcontenttype", ""),
            "size": int(all_props.get("getcontentlength", 0)) if all_props.get("getcontentlength", "").isdigit() else 0,
            "getctag": all_props.get("getctag", ""),
        })

    return results


def ical_escape(text):
    """Escape text for use as an iCalendar TEXT property value (RFC 5545 3.3.11)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def vcard_escape(text):
    """Escape text for use as a vCard property value (backslashes, newlines)."""
    return (
        text.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def ical_datetime(value):
    """Convert a human datetime to iCalendar format.

    Returns (formatted_string, is_date_only).
    """
    value = value.strip()
    # Preserve a trailing Z (UTC designator): fromisoformat() accepts it on
    # Python 3.11+ but silently drops it, which would turn a UTC time into a
    # floating local time.
    is_utc = value.endswith("Z")
    if is_utc:
        value = value[:-1]
    suffix = "Z" if is_utc else ""
    # Handle date-only: YYYY-MM-DD
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return value.replace("-", ""), True
    # Handle ISO format with T: YYYY-MM-DDTHH:MM:SS or YYYY-MM-DDTHH:MM
    if "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace(" ", "T"))
            return parsed.strftime("%Y%m%dT%H%M%S") + suffix, False
        except ValueError:
            pass
    # Handle space-separated: YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return parsed.strftime("%Y%m%dT%H%M%S") + suffix, False
    except ValueError:
        pass
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
        return parsed.strftime("%Y%m%dT%H%M%S") + suffix, False
    except ValueError:
        pass
    # Fallback: return as-is
    return value + suffix, "T" in value


def ical_replace_properties(body, updates, component):
    """Replace (or insert) property lines inside the first component block.

    Keeps every other property intact instead of rebuilding the object from a
    parsed subset, so alarms, categories and similar metadata survive edits.
    `updates` maps property base names (e.g. "SUMMARY") to full replacement
    lines (e.g. "SUMMARY:New title"). Original line parameters (e.g.
    DTSTART;TZID=...) are preserved on the replacement line.
    """
    begin, end = f"BEGIN:{component}", f"END:{component}"
    if begin not in body or end not in body:
        return None
    pre, rest = body.replace("\r\n", "\n").split(begin, 1)
    block, post = rest.split(end, 1)
    lines = block.split("\n")

    remaining = dict(updates)
    out = []
    for line in lines:
        name_part = line.split(":", 1)[0] if line.strip() else ""
        base = name_part.split(";", 1)[0].strip().upper()
        if line.strip() and base in remaining:
            new_line = remaining.pop(base)
            orig_params = name_part.split(";", 1)[1] if ";" in name_part else None
            new_name_part = new_line.split(":", 1)[0]
            if orig_params and ";" not in new_name_part:
                new_value = new_line.split(":", 1)[1] if ":" in new_line else ""
                out.append(f"{new_name_part};{orig_params}:{new_value}")
            else:
                out.append(new_line)
        else:
            out.append(line)

    if remaining:
        # Insert properties that were not present in the original block.
        insert_at = len(out) - 1 if out and not out[-1].strip() else len(out)
        for new_line in remaining.values():
            out.insert(insert_at, new_line)
            insert_at += 1
    return pre + begin + "\n".join(out) + end + post


def vcard_from_args(name, email=None, phone=None, organization=None, title=None, note=None):
    """Build a VCARD string from contact fields."""
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{vcard_escape(name)}",
    ]
    parts = name.rsplit(" ", 1)
    if len(parts) == 2:
        lines.append(f"N:{vcard_escape(parts[1]).replace(';', '\\;')};{vcard_escape(parts[0]).replace(';', '\\;')};;;")
    else:
        lines.append(f"N:;{vcard_escape(name).replace(';', '\\;')};;;")
    if email:
        for e in email.split(","):
            lines.append(f"EMAIL;TYPE=internet:{vcard_escape(e.strip())}")
    if phone:
        for p in phone.split(","):
            lines.append(f"TEL;TYPE=voice:{vcard_escape(p.strip())}")
    if organization:
        lines.append(f"ORG:{vcard_escape(organization)}")
    if title:
        lines.append(f"TITLE:{vcard_escape(title)}")
    if note:
        lines.append(f"NOTE:{vcard_escape(note)}")
    lines.append("END:VCARD")
    return "\n".join(lines)


def vtodo_from_args(title, due=None, priority=None, description=None, status=None, uid=None):
    """Build a VTODO iCalendar component from fields."""
    if uid is None:
        uid = f"hermes-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Hermes Agent//Nextcloud Task//EN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{ical_escape(title)}",
    ]
    if due:
        due_str, is_date = ical_datetime(due)
        if is_date:
            lines.append(f"DUE;VALUE=DATE:{due_str}")
        else:
            lines.append(f"DUE:{due_str}")
    if priority is not None:
        lines.append(f"PRIORITY:{priority}")
    if description:
        lines.append(f"DESCRIPTION:{ical_escape(description)}")
    if status:
        lines.append(f"STATUS:{status}")
    lines.append("END:VTODO")
    lines.append("END:VCALENDAR")
    return "\n".join(lines), uid


def vevent_from_args(summary, start, end, location=None, description=None, uid=None, tzid="UTC"):
    """Build a VEVENT iCalendar component from fields.
    Converts common datetime formats to iCalendar DATE-TIME format (YYYYMMDDTHHMMSS or YYYYMMDD).
    tzid: timezone identifier (e.g., 'UTC', 'Europe/Prague'). Defaults to UTC.
    """
    start_str, start_date = ical_datetime(start)
    end_str, end_date = ical_datetime(end)

    # Determine if these are all-day events (date-only)
    all_day = start_date == True and end_date == True

    if uid is None:
        uid = f"hermes-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Hermes Agent//Nextcloud Event//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
    ]

    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{start_str}")
        lines.append(f"DTEND;VALUE=DATE:{end_str}")
    else:
        # Use configured timezone or UTC
        lines.append(f"DTSTART;TZID={tzid}:{start_str}")
        lines.append(f"DTEND;TZID={tzid}:{end_str}")

    lines.append(f"SUMMARY:{ical_escape(summary)}")
    if location:
        lines.append(f"LOCATION:{ical_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{ical_escape(description)}")
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\n".join(lines)


def parse_icalendar(text, item_type="VEVENT"):
    """Parse a simple iCalendar component and return a dict.
    Only extracts fields from the target component block (VEVENT or VTODO).
    Uses split-based extraction for robustness with CRLF line endings.
    """
    result = {}

    # Extract the component block using split (more reliable than regex with CRLF)
    marker = f"BEGIN:{item_type}"
    if marker not in text:
        return result

    after_marker = text.split(marker, 1)[1]
    block = after_marker.split(f"END:{item_type}", 1)[0]

    # Helper to extract field value from block
    def get_field(block_text, field_name):
        prefix = f"{field_name}:"
        for line in block_text.split('\n'):
            line = line.strip()
            if line.startswith(prefix):
                return line[len(prefix):].strip()
            # Also try with parameters (e.g., DTSTART;TZID=Europe/Prague:...)
            if line.startswith(f"{field_name};") and ':' in line:
                return line.split(':', 1)[1].strip()
        return None

    result["uid"] = get_field(block, "UID") or ""
    result["summary"] = get_field(block, "SUMMARY") or ""
    result["start"] = get_field(block, "DTSTART") or ""
    result["end"] = get_field(block, "DTEND") or ""
    result["due"] = get_field(block, "DUE") or ""
    result["location"] = get_field(block, "LOCATION") or ""
    result["description"] = get_field(block, "DESCRIPTION") or ""

    pri = get_field(block, "PRIORITY")
    if pri:
        try:
            result["priority"] = int(pri)
        except ValueError:
            pass

    status = get_field(block, "STATUS")
    if status:
        result["status"] = status

    return result


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_check(env):
    """Verify credentials and connectivity."""
    url = dav_url(env, "")
    args = curl_base(env, accept="application/xml") + [
        "-X", "PROPFIND",
        "-H", "Depth: 0",
        url,
    ]
    code, body, err = curl_request(args)
    if code in (207, 200):
        return {
            "status": "success",
            "data": {
                "authenticated": True,
                "user": env["NEXTCLOUD_USER"],
            }
        }
    if err:
        return {"status": "error", "message": err}
    if code == 401:
        return {"status": "error", "message": "Authentication failed: check username and app password"}
    return {"status": "error", "message": f"Authentication failed (HTTP {code})"}


def _dav_user_prefix(env):
    """Return the URL-encoded WebDAV path prefix used in server hrefs."""
    return f"/remote.php/dav/files/{urllib.parse.quote(env['NEXTCLOUD_USER'], safe='')}"


def cmd_files_list(env, path="/"):
    """List files in a directory via WebDAV."""
    url = dav_url(env, path)
    results = xml_propfind(url, env, depth="1")
    if isinstance(results, dict) and "error" in results:
        return results
    user_path = _dav_user_prefix(env)
    formatted = []
    for r in results:
        href = r["href"]
        # Extract path relative to user (href is percent-encoded; decode for display)
        item_path = href[len(user_path):] if href.startswith(user_path) else href
        formatted.append({
            "path": "/" + urllib.parse.unquote(item_path).lstrip("/"),
            "type": r.get("type", "file"),
            "size": r.get("size", 0),
            "content_type": r.get("content_type", ""),
            "last_modified": r.get("last_modified", ""),
            "etag": r.get("etag", ""),
        })
    return {"status": "success", "data": formatted}


def cmd_files_get(env, path):
    """Get file content."""
    url = dav_url(env, path)
    args = curl_base(env) + ["-X", "GET", url]
    code, body, err = curl_request(args, timeout=120)
    if err:
        return http_error(code, err, f"Get '{path}'")
    if code == 404:
        return {"status": "error", "message": f"File not found: {path}"}
    if code != 200:
        return http_error(code, None, f"Get '{path}'")
    return {"status": "success", "data": {"path": path, "content": body}}


def cmd_files_upload(env, path, content):
    """Upload a file via PUT. Content is streamed via stdin (never argv),
    which also removes the ~128 KiB command-line argument size limit."""
    url = dav_url(env, path)
    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: application/octet-stream",
        "--data-binary", "@-",
        url,
    ]
    code, body, err = curl_request(args, input_data=content, timeout=120)
    if err:
        return http_error(code, err, f"Upload to '{path}'")
    if code not in (200, 201, 204):
        return http_error(code, None, f"Upload to '{path}'")
    return {"status": "success", "data": {"path": path, "uploaded": True}}


def cmd_files_mkdir(env, path):
    """Create a directory via MKCOL."""
    url = dav_url(env, path)
    args = curl_base(env) + ["-X", "MKCOL", url]
    code, body, err = curl_request(args)
    if err:
        return http_error(code, err, f"mkdir '{path}'")
    if code == 405:
        return {"status": "error", "message": f"Directory already exists: {path}"}
    if code not in (200, 201):
        return http_error(code, None, f"mkdir '{path}'")
    return {"status": "success", "data": {"path": path, "created": True}}


def cmd_files_delete(env, path):
    """Delete a file or directory via DELETE."""
    url = dav_url(env, path)
    args = curl_base(env) + ["-X", "DELETE", url]
    code, body, err = curl_request(args)
    if err:
        return http_error(code, err, f"Delete '{path}'")
    if code == 404:
        return {"status": "error", "message": f"Not found: {path}"}
    if code not in (200, 204):
        return http_error(code, None, f"Delete '{path}'")
    return {"status": "success", "data": {"path": path, "deleted": True}}


def cmd_files_move(env, src, dst):
    """Move or rename a file via MOVE."""
    src_url = dav_url(env, src)
    dst_url = dav_url(env, dst)
    args = curl_base(env) + [
        "-X", "MOVE",
        "-H", f"Destination: {dst_url}",
        src_url,
    ]
    code, body, err = curl_request(args)
    if err:
        return http_error(code, err, f"Move '{src}' -> '{dst}'")
    if code == 404:
        return {"status": "error", "message": f"Source not found: {src}"}
    if code == 412:
        return {"status": "error", "message": f"Destination already exists: {dst}"}
    if code not in (200, 201, 204):
        return http_error(code, None, f"Move '{src}' -> '{dst}'")
    return {"status": "success", "data": {"src": src, "dst": dst, "moved": True}}


def cmd_files_search(env, query):
    """Search for files by name using PROPFIND."""
    # Search all files recursively for the query string in filename
    url = dav_url(env, "")
    results = xml_propfind(url, env, depth="infinity")
    if isinstance(results, dict) and "error" in results:
        if results.get("http_code") == 400:
            return {"status": "error", "message": "Server rejected Depth: infinity search (Nextcloud disables it by default; set 'dav.propfind.depth_infinity' => true in config.php, or browse directories instead)"}
        return results
    query_lower = query.lower()
    user_path = _dav_user_prefix(env)
    matched = []
    for r in results:
        href = r["href"]
        item_path = urllib.parse.unquote(href[len(user_path):] if href.startswith(user_path) else href)
        filename = item_path.split("/")[-1]
        if query_lower in filename.lower():
            matched.append({
                "path": "/" + item_path.lstrip("/"),
                "type": r["type"],
                "size": r["size"],
            })
    return {"status": "success", "data": matched}


def cmd_notes_list(env, category=None):
    """List all notes via Nextcloud Notes API."""
    url = notes_url(env, "notes", params={"category": category} if category else None)
    args = curl_base(env) + ["-X", "GET", url]
    code, body, err = curl_request(args)
    if err:
        return http_error(code, err, "List notes")
    if code == 404:
        return {"status": "error", "message": "Notes API returned 404 - is the Notes app installed?"}
    if code != 200:
        return http_error(code, None, "List notes")
    try:
        notes = json.loads(body)
        return {"status": "success", "data": notes}
    except json.JSONDecodeError:
        return {"status": "error", "message": f"Failed to parse notes response: {body[:200]}"}


def cmd_notes_get(env, note_id):
    """Get a single note by ID."""
    url = notes_url(env, f"notes/{note_id}")
    args = curl_base(env) + ["-X", "GET", url]
    code, body, err = curl_request(args)
    if err:
        return http_error(code, err, f"Get note {note_id}")
    if code == 404:
        return {"status": "error", "message": f"Note {note_id} not found (or the Notes app is not installed)"}
    if code != 200:
        return http_error(code, None, f"Get note {note_id}")
    try:
        note = json.loads(body)
        return {"status": "success", "data": note}
    except json.JSONDecodeError:
        return {"status": "error", "message": f"Failed to parse note response: {body[:200]}"}


def cmd_notes_create(env, title, content, category=None):
    """Create a new note."""
    url = notes_url(env, "notes")
    payload = json.dumps({"title": title, "content": content, "category": category or ""})
    args = curl_base(env) + [
        "-X", "POST",
        "-H", "Content-Type: application/json",
        "--data-binary", "@-",
        url,
    ]
    code, body, err = curl_request(args, input_data=payload)
    if err:
        return http_error(code, err, "Create note")
    if code not in (200, 201):
        return http_error(code, None, "Create note")
    try:
        note = json.loads(body)
        return {"status": "success", "data": note}
    except json.JSONDecodeError:
        return {"status": "error", "message": f"Failed to parse create response: {body[:200]}"}


def cmd_notes_edit(env, note_id, title=None, content=None, category=None):
    """Update an existing note.
    Uses If-Match header with etag for optimistic concurrency control (per API v1 spec).
    """
    url = notes_url(env, f"notes/{note_id}")
    # Fetch existing note first to preserve fields and get etag
    existing = cmd_notes_get(env, note_id)
    if isinstance(existing, dict) and existing.get("status") == "error":
        return existing
    data = existing.get("data", {})
    if title is not None:
        data["title"] = title
    if content is not None:
        data["content"] = content
    if category is not None:
        data["category"] = category
    payload = json.dumps(data)
    etag = data.get("etag", "")
    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: application/json",
        "-H", f"If-Match: {etag}",
        "--data-binary", "@-",
        url,
    ]
    code, body, err = curl_request(args, input_data=payload)
    if err:
        return http_error(code, err, f"Edit note {note_id}")
    if code == 412:
        return {"status": "error", "message": f"Edit note {note_id} failed: etag mismatch (note changed concurrently)"}
    if code != 200:
        return http_error(code, None, f"Edit note {note_id}")
    try:
        note = json.loads(body)
        return {"status": "success", "data": note}
    except json.JSONDecodeError:
        return {"status": "error", "message": f"Failed to parse edit response: {body[:200]}"}


def cmd_notes_delete(env, note_id):
    """Delete a note. Per Notes API v1 docs, DELETE returns 200 OK."""
    url = notes_url(env, f"notes/{note_id}")
    args = curl_base(env) + ["-X", "DELETE", url]
    code, body, err = curl_request(args)
    if err:
        return http_error(code, err, f"Delete note {note_id}")
    if code == 404:
        return {"status": "error", "message": f"Note {note_id} not found"}
    if code not in (200, 204):
        return http_error(code, None, f"Delete note {note_id}")
    return {"status": "success", "data": {"id": note_id, "deleted": True}}


def cmd_calendars_list(env, cal_type="events"):
    """List available calendars (tasks or events)."""
    url = caldav_url(env, "")
    props = """
        <d:prop>
            <d:displayname/>
            <d:resourcetype/>
            <cal:calendar-description/>
            <cal:calendar-color/>
        </d:prop>
    """
    results = xml_propfind(url, env, depth="1", props=props)
    if isinstance(results, dict) and "error" in results:
        return results
    formatted = []
    for r in results:
        href = r.get("href", "")
        # Only show calendar collections (depth 1, not individual events)
        if not href.rstrip("/").endswith(env["NEXTCLOUD_USER"]) and \
           "/calendars/" in href and not re.search(r"/calendars/[^/]+/[^/]+$", href):
            # href-derived fallbacks are percent-encoded; decode so downstream
            # URL building (which re-encodes once) does not double-encode.
            displayname = r.get("displayname") or urllib.parse.unquote(href.split("/")[-2])
            formatted.append({
                "id": urllib.parse.unquote(href.split("/")[-1].rstrip("/")),
                "name": displayname,
                "type": cal_type,
                "href": href,
            })
    return {"status": "success", "data": formatted}


def cmd_tasks_list(env, calendar=None):
    """List tasks from a specific calendar or all.
    Uses PROPFIND + GET since Nextcloud CalDAV does not support REPORT method.
    """
    # First get all calendars to find task calendars
    calendars_url = caldav_url(env, "")
    all_calendars = xml_propfind(calendars_url, env, depth="1")
    if isinstance(all_calendars, dict) and "error" in all_calendars:
        return all_calendars

    task_calendars = []
    for c in all_calendars:
        href = c.get("href", "")
        # Look for calendars that contain VTODO (task) components
        if "/calendars/" in href and c.get("type") == "dir":
            cal_name = urllib.parse.unquote(href.split("/")[-1].rstrip("/"))
            if calendar is None or cal_name == calendar:
                task_calendars.append({"name": cal_name, "href": href})

    if calendar and not any(c["name"] == calendar for c in task_calendars):
        return {"status": "error", "message": f"Calendar '{calendar}' not found or does not support tasks"}

    all_tasks = []
    base_url = env["NEXTCLOUD_URL"].rstrip("/")

    for cal in task_calendars:
        cal_name = cal["name"]
        cal_href = cal.get("href", "").rstrip("/")
        cal_url = base_url + cal_href + "/"

        # Use PROPFIND with Depth: infinity to get all .ics files
        # (REPORT method is not supported by Nextcloud CalDAV servers)
        args = curl_base(env, accept="application/xml") + [
            "-X", "PROPFIND",
            "-H", "Depth: infinity",
            cal_url,
        ]
        code, output, err = curl_request(args, timeout=60)
        if err or code != 207:
            continue

        # Find all .ics hrefs
        ics_hrefs = re.findall(r"<d:href>([^<]+\.ics)</d:href>", output)
        # Filter out the calendar root itself
        ics_hrefs = [h for h in ics_hrefs if not h.rstrip("/").endswith(cal_href.rstrip("/"))]

        # Fetch and parse each .ics file
        for href in ics_hrefs:
            decoded = urllib.parse.unquote(href)
            encoded = urllib.parse.quote(decoded, safe="/:")
            ics_url = base_url + encoded

            get_args = curl_base(env) + [
                "-X", "GET",
                "-H", "Accept: text/calendar",
                ics_url,
            ]
            get_code, ics_data, get_err = curl_request(get_args)
            if get_err or get_code != 200 or not ics_data:
                continue
            if "BEGIN:VCALENDAR" not in ics_data:
                continue

            task_data = parse_icalendar(ics_data, item_type="VTODO")
            if task_data:
                task_data["calendar"] = cal_name
                task_data["href"] = href
                all_tasks.append(task_data)

    return {"status": "success", "data": all_tasks}


def cmd_tasks_create(env, title, calendar=None, due=None, priority=None, description=None):
    """Create a task (VTODO)."""
    # Find the right calendar
    calendars_result = cmd_calendars_list(env, cal_type="tasks")
    if calendars_result.get("status") != "success" or not calendars_result.get("data"):
        return {"status": "error", "message": "No task calendars found. Create a tasks calendar in Nextcloud first."}

    calendars = calendars_result["data"]
    if calendar:
        cal_names = [c["name"] for c in calendars if c["name"] == calendar]
        if not cal_names:
            return {"status": "error", "message": f"Calendar '{calendar}' not found"}
        cal_name = calendar
    else:
        cal_name = calendars[0]["name"]

    vtodo_body, uid = vtodo_from_args(title, due=due, priority=priority, description=description)
    cal_url = caldav_url(env, f"{cal_name}/{uid}.ics")

    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: text/calendar; charset=utf-8",
        "--data-binary", "@-",
        cal_url,
    ]
    code, body, err = curl_request(args, input_data=vtodo_body)
    if err:
        return http_error(code, err, "Create task")
    if code not in (200, 201, 204):
        return http_error(code, None, "Create task")
    return {"status": "success", "data": {"uid": uid, "calendar": cal_name, "summary": title, "created": True}}


def cmd_tasks_edit(env, uid, calendar=None, title=None, due=None, priority=None, description=None):
    """Update a task. Requires calendar to locate the task.
    Uses PROPFIND + GET since Nextcloud CalDAV does not support REPORT method.
    """
    # First find the task's current calendar
    calendars_result = cmd_calendars_list(env, cal_type="tasks")
    if calendars_result.get("status") != "success":
        return calendars_result

    calendars = calendars_result.get("data", [])
    base_url = env["NEXTCLOUD_URL"].rstrip("/")

    search_cals = [calendar] if calendar else [c["name"] for c in calendars]

    task_data = None
    found_cal = None
    found_href = None
    found_raw = None
    for cal_name in search_cals:
        cal_entry = next((c for c in calendars if c.get("name") == cal_name), None)
        if not cal_entry:
            continue
        cal_href = cal_entry.get("href", "").rstrip("/")
        cal_url = base_url + cal_href + "/"

        # PROPFIND with Depth: infinity to get all .ics files
        args = curl_base(env, accept="application/xml") + [
            "-X", "PROPFIND",
            "-H", "Depth: infinity",
            cal_url,
        ]
        prop_code, output, prop_err = curl_request(args, timeout=60)
        if prop_err or prop_code != 207:
            continue

        ics_hrefs = re.findall(r"<d:href>([^<]+\.ics)</d:href>", output)
        ics_hrefs = [h for h in ics_hrefs if not h.rstrip("/").endswith(cal_href.rstrip("/"))]

        for href in ics_hrefs:
            decoded = urllib.parse.unquote(href)
            encoded = urllib.parse.quote(decoded, safe="/:")
            ics_url = base_url + encoded

            get_args = curl_base(env) + [
                "-X", "GET",
                "-H", "Accept: text/calendar",
                ics_url,
            ]
            get_code, raw_ics, get_err = curl_request(get_args)
            if get_err or get_code != 200 or not raw_ics:
                continue
            if "BEGIN:VCALENDAR" not in raw_ics:
                continue

            parsed = parse_icalendar(raw_ics, item_type="VTODO")
            if parsed.get("uid") == uid:
                task_data = parsed
                found_cal = cal_name
                found_href = href
                found_raw = raw_ics
                break
        if task_data:
            break

    if not task_data:
        return {"status": "error", "message": f"Task '{uid}' not found"}

    # Update properties in place so alarms, categories and similar metadata
    # survive the edit; only the explicitly provided fields change.
    updates = {}
    if title is not None:
        updates["SUMMARY"] = f"SUMMARY:{ical_escape(title)}"
    if due is not None:
        due_str, is_date = ical_datetime(due)
        updates["DUE"] = f"DUE;VALUE=DATE:{due_str}" if is_date else f"DUE:{due_str}"
    if priority is not None:
        updates["PRIORITY"] = f"PRIORITY:{priority}"
    if description is not None:
        updates["DESCRIPTION"] = f"DESCRIPTION:{ical_escape(description)}"
    new_body = ical_replace_properties(found_raw, updates, "VTODO")
    if new_body is None:
        # Fallback: rebuild while preserving the original UID (the UID in the
        # body must match the request URL or the server rejects the PUT).
        summary = title if title is not None else task_data.get("summary", "")
        task_due = due if due is not None else task_data.get("due", "")
        task_priority = priority if priority is not None else task_data.get("priority")
        task_desc = description if description is not None else task_data.get("description", "")
        new_body, _ = vtodo_from_args(
            summary,
            due=task_due,
            priority=task_priority,
            description=task_desc,
            uid=uid,
        )

    cal_url = caldav_url(env, f"{found_cal}/{uid}.ics")
    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: text/calendar; charset=utf-8",
        "--data-binary", "@-",
        cal_url,
    ]
    code, body, err = curl_request(args, input_data=new_body)
    if err:
        return http_error(code, err, f"Edit task '{uid}'")
    if code not in (200, 201, 204):
        return http_error(code, None, f"Edit task '{uid}'")
    return {"status": "success", "data": {**task_data, "updated": True}}


def cmd_tasks_complete(env, uid, calendar=None):
    """Mark a task as completed.
    Directly updates the task with COMPLETED status without fetching twice.
    """
    # First find the task's current data
    calendars_result = cmd_calendars_list(env, cal_type="tasks")
    if calendars_result.get("status") != "success":
        return calendars_result

    calendars = calendars_result.get("data", [])
    base_url = env["NEXTCLOUD_URL"].rstrip("/")
    search_cals = [calendar] if calendar else [c["name"] for c in calendars]

    task_data = None
    found_cal = None
    found_raw = None
    for cal_name in search_cals:
        cal_entry = next((c for c in calendars if c.get("name") == cal_name), None)
        if not cal_entry:
            continue
        cal_href = cal_entry.get("href", "").rstrip("/")
        cal_url = base_url + cal_href + "/"

        args = curl_base(env, accept="application/xml") + [
            "-X", "PROPFIND", "-H", "Depth: infinity", cal_url,
        ]
        prop_code, output, prop_err = curl_request(args, timeout=60)
        if prop_err or prop_code != 207:
            continue

        ics_hrefs = re.findall(r"<d:href>([^<]+\.ics)</d:href>", output)
        ics_hrefs = [h for h in ics_hrefs if not h.rstrip("/").endswith(cal_href.rstrip("/"))]

        for href in ics_hrefs:
            decoded = urllib.parse.unquote(href)
            encoded = urllib.parse.quote(decoded, safe="/:")
            ics_url = base_url + encoded
            get_args = curl_base(env) + ["-X", "GET", "-H", "Accept: text/calendar", ics_url]
            get_code, raw_ics, get_err = curl_request(get_args)
            if get_err or get_code != 200 or not raw_ics or "BEGIN:VCALENDAR" not in raw_ics:
                continue
            parsed = parse_icalendar(raw_ics, item_type="VTODO")
            if parsed.get("uid") == uid:
                task_data = parsed
                found_cal = cal_name
                found_raw = raw_ics
                break
        if task_data:
            break

    if not task_data:
        return {"status": "error", "message": f"Task '{uid}' not found"}

    # Mark completed in place, keeping every other property intact.
    updates = {
        "STATUS": "STATUS:COMPLETED",
        "COMPLETED": f"COMPLETED:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
    }
    new_body = ical_replace_properties(found_raw, updates, "VTODO")
    if new_body is None:
        new_body, _ = vtodo_from_args(
            task_data.get("summary", ""),
            due=task_data.get("due", ""),
            priority=task_data.get("priority"),
            description=task_data.get("description", ""),
            status="COMPLETED",
            uid=uid,
        )

    cal_url = caldav_url(env, f"{found_cal}/{uid}.ics")
    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: text/calendar; charset=utf-8",
        "--data-binary", "@-",
        cal_url,
    ]
    code, body, err = curl_request(args, input_data=new_body)
    if err:
        return http_error(code, err, f"Complete task '{uid}'")
    if code not in (200, 201, 204):
        return http_error(code, None, f"Complete task '{uid}'")
    return {"status": "success", "data": {**task_data, "status": "COMPLETED", "updated": True}}


def cmd_tasks_delete(env, uid, calendar=None):
    """Delete a task."""
    calendars_result = cmd_calendars_list(env, cal_type="tasks")
    if calendars_result.get("status") != "success":
        return calendars_result

    calendars = calendars_result.get("data", [])
    search_cals = [calendar] if calendar else [c["name"] for c in calendars]

    for cal_name in search_cals:
        cal_url = caldav_url(env, f"{cal_name}/{uid}.ics")
        args = curl_base(env) + ["-X", "DELETE", cal_url]
        code, body, err = curl_request(args)
        if err:
            continue
        if code in (200, 204):
            return {"status": "success", "data": {"uid": uid, "deleted": True}}
    return {"status": "error", "message": f"Task '{uid}' not found in any calendar"}


def _parse_ical_dt(value):
    """Parse an iCalendar or human datetime string; returns datetime or None."""
    v = (value or "").strip()
    fmts = ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d",
            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
    for fmt in fmts:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def cmd_calendar_list(env, calendar=None, cal_from=None, cal_to=None):
    """List calendar events (VEVENT) using PROPFIND + GET since REPORT is not supported."""
    calendars_result = cmd_calendars_list(env, cal_type="events")
    if calendars_result.get("status") != "success" or not calendars_result.get("data"):
        return {"status": "error", "message": "No calendars found"}

    calendars = calendars_result["data"]
    if calendar:
        calendars = [c for c in calendars if c.get("name") == calendar]
        if not calendars:
            return {"status": "error", "message": f"Calendar '{calendar}' not found"}

    dt_from = _parse_ical_dt(cal_from) if cal_from else None
    dt_to = _parse_ical_dt(cal_to) if cal_to else None
    if cal_from and dt_from is None:
        return {"status": "error", "message": f"Unparseable --from value: {cal_from}"}
    if cal_to and dt_to is None:
        return {"status": "error", "message": f"Unparseable --to value: {cal_to}"}

    all_events = []
    base_url = env["NEXTCLOUD_URL"].rstrip("/")

    for cal in calendars:
        cal_name = cal.get("name", "")
        cal_href = cal.get("href", "").rstrip("/")
        cal_url = base_url + cal_href + "/"

        # Use PROPFIND with Depth: infinity to get all .ics files
        # (REPORT method is not supported by this Nextcloud server)
        args = curl_base(env, accept="application/xml") + [
            "-X", "PROPFIND",
            "-H", "Depth: infinity",
            cal_url,
        ]
        prop_code, output, prop_err = curl_request(args, timeout=60)
        if prop_err or prop_code != 207:
            continue

        # Find all .ics hrefs
        ics_hrefs = re.findall(r"<d:href>([^<]+\.ics)</d:href>", output)
        # Filter out the calendar root itself
        ics_hrefs = [h for h in ics_hrefs if not h.rstrip("/").endswith(cal_href.rstrip("/"))]

        # Fetch and parse each .ics file
        for href in ics_hrefs:
            # URL encode href properly
            decoded = urllib.parse.unquote(href)
            encoded = urllib.parse.quote(decoded, safe="/:")
            ics_url = base_url + encoded

            get_args = curl_base(env) + [
                "-X", "GET",
                "-H", "Accept: text/calendar",
                ics_url,
            ]
            get_code, ics_data, get_err = curl_request(get_args)
            if get_err or get_code != 200 or not ics_data:
                continue
            if "BEGIN:VCALENDAR" not in ics_data:
                continue

            event_data = parse_icalendar(ics_data, item_type="VEVENT")
            if event_data:
                event_data["calendar"] = cal_name
                event_data["href"] = href
                all_events.append(event_data)

    # Filter by date range (compare parsed datetimes, not raw strings)
    if dt_from is not None:
        kept = []
        for e in all_events:
            ev_start = _parse_ical_dt(e.get("start", ""))
            if ev_start is not None and ev_start >= dt_from:
                kept.append(e)
        all_events = kept
    if dt_to is not None:
        kept = []
        for e in all_events:
            ev_end = _parse_ical_dt(e.get("end", ""))
            if ev_end is not None and ev_end <= dt_to:
                kept.append(e)
        all_events = kept

    return {"status": "success", "data": all_events}


def cmd_calendar_create(env, summary, start, end, calendar=None, location=None, description=None):
    """Create a calendar event."""
    calendars_result = cmd_calendars_list(env, cal_type="events")
    if calendars_result.get("status") != "success" or not calendars_result.get("data"):
        return {"status": "error", "message": "No calendars found"}

    calendars = calendars_result["data"]
    selected_cal = None
    if calendar:
        selected_cal = next((c for c in calendars if c.get("name") == calendar), None)
        if not selected_cal:
            return {"status": "error", "message": f"Calendar '{calendar}' not found"}
    else:
        selected_cal = calendars[0]

    cal_href = selected_cal["href"].rstrip("/")
    # href is like /remote.php/dav/calendars/{user}/personal/ -> use href directly

    uid = f"hermes-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"
    tzid = env.get("NEXTCLOUD_TIMEZONE", "UTC")
    vevent_body = vevent_from_args(summary, start, end, location=location, description=description, uid=uid, tzid=tzid)

    # Build URL directly from href
    base_url = env["NEXTCLOUD_URL"].rstrip("/")
    cal_url = base_url + cal_href + "/" + uid + ".ics"

    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: text/calendar; charset=utf-8",
        "--data-binary", "@-",
        cal_url,
    ]
    code, body, err = curl_request(args, input_data=vevent_body)
    if err:
        return http_error(code, err, "Create event")
    if code not in (200, 201, 204):
        return http_error(code, None, "Create event")
    return {"status": "success", "data": {"uid": uid, "calendar": selected_cal.get("name", cal_href), "summary": summary, "start": start, "end": end, "created": True}}


def cmd_calendar_edit(env, uid, calendar=None, summary=None, start=None, end=None, location=None, description=None):
    """Update a calendar event.
    Uses PROPFIND + GET since Nextcloud CalDAV does not support REPORT method.
    """
    calendars_result = cmd_calendars_list(env, cal_type="events")
    if calendars_result.get("status") != "success":
        return calendars_result

    calendars = calendars_result.get("data", [])
    base_url = env["NEXTCLOUD_URL"].rstrip("/")
    search_cals = [calendar] if calendar else [c["name"] for c in calendars]

    event_data = None
    found_cal = None
    found_raw = None
    for cal_name in search_cals:
        cal_entry = next((c for c in calendars if c.get("name") == cal_name), None)
        if not cal_entry:
            continue
        cal_href = cal_entry.get("href", "").rstrip("/")
        cal_url = base_url + cal_href + "/"

        # PROPFIND with Depth: infinity to get all .ics files
        args = curl_base(env, accept="application/xml") + [
            "-X", "PROPFIND",
            "-H", "Depth: infinity",
            cal_url,
        ]
        prop_code, output, prop_err = curl_request(args, timeout=60)
        if prop_err or prop_code != 207:
            continue

        ics_hrefs = re.findall(r"<d:href>([^<]+\.ics)</d:href>", output)
        ics_hrefs = [h for h in ics_hrefs if not h.rstrip("/").endswith(cal_href.rstrip("/"))]

        for href in ics_hrefs:
            decoded = urllib.parse.unquote(href)
            encoded = urllib.parse.quote(decoded, safe="/:")
            ics_url = base_url + encoded

            get_args = curl_base(env) + [
                "-X", "GET",
                "-H", "Accept: text/calendar",
                ics_url,
            ]
            get_code, raw_ics, get_err = curl_request(get_args)
            if get_err or get_code != 200 or not raw_ics:
                continue
            if "BEGIN:VCALENDAR" not in raw_ics:
                continue

            parsed = parse_icalendar(raw_ics, item_type="VEVENT")
            if parsed.get("uid") == uid:
                event_data = parsed
                found_cal = cal_name
                found_raw = raw_ics
                break
        if event_data:
            break

    if not event_data:
        return {"status": "error", "message": f"Event '{uid}' not found"}

    # Update properties in place so alarms, attendees and similar metadata
    # survive the edit; only the explicitly provided fields change.
    updates = {}
    if summary is not None:
        updates["SUMMARY"] = f"SUMMARY:{ical_escape(summary)}"
    if start is not None:
        start_str, start_is_date = ical_datetime(start)
        updates["DTSTART"] = f"DTSTART;VALUE=DATE:{start_str}" if start_is_date else f"DTSTART:{start_str}"
    if end is not None:
        end_str, end_is_date = ical_datetime(end)
        updates["DTEND"] = f"DTEND;VALUE=DATE:{end_str}" if end_is_date else f"DTEND:{end_str}"
    if location is not None:
        updates["LOCATION"] = f"LOCATION:{ical_escape(location)}"
    if description is not None:
        updates["DESCRIPTION"] = f"DESCRIPTION:{ical_escape(description)}"
    new_body = ical_replace_properties(found_raw, updates, "VEVENT")
    if new_body is None:
        # Fallback: rebuild while preserving the original UID (the UID in the
        # body must match the request URL or the server rejects the PUT).
        ev_summary = summary if summary is not None else event_data.get("summary", "")
        ev_start = start if start is not None else event_data.get("start", "")
        ev_end = end if end is not None else event_data.get("end", "")
        ev_location = location if location is not None else event_data.get("location", "")
        ev_desc = description if description is not None else event_data.get("description", "")
        tzid = env.get("NEXTCLOUD_TIMEZONE", "UTC")
        new_body = vevent_from_args(ev_summary, ev_start, ev_end, location=ev_location, description=ev_desc, uid=uid, tzid=tzid)

    cal_url = caldav_url(env, f"{found_cal}/{uid}.ics")
    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: text/calendar; charset=utf-8",
        "--data-binary", "@-",
        cal_url,
    ]
    code, body, err = curl_request(args, input_data=new_body)
    if err:
        return http_error(code, err, f"Edit event '{uid}'")
    if code not in (200, 201, 204):
        return http_error(code, None, f"Edit event '{uid}'")
    return {"status": "success", "data": {**event_data, "updated": True}}


def cmd_calendar_delete(env, uid, calendar=None):
    """Delete a calendar event."""
    calendars_result = cmd_calendars_list(env, cal_type="events")
    if calendars_result.get("status") != "success":
        return calendars_result

    calendars = calendars_result.get("data", [])
    search_cals = [calendar] if calendar else [c["name"] for c in calendars]

    for cal_name in search_cals:
        cal_url = caldav_url(env, f"{cal_name}/{uid}.ics")
        args = curl_base(env) + ["-X", "DELETE", cal_url]
        code, body, err = curl_request(args)
        if err:
            continue
        if code in (200, 204):
            return {"status": "success", "data": {"uid": uid, "deleted": True}}
    return {"status": "error", "message": f"Event '{uid}' not found in any calendar"}


def cmd_addressbooks_list(env):
    """List available address books."""
    url = carddav_url(env, "")
    results = xml_propfind(url, env, depth="1")
    if isinstance(results, dict) and "error" in results:
        return results
    formatted = []
    for r in results:
        href = r.get("href", "")
        # Addressbooks are under /addressbooks/ and have displayname or specific path patterns
        if "/addressbooks/" in href:
            # href-derived fallback is percent-encoded; decode so downstream URL
            # building (which re-encodes once) does not double-encode.
            name = r.get("displayname") or urllib.parse.unquote(href.split("/")[-1].rstrip("/"))
            # Skip the parent /addressbooks/users/{user}/ directory itself
            if name == "users":
                continue
            formatted.append({
                "id": href.split("/")[-1].rstrip("/"),
                "name": name,
                "href": href,
            })
    return {"status": "success", "data": formatted}


def cmd_contacts_list(env, addressbook=None):
    """List contacts from an address book using PROPFIND + GET (REPORT not supported)."""
    if addressbook:
        abooks = [{"href": addressbook if addressbook.startswith("/") else carddav_url(env, addressbook + "/"), "name": addressbook}]
    else:
        ab_result = cmd_addressbooks_list(env)
        if ab_result.get("status") != "success":
            return ab_result
        abooks = ab_result.get("data", [])

    all_contacts = []
    for ab_entry in abooks:
        ab_href = ab_entry["href"]
        base_url = env["NEXTCLOUD_URL"].rstrip("/")
        ab_url = base_url + ab_href

        # Try Depth: 1 first, fall back to infinity if no vcf files found
        vcard_hrefs = []
        for depth in ("1", "infinity"):
            args = curl_base(env, accept="application/xml") + [
                "-X", "PROPFIND",
                "-H", f"Depth: {depth}",
                ab_url,
            ]
            prop_code, output, prop_err = curl_request(args, timeout=60)
            if prop_err or prop_code != 207:
                break
            # Match vcf hrefs - .vcf files or Database: prefixed vcf files
            vcard_hrefs = re.findall(r"<d:href>([^<]*(?:\.vcf|Database:[^<]*\.vcf)[^<]*)</d:href>", output)
            # Filter out the addressbook root itself
            vcard_hrefs = [h for h in vcard_hrefs if not h.rstrip("/").endswith(ab_href.rstrip("/"))]
            if vcard_hrefs:
                break

        for vcard_href in vcard_hrefs:
            # URL decode then re-encode to handle special chars like Database: prefix
            decoded = urllib.parse.unquote(vcard_href)
            encoded = urllib.parse.quote(decoded, safe="/:")
            vcard_url = base_url + encoded
            get_args = curl_base(env) + [
                "-X", "GET",
                "-H", "Accept: text/vcard, text/x-vcard",
                vcard_url,
            ]
            get_code, vcard_data, get_err = curl_request(get_args)
            if get_err or get_code != 200 or not vcard_data:
                continue

            # Parse vCard - fields may have parameters like FN;X-NC-SCOPE=...:value
            contact = {"addressbook": ab_entry.get("name", ab_href)}
            # FN may have params like FN;X-NC-SCOPE=...:value - extract value after last :
            fn_match = re.search(r"^FN(?:;[^:]*)*:(.+)$", vcard_data, re.MULTILINE)
            if fn_match:
                contact["fullname"] = fn_match.group(1).strip()
            n_match = re.search(r"^N(?:;[^:]*)*:([^;]+);([^;]*);([^;]*);([^;]*);([^;]*)$", vcard_data, re.MULTILINE)
            if n_match:
                contact["name"] = f"{n_match.group(2)};{n_match.group(1)}"
            emails = re.findall(r"^EMAIL(?:;[^:]*)*:([^\r\n]+)", vcard_data, re.MULTILINE)
            if emails:
                contact["emails"] = [e.strip() for e in emails if e.strip()]
            phones = re.findall(r"^TEL(?:;[^:]*)*:([^\r\n]+)", vcard_data, re.MULTILINE)
            if phones:
                contact["phones"] = [p.strip() for p in phones if p.strip()]
            org_match = re.search(r"^ORG:[^\r\n]+", vcard_data, re.MULTILINE)
            if org_match:
                contact["organization"] = org_match.group(1).strip()
            title_match = re.search(r"^TITLE:[^\r\n]+", vcard_data, re.MULTILINE)
            if title_match:
                contact["title"] = title_match.group(1).strip()
            note_match = re.search(r"^NOTE:[^\r\n]+", vcard_data, re.MULTILINE)
            if note_match:
                contact["note"] = note_match.group(1).strip()
            uid_match = re.search(r"^UID:[^\r\n]+", vcard_data, re.MULTILINE)
            if uid_match:
                contact["uid"] = uid_match.group(0)[4:].strip()  # strip "UID:" (4 chars)
            if contact.get("fullname"):
                all_contacts.append(contact)

    # Deduplicate by uid
    seen_uids = set()
    unique_contacts = []
    for contact in all_contacts:
        uid = contact.get("uid", contact.get("fullname", ""))
        if uid not in seen_uids:
            seen_uids.add(uid)
            unique_contacts.append(contact)

    return {"status": "success", "data": unique_contacts}


def cmd_contacts_search(env, query):
    """Search contacts by name or other fields."""
    result = cmd_contacts_list(env)
    if result.get("status") != "success":
        return result
    query_lower = query.lower()
    matched = [
        c for c in result.get("data", [])
        if query_lower in c.get("fullname", "").lower() or
           query_lower in c.get("organization", "").lower() or
           any(query_lower in e.lower() for e in c.get("emails", []))
    ]
    return {"status": "success", "data": matched}


def cmd_contacts_get(env, uid, addressbook=None):
    """Get a single contact by UID.
    Optimized: tries direct .vcf URL first, then falls back to PROPFIND scan.
    """
    if addressbook:
        ab_result = cmd_addressbooks_list(env)
        if ab_result.get("status") != "success":
            return ab_result
        ab_entry = next((a for a in ab_result.get("data", []) if a.get("name") == addressbook or a.get("href", "").rstrip("/").endswith(addressbook.rstrip("/"))), None)
        if ab_entry:
            abooks = [(ab_entry["href"], ab_entry["name"])]
        else:
            user = urllib.parse.quote(env["NEXTCLOUD_USER"], safe="")
            abooks = [(f"/remote.php/dav/addressbooks/users/{user}/{_quote_path(addressbook)}/", addressbook)]
    else:
        ab_result = cmd_addressbooks_list(env)
        if ab_result.get("status") != "success":
            return ab_result
        abooks = [(a["href"], a["name"]) for a in ab_result.get("data", [])]

    base_url = env["NEXTCLOUD_URL"].rstrip("/")

    for ab_href, ab_name in abooks:
        # Optimized: try direct URL first (Nextcloud stores contacts as {uid}.vcf)
        direct_url = base_url + ab_href.rstrip("/") + "/" + urllib.parse.quote(uid, safe="") + ".vcf"
        get_args = curl_base(env) + ["-X", "GET", "-H", "Accept: text/vcard", direct_url]
        direct_code, vcard_data, direct_err = curl_request(get_args)
        if not direct_err and direct_code == 200 and vcard_data and "BEGIN:VCARD" in vcard_data:
            # Found via direct URL - parse and return
            contact = {
                "addressbook": ab_name,
                "addressbook_href": ab_href,
                "uid": uid,
            }
            fn_match = re.search(r"^FN(?:;[^:]*)*:(.+)$", vcard_data, re.MULTILINE)
            if fn_match:
                contact["fullname"] = fn_match.group(1).strip()
            emails = re.findall(r"^EMAIL(?:;[^:]*)*:([^\r\n]+)", vcard_data, re.MULTILINE)
            if emails:
                contact["emails"] = [e.strip() for e in emails]
            phones = re.findall(r"^TEL(?:;[^:]*)*:([^\r\n]+)", vcard_data, re.MULTILINE)
            if phones:
                contact["phones"] = [p.strip() for p in phones]
            org_match = re.search(r"^ORG:[^\r\n]+", vcard_data, re.MULTILINE)
            if org_match:
                contact["organization"] = org_match.group(1).strip()
            title_match = re.search(r"^TITLE:[^\r\n]+", vcard_data, re.MULTILINE)
            if title_match:
                contact["title"] = title_match.group(1).strip()
            note_match = re.search(r"^NOTE:[^\r\n]+", vcard_data, re.MULTILINE)
            if note_match:
                contact["note"] = note_match.group(1).strip()
            return {"status": "success", "data": contact}

        # Fallback: scan all vcf files via PROPFIND
        ab_url = base_url + ab_href
        for depth in ("1", "infinity"):
            args = curl_base(env, accept="application/xml") + ["-X", "PROPFIND", "-H", f"Depth: {depth}", ab_url]
            prop_code, output, prop_err = curl_request(args, timeout=60)
            if prop_err or prop_code != 207:
                break

            vcard_hrefs = re.findall(r"<d:href>([^<]*(?:\.vcf|Database:[^<]*\.vcf)[^<]*)</d:href>", output)
            vcard_hrefs = [h for h in vcard_hrefs if not h.rstrip("/").endswith(ab_href.rstrip("/"))]
            if vcard_hrefs:
                break

        for vcard_href in vcard_hrefs:
            decoded = urllib.parse.unquote(vcard_href)
            encoded = urllib.parse.quote(decoded, safe="/:")
            vcard_url = base_url + encoded
            get_args = curl_base(env) + ["-X", "GET", "-H", "Accept: text/vcard", vcard_url]
            get_code, vcard_data, get_err = curl_request(get_args)
            if get_err or get_code != 200 or not vcard_data or "BEGIN:VCARD" not in vcard_data:
                continue
            uid_match = re.search(r"^UID:([^\r\n]+)", vcard_data, re.MULTILINE)
            file_uid = uid_match.group(1).strip() if uid_match else ""
            if file_uid != uid:
                continue
            contact = {
                "addressbook": ab_name,
                "addressbook_href": ab_href,
                "uid": file_uid,
                "vcard_href": vcard_href,
                "vcard_url": vcard_url,
            }
            fn_match = re.search(r"^FN(?:;[^:]*)*:(.+)$", vcard_data, re.MULTILINE)
            if fn_match:
                contact["fullname"] = fn_match.group(1).strip()
            emails = re.findall(r"^EMAIL(?:;[^:]*)*:([^\r\n]+)", vcard_data, re.MULTILINE)
            if emails:
                contact["emails"] = [e.strip() for e in emails]
            phones = re.findall(r"^TEL(?:;[^:]*)*:([^\r\n]+)", vcard_data, re.MULTILINE)
            if phones:
                contact["phones"] = [p.strip() for p in phones]
            org_match = re.search(r"^ORG:[^\r\n]+", vcard_data, re.MULTILINE)
            if org_match:
                contact["organization"] = org_match.group(1).strip()
            title_match = re.search(r"^TITLE:[^\r\n]+", vcard_data, re.MULTILINE)
            if title_match:
                contact["title"] = title_match.group(1).strip()
            note_match = re.search(r"^NOTE:[^\r\n]+", vcard_data, re.MULTILINE)
            if note_match:
                contact["note"] = note_match.group(1).strip()
            return {"status": "success", "data": contact}

    return {"status": "error", "message": f"Contact '{uid}' not found"}


def cmd_contacts_create(env, name, addressbook=None, email=None, phone=None, organization=None, title=None, note=None):
    """Create a contact."""
    if not addressbook:
        ab_result = cmd_addressbooks_list(env)
        if ab_result.get("status") != "success" or not ab_result.get("data"):
            return {"status": "error", "message": "No address books found"}
        addressbook = ab_result["data"][0]["name"]

    uid = f"hermes-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"
    vcard = vcard_from_args(name, email=email, phone=phone, organization=organization, title=title, note=note)

    contact_url = carddav_url(env, f"{addressbook}/{uid}.vcf")
    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: text/vcard; charset=utf-8",
        "--data-binary", "@-",
        contact_url,
    ]
    code, body, err = curl_request(args, input_data=vcard)
    if err:
        return http_error(code, err, "Create contact")
    if code not in (200, 201, 204):
        return http_error(code, None, "Create contact")
    return {"status": "success", "data": {"uid": uid, "addressbook": addressbook, "fullname": name, "created": True}}


def cmd_contacts_edit(env, uid, addressbook=None, name=None, email=None, phone=None, organization=None, title=None, note=None):
    """Update a contact."""
    # Find the contact first
    existing = cmd_contacts_get(env, uid, addressbook=addressbook)
    if existing.get("status") == "error":
        return existing

    contact = existing["data"]
    ab_href = contact.get("addressbook_href", "")
    ab_name = contact.get("addressbook") or addressbook or ""

    new_name = name if name is not None else contact.get("fullname", "")
    new_email = email if email is not None else ",".join(contact.get("emails", []))
    new_phone = phone if phone is not None else ",".join(contact.get("phones", []))
    new_org = organization if organization is not None else contact.get("organization", "")
    new_title = title if title is not None else contact.get("title", "")
    new_note = note if note is not None else contact.get("note", "")

    # Build vCard preserving UID
    vcard = vcard_from_args(new_name, email=new_email, phone=new_phone,
                            organization=new_org, title=new_title, note=new_note)
    # Preserve UID if it existed
    if contact.get("uid"):
        vcard = re.sub(r"^UID:.*$", f"UID:{contact['uid']}", vcard, flags=re.MULTILINE)

    base_url = env["NEXTCLOUD_URL"].rstrip("/")
    # Use vcard_href (URL-encoded filename) if available, otherwise build from href+uid
    vcard_href = contact.get("vcard_href", "")
    if vcard_href:
        contact_url = base_url + vcard_href
    elif ab_href:
        contact_url = base_url + ab_href + urllib.parse.quote(uid) + ".vcf"
    else:
        contact_url = carddav_url(env, f"{ab_name}/{uid}.vcf")
    args = curl_base(env) + [
        "-X", "PUT",
        "-H", "Content-Type: text/vcard; charset=utf-8",
        "--data-binary", "@-",
        contact_url,
    ]
    code, body, err = curl_request(args, input_data=vcard)
    if err:
        return http_error(code, err, f"Edit contact '{uid}'")
    if code not in (200, 201, 204):
        return http_error(code, None, f"Edit contact '{uid}'")
    return {"status": "success", "data": {**contact, "updated": True}}


def cmd_contacts_delete(env, uid, addressbook=None):
    """Delete a contact."""
    if addressbook:
        abooks = [addressbook]
    else:
        ab_result = cmd_addressbooks_list(env)
        if ab_result.get("status") != "success":
            return ab_result
        abooks = [a["name"] for a in ab_result.get("data", [])]

    for ab_name in abooks:
        contact_url = carddav_url(env, f"{ab_name}/{uid}.vcf")
        args = curl_base(env) + ["-X", "DELETE", contact_url]
        code, body, err = curl_request(args)
        if err:
            continue
        if code in (200, 204):
            return {"status": "success", "data": {"uid": uid, "deleted": True}}
    return {"status": "error", "message": f"Contact '{uid}' not found"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_upload(env, args):
    """Resolve upload content from --content or --content-file ('-' = stdin)."""
    content = args.content
    if getattr(args, "content_file", None):
        if args.content_file == "-":
            content = sys.stdin.read()
        else:
            with open(args.content_file, "r") as f:
                content = f.read()
    if content is None:
        return {"status": "error", "message": "Provide --content or --content-file ('-' for stdin)"}
    return cmd_files_upload(env, args.path, content)


def main():
    parser = argparse.ArgumentParser(description="Nextcloud API for Hermes Agent")
    parser.add_argument("--env-file", default=ENV_FILE)
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # check
    subparsers.add_parser("check", help="Verify credentials and connection")

    # files
    files_parser = subparsers.add_parser("files", help="Files operations")
    files_sub = files_parser.add_subparsers(dest="files_cmd")

    p = files_sub.add_parser("list", help="List directory")
    p.add_argument("--path", default="/")
    p.set_defaults(func=lambda args: cmd_files_list(env, args.path))

    p = files_sub.add_parser("get", help="Get file content")
    p.add_argument("--path", required=True)
    p.set_defaults(func=lambda args: cmd_files_get(env, args.path))

    p = files_sub.add_parser("upload", help="Upload file")
    p.add_argument("--path", required=True)
    p.add_argument("--content", default=None, help="File content (small payloads only)")
    p.add_argument("--content-file", default=None,
                   help="Read content from a file ('-' reads stdin; recommended for "
                        "large or sensitive content — keeps it out of the process list)")
    p.set_defaults(func=lambda args: _resolve_upload(env, args))

    p = files_sub.add_parser("mkdir", help="Create directory")
    p.add_argument("--path", required=True)
    p.set_defaults(func=lambda args: cmd_files_mkdir(env, args.path))

    p = files_sub.add_parser("delete", help="Delete file or directory")
    p.add_argument("--path", required=True)
    p.set_defaults(func=lambda args: cmd_files_delete(env, args.path))

    p = files_sub.add_parser("move", help="Move/rename file")
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p.set_defaults(func=lambda args: cmd_files_move(env, args.src, args.dst))

    p = files_sub.add_parser("search", help="Search files by name")
    p.add_argument("--query", required=True)
    p.set_defaults(func=lambda args: cmd_files_search(env, args.query))

    # notes
    notes_parser = subparsers.add_parser("notes", help="Notes operations")
    notes_sub = notes_parser.add_subparsers(dest="notes_cmd")

    p = notes_sub.add_parser("list", help="List notes")
    p.add_argument("--category", default=None)
    p.set_defaults(func=lambda args: cmd_notes_list(env, args.category))

    p = notes_sub.add_parser("get", help="Get note by ID")
    p.add_argument("--id", required=True, type=int)
    p.set_defaults(func=lambda args: cmd_notes_get(env, args.id))

    p = notes_sub.add_parser("create", help="Create note")
    p.add_argument("--title", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--category", default=None)
    p.set_defaults(func=lambda args: cmd_notes_create(env, args.title, args.content, args.category))

    p = notes_sub.add_parser("edit", help="Edit note")
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--title", default=None)
    p.add_argument("--content", default=None)
    p.add_argument("--category", default=None)
    p.set_defaults(func=lambda args: cmd_notes_edit(env, args.id, args.title, args.content, args.category))

    p = notes_sub.add_parser("delete", help="Delete note")
    p.add_argument("--id", required=True, type=int)
    p.set_defaults(func=lambda args: cmd_notes_delete(env, args.id))

    # calendars
    cal_parser = subparsers.add_parser("calendars", help="Calendar operations")
    cal_sub = cal_parser.add_subparsers(dest="cal_cmd")

    p = cal_sub.add_parser("list", help="List calendars")
    p.add_argument("--type", default="events", choices=["tasks", "events"])
    p.set_defaults(func=lambda args: cmd_calendars_list(env, args.type))

    # tasks
    tasks_parser = subparsers.add_parser("tasks", help="Task operations")
    tasks_sub = tasks_parser.add_subparsers(dest="tasks_cmd")

    p = tasks_sub.add_parser("list", help="List tasks")
    p.add_argument("--calendar", default=None)
    p.set_defaults(func=lambda args: cmd_tasks_list(env, args.calendar))

    p = tasks_sub.add_parser("create", help="Create task")
    p.add_argument("--title", required=True)
    p.add_argument("--calendar", default=None)
    p.add_argument("--due", default=None)
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--description", default=None)
    p.set_defaults(func=lambda args: cmd_tasks_create(env, args.title, args.calendar, args.due, args.priority, args.description))

    p = tasks_sub.add_parser("edit", help="Edit task")
    p.add_argument("--uid", required=True)
    p.add_argument("--calendar", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--due", default=None)
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--description", default=None)
    p.set_defaults(func=lambda args: cmd_tasks_edit(env, args.uid, args.calendar, args.title, args.due, args.priority, args.description))

    p = tasks_sub.add_parser("complete", help="Mark task complete")
    p.add_argument("--uid", required=True)
    p.add_argument("--calendar", default=None)
    p.set_defaults(func=lambda args: cmd_tasks_complete(env, args.uid, args.calendar))

    p = tasks_sub.add_parser("delete", help="Delete task")
    p.add_argument("--uid", required=True)
    p.add_argument("--calendar", default=None)
    p.set_defaults(func=lambda args: cmd_tasks_delete(env, args.uid, args.calendar))

    # calendar events
    calendar_parser = subparsers.add_parser("calendar", help="Calendar event operations")
    calendar_sub = calendar_parser.add_subparsers(dest="calendar_cmd")

    p = calendar_sub.add_parser("list", help="List events")
    p.add_argument("--from", dest="cal_from", default=None)
    p.add_argument("--to", dest="cal_to", default=None)
    p.add_argument("--calendar", default=None)
    p.set_defaults(func=lambda args: cmd_calendar_list(env, args.calendar, args.cal_from, args.cal_to))

    p = calendar_sub.add_parser("create", help="Create event")
    p.add_argument("--summary", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--calendar", default=None)
    p.add_argument("--location", default=None)
    p.add_argument("--description", default=None)
    p.set_defaults(func=lambda args: cmd_calendar_create(env, args.summary, args.start, args.end, args.calendar, args.location, args.description))

    p = calendar_sub.add_parser("edit", help="Edit event")
    p.add_argument("--uid", required=True)
    p.add_argument("--calendar", default=None)
    p.add_argument("--summary", default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--location", default=None)
    p.add_argument("--description", default=None)
    p.set_defaults(func=lambda args: cmd_calendar_edit(env, args.uid, args.calendar, args.summary, args.start, args.end, args.location, args.description))

    p = calendar_sub.add_parser("delete", help="Delete event")
    p.add_argument("--uid", required=True)
    p.add_argument("--calendar", default=None)
    p.set_defaults(func=lambda args: cmd_calendar_delete(env, args.uid, args.calendar))

    # contacts
    contacts_parser = subparsers.add_parser("contacts", help="Contact operations")
    contacts_sub = contacts_parser.add_subparsers(dest="contacts_cmd")

    p = contacts_sub.add_parser("list", help="List contacts")
    p.add_argument("--addressbook", default=None)
    p.set_defaults(func=lambda args: cmd_contacts_list(env, args.addressbook))

    p = contacts_sub.add_parser("search", help="Search contacts")
    p.add_argument("--query", required=True)
    p.set_defaults(func=lambda args: cmd_contacts_search(env, args.query))

    p = contacts_sub.add_parser("get", help="Get contact by UID")
    p.add_argument("--uid", required=True)
    p.add_argument("--addressbook", default=None)
    p.set_defaults(func=lambda args: cmd_contacts_get(env, args.uid, args.addressbook))

    p = contacts_sub.add_parser("create", help="Create contact")
    p.add_argument("--name", required=True)
    p.add_argument("--addressbook", default=None)
    p.add_argument("--email", default=None)
    p.add_argument("--phone", default=None)
    p.add_argument("--organization", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--note", default=None)
    p.set_defaults(func=lambda args: cmd_contacts_create(env, args.name, args.addressbook, args.email, args.phone, args.organization, args.title, args.note))

    p = contacts_sub.add_parser("edit", help="Edit contact")
    p.add_argument("--uid", required=True)
    p.add_argument("--addressbook", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--email", default=None)
    p.add_argument("--phone", default=None)
    p.add_argument("--organization", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--note", default=None)
    p.set_defaults(func=lambda args: cmd_contacts_edit(env, args.uid, args.addressbook, args.name, args.email, args.phone, args.organization, args.title, args.note))

    p = contacts_sub.add_parser("delete", help="Delete contact")
    p.add_argument("--uid", required=True)
    p.add_argument("--addressbook", default=None)
    p.set_defaults(func=lambda args: cmd_contacts_delete(env, args.uid, args.addressbook))

    # addressbooks
    ab_parser = subparsers.add_parser("addressbooks", help="Address book operations")
    ab_sub = ab_parser.add_subparsers(dest="ab_cmd")

    p = ab_sub.add_parser("list", help="List address books")
    p.set_defaults(func=lambda args: cmd_addressbooks_list(env))

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load env
    env = load_env(args.env_file)

    # Special case: check/setup commands don't need credentials yet
    if args.command == "check":
        print(json.dumps(cmd_check(env)))
        return

    # For all other commands, validate credentials first
    check_credentials(env)

    # Dispatch
    if hasattr(args, "func"):
        result = args.func(args)
        print(json.dumps(result))
    else:
        print(json.dumps({"status": "error", "message": f"Unknown command: {args.command}"}))


if __name__ == "__main__":
    main()
