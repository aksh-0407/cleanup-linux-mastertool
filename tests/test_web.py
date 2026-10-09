#!/usr/bin/env python3
"""
The dashboard (`clean ui`): who may talk to it, what it answers, and the
whole scan -> choose -> remove -> undo path through it.

- Most tests run the server in this process on temp data. The Trash list is
  limited to the test's own Trash, and every job that would change files is
  replaced by a stand-in that only prints (the command line it was given is
  checked instead), so nothing outside the test folder can be touched.
- What really removes, restores and deletes runs in the sealed namespace
  (tests/ui/serve.py; see helpers.SEAL), driven over HTTP from here.
- The browser tests drive Chrome through the same flows (tests/ui/flow.mjs;
  skipped without node 22+ and Chrome). With UI_SHOTS=FOLDER they also keep
  screenshots of every page there.
"""

import _thread
import contextlib
import http.client
import io
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
from html.parser import HTMLParser
from urllib.parse import quote
from unittest import mock

from helpers import CLEAN, HERE, SEAL, Base, blob, needs_unshare

from cleanup import choices as _choices
from cleanup import runs
from cleanup import settings as _settings
from cleanup.actions import trash as _trash
from cleanup.commands import ui as _ui
from cleanup.report.text import command_line
from cleanup.web import api, server
from cleanup.web import jobs as _jobs

NASTY = "<img src=x onerror=alert(1)>"
SERVE = os.path.join(HERE, "ui", "serve.py")
FLOW = os.path.join(HERE, "ui", "flow.mjs")

# Stand-ins for `clean ...` in jobs: one that only prints its command line,
# one that waits until it is stopped (SIGTERM: exit 130, as clean does).
PRINTS = "import sys; print('stand-in:', *sys.argv[1:])"
WAITS = ("import signal, sys, time\n"
         "signal.signal(signal.SIGTERM, lambda *a: sys.exit(130))\n"
         "print('waiting', flush=True)\n"
         "time.sleep(60)\n")


def _browser():
    """(node, chrome) when both are here, else None (node 22+ has its own
    WebSocket)."""
    node = shutil.which("node")
    chrome = next(filter(None, map(shutil.which, ("google-chrome", "chromium",
                                                  "chromium-browser"))), None)
    if not node or not chrome:
        return None
    r = subprocess.run([node, "-e", "process.exit(typeof WebSocket === 'function' ? 0 : 1)"],
                       capture_output=True)
    return (node, chrome) if r.returncode == 0 else None


BROWSER = _browser()


class Client:
    """HTTP to a dashboard, with exactly the headers given (a None value
    leaves a header out, Host included)."""

    def __init__(self, port, cookie=None):
        self.port = port
        self.cookie = cookie
        self.host = f"127.0.0.1:{port}"

    def send(self, method, path, headers, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in headers.items():
                if v is not None:
                    c.putheader(k, v)
            if body is not None:
                c.putheader("Content-Length", str(len(body)))
            c.endheaders(body)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def _headers(self, cookie, extra):
        h = {"Host": self.host}
        if cookie and self.cookie:
            h["Cookie"] = self.cookie
        h.update(extra or {})
        return h

    def get(self, path, headers=None, cookie=True, accept="text/html"):
        return self.send("GET", path, self._headers(cookie, dict({"Accept": accept},
                                                                 **(headers or {}))))

    def post(self, path, obj, headers=None, cookie=True, raw=None):
        h = self._headers(cookie, dict({"Origin": "http://" + self.host, "X-Clean": "1",
                                        "Content-Type": "application/json"},
                                       **(headers or {})))
        return self.send("POST", path, h, raw if raw is not None else json.dumps(obj).encode())

    def api(self, path, obj=None):
        """(status, decoded JSON)."""
        if obj is None:
            st, _, body = self.get(path, accept="application/json")
        else:
            st, _, body = self.post(path, obj)
        return st, json.loads(body)


class Tags(HTMLParser):
    """Every tag of a page, with its attributes."""

    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class WebBase(Base):
    """A dashboard in this process, on the test's own folders."""

    stand_in = PRINTS       # what a job that would change files runs instead
    real_scans = True       # scans (which only read) run the real clean

    def setUp(self):
        super().setUp()
        self.saved_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        _settings.reload()
        # Only the test's own Trash, never the drives' (all_trashes would
        # find every mounted drive's).
        patch = mock.patch.object(_trash, "all_trashes", self.trashes)
        patch.start()
        self.addCleanup(patch.stop)
        self.opened = []
        self.spawned = []
        self.app = server.App(self.data_dir, spawn=self.spawn, opener=self.opened.append)
        self.srv = server.Server(self.app, 0)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        self.c = Client(self.app.port, f"{self.app.cookie_name}={self.app.secret}")

    def tearDown(self):
        job = self.app.jobs.active()
        if job is not None:
            job.cancel()
            job.wait(30)
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.clear()
        os.environ.update(self.saved_env)
        _settings.reload()
        super().tearDown()

    @staticmethod
    def trashes(vols=None):
        home = _trash.home_trash_path()
        return [_trash.Trash(home, None)] if os.path.isdir(os.path.join(home, "info")) else []

    def spawn(self, argv, env):
        self.spawned.append(list(argv))
        reads_only = (argv[0] in ("dupes", "empty")
                      and not {"--delete", "--remove", "-n", "--force"} & set(argv))
        cmd = (_jobs.command(argv) if reads_only and self.real_scans
               else [sys.executable, "-c", self.stand_in, *argv])
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, start_new_session=True,
                                close_fds=True)

    # ------------------------------------------------------------ helpers

    def api(self, path, obj=None, status=200):
        st, data = self.c.api(path, obj)
        self.assertEqual(st, status, data)
        return data

    def refused(self, path, obj, status):
        """The error message of a request that must fail with `status`."""
        st, data = self.c.api(path, obj)
        self.assertEqual(st, status, data)
        return data["error"]

    def page(self, path, status=200):
        st, h, body = self.c.get(path)
        text = body.decode("utf-8", "surrogateescape")
        self.assertEqual(st, status, text[:2000])
        self.assertNotIn("internal error", text)
        return text

    def wait(self, jid, timeout=120):
        job = self.app.jobs.get(jid)
        self.assertTrue(job.wait(timeout), "the job didn't end")
        return job

    def sample(self):
        """Two identical photo folders, a loose copy, a folder with a nasty
        name whose file is also elsewhere, and an empty folder."""
        for top in ("Photos", "Backup/Photos"):
            self.write(f"{top}/a.jpg", blob("photo a", 6000))
            self.write(f"{top}/b.png", blob("photo b", 5000))
            self.write(f"{top}/page.html", b"<html><script>alert(1)</script></html>\n"
                       + blob("page", 2000))
            self.write(f"{top}/notes.txt", blob("notes", 3000))
        self.write("Loose/a (1).jpg", blob("photo a", 6000))
        self.write(f"{NASTY}/c.txt", blob("c", 3500))
        self.write("Other/c.txt", blob("c", 3500))
        self.write("Other/only.bin", blob("only", 1000))
        self.mk("Empty", "inner")

    def scan(self):
        """`clean dupes` of the sample; the run's name."""
        self.sample()
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.last_run().name

    def results(self, name):
        return self.app.results(self.app.run(name))

    def ref(self, name, rel, kind="d"):
        """The box name of the folder (or file) `rel` in run `name`."""
        res = self.results(name)
        path = os.path.join(self.root, rel)
        count = len(res.m.rows) if kind == "d" else len(res.m.files)
        found = [f"{kind}{i}" for i in range(count) if res.path_of(f"{kind}{i}") == path]
        self.assertEqual(len(found), 1, f"{rel} in run {name}")
        return found[0]

    def state(self, name, ref):
        res = self.results(name)
        with res.lock:
            return res.state().of(ref)[0]

    def file_id(self, name, rel):
        return self.ref(name, rel, "f")[1:]


# ---------------------------------------------------------------- who may ask

