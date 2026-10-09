"""
The dashboard's JSON API, under /api/. server.py has already checked who is
asking (the cookie, Host, Origin, X-Clean, JSON) before anything here runs.

Nothing here deletes or moves a file. What changes files is always a `clean
...` child process (jobs.py), started in two steps:

1. POST /api/confirm {"action": ...} works out, here and now, what the
   command would do (the same code the command uses) and answers with that
   for the dialog, plus a one-time confirmation good for 10 minutes;
2. POST /api/do {"nonce": ..., "typed": ...} starts exactly that command;
   deleting for good also needs the word "delete" typed.

A removal carries the digest of the plan that was shown (--plan-digest), so
if the choices, the files or the drives changed in between, `clean remove`
refuses rather than doing something else.

Paths travel as JSON strings; a name that isn't valid UTF-8 keeps its exact
bytes through the round trip (surrogate escapes, which JSON keeps as \\udcXX).
"""

import base64
import binascii
import contextlib
import io
import os
import secrets
import subprocess
import threading
import time

from cleanup import choices as _choices
from cleanup import runs, selection
from cleanup import settings as _settings
from cleanup.actions import remove as _remove
from cleanup.actions import trash as _trash
from cleanup.actions import undo as _undo
from cleanup.commands import history as _history
from cleanup.commands import remove as _remove_cmd
from cleanup.commands import trash as _trash_cmd
from cleanup.commands import undo as _undo_cmd
from cleanup.errors import UsageError
from cleanup.fsutil import human, is_within, printable, why
from cleanup.report import removal, restore
from cleanup.report.html import esc, esc_lines
from cleanup.report.text import command_line, plural, when
from cleanup.safety import check_delete_allowed
from cleanup.scan import cache as _cache
from cleanup.web import jobs as _jobs
from cleanup.web import view
from cleanup.web.server import HTTPError

NONCE_TTL = 600             # seconds a confirmation stays good
MAX_ARGS = 500
MAX_ARG = 4096
MAX_DIRS = 2000             # folders listed by the folder browser
TRASH_TTL = 60              # seconds the Trash sizes are reused
LIST = 50                   # items listed in a confirmation dialog
BACK = ("/space", "/trash", "/history")     # pages a confirmed job may return to

_parse_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def _str(body, key, required=True):
    v = body.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v or "\0" in v or len(v) > MAX_ARG:
        raise HTTPError(400, f"{key} is missing or not valid")
    return v


def _query(body):
    q = body.get("q")
    if q is None:
        q = {}
    if not isinstance(q, dict):
        raise HTTPError(400, "q must be an object")
    return {str(k): str(v) for k, v in q.items() if isinstance(v, (str, int))}


def parse(argv):
    """The `clean` command line parsed as the command would parse it; its
    complaint (or help, which isn't wanted here) becomes HTTPError 400."""
    from cleanup import cli
    out = io.StringIO()
    with _parse_lock, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            return cli.build_parser().parse_args(argv)
        except SystemExit:
            pass
    lines = [ln for ln in out.getvalue().strip().splitlines() if ln.strip()]
    msg = lines[-1] if lines else "bad options"
    if msg.startswith("clean") and ": error: " in msg:
        msg = msg.split(": error: ", 1)[1]
    raise HTTPError(400, f"the options are not valid: {msg}")


def path_token(path):
    """A path as text for a page attribute, exact whatever its bytes."""
    return base64.urlsafe_b64encode(os.fsencode(path)).decode("ascii")


def token_path(token):
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError, AttributeError):
        raise HTTPError(400, "bad path token")
    path = os.fsdecode(raw)
    if not os.path.isabs(path) or "\0" in path:
        raise HTTPError(400, "bad path token")
    return path


def glob_escape(path):
    """A path as a scan.exclude pattern that matches only itself."""
    return "".join({"[": "[[]", "*": "[*]", "?": "[?]"}.get(c, c) for c in path)


def _unmoved(path, moves):
    """A path where the drive is now, as the scan saw it."""
    for old, new in (moves or {}).items():
        if is_within(path, new):
            return old if path == new else old.rstrip("/") + path[len(new.rstrip("/")):]
    return path


def _volumes():
    return _trash_cmd._volumes()


def drive_label(vols, path):
    """'drive Backup Drive (/media/me/Backup Drive)' or 'this computer (/)'."""
    if vols is None:
        return "?"
    uuid, _, mnt = vols.locate(path)
    if mnt is None:
        return "?"
    home = os.path.expanduser("~")
    if vols.find_mount(os.path.realpath(home)) is mnt:
        return f"this computer's disk ({mnt.point})"
    name = vols.name(uuid) if uuid else None
    return f"drive {name} ({mnt.point})" if name else f"the drive at {mnt.point}"


