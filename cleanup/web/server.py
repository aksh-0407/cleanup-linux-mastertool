"""
The dashboard's HTTP server: who may talk to it, what every answer carries,
and which paths exist.

Who may talk to it:
- it listens on 127.0.0.1 only;
- a browser gets in through the one-time launch link `clean ui` opens (or
  prints): the link sets a cookie (HttpOnly, SameSite=Strict) that every
  other request must carry, so no other program or web page can use the
  dashboard without that link;
- Host must be 127.0.0.1:PORT or localhost:PORT (against DNS rebinding) and
  Origin, when the browser sends one, this same address (against other
  sites); a page from anywhere else can't even read a GET;
- a POST must also be JSON with an X-Clean header, which no plain HTML form
  can send, and anything that changes files needs a fresh confirmation
  (api.py).

Files are only ever served by name from a short list (a run's report and
lists) or by result number (previews): never by a path from the request.
"""

import errno
import hmac
import http.server
import json
import os
import secrets
import socket
import stat
import threading
import time
import urllib.parse

from cleanup import paths, runs, selection
from cleanup import settings as _settings
from cleanup.fsutil import is_within, printable
from cleanup.web import jobs as _jobs
from cleanup.web import view

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {"app.js": "text/javascript; charset=utf-8",
                "app.css": "text/css; charset=utf-8"}
TOKEN_TTL = 600             # seconds a launch link works (once)
MAX_BODY = 1 << 20
UI_JSON = "ui.json"         # in the data folder: how a second `clean ui` finds this one

PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src data:; "
            "media-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'")
# report.html carries its own policy (one hashed script, no network at all);
# this one is added on top of it.
REPORT_CSP = ("sandbox allow-scripts allow-same-origin; default-src 'none'; "
              "style-src 'unsafe-inline'; img-src data:; script-src 'unsafe-inline'; "
              "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TEXT_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'"
PREVIEW_CSP = ("sandbox; default-src 'none'; img-src 'self'; media-src 'self'; "
               "style-src 'unsafe-inline'; frame-ancestors 'none'")
PDF_CSP = "default-src 'none'; object-src 'self'; frame-ancestors 'none'"

# A run's files the dashboard serves, and how.
RUN_FILES = {
    "report.html": ("text/html; charset=utf-8", REPORT_CSP),
    "summary.txt": ("text/plain; charset=utf-8", TEXT_CSP),
    "files.tsv": ("text/plain; charset=utf-8", TEXT_CSP),
    "folders.tsv": ("text/plain; charset=utf-8", TEXT_CSP),
    "actions.tsv": ("text/plain; charset=utf-8", TEXT_CSP),
    "run.json": ("text/plain; charset=utf-8", TEXT_CSP),
    "selection.json": ("text/plain; charset=utf-8", TEXT_CSP),
    "data.json.gz": ("application/gzip", TEXT_CSP),
}

# What can be previewed, by extension. Text-like files (including HTML and
# SVG) are only ever sent as plain text.
_IMAGE = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
          "webp": "image/webp", "bmp": "image/bmp", "ico": "image/x-icon", "avif": "image/avif"}
_AUDIO = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg",
          "oga": "audio/ogg", "opus": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac"}
_VIDEO = {"mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "ogv": "video/ogg",
          "mov": "video/quicktime", "mkv": "video/x-matroska"}
_TEXT = ("txt text md markdown rst csv tsv log json xml yaml yml toml ini cfg conf "
         "html htm xhtml svg css js mjs ts py sh bash c h cc cpp hpp java kt go rs rb pl "
         "php lua sql tex srt vtt nfo m3u m3u8 desktop service bat ps1 diff patch").split()
PREVIEW_TYPES = dict(
    [(e, ("image", t)) for e, t in _IMAGE.items()]
    + [(e, ("audio", t)) for e, t in _AUDIO.items()]
    + [(e, ("video", t)) for e, t in _VIDEO.items()]
    + [(e, ("text", "text/plain; charset=utf-8")) for e in _TEXT]
    + [("pdf", ("pdf", "application/pdf"))])