class TestAccess(WebBase):
    def test_no_or_wrong_cookie_is_refused(self):
        name = self.scan()
        wrong = [None, f"{self.app.cookie_name}=", f"{self.app.cookie_name}={self.app.secret}x",
                 f"{self.app.cookie_name}={self.app.secret[:-1]}",
                 f"clean_ui_1={self.app.secret}", f"other={self.app.secret}"]
        for cookie in wrong:
            with self.subTest(cookie=cookie):
                h = {"Cookie": cookie}
                st, _, body = self.c.get("/", h, cookie=False)
                self.assertEqual(st, 403)
                self.assertIn(b"Not signed in", body)
                self.assertIn(b"clean ui", body)
                self.assertNotIn(b"/static/app.js", body)     # no dashboard around it
                for path in ("/api/trash", f"/api/results/{name}", f"/runs/{name}/run.json",
                             f"/preview/{name}/0", "/history"):
                    st, _, body = self.c.get(path, h, cookie=False, accept="application/json")
                    self.assertEqual(st, 403, path)
                    self.assertEqual(json.loads(body), {"error": "not signed in"})
                st, _, _ = self.c.post("/api/settings", {"key": "ui.theme", "op": "set",
                                                         "value": "dark"}, h, cookie=False)
                self.assertEqual(st, 403)
        self.assertFalse(os.path.exists(self.settings_file))
        self.assertIn(name, self.page("/history"))                   # with the cookie: yes

    def test_launch_link_works_once(self):
        url = self.app.launch_url()
        self.assertTrue(url.startswith(f"http://127.0.0.1:{self.app.port}/launch?t="))
        path = url.split(self.c.host, 1)[1]
        st, h, body = self.c.get(path, cookie=False)
        self.assertEqual(st, 200)
        cookie = h["set-cookie"]
        self.assertEqual(cookie, f"{self.app.cookie_name}={self.app.secret}; HttpOnly; "
                                 "SameSite=Strict; Path=/")
        self.assertIn(b'http-equiv="refresh" content="0;url=/"', body)
        # used up: only the not-signed-in page, without a cookie
        st, h, body = self.c.get(path, cookie=False)
        self.assertEqual(st, 403)
        self.assertNotIn("set-cookie", h)
        self.assertIn(b"Not signed in", body)
        # ... but a browser that has the cookie goes on to the dashboard
        st, h, _ = self.c.get(path)
        self.assertEqual((st, h.get("location")), (303, "/"))
        for bad in ("/launch", "/launch?t=", "/launch?t=nope", f"{path}x"):
            st, h, _ = self.c.get(bad, cookie=False)
            self.assertEqual(st, 403, bad)
            self.assertNotIn("set-cookie", h)
        # expired
        token = self.app.new_token()
        self.app.tokens[token] = time.time() - 1
        st, h, _ = self.c.get(f"/launch?t={token}", cookie=False)
        self.assertEqual(st, 403)
        self.assertNotIn("set-cookie", h)

    def test_host_must_be_this_dashboard(self):
        port = self.app.port
        for host in (None, "", "evil.example", f"evil.example:{port}", "127.0.0.1",
                     f"127.0.0.1:{port + 1}", f"127.0.0.2:{port}", f"[::1]:{port}",
                     f"127.0.0.1:{port}.evil.example", f"localhost.:{port}"):
            for path in ("/", "/static/app.js", "/launch?t=x", "/api/trash"):
                with self.subTest(host=host, path=path):
                    st, _, body = self.c.get(path, {"Host": host}, accept="application/json")
                    self.assertEqual(st, 403)
                    self.assertIn(b"wrong Host header", body)
            st, _, _ = self.c.post("/api/settings", {"key": "ui.theme", "op": "set",
                                                     "value": "dark"},
                                   {"Host": host, "Origin": f"http://{host}"})
            self.assertEqual(st, 403)
        self.assertFalse(os.path.exists(self.settings_file))
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            st, _, _ = self.c.get("/", {"Host": host})
            self.assertEqual(st, 200, host)

    def test_other_sites_are_refused(self):
        h = self.c.host
        for extra in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                      {"Origin": f"https://{h}"}, {"Origin": f"http://localhost:{self.app.port}"},
                      {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(extra=extra):
                for path in ("/", "/api/trash", "/static/app.js", "/launch?t=x"):
                    st, _, body = self.c.get(path, extra, accept="application/json")
                    self.assertEqual(st, 403, path)
                    self.assertIn(b"another site", body)
        self.assertEqual(self.c.get("/", {"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.c.get("/", {"Sec-Fetch-Site": "none"})[0], 200)

    def test_post_needs_origin_json_and_x_clean(self):
        body = {"key": "ui.theme", "op": "set", "value": "dark"}
        cases = [({"Origin": None}, 403), ({"Origin": "http://evil.example"}, 403),
                 ({"X-Clean": None}, 403), ({"X-Clean": "0"}, 403),
                 ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Sec-Fetch-Site": "same-site"}, 403),
                 ({"Content-Type": None}, 415), ({"Content-Type": "text/plain"}, 415),
                 ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
                 ({"Content-Type": "multipart/form-data; boundary=x"}, 415)]
        for extra, status in cases:
            with self.subTest(extra=extra):
                st, _, out = self.c.post("/api/settings", body, extra)
                self.assertEqual(st, status, out)
        self.assertFalse(os.path.exists(self.settings_file))
        for raw, status in ((b"{", 400), (b"[1]", 400), (b"\xff", 400), (b'"x"', 400)):
            st, _, _ = self.c.post("/api/settings", None, raw=raw)
            self.assertEqual(st, status, raw)
        st, _, _ = self.c.send("POST", "/api/settings", dict(
            self.c._headers(True, {"Origin": "http://" + self.c.host, "X-Clean": "1",
                                   "Content-Type": "application/json",
                                   "Content-Length": str(server.MAX_BODY + 1)})))
        self.assertEqual(st, 413)
        self.assertEqual(self.c.post("/static/app.js", {})[0], 405)
        self.assertEqual(self.c.send("PUT", "/", self.c._headers(True, {}))[0], 405)
        self.assertEqual(self.c.post("/api/settings", body)[0], 200)

    def test_every_answer_is_locked_down(self):
        name = self.scan()
        paths = ["/", "/scan", "/history", "/settings", "/trash", f"/runs/{name}",
                 f"/runs/{name}/report.html", f"/runs/{name}/summary.txt", "/static/app.js",
                 "/static/app.css", "/api/trash", "/nope", "/launch?t=x",
                 f"/preview/{name}/{self.file_id(name, 'Photos/page.html')}"]
        for path in paths:
            with self.subTest(path=path):
                st, h, _ = self.c.get(path, cookie=path != "/launch?t=x")
                self.assertEqual(h["x-content-type-options"], "nosniff")
                self.assertEqual(h["x-frame-options"], "DENY")
                self.assertEqual(h["referrer-policy"], "no-referrer")
                csp = h["content-security-policy"]
                self.assertIn("frame-ancestors 'none'", csp)
                if "script-src" in csp:
                    src = re.search(r"script-src ([^;]*)", csp).group(1)
                    if path.endswith("report.html"):   # its one script, in a sandbox
                        self.assertTrue(csp.startswith("sandbox "))
                    else:
                        self.assertEqual(src, "'self'")
                else:
                    self.assertIn("default-src 'none'", csp)
                self.assertIn(h["cache-control"], ("no-store", "no-cache"))
        page = self.page("/")
        for tag, attrs in Tags(page).tags:
            if tag == "script":
                self.assertTrue(attrs.get("src") == "/static/app.js"
                                or attrs.get("type") == "application/json", attrs)
            self.assertFalse(any(k.startswith("on") for k in attrs), (tag, attrs))
            self.assertNotIn("style", attrs)

    def test_admin_token_only_for_clean_ui(self):
        good = {"X-Clean-Admin": self.app.admin}
        st, _, body = self.c.send("POST", "/admin/token", self.c._headers(False, good), b"")
        self.assertEqual(st, 200)
        url = json.loads(body)["url"]
        st, h, _ = self.c.get(url.split(self.c.host, 1)[1], cookie=False)
        self.assertEqual(st, 200)
        self.assertIn("set-cookie", h)
        for extra in ({}, {"X-Clean-Admin": "nope"},
                      dict(good, Origin="http://" + self.c.host),
                      dict(good, Origin="http://evil.example")):
            st, _, _ = self.c.send("POST", "/admin/token", self.c._headers(False, extra), b"")
            self.assertEqual(st, 403, extra)
        st, _, _ = self.c.send("GET", "/admin/token", self.c._headers(False, good))
        self.assertEqual(st, 403)

    def test_paths_never_leave_the_list(self):
        name = self.scan()
        run = self.app.run(name)
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w") as f:
            f.write("SECRET")
        bad = ["/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py",
               "/static/server.py", "/static/app.js/../../api.py", "/static/", "/static/.",
               "/runs/../../../etc/passwd", f"/runs/{name}/..%2f..%2fsettings.json",
               f"/runs/{name}/%2e%2e", f"/runs/{name}/manifest.json.gz",
               f"/runs/{name}/selection.json.tmp", "/runs/2020-01-01_000000-dupes/run.json",
               f"/runs/{name}x/run.json", "/runs/x/run.json",
               f"/runs/{name}/run.json/x", f"/runs/%2e%2e%2f{name}/run.json",
               f"/preview/{name}/../../etc/passwd", f"/preview/{name}/%2e%2e",
               "/etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "//etc/passwd"]
        for path in bad:
            with self.subTest(path=path):
                st, _, body = self.c.get(path, accept="application/json")
                self.assertIn(st, (400, 404), body)
                self.assertNotIn(b"root:", body)
        self.assertEqual(self.c.get("/a%ffb")[0], 400)
        self.assertEqual(self.c.get("/a%00b", accept="application/json")[0], 404)
        # a run file made into a link to something else is not followed
        os.unlink(run.file("summary.txt"))
        os.symlink(secret, run.file("summary.txt"))
        st, _, body = self.c.get(f"/runs/{name}/summary.txt")
        self.assertEqual(st, 404)
        self.assertNotIn(b"SECRET", body)
        # nor is a run folder that is a link
        link = os.path.join(os.path.dirname(run.folder), "2020-01-01_000000-dupes")
        os.symlink(run.folder, link)
        self.assertEqual(self.c.get("/runs/2020-01-01_000000-dupes/run.json")[0], 404)
        self.assertEqual(self.c.get(f"/runs/{name}/run.json")[0], 200)


class TestPreview(WebBase):
    def test_previews_by_number_only(self):
        name = self.scan()
        n = self.file_id(name, "Photos/notes.txt")
        st, h, body = self.c.get(f"/preview/{name}/{n}", accept="*/*")
        self.assertEqual(st, 200)
        self.assertEqual(body, blob("notes", 3000))
        self.assertEqual(h["content-type"], "text/plain; charset=utf-8")
        self.assertTrue(h["content-security-policy"].startswith("sandbox;"))
        files = len(self.results(name).m.files)
        for bad in (str(files), str(files + 5), "-1", "1e3", "0x1", "%201", "%EF%BC%91",
                    "1.0", "9" * 13, "", "%2e%2e"):
            with self.subTest(bad=bad):
                st, _, _ = self.c.get(f"/preview/{name}/{bad}", accept="*/*")
                self.assertIn(st, (404,), bad)
        # only dupes runs have previews
        self.assertEqual(self.empty(self.root).returncode, 0)
        self.assertEqual(self.c.get(f"/preview/{self.last_run().name}/0")[0], 404)

    def test_html_and_svg_are_only_text(self):
        name = self.scan()
        n = self.file_id(name, "Photos/page.html")
        st, h, body = self.c.get(f"/preview/{name}/{n}", accept="text/html")
        self.assertEqual(st, 200)
        self.assertEqual(h["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", h["content-security-policy"])
        self.assertTrue(body.startswith(b"<html><script>"))

    def test_ranges(self):
        name = self.scan()
        n = self.file_id(name, "Photos/a.jpg")
        st, h, body = self.c.get(f"/preview/{name}/{n}", {"Range": "bytes=0-9"}, accept="*/*")
        self.assertEqual((st, body, h["content-range"]), (206, blob("photo a", 6000)[:10],
                                                          "bytes 0-9/6000"))
        self.assertEqual(h["content-type"], "image/jpeg")
        st, h, body = self.c.get(f"/preview/{name}/{n}", {"Range": "bytes=-5"}, accept="*/*")
        self.assertEqual((st, body), (206, blob("photo a", 6000)[-5:]))
        st, h, _ = self.c.get(f"/preview/{name}/{n}", {"Range": "bytes=6000-"}, accept="*/*")
        self.assertEqual((st, h["content-range"]), (416, "bytes */6000"))
        st, _, body = self.c.get(f"/preview/{name}/{n}", {"Range": "bytes=0-1,4-5"},
                                 accept="*/*")
        self.assertEqual((st, len(body)), (200, 6000))

    def test_changed_moved_and_linked_files_are_not_shown(self):
        name = self.scan()
        notes = self.file_id(name, "Photos/notes.txt")
        a = self.file_id(name, "Photos/a.jpg")
        b = self.file_id(name, "Photos/b.png")
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w") as f:
            f.write("SECRET" * 500)
        os.utime(os.path.join(self.root, "Photos/notes.txt"), (1, 1))
        st, _, body = self.c.get(f"/preview/{name}/{notes}", accept="*/*")
        self.assertEqual(st, 409)
        self.assertIn(b"changed since the scan", body)
        path = os.path.join(self.root, "Photos/a.jpg")
        os.unlink(path)
        self.assertEqual(self.c.get(f"/preview/{name}/{a}", accept="*/*")[0], 404)
        os.symlink(secret, path)
        st, _, body = self.c.get(f"/preview/{name}/{a}", accept="*/*")
        self.assertIn(st, (404, 409))
        self.assertNotIn(b"SECRET", body)
        # its folder replaced by a link to somewhere outside the scan
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.rename(os.path.join(self.root, "Photos"), elsewhere)
        os.symlink(elsewhere, os.path.join(self.root, "Photos"))
        st, _, body = self.c.get(f"/preview/{name}/{b}", accept="*/*")
        self.assertEqual(st, 404)
        self.assertIn(b"not where the scan saw it", body)

    def test_size_limit_and_kinds(self):
        name = self.scan()
        self.assertEqual(self.c.get(f"/preview/{name}/{self.file_id(name, 'Other/c.txt')}",
                                    accept="*/*")[0], 200)
        self.assertEqual(self.run_clean("config", "set", "ui.preview_max_mb", "0").returncode, 0)
        st, _, body = self.c.get(f"/preview/{name}/{self.file_id(name, 'Other/c.txt')}",
                                 accept="*/*")
        self.assertEqual(st, 413)
        self.assertIn(b"ui.preview_max_mb", body)
        # (a .bin file has no preview; it is in no group, so there is no number for it)
        self.write("x/y.bin", blob("bin", 4000))
        self.write("z/y.bin", blob("bin", 4000))
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0)
        name = self.last_run().name
        st, _, body = self.c.get(f"/preview/{name}/{self.file_id(name, 'x/y.bin')}",
                                 accept="*/*")
        self.assertEqual(st, 415)


# ---------------------------------------------------------------- confirmations

class TestConfirm(WebBase):
    def empty_jobs(self):
        return api.parse(["empty", self.root]).jobs

    def test_do_needs_a_fresh_confirmation(self):
        name = self.scan()
        for body in ({}, {"nonce": None}, {"nonce": 5}, {"nonce": "nope"}, {"nonce": ""},
                     {"action": "remove", "run": name, "method": "trash"}):
            with self.subTest(body=body):
                self.assertIn("confirm", self.refused("/api/do", body, 403))
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "trash"})
        self.assertEqual((c["title"], c["button"], c["typed"], c["danger"]),
                         ("Remove the selected copies", "Move to the Trash", None, False))
        self.assertIn("To the Trash", c["html"])
        self.assertEqual(c["cli"], f"clean remove {name} --trash")
        digest = self.api(f"/api/results/{name}")["footer"]["digest"]
        d = self.api("/api/do", {"nonce": c["nonce"]})
        self.assertEqual(d["url"], f"/job/{d['id']}")
        self.wait(d["id"])
        self.assertEqual(self.spawned, [["remove", name, "-y", "--trash", "--plan-digest",
                                         digest]])
        self.assertIn("used already", self.refused("/api/do", {"nonce": c["nonce"]}, 403))
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "trash"})
        self.app.nonces[c["nonce"]]["expires"] = time.time() - 1
        self.assertIn("expired", self.refused("/api/do", {"nonce": c["nonce"]}, 403))
        self.assertEqual(len(self.spawned), 1)

    def test_deleting_for_good_needs_the_word(self):
        name = self.scan()
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "permanent"})
        self.assertEqual((c["typed"], c["danger"], c["button"]), ("delete", True,
                                                                  "Delete for good"))
        self.assertIn("Deleted for good", c["html"])
        for typed in (None, "", "yes", "delet", "deletee", 1):
            self.assertIn("type delete", self.refused("/api/do", {"nonce": c["nonce"],
                                                                  "typed": typed}, 403))
        self.assertEqual(self.spawned, [])
        d = self.api("/api/do", {"nonce": c["nonce"], "typed": " Delete "})
        self.wait(d["id"])
        self.assertEqual(self.spawned[0][:4], ["remove", name, "-y", "--permanent"])
        self.assertIn("used already", self.refused("/api/do", {"nonce": c["nonce"],
                                                               "typed": "delete"}, 403))

    def test_jobs_only_scan(self):
        name = self.scan()
        root = self.root
        bad = [["remove", name, "-y"], ["remove", name, "-y", "--permanent"], ["undo", "-y"],
               ["trash", "empty", "-y"], ["trash", "restore", root, "-y"],
               ["history", "--prune"], ["history", "--delete", name, "-y"],
               ["config", "set", "ui.theme", "dark"], ["ui"], ["--help"],
               ["dupes", "--remove", root], ["dupes", "--remove", "-y", root],
               ["dupes", "-n", root], ["empty", "--delete", root], ["empty", "-d", root],
               ["empty", "--force", root], ["empty", "--delete", "--", root],
               ["dupes", "relative/path"], ["dupes", os.path.join(root, "nope")],
               ["dupes", os.path.join(root, "Other/c.txt")], ["dupes", "--bogus", root],
               ["dupes", "--help"], ["dupes", root + "\0x"], [], "dupes " + root, None,
               [1, 2], ["dupes"] + [root] * (api.MAX_ARGS + 1)]
        for argv in bad:
            with self.subTest(argv=argv):
                self.refused("/api/jobs", {"argv": argv}, 400)
        self.assertEqual(self.spawned, [])
        self.assertTrue(os.path.isfile(os.path.join(root, "Photos/a.jpg")))

    def test_one_job_at_a_time_and_cancel(self):
        self.stand_in = WAITS
        self.real_scans = False
        name = self.scan()
        j = self.api("/api/jobs", {"argv": ["dupes", "--", self.root]})
        for _ in range(100):
            if self.app.jobs.get(j["id"]).lines_since(0):
                break
            time.sleep(0.05)
        self.assertIn("another job is still running (clean dupes --)",
                      self.refused("/api/jobs", {"argv": ["empty", "--", self.root]}, 409))
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "trash"})
        self.assertIn("another job", self.refused("/api/do", {"nonce": c["nonce"]}, 409))
        self.assertEqual(self.refused("/api/jobs/99/cancel", {}, 404), "no such job")
        self.refused("/api/jobs/99", None, 404)
        out = self.api(f"/api/jobs/{j['id']}/cancel", {})
        self.assertEqual(out["cancelled"], True)
        job = self.wait(j["id"])
        self.assertEqual((job.code, job.cancelled), (130, True))
        s = self.api(f"/api/jobs/{j['id']}")
        self.assertEqual((s["running"], s["code"], s["cancelled"], s["lines"], s["next"]),
                         (False, 130, True, ["waiting"], 1))
        self.assertEqual(self.api(f"/api/jobs/{j['id']}?since=1")["lines"], [])
        self.assertEqual(self.api(f"/api/jobs/{j['id']}?since=x")["lines"], ["waiting"])
        self.assertEqual(self.api(f"/api/jobs/{j['id']}/cancel", {})["cancelled"], False)
        self.assertEqual(len(self.spawned), 1)
        # the confirmation refused for the busy job still works once it ended
        self.stand_in = PRINTS
        d = self.api("/api/do", {"nonce": c["nonce"]})
        self.wait(d["id"])
        self.assertEqual(self.spawned[-1][:4], ["remove", name, "-y", "--trash"])

    def test_refusals_say_why(self):
        name = self.scan()
        self.assertEqual(self.refused("/api/confirm", {"action": "rm"}, 400), "unknown action")
        self.refused("/api/confirm", {"action": "remove", "run": name, "method": "fast"}, 400)
        self.refused("/api/confirm", {"action": "remove", "run": "../x", "method": "trash"}, 404)
        self.refused("/api/confirm", {"action": "undo", "run": name}, 404)
        self.refused("/api/confirm", {"action": "trash_empty", "older_than": "soon"}, 400)
        self.refused("/api/confirm", {"action": "trash_empty", "older_than": -1}, 400)
        self.api(f"/api/selection/{name}", {"op": "none"})
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "trash"})
        self.assertEqual(c["refused"], "Nothing to remove")
        self.assertNotIn("nonce", c)
        for body, why in (
                ({"action": "trash_empty"}, "Nothing to empty."),
                ({"action": "trash_empty", "older_than": 30},
                 "Nothing to empty that has been in the Trash 30 days or more."),
                ({"action": "trash_restore", "token": api.path_token(self.root)},
                 f"{self.root} is not in the Trash (any more)."),
                ({"action": "history_prune"}, "Nothing to prune: a run goes once it is older "
                                              "than 90 days and not among the newest 30.")):
            self.assertEqual(self.api("/api/confirm", body)["refused"], why)
        running = self.fake_run("2020-01-01_000000-dupes", status="running", pid=os.getpid(),
                                proc_start=runs.proc_start(os.getpid()))
        c = self.api("/api/confirm", {"action": "history_delete",
                                      "run": os.path.basename(running)})
        self.assertIn("still running", c["refused"])
        self.assertTrue(os.path.isdir(running))
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.app.nonces, {})

    def test_what_each_confirmation_runs(self):
        name = self.scan()
        r = self.empty("-x", "--exclude", "*.tmp", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        empty = self.last_run().name
        # an old run (and only the newest kept), and a Trash item
        old = self.fake_run("2020-01-01_000000-dupes")
        self.assertEqual(self.run_clean("config", "set", "history.keep_runs", "1").returncode, 0)
        trash = _trash.home_trash_path()
        os.makedirs(os.path.join(trash, "files"))
        os.makedirs(os.path.join(trash, "info"))
        gone = os.path.join(self.root, "gone.txt")
        with open(os.path.join(trash, "files", "gone.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(trash, "info", "gone.txt.trashinfo"), "w") as f:
            f.write(f"[Trash Info]\nPath={gone}\nDeletionDate=2020-01-01T00:00:00\n")
        cases = [
            ({"action": "history_delete", "run": name},
             ["history", "--delete", name, "-y"], "/history", None),
            ({"action": "history_prune"}, ["history", "--prune"], "/history", None),
            ({"action": "empty_delete", "run": empty},
             ["empty", "--delete", "-x", "--default-excludes", "-j", str(self.empty_jobs()),
              "--exclude=*.tmp", "--", self.root], None, None),
            ({"action": "trash_restore", "token": api.path_token(gone)},
             ["trash", "restore", gone, "-y"], "/trash", None),
            ({"action": "trash_empty", "older_than": "30", "drive": "home"},
             ["trash", "empty", "--older-than", "30", "--drive", "home", "-y"], "/trash",
             "delete"),
            ({"action": "trash_empty"}, ["trash", "empty", "-y"], "/trash", "delete"),
        ]
        for body, argv, after, typed in cases:
            with self.subTest(action=body["action"]):
                c = self.api("/api/confirm", body)
                self.assertNotIn("refused", c, c)
                self.assertEqual(c["typed"], typed)
                self.assertEqual(c["cli"], command_line([a for a in argv if a != "-y"]))
                d = self.api("/api/do", {"nonce": c["nonce"], "typed": typed})
                job = self.wait(d["id"])
                self.assertEqual(self.spawned[-1], argv)
                self.assertEqual(job.after, after)
        self.assertTrue(os.path.isdir(old))       # stand-ins only: nothing was done
        self.assertTrue(os.path.isdir(os.path.join(self.root, "Empty/inner")))

    def test_nasty_names_in_the_dialog_are_text(self):
        self.sample()
        os.rename(os.path.join(self.root, "Other"), os.path.join(self.root, NASTY + "2"))
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        name = self.last_run().name
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "trash"})
        self.assertNotIn("<img", c["html"])
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", c["html"])


# ---------------------------------------------------------------- the API

class TestAPI(WebBase):
    def test_browse(self):
        self.mk("b", "Zeta")
        self.mk("b", "alpha")
        self.mk("b", ".hidden")
        self.write("b/file.txt", "x")
        odd = os.path.join(self.root, "b", os.fsdecode(b"caf\xe9"))
        os.mkdir(odd)
        top = os.path.join(self.root, "b")
        out = self.api("/api/browse?path=" + quote(top))
        self.assertEqual(out["path"], top)
        names = [d["name"] for d in out["dirs"]]
        self.assertEqual(names, ["alpha", "caf\\xe9", "Zeta"])
        self.assertEqual(out["places"][0]["label"], "Home")
        self.assertEqual(out["places"][-1], {"label": "Computer (/)", "path": "/",
                                             "token": api.path_token("/")})
        tok = next(d["token"] for d in out["dirs"] if d["name"] == "caf\\xe9")
        inner = self.api("/api/browse?t=" + tok)
        self.assertEqual(os.fsencode(inner["path"]), os.fsencode(odd))
        self.assertEqual(inner["parent"], out["token"])
        self.assertEqual(inner["dirs"], [])
        hidden = self.api("/api/browse?hidden=1&path=" + quote(top))
        self.assertIn(".hidden", [d["name"] for d in hidden["dirs"]])
        self.assertIsNotNone(self.api("/api/browse?path=" + quote(top + "/nope"))["error"])
        for q in ("path=relative", "t=%2F%2F%2F", "t=bm90IGFic29sdXRl", "path=/a%00b"):
            self.refused("/api/browse?" + q, None, 400)
        os.chmod(top, 0)
        try:
            if os.access(top, os.R_OK):
                self.skipTest("running as root")
            self.assertIn("ermission", self.api("/api/browse?path=" + quote(top))["error"])
        finally:
            os.chmod(top, 0o755)

    def test_a_scan_from_the_dashboard(self):
        self.sample()
        j = self.api("/api/jobs", {"argv": ["dupes", "--min-size", "1", "--", self.root]})
        self.assertEqual(j["cli"], f"clean dupes --min-size 1 -- {self.root}")
        job = self.wait(j["id"])
        self.assertEqual(job.code, 0, job.lines_since(0))
        s = self.api(f"/api/jobs/{j['id']}")
        self.assertEqual((s["kind"], s["running"], s["code"]), ("scan", False, 0))
        self.assertEqual(s["run"], self.last_run().name)
        self.assertTrue(any("duplicate" in line for line in s["lines"]), s["lines"])
        self.assertFalse(any(line.startswith(_jobs.Progress.LINE_MARK) for line in s["lines"]))
        self.assertEqual(self.spawned, [["dupes", "--min-size", "1", "--", self.root]])
        self.page(f"/job/{j['id']}")
        self.page(f"/runs/{s['run']}")
        self.page("/job/999", 404)
        j = self.api("/api/jobs", {"argv": ["empty", self.root]})
        self.assertEqual(self.wait(j["id"]).code, 0)
        self.assertEqual(self.api(f"/api/jobs/{j['id']}")["kind"], "empty")
        self.assertEqual(self.last_run().command, "empty")

    def test_choices_are_saved_with_the_run(self):
        name = self.scan()
        run = self.app.run(name)
        backup = self.ref(name, "Backup")
        photos = self.ref(name, "Photos")
        inner = self.ref(name, "Backup/Photos")
        # suggested: Backup (only a copy of Photos) goes, its insides with it
        self.assertEqual([self.state(name, r) for r in (backup, inner, photos)],
                         ["go", "with", "kept"])
        goes = backup
        before = self.api(f"/api/results/{name}")
        self.assertEqual(before["tab"], "families")
        self.assertIn(f'data-ref="{goes}"', before["html"])
        self.assertEqual(before["cli"], f"clean remove {name} -n")
        out = self.api(f"/api/selection/{name}", {"op": "set", "ref": goes, "value": "keep",
                                                   "q": {"tab": "families"}})
        self.assertEqual(out["footer"]["chosen"], 1)
        self.assertLess(out["footer"]["items"], before["footer"]["items"])
        self.assertEqual(self.state(name, goes), "keep")
        self.assertEqual(self.state(name, photos), "kept")
        with open(run.file(_choices.NAME)) as f:
            self.assertEqual(json.load(f)["choices"], {goes: "keep"})
        # a new dashboard (or `clean remove`) sees the same
        app2 = server.App(self.data_dir)
        saved, _ = app2.results(app2.run(name)).saved()
        self.assertEqual(saved.choices, {goes: "keep"})
        r = self.run_clean("remove", name, "-n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"using the choices made in the dashboard", r.stdout)
        self.assertNotIn(os.path.join(self.root, "Backup").encode(), r.stdout)
        self.assertIn(os.path.join(self.root, "Loose").encode(), r.stdout)
        # the dashboard's own dialog goes without the note (and its --no-choices)
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "trash"})
        self.assertNotIn("choices made in the dashboard", c["html"])
        self.assertIn("Loose", c["html"])
        self.api(f"/api/selection/{name}", {"op": "unset", "ref": goes})
        self.assertFalse(run.has(_choices.NAME))         # back to the suggestions
        self.api(f"/api/selection/{name}", {"op": "rules",
                                             "values": {"keep_in": [self.root + "/Backup"]}})
        with open(run.file(_choices.NAME)) as f:
            self.assertEqual(json.load(f)["rules"]["keep_in"], [self.root + "/Backup"])
        self.assertEqual(self.state(name, backup), "kept")
        out = self.api(f"/api/selection/{name}", {"op": "reset"})
        self.assertEqual(out["message"], "Back to the suggestions.")
        self.assertFalse(run.has(_choices.NAME))
        for body in ({"op": "set", "ref": "d999999", "value": "keep"},
                     {"op": "set", "ref": "x1", "value": "keep"},
                     {"op": "set", "ref": None, "value": "keep"},
                     {"op": "set", "ref": goes, "value": "maybe"},
                     {"op": "frobnicate"}, {"op": "rules", "values": {"nope": 1}},
                     {"op": "rules", "values": {"keep_in": ["/etc"]}},
                     {"op": "rules", "values": {"keep_in": "/etc"}},
                     {"op": "rules", "values": {}}, {"op": "invert", "card": "F999"},
                     {"op": "keep_all"}, {"op": "set", "ref": goes, "value": "keep", "q": []}):
            with self.subTest(body=body):
                self.refused(f"/api/selection/{name}", body, 400)
        self.assertFalse(run.has(_choices.NAME))
        self.refused("/api/selection/2020-01-01_000000-dupes", {"op": "reset"}, 404)

    def test_names_are_text_everywhere(self):
        name = self.scan()
        pages = [self.page(f"/runs/{name}"), self.page("/history"), self.page("/"),
                 self.api(f"/api/results/{name}?tab=covered")["html"],
                 self.api(f"/api/results/{name}?tab=families")["html"],
                 self.api(f"/api/results/{name}?tab=groups&folded=1")["html"]]
        self.assertTrue(any("&lt;img src=x onerror=alert(1)&gt;" in p for p in pages[3:]))
        for text in pages:
            self.assertNotIn("<img", text)
            for tag, attrs in Tags(text).tags:
                self.assertNotEqual(tag, "img")
                self.assertFalse(any(k.startswith("on") for k in attrs), (tag, attrs))
        self.assertNotIn("<img", self.page("/scan?path=" + quote(f"{self.root}/{NASTY}")))

    def test_text_keeps_its_lines(self):
        """Lists and summaries show one line per line: a name's own line
        break stays \\n inside its line."""
        self.api("/api/settings", {"key": "scan.exclude", "op": "set",
                                   "value": ["*.tmp", "a\nb", "<x>"]})
        areas = re.findall(r'<textarea data-input="([^"]+)"[^>]*>(.*?)</textarea>',
                           self.page("/settings"), re.S)
        areas = dict(areas)
        self.assertEqual(areas["junk.names"].split("\n"), list(_settings.JUNK_NAMES))
        self.assertEqual(areas["scan.exclude"], "*.tmp\na\\nb\n&lt;x&gt;")
        self.sample()
        r = self.run_clean("empty", "--exclude", "*.bak", "--exclude", "*.old", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        name = self.last_run().name
        text = self.page(f"/runs/{name}")
        summary = re.search(r'<pre class="out">(.*?)</pre>', text, re.S).group(1)
        self.assertNotIn("\\n", summary)
        self.assertGreater(summary.count("\n"), 3)
        scan = self.page(f"/scan?kind=empty&from={name}")
        self.assertRegex(scan, r'data-flag="--exclude"[^>]*>\*\.bak\n\*\.old</textarea>')
        # an example of several lines shows as lines, not as "\n"
        self.assertIn('placeholder="IMG_*&#10;*.mp4"', self.page("/scan"))

    def test_settings(self):
        out = self.api("/api/settings", {"key": "dupes.min_size", "op": "set", "value": "2K"})
        self.assertEqual(out, {"key": "dupes.min_size", "value": "2.0 KB (2048)",
                               "source": "settings file",
                               "cli": "clean config set dupes.min_size 2048"})
        with open(self.settings_file) as f:
            self.assertEqual(json.load(f), {"dupes.min_size": 2048})
        out = self.api("/api/settings", {"key": "scan.exclude", "op": "set",
                                         "value": ["*.tmp", " ", " node_modules "]})
        self.assertEqual(out["cli"], "clean config set scan.exclude '*.tmp' node_modules")
        out = self.api("/api/settings", {"key": "ui.open_browser", "op": "set", "value": False})
        self.assertEqual(out["value"], "false")
        self.api("/api/settings", {"key": "ui.theme", "op": "set", "value": "dark"})
        self.assertIn('data-theme="dark"', self.page("/settings"))
        with open(self.settings_file) as f:
            self.assertEqual(json.load(f), {"dupes.min_size": 2048,
                                            "scan.exclude": ["*.tmp", "node_modules"],
                                            "ui.open_browser": False, "ui.theme": "dark"})
        out = self.api("/api/settings", {"key": "dupes.min_size", "op": "reset"})
        self.assertEqual((out["source"], out["cli"]), ("default",
                                                       "clean config reset dupes.min_size"))
        for body in ({"key": "dupes.min_size", "op": "set", "value": "lots"},
                     {"key": "ui.theme", "op": "set", "value": "pink"},
                     {"key": "ui.port", "op": "set", "value": "70000"},
                     {"key": "scan.exclude", "op": "set", "value": "*.tmp"},
                     {"key": "ui.theme", "op": "set", "value": None},
                     {"key": "ui.theme", "op": "set", "value": ["dark"]},
                     {"key": "nope", "op": "set", "value": "1"}, {"key": None, "op": "reset"},
                     {"key": "ui.theme", "op": "drop"},
                     {"key": "paths.data", "op": "set", "value": "/tmp/x"},
                     {"key": "paths.data", "op": "reset"}):
            with self.subTest(body=body):
                self.refused("/api/settings", body, 400)
        with open(self.settings_file) as f:
            self.assertEqual(json.load(f), {"scan.exclude": ["*.tmp", "node_modules"],
                                            "ui.open_browser": False, "ui.theme": "dark"})
        os.environ["CLEAN_UI_THEME"] = "light"
        try:
            out = self.api("/api/settings", {"key": "ui.theme", "op": "set", "value": "auto"})
            self.assertIn("CLEAN_UI_THEME is set in the environment", out["message"])
            self.assertIn('data-theme="light"', self.page("/settings"))
        finally:
            del os.environ["CLEAN_UI_THEME"]
        # `clean config set` in a terminal counts at once
        self.assertEqual(self.run_clean("config", "set", "ui.theme", "light").returncode, 0)
        self.assertIn('data-theme="light"', self.page("/"))

    def test_ignore_this_folder(self):
        self.sample()
        self.write("odd [1]*?/c.txt", blob("c", 3500))
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        name = self.last_run().name
        ref = self.ref(name, "odd [1]*?")
        out = self.api("/api/ignore", {"run": name, "ref": ref})
        self.assertIn("is skipped by every scan from now on", out["message"])
        self.assertEqual(self.state(name, ref), "keep")
        pattern = self.root + "/odd [[]1][*][?]"
        with open(self.settings_file) as f:
            self.assertEqual(json.load(f), {"scan.exclude": [pattern]})
        self.api("/api/ignore", {"run": name, "ref": ref})          # once only
        with open(self.settings_file) as f:
            self.assertEqual(json.load(f), {"scan.exclude": [pattern]})
        # it works: the next scan doesn't see that folder (and only that one)
        self.write("odd [1]xy/c.txt", blob("c", 3500))
        self.assertEqual(self.dupes(self.root).returncode, 0)
        groups = self.report_groups()
        self.assertFalse(any(p.startswith("odd [1]*?") for g in groups for p in g))
        self.assertTrue(any(p.startswith("odd [1]xy") for g in groups for p in g))
        self.refused("/api/ignore", {"run": name, "ref": self.ref(name, "Other/c.txt", "f")},
                     400)
        self.refused("/api/ignore", {"run": name, "ref": "d99999"}, 400)
        os.environ["CLEAN_SCAN_EXCLUDE"] = "x"
        try:
            self.assertIn("CLEAN_SCAN_EXCLUDE",
                          self.refused("/api/ignore", {"run": name, "ref": ref}, 409))
        finally:
            del os.environ["CLEAN_SCAN_EXCLUDE"]

    def test_open_shows_folders_only(self):
        name = self.scan()
        photos = os.path.join(self.root, "Photos")
        self.assertEqual(self.api("/api/open", {"run": name, "ref": self.ref(name, "Photos")}),
                         {"opened": photos})
        self.api("/api/open", {"run": name, "ref": self.ref(name, "Photos/page.html", "f")})
        self.api("/api/open", {"place": "data"})
        self.assertEqual(self.opened, [photos, photos, self.data_dir])
        for body, status in (({"place": "home"}, 400), ({"place": "/etc"}, 400),
                             ({"run": name, "ref": "d99999"}, 400),
                             ({"run": name, "ref": "../x"}, 400), ({"run": name}, 400),
                             ({"run": "x", "ref": "d0"}, 404)):
            self.refused("/api/open", body, status)
        # a folder made into a link, or gone, is not opened
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.rename(photos, elsewhere)
        self.refused("/api/open", {"run": name, "ref": self.ref(name, "Photos")}, 404)
        os.symlink(elsewhere, photos)
        self.refused("/api/open", {"run": name, "ref": self.ref(name, "Photos")}, 404)
        self.refused("/api/open", {"run": name, "ref": self.ref(name, "Photos/a.jpg", "f")},
                     404)
        self.assertEqual(len(self.opened), 3)

    def test_every_page(self):
        name = self.scan()
        self.assertEqual(self.empty(self.root).returncode, 0)
        empty = self.last_run().name
        pages = {"/": "Home", "/scan": "New scan", "/scan?kind=empty": "New scan",
                 f"/scan?from={name}": "New scan", f"/scan?path={self.root}": "New scan",
                 "/history": "History", "/settings": "Settings", "/trash": "Trash",
                 f"/runs/{name}": "Duplicates", f"/runs/{empty}": "Empty folders",
                 f"/runs/{name}?tab=groups&page=99&sort=path&q=a": "Duplicates"}
        for path, title in pages.items():
            with self.subTest(path=path):
                text = self.page(path)
                self.assertIn("<title>", text)
                self.assertIn('<script src="/static/app.js" defer></script>', text)
                self.assertIn('class="cli"', text)              # the same in a terminal
        self.assertEqual(self.c.get("/index.html")[1]["location"], "/history")
        st, h, body = self.c.get(f"/runs/{name}/report.html")
        self.assertEqual((st, h["content-type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"clean ui", body)
        st, h, _ = self.c.get(f"/runs/{name}/summary.txt")
        self.assertEqual((st, h["content-type"]), (200, "text/plain; charset=utf-8"))
        st, h, _ = self.c.get(f"/runs/{name}/data.json.gz")
        self.assertIn("attachment", h["content-disposition"])
        self.assertIn("there is no such page", self.page("/nope", 404))
        self.assertIn("no such run", self.page("/runs/nope", 404))
        st, _, body = self.c.get("/api/nope", accept="application/json")
        self.assertEqual((st, json.loads(body)), (404, {"error": "no such API"}))
        self.assertEqual(self.c.get("/favicon.ico", cookie=False)[0], 204)
        st, h, body = self.c.get("/static/app.css", cookie=False)
        self.assertEqual((st, h["content-type"]), (200, "text/css; charset=utf-8"))

    def test_trash_page_and_summary(self):
        trash = _trash.home_trash_path()
        os.makedirs(os.path.join(trash, "files", "old folder"))
        os.makedirs(os.path.join(trash, "info"))
        with open(os.path.join(trash, "files", "old folder", "x.bin"), "wb") as f:
            f.write(b"x" * 5000)
        original = os.path.join(self.root, NASTY)
        with open(os.path.join(trash, "info", "old folder.trashinfo"), "w") as f:
            f.write("[Trash Info]\nPath=" + original.replace(" ", "%20")
                    + "\nDeletionDate=2020-01-02T03:04:05\n")
        out = self.api("/api/trash?fresh=1")["trashes"]
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["items"], out[0]["files"], out[0]["home"]), (1, 1, True))
        self.assertGreaterEqual(out[0]["bytes"], 5000)
        text = self.page("/trash")
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", text)
        self.assertIn(f'data-token="{api.path_token(original)}"', text)
        self.assertNotIn("<img", text)


class TestSpacePages(WebBase):
    """The Space page, a space run's page, and the jobs they start. The look
    itself runs sealed (clean space lists every drive's Trash); the pages
    list only the test's own Trash."""

    @staticmethod
    def init(text):
        m = re.search(r'<script type="application/json" id="init">(.*?)</script>', text, re.S)
        return json.loads(m.group(1))

    def look(self):
        """The sample plus clutter, a duplicates scan of it, then `clean
        space` of it; the space run."""
        self.sample()
        self.write("Docs/zero.txt", b"")
        self.write("Docs/x.tmp", "temp")
        self.write(f"Docs/{NASTY}.tmp", "temp")
        os.symlink("nowhere", os.path.join(self.root, "Docs", "broken"))
        self.write(f"Big/{NASTY}/inner/big.bin", blob("big", 2_500_000))
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.dupes_run = self.last_run().name
        self.sealed("export CLEAN_SPACE_SYSTEM_CACHES=0\nstep look space "
                    + shlex.quote(self.root))
        rc, out, err = self.step("look")
        self.assertEqual(rc, 0, err)
        run = self.last_run()
        self.assertEqual((run.command, run.status), ("space", "done"))
        return run

    def test_before_any_look(self):
        self.assertIn('<a class="button" href="/space"', self.page("/"))
        text = self.page("/space")
        self.assertIn("<h1>Space</h1>", text)
        self.assertIn('class="cli"', text)
        self.assertIn("<h2>Drives</h2>", text)
        self.assertIn("Nothing looked at yet", text)
        self.assertIn('href="/scan?kind=space"', text)
        for act in ("cache_prune", "cache_clear", "history_prune"):
            self.assertNotIn(f'data-action="{act}"', text)
        self.assertIn('data-place="data"', text)
        for action, why in (("cache_prune", "There is no hash cache yet: nothing to prune."),
                            ("cache_clear", "There is no hash cache: nothing to delete.")):
            with self.subTest(action=action):
                self.assertEqual(self.api("/api/confirm", {"action": action})["refused"], why)
        # A look starts at the home folder unless told otherwise.
        init = self.init(self.page("/scan?kind=space"))
        self.assertEqual((init["kind"], init["roots"]), ("space", [os.path.expanduser("~")]))
        init = self.init(self.page(f"/scan?kind=space&path={quote(self.root)}"))
        self.assertEqual(init["roots"], [self.root])
        self.assertEqual(self.spawned, [])

    @needs_unshare
    def test_a_look_and_its_pages(self):
        name = self.look().name
        url = f"/runs/{name}"
        text = self.page("/space")
        self.assertNotIn("Nothing looked at yet", text)
        self.assertIn(f'<a class="small-link" href="{url}">The whole look</a>', text)
        self.assertIn('<details class="node"', text)
        self.assertIn('<span class="nm d1">Big/</span>', text)
        self.assertIn("<p>1 zero-byte file, 1 broken symlink, 2 temp leftovers (8.0 KB), "
                      "2 empty folders <a", text)
        self.assertIn(f'href="{url}#clutter"', text)
        self.assertIn(f'href="/runs/{self.dupes_run}"', text)       # its newest dupes scan
        self.assertIn(f'href="/scan?kind=space&amp;from={name}"', text)   # look again
        for act in ("cache_prune", "cache_clear"):                  # the dupes scan made one
            self.assertIn(f'data-action="{act}"', text)
        self.assertNotIn('data-action="history_prune"', text)
        self.assertNotIn("<img", text)
        page = self.page(url)
        for part in ('id="folders"', 'id="clutter"', "Read-only: nothing was deleted",
                     "&lt;img src=x onerror=alert(1)&gt;.tmp",
                     f'href="/scan?kind=space&amp;from={name}"', f'href="/runs/{self.dupes_run}"',
                     f'href="{url}/report.html"', 'data-action="history_delete"'):
            self.assertIn(part, page)
        self.assertNotIn("<img", page)
        self.assertNotIn('<p class="crumbs">', page)
        d = self.app.misc["space"][2]
        n = next(i for i in range(len(d.folders)) if d.path(i).endswith("/inner"))
        deeper = self.page(f"{url}?dir={n}")
        self.assertIn(f'<p class="crumbs"><a href="{url}#folders">All</a>', deeper)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;</a>", deeper)
        self.assertNotIn("<img", deeper)
        self.assertIn("dir must be a folder&#x27;s number", self.page(f"{url}?dir=x", 400))
        for bad in ("999", "-1"):
            self.assertIn("this run has no such folder", self.page(f"{url}?dir={bad}", 404))
        st, h, body = self.c.get(f"{url}/report.html")
        self.assertEqual((st, h["content-type"]), (200, "text/html; charset=utf-8"))
        self.assertNotIn(b"<img", body)
        # Looking again, or scanning the same folders for duplicates
        for kind, want in (("space", "space"), ("dupes", "dupes"), (None, "dupes")):
            with self.subTest(kind=kind):
                q = f"kind={kind}&from={name}" if kind else f"from={name}"
                init = self.init(self.page(f"/scan?{q}"))
                self.assertEqual((init["kind"], init["roots"]), (want, [self.root]))
        # Without a duplicates scan of these folders, the page offers one.
        shutil.rmtree(self.app.run(self.dupes_run).folder)
        text = self.page("/space")
        self.assertIn(f'href="/scan?kind=dupes&amp;from={name}"', text)
        self.assertNotIn(f'href="/runs/{self.dupes_run}"', text)
        self.assertEqual(self.spawned, [])

    def test_a_look_from_the_dashboard(self):
        self.sample()
        j = self.api("/api/jobs", {"argv": ["space", "--top", "5", "--", self.root]})
        self.assertEqual(j["cli"], f"clean space --top 5 -- {self.root}")
        job = self.wait(j["id"])
        self.assertEqual(job.code, 0, job.lines_since(0))
        self.assertEqual(self.api(f"/api/jobs/{j['id']}")["kind"], "space")
        # A look is started for real only by `clean ui`; here a stand-in says what it got.
        self.assertEqual(self.spawned, [["space", "--top", "5", "--", self.root]])
        root = self.root
        for argv in (["space", "relative/path"], ["space", os.path.join(root, "nope")],
                     ["space", os.path.join(root, "Other/c.txt")], ["space", "--bogus", root],
                     ["space", "--help"], ["space", "--top", "0", root],
                     ["cache", "prune"], ["cache", "clear", "-y"], ["cache", "info"]):
            with self.subTest(argv=argv):
                self.refused("/api/jobs", {"argv": argv}, 400)
        self.assertEqual(len(self.spawned), 1)

    def test_the_hash_cache_and_old_runs(self):
        self.sample()
        self.assertEqual(self.dupes(self.root).returncode, 0)
        cache = os.path.join(self.tmp, "xdg", "cache", "clean", "hashes.sqlite")
        self.assertTrue(os.path.isfile(cache))
        old = self.fake_run("2020-01-01_000000-dupes")
        self.assertEqual(self.run_clean("config", "set", "history.keep_runs", "1").returncode, 0)
        text = self.page("/space")
        for act in ("cache_prune", "cache_clear", "history_prune"):
            self.assertIn(f'data-action="{act}"', text)
        self.assertIn("1 would go now: clean history --prune", text)
        for action, argv, danger in (("cache_prune", ["cache", "prune"], False),
                                     ("cache_clear", ["cache", "clear", "-y"], True)):
            with self.subTest(action=action):
                c = self.api("/api/confirm", {"action": action})
                self.assertNotIn("refused", c, c)
                self.assertEqual((c["danger"], c["typed"]), (danger, None))
                self.assertEqual(c["cli"], command_line([a for a in argv if a != "-y"]))
                self.assertIn("no file it names is touched", c["html"])
                d = self.api("/api/do", {"nonce": c["nonce"], "typed": None})
                job = self.wait(d["id"])
                self.assertEqual((self.spawned[-1], job.after), (argv, "/space"))
        # Back to the page it came from, if that is one on the list
        for back, after in (("/space", "/space"), (None, "/history"),
                            ("https://example.com/", "/history"), ("//example.com", "/history"),
                            ("/space/../runs", "/history"), (["/space"], "/history")):
            with self.subTest(back=back):
                c = self.api("/api/confirm", {"action": "history_prune", "back": back})
                d = self.api("/api/do", {"nonce": c["nonce"], "typed": None})
                self.assertEqual(self.wait(d["id"]).after, after)
                self.assertEqual(self.spawned[-1], ["history", "--prune"])
        self.assertTrue(os.path.isfile(cache))      # stand-ins only: nothing was done
        self.assertTrue(os.path.isdir(old))
        # A cache that can't be read can only be deleted.
        with open(cache, "wb") as f:
            f.write(b"not a database" * 100)
        self.assertIn("can't be read", self.api("/api/confirm", {"action": "cache_prune"})
                      ["refused"])
        self.assertNotIn("refused", self.api("/api/confirm", {"action": "cache_clear"}))
        self.assertIn("can&#x27;t be read", self.page("/space"))


class TestOpenOnAScan(WebBase):
    """A launch link that opens on a page: `clean ui FOLDER...` and the Files
    menu entries open a new scan with the folders filled in."""

    init = staticmethod(TestSpacePages.init)

    def test_only_this_dashboards_pages(self):
        good = ("/", "/scan?kind=dupes&path=%2Fa%20b&path=%2Fc", "/history", "/runs/x?card=L1")
        for where in good:
            self.assertEqual(server.local_page(where), where)
        bad = (None, "", "scan", "//evil.example", "///evil.example", "/\\evil.example",
               "/a\\b", "http://evil.example/", "javascript:alert(1)", "/\t/evil.example",
               "/a\nLocation: x", "/a\rb", "/a\x7fb", "/" + "a" * 20000, 7)
        for where in bad:
            self.assertEqual(server.local_page(where), "/", where)

    def test_launch_link_with_a_page(self):
        a, b = os.path.join(self.tmp, "one place"), os.path.join(self.tmp, "two")
        os.mkdir(a)
        os.mkdir(b)
        then = f"/scan?kind=empty&path={quote(a)}&path={quote(b)}"
        path = self.app.launch_url().split(self.c.host, 1)[1] + "&next=" + quote(then, safe="")
        st, h, body = self.c.get(path, cookie=False)
        self.assertEqual(st, 200)
        self.assertIn("set-cookie", h)
        self.assertIn(f'http-equiv="refresh" content="0;url={then.replace("&", "&amp;")}"'
                      .encode(), body)
        st, h, _ = self.c.get(path)                 # used up, but this browser is signed in
        self.assertEqual((st, h.get("location")), (303, then))
        init = self.init(self.page(then))
        self.assertEqual((init["kind"], init["roots"]), ("empty", [a, b]))
        for bad in ("//evil.example/", "http://evil.example/", "/\\evil.example"):
            with self.subTest(bad=bad):
                path = self.app.launch_url().split(self.c.host, 1)[1] + "&next=" + quote(bad)
                st, h, body = self.c.get(path, cookie=False)
                self.assertEqual(st, 200)
                self.assertIn(b'content="0;url=/"', body)
                st, h, _ = self.c.get(path)
                self.assertEqual((st, h.get("location")), (303, "/"))

    def test_several_folders(self):
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        os.mkdir(a)
        os.mkdir(b)
        q = "&".join(f"path={quote(p)}" for p in (a, b, a + "/", "relative", a + "/nope", ""))
        for kind in ("dupes", "empty", "space"):
            with self.subTest(kind=kind):
                init = self.init(self.page(f"/scan?kind={kind}&{q}"))
                self.assertEqual((init["kind"], init["roots"]), (kind, [a, b]))
        self.assertEqual(self.spawned, [])


class TestServeCommand(WebBase):
    """`clean ui` itself."""

    def start_ui(self, *args):
        env = dict(self.env, T=self.tmp, PY=sys.executable, CLEAN=CLEAN)
        quoted = " ".join(f"'{a}'" for a in args)
        p = subprocess.Popen(["unshare", "-rm", "sh", "-c",
                              SEAL + f'exec "$PY" "$CLEAN" ui --no-browser {quoted}'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        fd, text = p.stdout.fileno(), b""
        while text.count(b"\n") < 4:
            ready, _, _ = select.select([fd], [], [], 30)
            self.assertTrue(ready, f"no link printed: {text}")
            chunk = os.read(fd, 4096)
            if not chunk:
                self.fail(f"clean ui ended: {text} {p.communicate(timeout=30)[1]}")
            text += chunk
        return p, text.decode().splitlines()

    @needs_unshare
    def test_link_reuse_and_stop(self):
        p, lines = self.start_ui()
        m = re.match(r"Dashboard at http://127\.0\.0\.1:(\d+)/ \(only this computer can reach "
                     r"it\)$", lines[0])
        self.assertTrue(m, lines)
        port = int(m.group(1))
        self.assertEqual(lines[1], "Open the dashboard with this link (it works once, within "
                                   "10 minutes):")
        self.assertTrue(lines[2].startswith(f"  http://127.0.0.1:{port}/launch?t="))
        self.assertEqual(lines[3], "Stop it with Ctrl-C.")
        info = os.path.join(self.data_dir, "ui.json")
        self.assertEqual(stat.S_IMODE(os.stat(info).st_mode), 0o600)
        c = Client(port)
        st, h, _ = c.get(lines[2].strip().split(c.host, 1)[1], cookie=False)
        self.assertEqual(st, 200)
        c.cookie = h["set-cookie"].split(";")[0]
        self.assertEqual(c.get("/")[0], 200)
        # a second `clean ui` gives a new link to this one
        r = self.run_sealed('"$PY" "$CLEAN" ui --no-browser')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode().splitlines()
        self.assertEqual(out[0], "Open the dashboard that is already running with this link "
                                 "(it works once, within 10 minutes):")
        st, h, _ = c.get(out[1].strip().split(c.host, 1)[1], cookie=False)
        self.assertEqual((st, h["set-cookie"].split(";")[0]), (200, c.cookie))
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(out.decode().strip(), "Dashboard stopped.")
        self.assertFalse(os.path.exists(info))
        with self.assertRaises(OSError):
            c.get("/")

    @needs_unshare
    def test_open_on_a_new_scan(self):
        a, b = os.path.join(self.tmp, "one place"), os.path.join(self.tmp, "two")
        os.mkdir(a)
        os.mkdir(b)
        # Nothing running: --if-running only says so.
        r = self.run_clean("ui", "--if-running", "--no-browser", "--scan", "space", a)
        self.assertEqual((r.returncode, r.stdout), (1, b""))
        self.assertEqual(r.stderr, b"No dashboard is running: start one with `clean ui`.\n")
        p, lines = self.start_ui("--scan", "space", a, b)
        port = int(re.search(r":(\d+)/", lines[0]).group(1))
        then = "/scan?" + "&".join(["kind=space", "path=" + quote(a, safe=""),
                                     "path=" + quote(b, safe="")]).replace("%20", "+")
        link = lines[2].strip()
        self.assertTrue(link.endswith("&next=" + quote(then, safe="")), link)
        c = Client(port)
        st, h, body = c.get(link.split(c.host, 1)[1], cookie=False)
        self.assertEqual(st, 200)
        self.assertIn(f'content="0;url={then.replace("&", "&amp;")}"'.encode(), body)
        c.cookie = h["set-cookie"].split(";")[0]
        st, _, body = c.get(then)
        self.assertEqual(st, 200)
        init = json.loads(re.search(rb'<script type="application/json" id="init">(.*?)</script>',
                                    body, re.S).group(1))
        self.assertEqual((init["kind"], init["roots"]), ("space", [a, b]))
        # The one running is used: no second dashboard. Folders default to dupes.
        r = self.run_sealed('cd "$T" && "$PY" "$CLEAN" ui --if-running --no-browser two')
        self.assertEqual(r.returncode, 0, r.stderr)
        link = r.stdout.decode().splitlines()[1].strip()
        self.assertTrue(link.endswith("&next=" + quote(f"/scan?kind=dupes&path={quote(b, safe='')}",
                                                       safe="")), link)
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        for args, why in ((["nope"], "not a folder: nope"),
                          (["--scan", "bogus"], "invalid choice: 'bogus'")):
            r = self.run_clean("ui", "--if-running", *args)
            self.assertEqual(r.returncode, 2)
            self.assertIn(why.encode(), r.stderr)
        odd = os.path.join(os.fsencode(self.tmp), b"odd \xff")
        os.mkdir(odd)
        r = self.run_clean("ui", "--if-running", odd)
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"can't take a folder whose name isn't valid text", r.stderr)

    @needs_unshare
    def test_port_in_use_and_bad_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            r = self.run_sealed(f'"$PY" "$CLEAN" ui --no-browser --port {port}', timeout=30)
        finally:
            s.close()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(f"port {port} is in use".encode(), r.stderr)
        for bad in ("x", "-1", "65536"):
            r = self.run_clean("ui", "--port", bad)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b"not a port number", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "ui.json")))

    def serve_with_job(self, interrupts, hard=()):
        """_serve with a job running and Ctrl-C pressed `interrupts` times
        (the first once the job runs, the second once it has warned)."""
        self.stand_in = WAITS
        self.real_scans = False
        self.sample()
        j = self.api("/api/jobs", {"argv": ["dupes", self.root]})
        job = self.app.jobs.get(j["id"])
        out = self.serve(job, interrupts, hard)
        return job, out

    def serve(self, job, interrupts, hard=()):
        """Run _serve (on a second server) and press Ctrl-C `interrupts`
        times: the first once the job runs, the next once it has warned."""
        port = self.app.port
        srv = server.Server(self.app, 0)
        self.app.port = port            # (the test's own server keeps answering)
        out = io.StringIO()

        def press():
            for i in range(interrupts):
                for _ in range(400):
                    if i == 0 and job.lines_since(0) or i > 0 and "again" in out.getvalue():
                        break
                    time.sleep(0.025)
                time.sleep(0.2)         # (its print is over: the Ctrl-C lands in a wait)
                _thread.interrupt_main()

        t = threading.Thread(target=press, daemon=True)
        t.start()
        with contextlib.redirect_stdout(out):
            _ui._serve(srv, self.app, list(hard))
        t.join()
        srv.server_close()
        return out.getvalue()

    def test_ctrl_c_twice_cancels_the_job(self):
        job, out = self.serve_with_job(2)
        self.assertIn("A job is running: clean dupes", out)
        self.assertIn("Press Ctrl-C again to cancel it now", out)
        self.assertIn("Cancelling the job...", out)
        self.assertIn("The job stopped (exit code 130).", out)
        self.assertEqual((job.code, job.cancelled), (130, True))

    def test_a_kill_cancels_the_job_at_once(self):
        job, out = self.serve_with_job(1, hard=[signal.SIGTERM])
        self.assertNotIn("Press Ctrl-C again", out)
        self.assertIn("The job stopped (exit code 130).", out)
        self.assertEqual(job.code, 130)

    def test_ctrl_c_waits_for_the_job(self):
        self.stand_in = "import time; print('working', flush=True); time.sleep(1.5)"
        self.real_scans = False
        self.sample()
        j = self.api("/api/jobs", {"argv": ["dupes", self.root]})
        job = self.app.jobs.get(j["id"])
        out = self.serve(job, 1)
        self.assertIn("The dashboard stops when it ends", out)
        self.assertNotIn("Cancelling", out)
        self.assertFalse(job.running)
        self.assertEqual((job.code, job.cancelled), (0, False))

    def test_ctrl_c_without_a_job_stops_at_once(self):
        t0 = time.monotonic()
        port = self.app.port
        srv = server.Server(self.app, 0)
        self.app.port = port
        threading.Timer(0.3, _thread.interrupt_main).start()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _ui._serve(srv, self.app, [])
        srv.server_close()
        self.assertEqual(buf.getvalue(), "")
        self.assertLess(time.monotonic() - t0, 5)

    def test_cancel_stops_a_real_scan(self):
        """Cancel is Ctrl-C for the real `clean`: it stops and saves the run
        as interrupted."""
        for i in range(300):
            self.mk(f"d{i}")
        self.real_scans = False
        self.stand_in = (
            "import os, runpy, sys, time\n"
            "real = os.scandir\n"
            "def slowed(*a, **k):\n"
            "    time.sleep(0.01)\n"
            "    return real(*a, **k)\n"
            "os.scandir = slowed\n"
            f"sys.argv = [{CLEAN!r}] + sys.argv[1:]\n"
            f"runpy.run_path({CLEAN!r}, run_name='__main__')\n")
        j = self.api("/api/jobs", {"argv": ["empty", self.root]})
        job = self.app.jobs.get(j["id"])
        for _ in range(200):
            if job.find_run():
                break
            time.sleep(0.05)
        self.assertTrue(self.api(f"/api/jobs/{j['id']}/cancel", {})["cancelled"])
        self.assertTrue(job.wait(30))
        self.assertEqual(job.code, 130)
        self.assertEqual(self.run_meta()["status"], "interrupted")
        self.assertEqual(self.api(f"/api/jobs/{j['id']}")["run"], self.last_run().name)
        self.assertEqual(len(os.listdir(self.root)), 300)