def _start(app, argv, kind, after=None):
    try:
        job = app.jobs.start(argv, kind, after)
    except _jobs.Busy as e:
        busy = e.args[0]
        raise HTTPError(409, f"another job is still running (clean {' '.join(busy.argv[:2])}"
                             f"): wait for it to end, or cancel it")
    app.misc.pop("trash", None)
    return {"id": job.id, "url": f"/job/{job.id}", "cli": command_line(argv)}


# ---------------------------------------------------------------- GET

def get(app, parts, q):
    if parts == ["browse"]:
        path = token_path(q["t"]) if q.get("t") else q.get("path")
        return _json(browse(path, q.get("hidden") == "1"))
    if len(parts) == 2 and parts[0] == "jobs":
        job = app.jobs.get(parts[1])
        if job is None:
            raise HTTPError(404, "no such job (the dashboard was restarted?)")
        try:
            since = max(0, int(q.get("since") or 0))
        except ValueError:
            since = 0
        out = job.state(since)
        out["cli"] = command_line(job.argv)
        return _json(out)
    if len(parts) == 2 and parts[0] == "results":
        run = app.run(parts[1], "dupes")
        out = view.results(app.results(run), q)
        out["cli"] = results_cli(run)
        return _json(out)
    if parts == ["trash"]:
        return _json({"trashes": trash_summary(app, fresh=q.get("fresh") == "1")})
    raise HTTPError(404, "no such API")


def _json(obj, status=200):
    from cleanup.web.server import json_reply
    return json_reply(obj, status)


def results_cli(run):
    return command_line(["remove", run.name, "-n"])


def browse(path, hidden=False):
    """The folders in `path`, for the folder picker."""
    home = os.path.expanduser("~")
    path = path or home
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise HTTPError(400, "give a full path")
    path = os.path.normpath(path)
    dirs, error, more = [], None, 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if not hidden and e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                dirs.append(e.name)
    except OSError as e:
        error = why(e)
    dirs.sort(key=lambda n: (n.lower(), n))
    if len(dirs) > MAX_DIRS:
        more = len(dirs) - MAX_DIRS
        dirs = dirs[:MAX_DIRS]
    places = [{"label": "Home", "path": home}]
    vols = _volumes()
    if vols is not None:
        seen = {home}
        for m in sorted(vols.mounts, key=lambda m: m.point):
            if m.point in seen or not m.point.startswith(("/media/", "/run/media/", "/mnt/")):
                continue
            seen.add(m.point)
            places.append({"label": m.label or os.path.basename(m.point), "path": m.point})
    places.append({"label": "Computer (/)", "path": "/"})
    for p in places:
        p["token"] = path_token(p["path"])
    parent = os.path.dirname(path) if path != "/" else None
    # Paths travel as tokens (exact bytes) to come back here, and as text
    # with JSON's escapes for a scan's command line; "shown" is for reading.
    return {"path": path, "shown": printable(path), "token": path_token(path),
            "parent": parent and path_token(parent),
            "dirs": [{"name": printable(n), "path": os.path.join(path, n),
                      "token": path_token(os.path.join(path, n))} for n in dirs],
            "more": more, "error": error, "places": places}


# ---------------------------------------------------------------- the Trash

def origins(data_dir):
    """realpath of a Trash item -> the `clean remove` run that put it there."""
    out = {}
    for r in runs.history(data_dir):
        if r.command != "remove" or not r.has(runs.ACTIONS):
            continue
        try:
            rows = _remove.read_log(r.file(runs.ACTIONS))
        except OSError:
            continue
        for row in rows:
            if row[2] == "trash" and row[4]:
                out.setdefault(os.path.realpath(row[4]), r.name)
    return out


def trash_summary(app, fresh=False, items=False):
    """Each Trash: where, its path, how many items and bytes; with `items`,
    every item too (newest first). Sizes are reused for a minute."""
    now = time.time()
    hit = app.misc.get("trash")
    if hit is not None and not fresh and not items and now - hit[0] < TRASH_TTL:
        return hit[1]
    vols = _volumes()
    mine = origins(app.data_dir) if items else {}
    out = []
    for t in _trash.all_trashes(vols):
        entries = t.entries() + _trash_cmd._orphans(t)
        total = files = 0
        rows = []
        for e in entries:
            size, n = e.size()
            total += size
            files += n
            if items:
                rows.append({"original": printable(e.original), "token": path_token(e.original),
                             "deleted": e.deleted, "date": _trash_cmd._date(e),
                             "bytes": size, "files": n, "missing": e.missing,
                             "orphan": e.original == e.file,
                             "run": mine.get(os.path.realpath(e.file)),
                             "exists": os.path.lexists(e.original)})
        rows.sort(key=lambda r: r["deleted"] or "", reverse=True)
        out.append({"where": _trash_cmd._where(t, vols), "path": t.path,
                    "drive": "home" if t.is_home else t.top, "home": t.is_home,
                    "items": len(entries), "bytes": total, "files": files,
                    **({"entries": rows} if items else {})})
    if not items:
        app.misc["trash"] = (now, out)
    return out


