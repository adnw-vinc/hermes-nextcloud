#!/usr/bin/env python3
"""Offline tests for the Nextcloud Hermes skill.

Two layers, both network-free except for localhost:

1. HelperTests — pure-function checks (iCal/vCard escaping, datetime
   conversion, in-place property replacement, URL quoting, and the guarantee
   that credentials/file content never appear in curl's argv).
2. MockServerTests — an integration suite against a minimal in-process HTTP
   mock that speaks enough WebDAV/Notes to exercise the CLI end to end,
   including correct handling of 404/401/405 responses.

Run:  python3 tests/test_offline.py
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
SCRIPTS_DIR = os.path.abspath(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import nextcloud_api  # noqa: E402

API = os.path.join(SCRIPTS_DIR, "nextcloud_api.py")

USER = "smoke"
TOKEN = "testtoken-not-a-secret"
BASIC = base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()

PROPFIND_ROOT = (
    '<?xml version="1.0"?>'
    '<d:multistatus xmlns:d="DAV:">'
    "<d:response>"
    f"<d:href>/remote.php/dav/files/{USER}/</d:href>"
    "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    "</d:response>"
    "<d:response>"
    f"<d:href>/remote.php/dav/files/{USER}/hello.txt</d:href>"
    "<d:propstat><d:prop>"
    "<d:getcontentlength>11</d:getcontentlength>"
    "<d:getcontenttype>text/plain</d:getcontenttype>"
    "<d:getlastmodified>Mon, 01 Jan 2026 00:00:00 GMT</d:getlastmodified>"
    '<d:etag>&quot;abc&quot;</d:etag>'
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    "</d:response>"
    "<d:response>"
    f"<d:href>/remote.php/dav/files/{USER}/my%20file.txt</d:href>"
    "<d:propstat><d:prop>"
    "<d:getcontentlength>5</d:getcontentlength>"
    "<d:getcontenttype>text/plain</d:getcontenttype>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    "</d:response>"
    "</d:multistatus>"
)

NOTE_1 = {"id": 1, "title": "First", "content": "Body", "category": "", "etag": "w01"}


class MockNextcloudHandler(BaseHTTPRequestHandler):
    """Minimal Nextcloud stand-in: WebDAV files + Notes API, with Basic auth."""

    stored_uploads = {}
    server_version = "MockNextcloud/1.0"

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        return header == f"Basic {BASIC}"

    def _deny(self):
        body = b'<?xml version="1.0"?><d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns"><s:message>Credentials were not provided</s:message></d:error>'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Nextcloud"')
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, code, body=b"", ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def log_message(self, format, *args):  # noqa: A002 - base signature; keep test output clean
        pass

    # ---- WebDAV ----
    def do_PROPFIND(self):
        if not self._authorized():
            return self._deny()
        self._read_body()
        if self.path.rstrip("/") == f"/remote.php/dav/files/{USER}":
            self._reply(207, PROPFIND_ROOT.encode(), "application/xml")
        else:
            self._reply(404, b"Not found", "text/plain")

    def do_GET(self):
        if not self._authorized():
            return self._deny()
        path = urllib.parse.unquote(self.path)
        if path == f"/remote.php/dav/files/{USER}/hello.txt":
            self._reply(200, b"hello world", "text/plain")
        elif path.startswith("/index.php/apps/notes/api/v1/notes/1"):
            self._reply(200, json.dumps(NOTE_1).encode(), "application/json")
        else:
            self._reply(404, b"Not found", "text/plain")

    def do_PUT(self):
        if not self._authorized():
            return self._deny()
        body = self._read_body()
        path = urllib.parse.unquote(self.path)
        if path.startswith("/index.php/apps/notes/api/v1/notes/1"):
            if self.headers.get("If-Match") != NOTE_1["etag"]:
                return self._reply(412, b"etag mismatch", "text/plain")
            note = dict(NOTE_1)
            note.update(json.loads(body))
            note["etag"] = "w02"
            self._reply(200, json.dumps(note).encode(), "application/json")
        elif path == f"/remote.php/dav/files/{USER}/uploaded.txt":
            MockNextcloudHandler.stored_uploads["uploaded.txt"] = body
            self._reply(201, b"", "application/xml")
        else:
            self._reply(409, b"Parent collection missing", "text/plain")

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        body = self._read_body()
        if self.path.rstrip("/") == "/index.php/apps/notes/api/v1/notes":
            note = {"id": 2, "etag": "w01"}
            note.update(json.loads(body))
            self._reply(200, json.dumps(note).encode(), "application/json")
        else:
            self._reply(404, b"Not found", "text/plain")

    def do_DELETE(self):
        if not self._authorized():
            return self._deny()
        path = urllib.parse.unquote(self.path)
        if path == f"/remote.php/dav/files/{USER}/missing.txt":
            self._reply(404, b"Not found", "text/plain")
        elif path == f"/remote.php/dav/files/{USER}/hello.txt":
            self._reply(204, b"")
        elif path.startswith("/index.php/apps/notes/api/v1/notes/1"):
            self._reply(200, b"{}")
        else:
            self._reply(404, b"Not found", "text/plain")

    def do_MKCOL(self):
        if not self._authorized():
            return self._deny()
        self._read_body()
        if self.path.rstrip("/") == f"/remote.php/dav/files/{USER}/existing":
            self._reply(405, b"Already exists", "text/plain")
        else:
            self._reply(201, b"", "application/xml")

    def do_MOVE(self):
        if not self._authorized():
            return self._deny()
        if self.path == f"/remote.php/dav/files/{USER}/hello.txt" and self.headers.get("Destination"):
            self._reply(201, b"", "application/xml")
        else:
            self._reply(404, b"Not found", "text/plain")


class CLIBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockNextcloudHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def run_cli(self, args, token=TOKEN, env_file=None, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NEXTCLOUD_")}
        env.update({
            "NEXTCLOUD_URL": f"http://127.0.0.1:{self.port}",
            "NEXTCLOUD_USER": USER,
            "NEXTCLOUD_TOKEN": token,
        })
        if extra_env:
            env.update(extra_env)
        cmd = [sys.executable, API]
        if env_file:
            cmd += ["--env-file", env_file]
        cmd += args
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(proc.returncode, 0, f"CLI failed: {proc.stderr}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"Non-JSON CLI output: {proc.stdout!r} stderr: {proc.stderr!r}")


class HelperTests(unittest.TestCase):
    def test_ical_escape(self):
        self.assertEqual(nextcloud_api.ical_escape("a;b,c\nd"), "a\\;b\\,c\\nd")
        self.assertEqual(nextcloud_api.ical_escape("back\\slash"), "back\\\\slash")

    def test_vcard_escape(self):
        self.assertEqual(nextcloud_api.vcard_escape("a\nb"), "a\\nb")

    def test_ical_datetime(self):
        self.assertEqual(nextcloud_api.ical_datetime("2026-06-30"), ("20260630", True))
        self.assertEqual(nextcloud_api.ical_datetime("2026-06-30T17:00:00Z"), ("20260630T170000Z", False))
        self.assertEqual(nextcloud_api.ical_datetime("2026-06-30 17:00"), ("20260630T170000", False))

    def test_vtodo_from_args_preserves_uid(self):
        body, uid = nextcloud_api.vtodo_from_args("T", uid="hermes-fixed-uid")
        self.assertIn("UID:hermes-fixed-uid", body)
        self.assertEqual(uid, "hermes-fixed-uid")

    def test_vtodo_due_documented_format(self):
        # SKILL.md documents "2026-06-30T17:00:00Z" — must not leak verbatim
        body, _ = nextcloud_api.vtodo_from_args("T", due="2026-06-30T17:00:00Z")
        self.assertIn("DUE:20260630T170000Z", body)

    def test_ical_replace_properties(self):
        original = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VTODO\nUID:x\n"
            "SUMMARY:Old\nDTSTART;TZID=Europe/Prague:20260101T090000\n"
            "CATEGORIES:keepme\nEND:VTODO\nEND:VCALENDAR\n"
        )
        out = nextcloud_api.ical_replace_properties(
            original, {"SUMMARY": "SUMMARY:New", "DTSTART": "DTSTART:20260202T100000"}, "VTODO"
        )
        self.assertIn("SUMMARY:New", out)
        # original parameter preserved on the replacement line
        self.assertIn("DTSTART;TZID=Europe/Prague:20260202T100000", out)
        # untouched properties survive
        self.assertIn("CATEGORIES:keepme", out)
        self.assertIn("UID:x", out)

    def test_ical_replace_properties_inserts_missing(self):
        original = "BEGIN:VCALENDAR\nBEGIN:VTODO\nUID:x\nEND:VTODO\nEND:VCALENDAR\n"
        out = nextcloud_api.ical_replace_properties(
            original, {"STATUS": "STATUS:COMPLETED", "COMPLETED": "COMPLETED:20260101T000000Z"}, "VTODO"
        )
        self.assertIn("STATUS:COMPLETED", out)
        self.assertIn("COMPLETED:20260101T000000Z", out)

    def test_dav_url_quotes_spaces(self):
        env = {"NEXTCLOUD_URL": "https://nc.example", "NEXTCLOUD_USER": USER}
        url = nextcloud_api.dav_url(env, "/My Documents/notes.txt")
        self.assertEqual(url, "https://nc.example/remote.php/dav/files/smoke/My%20Documents/notes.txt")

    def test_notes_url_encodes_category(self):
        env = {"NEXTCLOUD_URL": "https://nc.example"}
        url = nextcloud_api.notes_url(env, "notes", params={"category": "work & life"})
        self.assertIn("category=work+%26+life", url)

    def test_credentials_never_in_argv(self):
        """The app password must not appear in curl's command line."""
        env = {"NEXTCLOUD_URL": "https://nc.example", "NEXTCLOUD_USER": USER, "NEXTCLOUD_TOKEN": TOKEN}
        captured = {}

        def fake_run(args, **kwargs):
            captured["argv"] = list(args)
            captured["input"] = kwargs.get("input")
            raise SystemExit  # stop before any real call

        orig = nextcloud_api.subprocess.run
        nextcloud_api.subprocess.run = fake_run
        try:
            with self.assertRaises(SystemExit):
                nextcloud_api.cmd_files_upload(env, "/uploaded.txt", "secret-content")
        finally:
            nextcloud_api.subprocess.run = orig
        argv_str = " ".join(captured["argv"])
        self.assertNotIn(TOKEN, argv_str, "token leaked into argv")
        self.assertNotIn("secret-content", argv_str, "file content leaked into argv")
        self.assertIn("--data-binary", argv_str)
        self.assertIn("@-", argv_str)
        # content travels via stdin instead
        self.assertEqual(captured["input"], "secret-content")