view.PREVIEWABLE = frozenset(PREVIEW_TYPES)


class Reply:
    """An answer: status, body (bytes), content type, extra headers."""

    def __init__(self, status, body=b"", ctype="text/plain; charset=utf-8", headers=None,
                 csp=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8", "surrogateescape")
        self.ctype = ctype
        self.headers = dict(headers or {})
        self.csp = csp


def html_reply(text, status=200):
    return Reply(status, text, "text/html; charset=utf-8", csp=PAGE_CSP)


def json_reply(obj, status=200):
    return Reply(status, json.dumps(obj, ensure_ascii=True, separators=(",", ":")),
                 "application/json; charset=utf-8")


def error(status, message):
    return json_reply({"error": message}, status)


def redirect(where):
    return Reply(303, b"", headers={"Location": where})


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- the app

class App:
    """One dashboard: its secrets, its jobs, the runs it has open."""

    def __init__(self, data_dir=None, spawn=None, opener=None):
        self.data_dir = data_dir or paths.data_dir()
        self.secret = secrets.token_urlsafe(32)
        self.admin = secrets.token_urlsafe(32)
        self.tokens = {}            # launch token -> expiry
        self.nonces = {}            # confirmation -> what it allows, until when (api.confirm)
        self.lock = threading.Lock()
        self.jobs = _jobs.Jobs(self.data_dir, spawn)
        self.cache = view.Cache()
        self.selection_lock = threading.Lock()
        self.opener = opener        # opens a folder on the desktop (a hook for tests)
        self.port = None
        self.started = time.time()
        self.misc = {}              # small caches (Trash sizes, ...)

    @property
    def hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @property
    def cookie_name(self):
        return f"clean_ui_{self.port}"

    def new_token(self):
        with self.lock:
            now = time.time()
            self.tokens = {t: e for t, e in self.tokens.items() if e > now}
            token = secrets.token_urlsafe(24)
            self.tokens[token] = now + TOKEN_TTL
            return token

    def use_token(self, token):
        with self.lock:
            expiry = self.tokens.pop(token, None) if isinstance(token, str) else None
            return expiry is not None and expiry > time.time()

    def launch_url(self):
        return f"http://127.0.0.1:{self.port}/launch?t={self.new_token()}"

    # ------------------------------------------------------------ runs

    def run(self, name, command=None):
        """The run folder `name` (exactly a run's name, never a path), or
        HTTPError 404."""
        if not isinstance(name, str) or not runs.NAME_RE.match(name):
            raise HTTPError(404, "no such run")
        folder = os.path.join(runs.runs_dir(self.data_dir), name)
        if os.path.islink(folder) or not os.path.isdir(folder):
            raise HTTPError(404, f"there is no run {name} (deleted?)")
        run = runs.load(folder)
        for r in runs.history(self.data_dir):
            if r.name == name:
                run.number = r.number
                break
        if command is not None and run.command != command:
            raise HTTPError(404, f"run {name} is not a `clean {command}` run")
        return run

    def results(self, run):
        try:
            return self.cache.get(run)
        except view.ViewError as e:
            raise HTTPError(409, str(e))


# ---------------------------------------------------------------- requests

class Query(dict):
    """A query string: each name's last value, and all of them by every()."""

    def __init__(self, text=""):
        self.lists = urllib.parse.parse_qs(text, keep_blank_values=True)
        super().__init__((k, v[-1]) for k, v in self.lists.items())

    def every(self, name):
        return list(self.lists.get(name, ()))


class Request:
    def __init__(self, handler, method):
        self.handler = handler
        self.method = method
        parts = urllib.parse.urlsplit(handler.path)
        self.raw_path = parts.path
        self.path = urllib.parse.unquote(parts.path, errors="strict")
        self.query = Query(parts.query)
        self.headers = handler.headers
        self.body = None

    def header(self, name):
        return self.headers.get(name)


def _cookie(header, name):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def check(app, req):
    """Who is asking: raises HTTPError unless the request may be answered."""
    host = req.header("Host")
    if host not in app.hosts:
        raise HTTPError(403, "wrong Host header (the dashboard answers only at "
                             "127.0.0.1 or localhost)")
    origin = req.header("Origin")
    if origin is not None and origin != "http://" + host:
        raise HTTPError(403, "request from another site refused")
    site = req.header("Sec-Fetch-Site")
    if req.method == "GET" and site not in (None, "same-origin", "none"):
        raise HTTPError(403, "request from another site refused")
    if req.path == "/admin/token":
        return
    if req.path.startswith("/static/") or req.path in ("/launch", "/favicon.ico"):
        if req.method != "GET":
            raise HTTPError(405, "not allowed")
        return
    got = _cookie(req.header("Cookie"), app.cookie_name)
    if got is None or not hmac.compare_digest(got.encode(), app.secret.encode()):
        raise HTTPError(403, "not signed in")
    if req.method == "POST":
        if origin is None or site not in (None, "same-origin"):
            raise HTTPError(403, "request from another site refused")
        if req.header("X-Clean") != "1":
            raise HTTPError(403, "missing X-Clean header")
        ctype = (req.header("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPError(415, "the body must be JSON")


def read_body(req):
    try:
        n = int(req.header("Content-Length") or "0")
    except ValueError:
        raise HTTPError(400, "bad Content-Length")
    if n < 0 or n > MAX_BODY:
        raise HTTPError(413, "request too big")
    raw = req.handler.rfile.read(n) if n else b""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise HTTPError(400, "the body is not valid JSON")
    if not isinstance(body, dict):
        raise HTTPError(400, "the body must be a JSON object")
    return body


# ---------------------------------------------------------------- answers

def not_signed_in_page():
    from cleanup.web import pages
    return pages.message_page(
        "Not signed in",
        "This dashboard opens only through the link `clean ui` gives (it works once, "
        "for 10 minutes). Run `clean ui` again in a terminal: it opens a new link to "
        "this same dashboard.", status=403)


def launch(app, req):
    then = local_page(req.query.get("next"))
    if not app.use_token(req.query.get("t")):
        got = _cookie(req.header("Cookie"), app.cookie_name)
        if got is not None and hmac.compare_digest(got.encode(), app.secret.encode()):
            return redirect(then)
        return not_signed_in_page()
    from cleanup.web import pages
    reply = pages.message_page("Opening the dashboard", "One moment...", refresh=then)
    reply.headers["Set-Cookie"] = (f"{app.cookie_name}={app.secret}; HttpOnly; "
                                   "SameSite=Strict; Path=/")
    return reply


def local_page(where):
    """`where` if it is a page of this dashboard ("/scan?kind=..."), else "/":
    a launch link never sends the browser anywhere else."""
    if not isinstance(where, str) or len(where) > 16384 or not where.startswith("/") \
            or where.startswith("//") or any(c < " " or c in "\\\x7f" for c in where):
        return "/"
    parts = urllib.parse.urlsplit(where)
    if parts.scheme or parts.netloc:
        return "/"
    return where


def admin_token(app, req):
    key = req.header("X-Clean-Admin") or ""
    if req.method != "POST" or req.header("Origin") is not None \
            or not hmac.compare_digest(key.encode(), app.admin.encode()):
        raise HTTPError(403, "not allowed")
    return json_reply({"url": app.launch_url()})


def static(name):
    ctype = STATIC_FILES.get(name)
    if ctype is None:
        raise HTTPError(404, "not found")
    try:
        with open(os.path.join(STATIC, name), "rb") as f:
            body = f.read()
    except OSError:
        raise HTTPError(404, "not found")
    if name == "app.css":           # the report styles first: one look everywhere
        from cleanup.report import html as _html
        body = _html.CSS.encode() + b"\n" + body
    return Reply(200, body, ctype, {"Cache-Control": "no-cache"})


def run_file(app, name, fname):
    how = RUN_FILES.get(fname)
    if how is None:
        raise HTTPError(404, "not found")
    run = app.run(name)
    path = run.file(fname)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise HTTPError(404, f"run {name} has no {fname}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HTTPError(404, f"run {name} has no {fname}")
        with os.fdopen(fd, "rb", closefd=False) as f:
            body = f.read(64 << 20)
    finally:
        os.close(fd)
    headers = {}
    if fname.endswith(".gz"):
        headers["Content-Disposition"] = f'attachment; filename="{name}-{fname}"'
    return Reply(200, body, how[0], headers, csp=how[1])


# ---------------------------------------------------------------- previews

def preview_target(app, name, idx):
    """(open file, size, kind, content type) of file number `idx` of run
    `name`, checked to be the very file the scan saw."""
    run = app.run(name, "dupes")
    if not (idx.isascii() and idx.isdigit()) or len(idx) > 12:
        raise HTTPError(404, "no such file in this run")
    res = app.results(run)
    m = res.m
    n = int(idx)
    if n >= len(m.files):
        raise HTTPError(404, "no such file in this run")
    d, fname, size, mtime = m.files[n][:4]
    kind = PREVIEW_TYPES.get(selection.ext_of(fname))
    if kind is None:
        raise HTTPError(415, "this kind of file has no preview")
    limit = _settings.current().get("ui.preview_max_mb") << 20
    if size > limit:
        raise HTTPError(413, f"too big to preview (the limit is ui.preview_max_mb, "
                             f"{limit >> 20} MB)")
    folder = os.path.realpath(m.paths[d])
    tops = [os.path.realpath(r) for r in m.roots]
    if not any(is_within(folder, t) for t in tops) or "/" in fname or fname in (".", ".."):
        raise HTTPError(404, "this file is not where the scan saw it")
    try:
        fd = os.open(os.path.join(folder, fname),
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise HTTPError(404 if e.errno in (errno.ENOENT, errno.ENOTDIR) else 409,
                        "the file is not there any more" if e.errno == errno.ENOENT
                        else f"the file can't be opened ({printable(e.strerror or e)})")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size != size or st.st_mtime_ns != mtime:
        os.close(fd)
        raise HTTPError(409, "the file has changed since the scan")
    return fd, size, kind[0], kind[1]


def _range(header, size):
    """(start, end) of a single 'bytes=a-b' range, None for the whole file,
    or HTTPError 416."""
    if not header:
        return None
    unit, _, spec = header.partition("=")
    if unit.strip() != "bytes" or "," in spec:
        return None
    a, _, b = spec.strip().partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                raise ValueError
            start, end = max(size - n, 0), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPError(416, "range not satisfiable")
    return start, end


# ---------------------------------------------------------------- the handler

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "clean-ui"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, fmt, *args):
        if self.server.app_verbose:
            super().log_message(fmt, *args)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_HEAD(self):
        self._send(Reply(405, "only GET and POST"), head=True)

    def _other(self):
        self._send(Reply(405, "only GET and POST"))

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other

    def _handle(self, method):
        app = self.server.app
        try:
            try:
                req = Request(self, method)
            except (UnicodeDecodeError, ValueError):
                raise HTTPError(400, "bad path")
            check(app, req)
            if method == "POST" and req.path != "/admin/token":
                req.body = read_body(req)
            reply = route(app, req, self)
        except HTTPError as e:
            reply = self._failure(e)
        except Exception as e:     # a bug: say so, keep serving
            import traceback
            traceback.print_exc()
            reply = self._failure(HTTPError(500, f"internal error: {type(e).__name__}: {e}"))
        if reply is not None:
            self._send(reply)

    def _failure(self, e):
        path = self.path.split("?")[0]
        wants_page = (self.command == "GET" and not path.startswith(("/api/", "/preview/",
                                                                     "/static/"))
                      and "text/html" in (self.headers.get("Accept") or ""))
        if wants_page:
            if e.message == "not signed in":
                return not_signed_in_page()
            from cleanup.web import pages
            return pages.message_page("Can't show this", e.message, status=e.status)
        return error(e.status, e.message)

    def _common(self, csp):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", csp or "default-src 'none'; "
                                                          "frame-ancestors 'none'")

    def _send(self, reply, head=False):
        try:
            self.send_response(reply.status)
            self._common(reply.csp)
            self.send_header("Content-Type", reply.ctype)
            self.send_header("Content-Length", str(len(reply.body)))
            reply.headers.setdefault("Cache-Control", "no-store")
            for k, v in reply.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if not head and reply.body:
                self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_file(self, fd, size, ctype, csp, kind):
        """Send an open file (a preview), whole or the range asked for."""
        try:
            try:
                rng = _range(self.headers.get("Range"), size)
            except HTTPError as e:
                self.send_response(e.status)
                self._common(csp)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = rng if rng else (0, size - 1)
            length = max(end - start + 1, 0)
            self.send_response(206 if rng else 200)
            self._common(csp)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if kind == "pdf":
                self.send_header("Content-Disposition", "inline")
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            os.lseek(fd, start, os.SEEK_SET)
            left = length
            while left > 0:
                chunk = os.read(fd, min(left, 1 << 16))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
            if left > 0:
                self.close_connection = True     # the file shrank: the length was wrong
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            os.close(fd)


def route(app, req, handler):
    """The reply for a checked request (None if it was sent already)."""
    from cleanup.web import api, pages
    path, method = req.path, req.method
    parts = [p for p in path.split("/") if p != ""]
    if path == "/launch":
        return launch(app, req)
    if path == "/admin/token":
        return admin_token(app, req)
    if path == "/favicon.ico":
        return Reply(204)
    if parts[:1] == ["static"]:         # (answered without the cookie: nothing else here)
        if len(parts) != 2:
            raise HTTPError(404, "not found")
        return static(parts[1])
    _settings.reload()              # `clean config set` in a terminal counts at once
    if parts[:1] == ["api"]:
        if method == "GET":
            return api.get(app, parts[1:], req.query)
        return api.post(app, parts[1:], req.body)
    if method != "GET":
        raise HTTPError(405, "only GET here")
    if parts[:1] == ["preview"] and len(parts) == 3:
        fd, size, kind, ctype = preview_target(app, parts[1], parts[2])
        handler.send_file(fd, size, ctype, PDF_CSP if kind == "pdf" else PREVIEW_CSP, kind)
        return None
    if parts[:1] == ["runs"] and len(parts) == 3:
        return run_file(app, parts[1], parts[2])
    if path == "/index.html":
        return redirect("/history")
    return pages.get(app, parts, req.query)


# ---------------------------------------------------------------- serving

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, app, port=0, verbose=False):
        self.app = app
        self.app_verbose = verbose
        super().__init__(("127.0.0.1", port), Handler)
        app.port = self.server_address[1]

    def handle_error(self, request, client_address):
        pass        # a browser closing a connection early is not news


def ui_file(data_dir):
    return os.path.join(data_dir, UI_JSON)


def write_ui_file(app):
    """Note where this dashboard is, so a second `clean ui` opens it
    instead of starting another (only this user can read it)."""
    os.makedirs(app.data_dir, exist_ok=True)
    path = ui_file(app.data_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "proc_start": runs.proc_start(os.getpid()),
                   "port": app.port, "admin": app.admin}, f)
    os.replace(tmp, path)


def remove_ui_file(app):
    path = ui_file(app.data_dir)
    try:
        with open(path) as f:
            if json.load(f).get("pid") != os.getpid():
                return
        os.unlink(path)
    except (OSError, ValueError, AttributeError):
        pass


def running_dashboard(data_dir):
    """A new launch link from the dashboard already serving `data_dir`, or
    None if there is none."""
    import http.client
    try:
        with open(ui_file(data_dir)) as f:
            info = json.load(f)
        pid, port, admin = int(info["pid"]), int(info["port"]), str(info["admin"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not runs.is_alive({"pid": pid, "proc_start": info.get("proc_start")}):
        return None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/admin/token", body=b"",
                     headers={"X-Clean-Admin": admin, "Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status != 200:
            return None
        return json.loads(body.decode())["url"]
    except (OSError, ValueError, KeyError, socket.timeout):
        return None