# ---------------------------------------------------------------- POST

def post(app, parts, body):
    if parts == ["jobs"]:
        return _json(start_scan(app, body))
    if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
        job = app.jobs.get(parts[1])
        if job is None:
            raise HTTPError(404, "no such job")
        return _json({"cancelled": job.cancel(), "running": job.running})
    if len(parts) == 2 and parts[0] == "selection":
        return _json(change_selection(app, parts[1], body))
    if parts == ["confirm"]:
        return _json(confirm(app, body))
    if parts == ["do"]:
        return _json(do(app, body))
    if parts == ["open"]:
        return _json(open_folder(app, body))
    if parts == ["ignore"]:
        return _json(ignore(app, body))
    if parts == ["settings"]:
        return _json(change_setting(body))
    raise HTTPError(404, "no such API")


# ---------------------------------------------------------------- scans

def check_argv(argv):
    if (not isinstance(argv, list) or not argv or len(argv) > MAX_ARGS
            or not all(isinstance(a, str) and "\0" not in a and len(a) <= MAX_ARG
                       for a in argv)):
        raise HTTPError(400, "argv must be a list of strings")
    return argv


def start_scan(app, body):
    """Start `clean dupes ...`, `clean empty ...` (which only list) or `clean
    space ...` (which only looks)."""
    argv = check_argv(body.get("argv"))
    if argv[0] not in ("dupes", "empty", "space"):
        raise HTTPError(400, "only `clean dupes`, `clean empty` and `clean space` start from "
                             "here")
    args = parse(argv)
    if argv[0] == "dupes" and (args.remove or args.dry_run):
        raise HTTPError(400, "the dashboard scans first and removes after you have chosen: "
                             "leave out --remove and -n")
    if argv[0] == "empty" and (args.delete or args.force):
        raise HTTPError(400, "the dashboard lists empty folders first; deleting them is a "
                             "separate, confirmed step: leave out --delete and --force")
    for p in args.paths:
        if not os.path.isabs(p):
            raise HTTPError(400, f"{printable(p)} is not a full path")
        if not os.path.isdir(p):
            raise HTTPError(400, f"{printable(p)} is not a folder (any more)")
    return _start(app, argv, {"dupes": "scan"}.get(argv[0], argv[0]))


# ---------------------------------------------------------------- choosing

def change_selection(app, name, body):
    """Change the choices saved for a dupes run; answer with the cards as
    they are now (for the query in "q")."""
    run = app.run(name, "dupes")
    res = app.results(run)
    q = _query(body)
    op = body.get("op")
    message = None
    with app.selection_lock:
        saved, _ = res.saved()
        if op == "set":
            ref = _ref(res, body)
            value = body.get("value")
            if value not in selection.CHOICES:
                raise HTTPError(400, "value must be keep or remove")
            saved.choices[ref] = value
        elif op == "unset":
            saved.choices.pop(_ref(res, body), None)
        elif op == "reset":
            saved = _choices.Saved()
            message = "Back to the suggestions."
        elif op == "clear_choices":
            saved.choices = {}
            message = "Your ticks and unticks are cleared; the rules stay."
        elif op == "none":
            saved.rules["base"] = "none"
            saved.choices = {}
            message = "Nothing is selected now. Tick what should go."
        elif op == "rules":
            values = body.get("values")
            if not isinstance(values, dict) or not values:
                raise HTTPError(400, "values must be an object")
            for key, value in values.items():
                saved.rules[key] = _rule(res, key, value)
        elif op in ("invert", "keep_all", "reset_card"):
            st = res.state(saved)
            if body.get("card") is not None:
                card = res.by_id.get(body.get("card"))
                if card is None:
                    raise HTTPError(400, "no such result")
                cards = [card]
            elif op == "invert":
                # look-alikes are never selected in bulk: only by a tick each
                cards = [c for c in view.query(res, st, q)[1] if c.kind != "L"]
            else:
                raise HTTPError(400, "which result?")
            for c in cards:
                for ref in c.refs():
                    if op == "reset_card":
                        saved.choices.pop(ref, None)
                        continue
                    state = st.of(ref)[0]
                    if op == "keep_all":
                        if state not in ("with", "off", "done"):
                            saved.choices[ref] = "keep"
                    elif state in ("go", "merge", "blocked"):
                        saved.choices[ref] = "keep"
                    elif state in ("keep", "kept"):
                        saved.choices[ref] = "remove"
            if op == "invert":
                message = (f"Inverted {plural(len(cards), 'result')}." if cards else
                           "Look-alikes are never selected in bulk; tick each file to remove.")
        else:
            raise HTTPError(400, "unknown operation")
        try:
            if saved.is_default():
                _choices.clear(run)
            else:
                _choices.save(run, saved)
        except OSError as e:
            raise HTTPError(409, f"the choices can't be saved in the run folder: {why(e)}")
    out = view.results(res, q)
    out["cli"] = results_cli(run)
    out["rules"] = saved.rules
    if message:
        out["message"] = message
    return out