class MockServerTests(CLIBase):
    def test_check_success(self):
        result = self.run_cli(["check"])
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["data"]["authenticated"])

    def test_check_bad_token(self):
        result = self.run_cli(["check"], token="wrong-token")
        self.assertEqual(result["status"], "error")
        self.assertIn("Authentication", result["message"])

    def test_files_list_decodes_names(self):
        result = self.run_cli(["files", "list"])
        self.assertEqual(result["status"], "success")
        paths = [item["path"] for item in result["data"]]
        self.assertIn("/hello.txt", paths)
        self.assertIn("/my file.txt", paths)  # percent-encoded href decoded

    def test_files_get_found(self):
        result = self.run_cli(["files", "get", "--path", "/hello.txt"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["content"], "hello world")

    def test_files_get_missing_is_error(self):
        """A 404 must be reported as an error, not as file content."""
        result = self.run_cli(["files", "get", "--path", "/missing.txt"])
        self.assertEqual(result["status"], "error")
        self.assertIn("File not found", result["message"])

    def test_files_delete_missing_is_error(self):
        """A 404 on delete must be reported as an error, not success."""
        result = self.run_cli(["files", "delete", "--path", "/missing.txt"])
        self.assertEqual(result["status"], "error")
        self.assertIn("Not found", result["message"])

    def test_files_delete_success(self):
        result = self.run_cli(["files", "delete", "--path", "/hello.txt"])
        self.assertEqual(result["status"], "success")

    def test_files_upload_exact_content(self):
        content = "line one\nline two\nwith, commas; and semicolons\n"
        result = self.run_cli(["files", "upload", "--path", "/uploaded.txt", "--content", content])
        self.assertEqual(result["status"], "success")
        stored = MockNextcloudHandler.stored_uploads.get("uploaded.txt", b"")
        self.assertEqual(stored.decode(), content)

    def test_files_upload_large_content_via_stdin(self):
        """200 KiB via --content-file - must survive — argv-based approaches cap at ~128 KiB."""
        content = "x" * 200_000
        env = {k: v for k, v in os.environ.items() if not k.startswith("NEXTCLOUD_")}
        env.update({
            "NEXTCLOUD_URL": f"http://127.0.0.1:{self.port}",
            "NEXTCLOUD_USER": USER,
            "NEXTCLOUD_TOKEN": TOKEN,
        })
        proc = subprocess.run(
            [sys.executable, API, "files", "upload", "--path", "/uploaded.txt", "--content-file", "-"],
            input=content, capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(MockNextcloudHandler.stored_uploads.get("uploaded.txt", b"")), 200_000)

    def test_files_upload_requires_content(self):
        result = self.run_cli(["files", "upload", "--path", "/uploaded.txt"])
        self.assertEqual(result["status"], "error")
        self.assertIn("Provide --content", result["message"])

    def test_files_mkdir_already_exists(self):
        result = self.run_cli(["files", "mkdir", "--path", "/existing"])
        self.assertEqual(result["status"], "error")
        self.assertIn("already exists", result["message"])

    def test_files_move_success(self):
        result = self.run_cli(["files", "move", "--src", "/hello.txt", "--dst", "/renamed.txt"])
        self.assertEqual(result["status"], "success")

    def test_notes_create(self):
        result = self.run_cli(["notes", "create", "--title", "New", "--content", "Body"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["title"], "New")

    def test_notes_edit(self):
        result = self.run_cli(["notes", "edit", "--id", "1", "--title", "Changed"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["title"], "Changed")

    def test_notes_delete(self):
        result = self.run_cli(["notes", "delete", "--id", "1"])
        self.assertEqual(result["status"], "success")

    def test_env_file_flag(self):
        """--env-file must be honored (previously accepted and ignored)."""
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write(f'NEXTCLOUD_URL="http://127.0.0.1:{self.port}"\n')
            f.write(f'NEXTCLOUD_USER="{USER}"\n')
            f.write(f'NEXTCLOUD_TOKEN="{TOKEN}"\n')
            env_path = f.name
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith("NEXTCLOUD_")}
            proc = subprocess.run(
                [sys.executable, API, "--env-file", env_path, "check"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "success")
        finally:
            os.unlink(env_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