# ---------------------------------------------------------------- for real, sealed

class SealedServer:
    """tests/ui/serve.py in the sealed namespace: a dashboard whose jobs
    really remove and restore, on the test's folders only."""

    def __init__(self, test):
        self.opened = os.path.join(test.tmp, "opened.txt")
        self.log = open(os.path.join(test.tmp, "serve.err"), "wb")
        env = dict(test.env, T=test.tmp, PY=sys.executable, SERVE=SERVE, DATA=test.data_dir,
                   OPENED=self.opened)
        self.p = subprocess.Popen(
            ["unshare", "-rm", "sh", "-c", SEAL + 'exec "$PY" "$SERVE" "$DATA" "$OPENED"'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, env=env)
        ready, _, _ = select.select([self.p.stdout], [], [], 30)
        line = self.p.stdout.readline() if ready else b""
        if not line:
            self.stop()
            raise AssertionError("serve.py didn't start: " + self.errors())
        info = json.loads(line)
        self.url = info["url"]
        self.c = Client(info["port"], info["cookie"])

    def errors(self):
        with open(self.log.name, errors="replace") as f:
            return f.read()

    def stop(self):
        if self.p.poll() is None:
            self.p.stdin.close()
            try:
                self.p.wait(60)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        for f in (self.p.stdin, self.p.stdout, self.log):
            f.close()


@needs_unshare
class TestSealed(Base):
    """Remove, undo, delete, restore and empty through the dashboard, for
    real, in the sealed namespace."""

    def setUp(self):
        super().setUp()
        self.s = SealedServer(self)
        self.addCleanup(self.s.stop)
        self.c = self.s.c
        self.trash = os.path.join(self.tmp, "xdg", "data", "Trash")

    api = WebBase.api
    refused = WebBase.refused
    page = WebBase.page
    sample = WebBase.sample

    def job(self, j, code=0):
        for _ in range(1200):
            s = self.api(f"/api/jobs/{j['id']}")
            if not s["running"]:
                self.assertEqual(s["code"], code, "\n".join(s["lines"]) + self.s.errors())
                return s
            time.sleep(0.1)
        self.fail("the job didn't end")

    def do(self, action, typed=None, **body):
        c = self.api("/api/confirm", dict(body, action=action))
        self.assertNotIn("refused", c, c)
        return self.job(self.api("/api/do", {"nonce": c["nonce"], "typed": typed}))

    def scan(self, kind="dupes"):
        return self.job(self.api("/api/jobs", {"argv": [kind, "--", self.root]}))["run"]

    def trashed(self):
        files = os.path.join(self.trash, "files")
        return sorted(os.listdir(files)) if os.path.isdir(files) else []

    def test_remove_to_trash_then_undo(self):
        self.sample()
        before = self.snapshot()
        name = self.scan()
        r = self.do("remove", run=name, method="trash")
        rm = r["run"]
        self.assertEqual(r["kind"], "remove")
        gone = set(before) - set(self.snapshot())
        self.assertTrue(gone)
        self.assertTrue(self.trashed())
        for path in gone:          # never the last copy
            self.assertIn(before[path], self.snapshot().values(), path)
        text = self.page(f"/runs/{rm}")
        self.assertIn('data-action="undo"', text)
        self.assertIn('data-action="trash_empty_run"', text)
        self.assertIn(f"/runs/{name}", text)          # Select more
        trash = self.page("/trash")
        self.assertIn(f"/runs/{rm}", trash)
        # the choices page knows those went
        out = self.api(f"/api/results/{name}")
        self.assertEqual(out["footer"]["items"], 0)
        self.assertGreater(out["footer"]["done"], 0)
        c = self.api("/api/confirm", {"action": "undo", "run": rm})
        self.assertNotIn("\\n", c["html"])          # its list, one line each
        self.assertIn("\n  folder  ", c["html"])
        u = self.do("undo", run=rm)
        self.assertEqual(self.snapshot(), before)          # byte for byte
        self.assertEqual(self.trashed(), [])
        self.assertNotIn('data-action="undo"', self.page(f"/runs/{rm}"))
        self.page(f"/runs/{u['run']}")
        c = self.api("/api/confirm", {"action": "undo", "run": rm})
        self.assertIn("refused", c)

    def test_delete_for_good_and_empty_folders(self):
        self.sample()
        before = self.snapshot()
        name = self.scan()
        c = self.api("/api/confirm", {"action": "remove", "run": name, "method": "permanent"})
        self.assertIn("type delete", self.refused("/api/do", {"nonce": c["nonce"],
                                                              "typed": "no"}, 403))
        self.assertEqual(self.snapshot(), before)
        self.job(self.api("/api/do", {"nonce": c["nonce"], "typed": "delete"}))
        after = self.snapshot()
        self.assertLess(len(after), len(before))
        self.assertEqual(set(before.values()), set(after.values()))     # a copy of each
        self.assertEqual(self.trashed(), [])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "Empty", "inner")))
        empty = self.scan("empty")
        e = self.do("empty_delete", run=empty)
        self.assertEqual(e["kind"], "empty")
        self.assertFalse(os.path.exists(os.path.join(self.root, "Empty")))
        self.assertEqual(self.snapshot(), after)
        self.assertIn("deleted already", self.api("/api/confirm", {
            "action": "empty_delete", "run": e["run"]})["refused"])

    def test_trash_restore_and_empty(self):
        self.sample()
        before = self.snapshot()
        name = self.scan()
        rm = self.do("remove", run=name, method="trash")["run"]
        trash = self.api("/api/trash?fresh=1")["trashes"]
        self.assertEqual([t["home"] for t in trash], [True])
        gone = sorted(set(before) - set(self.snapshot()))
        # restore one item from the Trash page
        page = self.page("/trash")
        tokens = re.findall(r'data-action="trash_restore" data-token="([^"]+)"', page)
        self.assertTrue(tokens)
        back = api.token_path(tokens[0])
        self.do("trash_restore", token=tokens[0])
        self.assertTrue(os.path.lexists(back))
        # and the rest of that removal, for good
        left = self.trashed()
        self.assertEqual(len(left), len(tokens) - 1)
        if left:
            c = self.api("/api/confirm", {"action": "trash_empty_run", "run": rm})
            self.assertEqual(c["typed"], "delete")
            self.job(self.api("/api/do", {"nonce": c["nonce"], "typed": "delete"}))
            self.assertEqual(self.trashed(), [])
        self.assertTrue(set(gone) - set(self.snapshot()) or not left)
        self.assertEqual(self.api("/api/confirm", {"action": "trash_empty"})["refused"],
                         "Nothing to empty.")

    def test_history_delete_and_prune(self):
        self.sample()
        name = self.scan()
        empty = self.scan("empty")
        folder = os.path.join(self.data_dir, "runs", name)
        h = self.do("history_delete", run=name)
        self.assertEqual(h["after"], "/history")
        self.assertFalse(os.path.exists(folder))
        self.assertNotIn(name, self.page("/history"))
        self.assertIn(empty, self.page("/history"))
        self.refused("/api/confirm", {"action": "history_delete", "run": name}, 404)
        for i in range(3):
            self.fake_run(f"2020-01-0{i + 1}_000000-dupes")
        st, _, _ = self.c.post("/api/settings", {"key": "history.keep_runs", "op": "set",
                                                 "value": "1"})
        self.assertEqual(st, 200)
        c = self.api("/api/confirm", {"action": "history_prune"})
        self.assertIn("Delete 3 old runs?", c["html"])
        self.job(self.api("/api/do", {"nonce": c["nonce"]}))
        self.assertEqual([r.name for r in self.runs()], [empty])