def _ref(res, body):
    ref = body.get("ref")
    if res.path_of(ref) is None:
        raise HTTPError(400, "no such folder or file in this run")
    return ref


def _rule(res, key, value):
    if key not in _choices.DEFAULT_RULES:
        raise HTTPError(400, f"unknown rule {key!r}")
    if key in ("keep_in", "remove_from"):
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            raise HTTPError(400, f"{key} must be a list of folders")
        out = []
        for p in value:
            p = os.path.normpath(p) if os.path.isabs(p) else p
            if not os.path.isabs(p) or not any(is_within(p, r) for r in res.m.roots):
                raise HTTPError(400, f"{printable(p)} is not inside the scanned folders")
            out.append(_unmoved(p, res.moves))
        value = out
    try:
        return _choices.check_rule(key, value)
    except ValueError as e:
        raise HTTPError(400, str(e))


# ---------------------------------------------------------------- confirmations

def confirm(app, body):
    """What an action would do, for the dialog, and a confirmation for
    /api/do; or {"refused": why} if it can't be done."""
    action = body.get("action")
    make = CONFIRMS.get(action)
    if make is None:
        raise HTTPError(400, "unknown action")
    try:
        c = make(app, body)
    except UsageError as e:
        c = {"refused": str(e)}
    if "refused" in c:
        return {"refused": c["refused"], "title": c.get("title", "Can't do this"),
                "html": c.get("html", "")}
    if body.get("back") in BACK:
        c["after"] = body["back"]
    nonce = secrets.token_urlsafe(24)
    now = time.time()
    with app.lock:
        app.nonces = {k: v for k, v in app.nonces.items() if v["expires"] > now}
        app.nonces[nonce] = {"expires": now + NONCE_TTL, "action": action, "argv": c["argv"],
                             "kind": c["kind"], "after": c.get("after"),
                             "typed": c.get("typed")}
    return {"nonce": nonce, "title": c["title"], "html": c["html"], "typed": c.get("typed"),
            "button": c["button"], "danger": bool(c.get("danger")),
            "cli": c.get("cli") or command_line([a for a in c["argv"] if a != "-y"])}


def do(app, body):
    nonce = body.get("nonce")
    if not isinstance(nonce, str):
        raise HTTPError(403, "this needs a confirmation: confirm again")
    with app.lock:
        c = app.nonces.pop(nonce, None)
    if c is None or c["expires"] < time.time():
        raise HTTPError(403, "this confirmation was used already or has expired: confirm "
                             "again")
    if c["typed"]:
        typed = body.get("typed")
        if not isinstance(typed, str) or typed.strip().lower() != c["typed"]:
            with app.lock:
                app.nonces[nonce] = c       # not used up: type it and try again
            raise HTTPError(403, f"type {c['typed']} to confirm")
    try:
        return _start(app, c["argv"], c["kind"], c["after"])
    except HTTPError:
        with app.lock:
            app.nonces[nonce] = c           # not used up: another job was running
        raise


def _quietly(fn, *a):
    """Run a command's planning code without its terminal output."""
    with _parse_lock, contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


def _list_html(items, cap=LIST, cls="path"):
    """<ul> of (text, note) pairs, with '... and N more'."""
    rows = [f'<li><span class="{cls}">{esc(t)}</span>'
            + (f' <span class="muted">{esc(n)}</span>' if n else "") + "</li>"
            for t, n in items[:cap]]
    if len(items) > cap:
        rows.append(f'<li class="muted">... and {len(items) - cap:,} more</li>')
    return '<ul class="dlist">' + "".join(rows) + "</ul>"


def _notes_html(notes):
    return "".join(f'<p class="note">{esc(n)}</p>' for n in notes
                   if n != _remove_cmd.CHOICES_NOTE)


def confirm_remove(app, body):
    run = app.run(body.get("run"), "dupes")
    method = body.get("method")
    if method not in ("trash", "permanent"):
        raise HTTPError(400, "method must be trash or permanent")
    argv = ["remove", run.name, "-y", "--" + method]
    args = parse(argv)
    p = _quietly(_remove_cmd.prepare, args, run)
    for root in _remove_cmd.busy_roots(p.m, p.plan, p.unavailable):
        check_delete_allowed(root, False)
    info = p.info
    blocked = [(path, reason) for _, path, reason in info["blocked"]]
    if not p.plan.items:
        html = "<p>Nothing is selected to remove.</p>"
        if blocked:
            html += ("<p>These were chosen but can't go:</p>" + _list_html(blocked))
        return {"refused": "Nothing to remove", "title": "Nothing to remove",
                "html": html + _notes_html(p.notes)}
    rows = removal.plan_rows(p.m, p.plan)
    drives = {}
    for r in rows:
        d = drives.setdefault(drive_label(p.vols, r["path"]),
                              {"items": 0, "files": 0, "bytes": 0, "merges": 0})
        d["items"] += 1
        d["files"] += r["files"]
        d["bytes"] += r["bytes"]
        d["merges"] += r["kind"] == "merge"
    table = "".join(
        f'<tr><td>{esc(name)}</td><td class="n">{d["items"]:,}</td>'
        f'<td class="n">{d["files"]:,}</td><td class="n">{esc(human(d["bytes"]))}</td></tr>'
        for name, d in sorted(drives.items()))
    if method == "trash":
        how = ("<p><b>To the Trash</b> of each drive (on that same drive, never copied): "
               "<code>clean undo</code> or the Undo button brings everything back, and the "
               "space is freed once the Trash is emptied.</p>")
    else:
        how = ("<p class=\"danger-text\"><b>Deleted for good.</b> There is no undo, and the "
               "Trash is not used.</p>")
    html = (f'<p class="question">{esc(removal.question(info))}</p>{how}'
            '<table class="num"><thead><tr><th>Where</th><th class="n">Items</th>'
            f'<th class="n">Files</th><th class="n">Size</th></tr></thead><tbody>{table}'
            '</tbody></table>'
            "<p class=\"muted\">Just before each item, every file in it and the copy that "
            "stays are read again: anything changed since the scan is skipped, and the last "
            "copy of anything is never removed.</p>")
    items = [(r["path"], ("merge into " + r["into"]) if r["kind"] == "merge" else r["why"])
             for r in rows]
    html += (f'<details class="block"><summary>{esc(plural(len(items), "item"))}</summary>'
             + _list_html(items) + "</details>")
    if blocked:
        html += (f'<details class="block"><summary>{esc(plural(len(blocked), "item"))} '
                 'blocked</summary>' + _list_html(blocked) + "</details>")
    html += _notes_html(p.notes)
    return {"title": "Remove the selected copies", "html": html,
            "argv": argv + ["--plan-digest", p.digest], "kind": "remove",
            "typed": "delete" if method == "permanent" else None,
            "button": "Move to the Trash" if method == "trash" else "Delete for good",
            "danger": method == "permanent",
            "cli": command_line(["remove", run.name, "--" + method])}


def confirm_undo(app, body):
    run = app.run(body.get("run"), "remove")
    source = _undo_cmd._source(run.folder)
    vols = _volumes()
    moves, unavailable, notes = _undo_cmd._locate(source, vols)
    info = restore.new_info(source, _undo_cmd._kinds(source), notes, True)
    try:
        planned = _undo.Undoer(source, moves, dry_run=True, unavailable=unavailable).go()
    except OSError as e:
        raise UsageError(f"cannot read what run {source.name} did: {why(e)}")
    restore.add_outcomes(info, planned)
    if not info["rows"]:
        return {"refused": f"Nothing to undo: run {source.name} removed nothing."}
    if not restore.to_do(info):
        return {"refused": "Nothing can be put back.",
                "html": f"<pre class=\"out\">{esc_lines(restore.preview(info, cap=LIST))}</pre>"}
    html = (f'<p class="question">{esc(restore.question(info))}</p>'
            f'<pre class="out">{esc_lines(restore.preview(info, cap=LIST))}</pre>'
            "<p class=\"muted\">Nothing that is there again now is overwritten.</p>"
            + _notes_html(notes))
    return {"title": "Undo this removal", "html": html, "argv": ["undo", run.name, "-y"],
            "kind": "undo", "button": "Put it back"}


def _entries_html(entries, sizes):
    items = [(e.original, f"{_trash_cmd._date(e)} · {human(sizes[id(e)][0])}")
             for e in entries]
    return _list_html(items)