# ---------------------------------------------------------------- in the browser

@unittest.skipUnless(shutil.which("node"), "needs node")
class TestScripts(unittest.TestCase):
    def test_scripts_are_valid(self):
        static = os.path.join(os.path.dirname(HERE), "cleanup", "web", "static")
        for path in (os.path.join(static, "app.js"), FLOW,
                     os.path.join(HERE, "ui", "report.mjs")):
            r = subprocess.run([shutil.which("node"), "--check", path], capture_output=True)
            self.assertEqual(r.returncode, 0, (path, r.stderr))


@needs_unshare
@unittest.skipUnless(BROWSER, "needs node 22+ and Chrome")
class TestBrowser(Base):
    """A person using the dashboard in Chrome: scan, choose, remove, see
    what was done, undo; change a setting; delete a run. The page asks
    nothing of any other address and logs no error."""

    def setUp(self):
        super().setUp()
        self.env["CLEAN_SPACE_SYSTEM_CACHES"] = "0"     # (the journal, snaps: slow, and not ours)
        self.s = SealedServer(self)
        self.addCleanup(self.s.stop)
        self.c = self.s.c

    api = WebBase.api
    sample = WebBase.sample

    def flow(self, shots=None):
        node, chrome = BROWSER
        config = {"url": self.s.url, "root": self.root, "shots": shots}
        path = os.path.join(self.tmp, "flow.json")
        with open(path, "w") as f:
            json.dump(config, f)
        r = subprocess.run([node, FLOW, path], capture_output=True, timeout=600,
                           env=dict(self.env, CHROME=chrome, TMPDIR=self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace")[-5000:]
                         + self.s.errors())
        if shots:                       # (what it saw, next to the pictures)
            with open(os.path.join(shots, "flow.json"), "wb") as f:
                f.write(r.stdout)
        return json.loads(r.stdout)

    def test_scan_choose_remove_undo_settings_history(self):
        self.sample()
        self.write("Space/deep/blob.bin", os.urandom(2 << 20))   # a folder space keeps
        before = self.snapshot()
        shots = os.environ.get("UI_SHOTS") or None
        out = self.flow(shots)
        self.assertEqual(out["errors"], [])
        port = self.c.port
        self.assertEqual([u for u in out["requests"]
                          if not u.startswith(f"http://127.0.0.1:{port}/")], [])
        self.assertEqual(out["overflow"], [], "the page is wider than the window")
        steps = out["steps"]
        # scan -> the results, as a person sees them
        self.assertTrue(steps["results"]["remove_enabled"])
        self.assertIn("Selected", steps["results"]["foot"])
        # unticking a copy changes the total, and is saved
        self.assertNotEqual(steps["results"]["foot"], steps["unticked"]["foot"])
        self.assertEqual(steps["unticked"]["saved"], True)
        self.assertIn("1 choice of yours", steps["unticked"]["how"])
        # remove -> cleanup completed (and the Trash page shows it), then undo -> all back
        self.assertEqual(steps["completed"]["title"], "Cleanup completed")
        self.assertEqual(steps["completed"]["buttons"],
                         ["Select more", "Scan again", "View report", "Undo",
                          "Empty these from the Trash"])
        self.assertGreater(steps["trash"]["items"], 0)
        self.assertEqual(steps["undone"]["path"].split("/")[1], "runs")
        self.assertEqual(steps["undone"]["title"], "Undo")
        self.assertEqual(self.snapshot(), before)
        # a finished job's page, visited again, stays (with its output)
        self.assertEqual(steps["job"]["head"], "Finished.")
        self.assertEqual(steps["missing"]["title"], "Can't show this")
        # the setting was saved to the file and shows after a reload
        self.assertEqual(steps["setting"]["after_reload"], "2K")
        with open(self.settings_file) as f:
            saved = json.load(f)
        self.assertEqual(saved.get("dupes.min_size"), 2048)
        # a list saved as shown is the same list
        self.assertEqual(saved.get("junk.names"), list(_settings.JUNK_NAMES))
        # the run deleted from the history page is gone
        self.assertEqual(steps["history"]["path"], "/history")
        self.assertNotIn(steps["history"]["deleted"], [r.name for r in self.runs()])
        self.assertFalse(steps["history"]["listed"])
        # space: nothing looked at yet; the wizard starts with the home folder; the look
        # (of the test's folder only) shows its tree, and a folder on its own
        self.assertTrue(steps["spaceBefore"]["empty"])
        sp = steps["space"]
        self.assertEqual(sp["first_roots"], [os.path.expanduser("~")])
        self.assertEqual(sp["title"], "Space")
        run = next(r for r in self.runs() if r.name == sp["run"])
        self.assertEqual((run.command, run.status, run.roots), ("space", "done", [self.root]))
        self.assertGreater(sp["rows"], 2)
        self.assertGreater(sp["bars"], 1)
        self.assertEqual(sp["sections"][:3], ["Drives", "Folders", "Biggest files"])
        self.assertIn("Clutter", sp["sections"])
        self.assertTrue(sp["folder"]["crumbs"].startswith("All /"), sp["folder"])
        self.assertTrue(sp["folder"]["top"].startswith(self.root + "/"), sp["folder"])
        # the Space page after it, and pruning the hash cache from there comes back to it
        self.assertTrue(steps["spaceAfter"]["tree"])
        self.assertIn("Prune the hash cache...", steps["spaceAfter"]["buttons"])
        self.assertEqual(steps["spaceAfter"]["prune"], "Prune the hash cache")
        self.assertEqual(steps["spaceAfter"]["back"], "/space")
        if shots:
            names = sorted(os.listdir(shots))
            self.assertTrue(any("-dark-phone" in n for n in names), names)


if __name__ == "__main__":
    unittest.main()