def confirm_trash_empty_run(app, body):
    run = app.run(body.get("run"), "remove")
    vols = _volumes()
    entries, src, offline = _quietly(_trash_cmd._run_entries, run.folder, vols)
    if not entries:
        return {"refused": f"Nothing that run {run.name} moved to the Trash is still there."}
    sizes = {id(e): e.size() for e in entries}
    total = sum(s for s, _ in sizes.values())
    files = sum(n for _, n in sizes.values())
    html = (f'<p class="question">Delete {esc(plural(len(entries), "item"))} '
            f'({esc(plural(files, "file"))}, {esc(human(total))}) that this removal put in the '
            "Trash, for good?</p><p class=\"danger-text\">This frees the space, and can't be "
            "undone: Undo can't bring them back after this.</p>"
            + _entries_html(entries, sizes)
            + (f'<p class="note">{esc(plural(offline, "item"))} on a drive that is not '
               "connected stay.</p>" if offline else ""))
    return {"title": "Empty these from the Trash", "html": html,
            "argv": ["trash", "empty", "--run", run.name, "-y"], "kind": "trash",
            "after": f"/runs/{run.name}", "typed": "delete", "button": "Delete for good",
            "danger": True}


def confirm_trash_empty(app, body):
    vols = _volumes()
    drive = _str(body, "drive", required=False)
    days = body.get("older_than")
    if days in (None, ""):
        days = None
    else:
        try:
            days = float(days)
        except (TypeError, ValueError):
            raise HTTPError(400, "older_than must be a number of days")
        if days < 0 or days != days or days > 1e6:
            raise HTTPError(400, "older_than must be a number of days")
    trashes = _trash_cmd._trashes(vols, drive)
    entries = [e for t in trashes for e in t.entries() + _trash_cmd._orphans(t)]
    if days is not None:
        entries = [e for e in entries
                   if _trash.age_days(e) is not None and _trash.age_days(e) >= days]
    if not entries:
        return {"refused": "Nothing to empty" + (f" that has been in the Trash {days:g} days "
                                                 "or more" if days is not None else "") + "."}
    sizes = {id(e): e.size() for e in entries}
    total = sum(s for s, _ in sizes.values())
    files = sum(n for _, n in sizes.values())
    where = ", ".join(_trash_cmd._where(t, vols) for t in trashes)
    html = (f'<p class="question">Delete {esc(plural(len(entries), "item"))} '
            f'({esc(plural(files, "file"))}, {esc(human(total))}) from the Trash of '
            f"{esc(where)} for good?</p>"
            "<p class=\"danger-text\">This can't be undone.</p>"
            + _entries_html(entries, sizes))
    argv = ["trash", "empty"] + (["--older-than", f"{days:g}"] if days is not None else []) \
        + (["--drive", drive] if drive else []) + ["-y"]
    return {"title": "Empty the Trash", "html": html, "argv": argv, "kind": "trash",
            "after": "/trash", "typed": "delete", "button": "Delete for good", "danger": True}


def confirm_trash_restore(app, body):
    original = token_path(_str(body, "token"))
    vols = _volumes()
    entries = [e for t in _trash.all_trashes(vols) for e in t.entries() if not e.missing]
    picks = {}
    for e in entries:
        if is_within(e.original, original):
            best = picks.get(e.original)
            if best is None or (e.deleted or "") > (best.deleted or ""):
                picks[e.original] = e
    if not picks:
        return {"refused": f"{printable(original)} is not in the Trash (any more)."}
    todo = sorted(picks.values(), key=lambda e: e.original)
    blocked = [e for e in todo if os.path.lexists(e.original)]
    ready = [e for e in todo if e not in blocked]
    if not ready:
        return {"refused": f"{printable(original)} exists again, so its item stays in the "
                           "Trash (nothing is ever overwritten)."}
    sizes = {id(e): e.size() for e in todo}
    html = (f'<p class="question">Put back {esc(plural(len(ready), "item"))} where '
            f'{"it was" if len(ready) == 1 else "they were"}?</p>'
            + _entries_html(ready, sizes)
            + (f"<p class=\"note\">{esc(plural(len(blocked), 'item'))} stay in the Trash: "
               "something is there again.</p>" if blocked else ""))
    return {"title": "Restore from the Trash", "html": html,
            "argv": ["trash", "restore", original, "-y"], "kind": "trash", "after": "/trash",
            "button": "Restore"}


def confirm_empty_delete(app, body):
    run = app.run(body.get("run"), "empty")
    if run.meta.get("mode") != "dry-run":
        return {"refused": f"run {run.name} deleted already; scan again to look for more."}
    if run.status == "running":
        return {"refused": f"run {run.name} is still running."}
    argv = run.meta.get("argv")
    if not isinstance(argv, list) or not argv or argv[0] != "empty":
        return {"refused": f"run {run.name} doesn't say how it was started."}
    args = parse([str(a) for a in argv])
    roots = run.roots
    if not roots:
        return {"refused": f"run {run.name} doesn't say what it scanned."}
    for r in roots:
        if not os.path.isdir(r):
            return {"refused": f"{printable(r)} is not there any more."}
        try:
            check_delete_allowed(r, False)
        except UsageError as e:
            return {"refused": f"{e} (only `clean empty --force` in a terminal can do that)"}
    new = ["empty", "--delete", "-x" if args.one_file_system else "--no-one-file-system",
           "--default-excludes" if args.default_excludes else "--no-default-excludes",
           "-j", str(args.jobs)]
    for pat in args.exclude:
        new.append("--exclude=" + pat)
    new += ["--"] + roots
    head = run.headline or ""
    html = (f'<p class="question">Delete the empty folders in '
            f'{esc(plural(len(roots), "place"))}?</p>'
            f'<p>The scan found: {esc(head)}</p>'
            + _list_html([(r, "") for r in roots])
            + "<p class=\"muted\">It looks again first and removes only folders that are "
              "still empty (with rmdir, which can't remove a folder holding anything). "
              "Empty folders don't go to the Trash.</p>")
    return {"title": "Delete empty folders", "html": html, "argv": new, "kind": "empty",
            "button": "Delete empty folders"}


def confirm_history_delete(app, body):
    run = app.run(body.get("run"))
    refused, warn = _history.delete_check(run)
    if refused:
        return {"refused": f"{refused}: it can't be deleted until it ends."}
    html = (f'<p class="question">Delete run {esc(run.name)} ({esc(run.label)}) and '
            "everything saved with it?</p>"
            f'<p class="muted">{esc(run.headline or run.status)}</p>'
            + (f'<p class="note">Note: {esc(warn)}.</p>' if warn else "")
            + "<p class=\"muted\">Only the record goes: no file it found or removed is "
              "touched.</p>")
    return {"title": "Delete a run", "html": html,
            "argv": ["history", "--delete", run.name, "-y"], "kind": "history",
            "after": "/history", "button": "Delete the run", "danger": bool(warn)}


def confirm_history_prune(app, body):
    s = _settings.current()
    days, keep = s.get("history.keep_days"), s.get("history.keep_runs")
    if not days:
        return {"refused": "history.keep_days is 0, so runs are kept until you delete them."}
    remove, kept = runs.prune_plan(runs.history(app.data_dir), days, keep)
    if not remove:
        return {"refused": f"Nothing to prune: a run goes once it is older than "
                           f"{plural(days, 'day')} and not among the newest {keep:,}."}
    html = (f'<p class="question">Delete {esc(plural(len(remove), "old run"))}?</p>'
            + _list_html([(r.name, r.headline) for r in remove])
            + (f'<p class="note">{esc(plural(len(kept), "old run"))} stay: still needed for '
               "undo, or still running.</p>" if kept else ""))
    return {"title": "Prune the history", "html": html, "argv": ["history", "--prune"],
            "kind": "history", "after": "/history", "button": "Delete them"}


def _cache_facts(i):
    facts = [human(i["bytes"])]
    if not i["error"]:
        facts.append(plural(i["entries"], "file") + " remembered")
    return esc(", ".join(facts))


def confirm_cache_prune(app, body):
    i = _cache.info()
    if not i["exists"]:
        return {"refused": "There is no hash cache yet: nothing to prune."}
    if i["error"]:
        return {"refused": f"The hash cache can't be read ({i['error']}): delete it instead; "
                           "the next scan makes a new one."}
    mb = _settings.current().get("cache.max_mb")
    html = (f'<p class="question">Prune the hash cache ({_cache_facts(i)})?</p>'
            "<p>It drops the entries of files that are gone or have changed"
            + (f", then those used longest ago while it is over {mb} MB (cache.max_mb)"
               if mb else "")
            + ", and compacts the file. Entries on drives that aren't connected stay.</p>"
            '<p class="muted">Only the cache changes: no file it names is touched.</p>'
            + (f'<p class="muted">Least recently used entry: {esc(when(i["oldest"]))}.</p>'
               if i["oldest"] else ""))
    return {"title": "Prune the hash cache", "html": html, "argv": ["cache", "prune"],
            "kind": "cache", "after": "/space", "button": "Prune it"}


def confirm_cache_clear(app, body):
    i = _cache.info()
    if not i["exists"]:
        return {"refused": "There is no hash cache: nothing to delete."}
    html = (f'<p class="question">Delete the hash cache ({_cache_facts(i)})?</p>'
            "<p>The next duplicates scan reads every file again, so it takes longer; "
            "nothing else changes.</p>"
            '<p class="muted">Only the cache goes: no file it names is touched.</p>')
    return {"title": "Delete the hash cache", "html": html, "argv": ["cache", "clear", "-y"],
            "kind": "cache", "after": "/space", "button": "Delete it", "danger": True}


CONFIRMS = {
    "remove": confirm_remove,
    "undo": confirm_undo,
    "trash_empty_run": confirm_trash_empty_run,
    "trash_empty": confirm_trash_empty,
    "trash_restore": confirm_trash_restore,
    "empty_delete": confirm_empty_delete,
    "history_delete": confirm_history_delete,
    "history_prune": confirm_history_prune,
    "cache_prune": confirm_cache_prune,
    "cache_clear": confirm_cache_clear,
}



# ---------------------------------------------------------------- the desktop

def open_folder(app, body):
    """Show a folder of a run's results in the file manager. Only ever a
    folder: a file opens the folder it is in (opening a file could run it)."""
    if body.get("place") is not None:
        from cleanup import paths
        place = {"data": app.data_dir, "config": paths.config_dir(),
                 "cache": paths.cache_dir()}.get(body.get("place"))
        if place is None:
            raise HTTPError(400, "no such place")
        path = place
    else:
        run = app.run(body.get("run"), "dupes")
        res = app.results(run)
        ref = body.get("ref")
        path = res.path_of(ref)
        if path is None:
            raise HTTPError(400, "no such folder or file in this run")
        if ref[0] == "f":
            path = os.path.dirname(path)
    if os.path.islink(path) or not os.path.isdir(path):
        raise HTTPError(404, f"{printable(path)} is not there any more")
    if app.opener is not None:
        app.opener(path)
    else:
        try:
            subprocess.Popen(["xdg-open", path], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        except OSError as e:
            raise HTTPError(409, f"the file manager can't be started: {why(e)}")
    return {"opened": printable(path)}


# ---------------------------------------------------------------- settings

def ignore(app, body):
    """"Ignore this folder": every later scan skips it (scan.exclude), and
    this one keeps it."""
    run = app.run(body.get("run"), "dupes")
    res = app.results(run)
    ref = body.get("ref")
    path = res.path_of(ref)
    if path is None or ref[0] != "d":
        raise HTTPError(400, "no such folder in this run")
    s = _settings.reload()
    if s.source("scan.exclude").startswith("environment"):
        raise HTTPError(409, "scan.exclude is set by the environment variable "
                             "CLEAN_SCAN_EXCLUDE, which the dashboard can't change")
    pattern = glob_escape(path)
    current = s.get("scan.exclude")
    if pattern not in current:
        try:
            s.set("scan.exclude", current + [pattern])
        except (_settings.SettingsError, OSError) as e:
            raise HTTPError(409, str(e) if isinstance(e, _settings.SettingsError) else why(e))
    _settings.reload()
    body = dict(body, op="set", value="keep")
    out = change_selection(app, run.name, body)
    out["message"] = (f"{printable(path)} is skipped by every scan from now on (setting "
                      "scan.exclude), and stays here.")
    return out


def change_setting(body):
    name = body.get("key")
    key = _settings.KEYS.get(name) if isinstance(name, str) else None
    if key is None:
        raise HTTPError(400, "no such setting")
    if name == "paths.data":
        raise HTTPError(400, "paths.data is changed with `clean config set paths.data ...` "
                             "in a terminal (the dashboard keeps the data folder it started "
                             "with)")
    s = _settings.reload()
    op = body.get("op")
    try:
        if op == "reset":
            s.reset([name])
            cli = command_line(["config", "reset", name])
        elif op == "set":
            value = body.get("value")
            if key.kind == "list":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError("must be a list of strings")
                value = _settings.check_value(key, [v.strip() for v in value if v.strip()])
                cli = command_line(["config", "set", name] + (value or ["[]"]))
            else:
                if isinstance(value, bool):
                    value = "true" if value else "false"
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    raise ValueError("is not valid")
                value = _settings.parse_text(key, [str(value)])
                cli = command_line(["config", "set", name, _settings.text_value(key, value)])
            s.set(name, value)
        else:
            raise HTTPError(400, "op must be set or reset")
    except ValueError as e:
        raise HTTPError(400, f"{name} {e}")
    except _settings.SettingsError as e:
        raise HTTPError(409, str(e))
    except OSError as e:
        raise HTTPError(409, f"the settings file can't be written: {why(e)}")
    s = _settings.reload()
    out = {"key": name, "value": _settings.format_value(key, s.get(name)),
           "source": s.source(name), "cli": cli}
    if s.source(name).startswith("environment"):
        out["message"] = (f"Saved, but {key.env} is set in the environment, and it wins "
                          "over the settings file.")
    return out
