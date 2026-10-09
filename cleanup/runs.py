"""
Runs: every scan or clean-up is saved as a folder under <data>/runs/, so its
results can be looked at again later (`clean history`, `clean show`, the
index.html page) and, for removals, undone.

    <data>/runs/2026-10-07_143012-empty/
        run.json        what ran, on which drives, how it ended
        summary.txt     the text summary (what `clean show` prints)
        report.html     the same as a page that opens offline from the file
        data.json.gz    the full results, for programs
        folders.tsv     flat list for grep and spreadsheets (files.tsv for dupes)

run.json is written when the run starts, with status "running", and again
when it ends: "done", "interrupted" (Ctrl-C, kill, hang-up), "drive-gone" or
"failed". A process that dies without a word (kill -9, a crash, a power cut)
leaves "running" behind; history shows that run as "unfinished", found by
checking whether the process that wrote it is still alive.

index.html in the data folder is rebuilt from the run folders after every
run, so deleting a run folder simply removes it from the history.
"""

import errno
import fcntl
import itertools
import json
import os
import re
import shutil
import socket
import sys
import time
from datetime import datetime

from cleanup import __version__, paths, volumes
from cleanup import settings as _settings
from cleanup.errors import DriveGone, UsageError
from cleanup.fsutil import atomic_write, json_text, printable, why
from cleanup.safety import StopFlag

FORMAT = 1
RUNS = "runs"
RUN_JSON = "run.json"
INDEX = "index.html"
LOCK = "lock"
ACTIONS = "actions.tsv"     # written by runs that remove or move things; undo reads it

NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})-([a-z]+)(?:-(\d+))?$")
FINAL = ("done", "interrupted", "drive-gone", "failed")
UNFINISHED = "unfinished"   # "running", but the process is gone


def runs_dir(data_dir=None):
    return os.path.join(data_dir or paths.data_dir(), RUNS)


def _iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- liveness

def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip() or None
    except OSError:
        return None


def proc_start(pid):
    """When process `pid` started (clock ticks since boot), or None if there is
    no such process. Together with the pid this names one process: a pid can
    be reused, its start time can't."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        # The name in (...) may contain spaces and parentheses; the fields
        # after the last ")" start with the 3rd, so the 22nd is index 19.
        return int(data[data.rindex(b")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def is_alive(meta):
    """Is the process that started this run still running it?"""
    host = meta.get("host")
    if host and host != socket.gethostname():
        return True     # another computer's run (a shared data folder): can't tell
    then, now = meta.get("boot_id"), boot_id()
    if then and now and then != now:
        return False    # the computer has restarted since
    pid = meta.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    started = proc_start(pid)
    if started is None:
        return False
    return meta.get("proc_start") in (None, started)


# ---------------------------------------------------------------- one run

class Run:
    """A run folder and its run.json."""

    def __init__(self, folder, meta):
        self.folder = folder
        self.name = os.path.basename(folder)
        self.meta = meta
        self.number = None      # place in the history, 1 = newest
        self.closed = False
        self.unsaved = []       # result files that could not be written
        self.coming = ()        # result files complete() is about to write

    def file(self, name):
        return os.path.join(self.folder, name)

    def has(self, name):
        return os.path.isfile(self.file(name))

    @property
    def command(self):
        command = self.meta.get("command")
        if isinstance(command, str):
            return command
        m = NAME_RE.match(self.name)
        return m.group(3) if m else "?"

    @property
    def started_ts(self):
        ts = self.meta.get("started_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return float(ts)
        m = NAME_RE.match(self.name)
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S").timestamp()
        except (AttributeError, ValueError):
            return 0.0

    @property
    def status(self):
        """running, done, interrupted, drive-gone, failed, or unfinished (the
        process died without saying, or run.json is missing or unreadable)."""
        status = self.meta.get("status")
        if status == "running":
            return "running" if is_alive(self.meta) else UNFINISHED
        return status if status in FINAL else UNFINISHED

    @property
    def headline(self):
        h = self.meta.get("headline")
        return h if isinstance(h, str) else ""

    @property
    def roots(self):
        """The scanned folders, as paths (where they were at the time)."""
        roots = self.meta.get("roots")
        if not isinstance(roots, list):
            return []
        return [r["path"] for r in roots if isinstance(r, dict) and isinstance(r.get("path"), str)]

    @property
    def label(self):
        """What ran, briefly: "empty --delete", "empty", "dupes", "remove
        --permanent", "remove --dry-run"."""
        mode = self.meta.get("mode")
        flag = {"delete": " --delete", "permanent": " --permanent"}.get(mode, "")
        if mode == "dry-run" and self.command in ("remove", "undo"):
            flag = " --dry-run"
        if self.command == "trash" and isinstance(mode, str):
            flag = " " + mode
        if self.meta.get("link") is True:
            flag += " --link"
        return self.command + flag

    def save(self):
        data = (json_text(self.meta, indent=2) + "\n").encode("utf-8")
        try:
            atomic_write(self.file(RUN_JSON), data)
        except OSError as e:
            if e.errno not in (errno.ENOSPC, errno.EDQUOT):
                raise
            # The disk is full, so there is no room for a new copy; overwrite
            # the old one in place, reusing its space, so the run still ends
            # with its real status rather than looking unfinished.
            fd = os.open(self.file(RUN_JSON), os.O_WRONLY | os.O_CLOEXEC)
            try:
                os.pwrite(fd, data, 0)
                os.ftruncate(fd, len(data))
                os.fsync(fd)
            finally:
                os.close(fd)

    def conclude(self, status, exit_code, headline, totals=None, error=None):
        """Record how the run ended, in memory: the result files are rendered
        next (they show the final status), then close() saves run.json."""
        now = time.time()
        self.meta.update(status=status, exit_code=exit_code, headline=headline,
                         finished=_iso(now), finished_ts=now,
                         duration_s=round(now - self.meta.get("started_ts", now), 3))
        if totals is not None:
            self.meta["totals"] = totals
        if error:
            self.meta["error"] = error

    def write(self, name, make):
        """Save one result file, made by make() (text or bytes). Returns None,
        or what went wrong."""
        try:
            data = make()
            if isinstance(data, str):
                data = data.encode("utf-8", "backslashreplace")
            atomic_write(self.file(name), data)
        except OSError as e:
            self.unsaved.append(name)
            return f"could not save {name} in {printable(self.folder)}: {why(e)}"
        return None

    def close(self, warnings=()):
        """Save run.json with the final status, then prune old runs and rebuild
        index.html (neither of which can fail the run). `warnings`: result
        files that could not be saved, which make a clean exit status 1."""
        warnings = [w for w in warnings if w]
        if warnings:
            self.meta["warnings"] = warnings
            if self.meta.get("exit_code") == 0:
                self.meta["exit_code"] = 1
        self.save()
        self.closed = True
        housekeeping(os.path.dirname(os.path.dirname(self.folder)))

    def complete(self, files, problems=()):
        """Write the result files, [(name, make)] in order, then close the run.
        Each make() runs only when its turn comes, so a later file (the
        summary) can say which earlier ones failed. Ctrl-C waits until this is
        done, so a run is never left half-saved. Returns the warnings, already
        printed: what could not be saved, `problems` included."""
        problems = [p for p in problems if p]
        self.coming = [name for name, _ in files]   # a page can link the ones after it
        with StopFlag():
            for name, make in files:
                problems.append(self.write(name, make))
            problems = [p for p in problems if p]
            for p in problems:
                print(f"clean: warning: {p}", file=sys.stderr)
            try:
                self.close(problems)
            except OSError as e:
                msg = f"could not save {RUN_JSON} in {printable(self.folder)}: {why(e)}"
                print(f"clean: warning: {msg}", file=sys.stderr)
                problems.append(msg)
        return problems

    def finish(self, status, exit_code, headline, totals=None, error=None):
        self.conclude(status, exit_code, headline, totals, error)
        self.close()


def load(folder):
    folder = os.path.abspath(folder)
    try:
        with open(os.path.join(folder, RUN_JSON), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    return Run(folder, meta if isinstance(meta, dict) else {})


def describe_roots(roots):
    """[{path, uuid, inside, mount, fstype, drive}] for run.json: each folder
    also as (filesystem UUID, path inside it), which still finds it after the
    drive is mounted somewhere else."""
    try:
        vols = volumes.Volumes()
    except (OSError, ValueError):
        vols = None
    out = []
    for root in roots:
        entry = {"path": root}
        if vols is not None:
            uuid, inside, m = vols.locate(root)
            if m is not None:
                entry.update(uuid=uuid, inside=inside, mount=m.point, fstype=m.fstype,
                             drive=vols.name(uuid) if uuid else None)
        out.append(entry)
    return out


def _cwd():
    try:
        return os.getcwd()
    except OSError:
        return None


def start(command, mode, roots, argv=None, data_dir=None):
    """Create a run folder and its run.json (status "running") before any work
    starts, so a data folder that can't be written fails at once, not after
    hours of scanning."""
    data = data_dir or paths.data_dir()
    now = time.time()
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d_%H%M%S")
    folder = None
    try:
        os.makedirs(runs_dir(data), exist_ok=True)
        for n in itertools.count(1):
            name = f"{stamp}-{command}" + (f"-{n}" if n > 1 else "")
            try:
                os.mkdir(os.path.join(runs_dir(data), name))
            except FileExistsError:
                continue
            folder = os.path.join(runs_dir(data), name)
            break
        run = Run(folder, {
            "format": FORMAT, "name": name, "command": command, "mode": mode,
            "argv": list(sys.argv[1:] if argv is None else argv), "cwd": _cwd(),
            "version": __version__, "roots": describe_roots(roots),
            "status": "running", "pid": os.getpid(), "proc_start": proc_start(os.getpid()),
            "boot_id": boot_id(), "host": socket.gethostname(),
            "started": _iso(now), "started_ts": now,
        })
        run.save()
    except OSError as e:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        raise UsageError(f"cannot save the run in the data folder {printable(data)}: "
                         f"{why(e)} (the setting paths.data chooses another folder)")
    return run


class recording:
    """`with recording(run):` marks the run as ended however the block ends: a
    command finishes the run itself, but if something escapes (Ctrl-C at an odd
    moment, a lost drive, a bug), run.json still says what happened."""

    def __init__(self, run):
        self.run = run

    def __enter__(self):
        return self.run

    def __exit__(self, kind, exc, tb):
        if self.run.closed:
            return False
        if kind is None:
            status, code, text = "failed", 1, "ended without recording a result"
        elif issubclass(kind, KeyboardInterrupt):
            status, code, text = "interrupted", 130, "interrupted"
        elif issubclass(kind, DriveGone):
            status, code, text = "drive-gone", 1, str(exc)
        else:
            status, code, text = "failed", 1, f"{kind.__name__}: {exc}"
        try:
            self.run.finish(status, code, text, error=text)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------- history

def history(data_dir=None):
    """Every run, newest first, numbered from 1. Anything in runs/ that isn't
    a run folder (a stray file, a symlink) is ignored."""
    folder = runs_dir(data_dir)
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        path = os.path.join(folder, name)
        if NAME_RE.match(name) and not os.path.islink(path) and os.path.isdir(path):
            found.append(load(path))
    found.sort(key=lambda r: (r.started_ts, r.name), reverse=True)
    for i, run in enumerate(found, 1):
        run.number = i
    return found


def resolve(spec, data_dir=None):
    """The run named by `spec`: "last" (or nothing), a number from `clean
    history` (1 = newest), a unique start of a run's name, or the path of a
    run folder."""
    runs = history(data_dir)
    spec = (spec or "last").strip()
    if spec.startswith("#"):
        spec = spec[1:]
    numeric = spec == "last" or (spec.isdigit() and not (
        int(spec) > len(runs) and any(r.name.startswith(spec) for r in runs)))
    if numeric:     # (a number too big to be one may still start a name: 2026)
        n = 1 if spec == "last" else int(spec)
        if not runs:
            raise UsageError("there are no runs yet; `clean empty` and `clean dupes` "
                             "save one each time")
        if 1 <= n <= len(runs):
            return runs[n - 1]
        raise UsageError(f"there is no run {n}: the history has {len(runs)} "
                         f"(`clean history` lists them)")
    if "/" in spec:
        folder = os.path.realpath(os.path.expanduser(spec))
        if os.path.isfile(folder):
            folder = os.path.dirname(folder)
        for run in runs:
            if os.path.realpath(run.folder) == folder:
                return run
        if os.path.isfile(os.path.join(folder, RUN_JSON)):
            return load(folder)
        raise UsageError(f"{printable(spec)} is not a run folder (it has no {RUN_JSON})")
    matches = [r for r in runs if r.name.startswith(spec)]
    exact = [r for r in matches if r.name == spec]
    if exact:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UsageError(f"no run is called {printable(spec)}...; `clean history` lists them")
    names = ", ".join(r.name for r in matches[:4]) + (", ..." if len(matches) > 4 else "")
    raise UsageError(f"{printable(spec)} could be any of {len(matches)} runs ({names}); "
                     f"give more of the name, or its number")


# ---------------------------------------------------------------- retention

def keeps_undo(run):
    """A removal that moved things to the Trash or elsewhere is what `clean
    undo` reads to bring them back, so it is never pruned while it has its
    actions log (unless it was undone already, or deleted for good, or what
    it put in the Trash has been emptied since, or it did nothing)."""
    if run.command != "remove" or not _logged_a_step(run.file(ACTIONS)):
        return False
    if run.meta.get("mode") in ("permanent", "dry-run"):
        return False
    if isinstance(run.meta.get("emptied_by"), str):
        return False            # what it put in the Trash is all deleted for good
    undone = run.meta.get("undone_by")
    return not (isinstance(undone, dict) and undone.get("full") is True)


def _logged_a_step(path):
    """Does the actions log at `path` name any step (a line after its
    header)? A removal that found everything gone already has none."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    _, _, rest = head.partition(b"\n")
    return bool(rest.strip())


def prune_plan(runs, keep_days, keep_runs, now=None):
    """(to_remove, kept_back) for `runs` (newest first): runs older than
    keep_days days that aren't among the newest keep_runs may go (keep_days 0
    keeps everything). kept_back lists [(run, reason)] for old runs that stay
    anyway: still running, or an undo record."""
    if keep_days <= 0:
        return [], []
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    remove, kept = [], []
    for i, run in enumerate(runs):
        if i < keep_runs or run.started_ts >= cutoff:
            continue
        if run.status == "running":
            kept.append((run, "still running"))
        elif keeps_undo(run):
            kept.append((run, "its undo record (clean undo) is still needed"))
        else:
            remove.append(run)
    return remove, kept


def delete_run(run, data_dir=None):
    """Delete one run folder. Only ever a real folder directly inside runs/."""
    parent = os.path.realpath(runs_dir(data_dir))
    folder = run.folder
    if (os.path.islink(folder) or not NAME_RE.match(os.path.basename(folder))
            or os.path.realpath(os.path.dirname(folder)) != parent):
        raise OSError(errno.EPERM, "not a run folder", folder)
    try:
        shutil.rmtree(folder)
    except OSError:
        if os.path.lexists(folder):
            raise
        # else: already gone (another clean pruned it at the same time)


def prune(data_dir=None, keep_days=None, keep_runs=None, dry_run=False):
    """Apply the retention settings. Returns (removed, kept_back, failed)."""
    s = _settings.current()
    keep_days = s.get("history.keep_days") if keep_days is None else keep_days
    keep_runs = s.get("history.keep_runs") if keep_runs is None else keep_runs
    remove, kept = prune_plan(history(data_dir), keep_days, keep_runs)
    failed = []
    if not dry_run:
        for run in remove:
            try:
                delete_run(run, data_dir)
            except OSError as e:
                failed.append((run, why(e)))
    return [r for r in remove if r not in [f[0] for f in failed]], kept, failed


def rebuild_index(data_dir=None):
    """Write <data>/index.html, listing every run. A lock keeps two runs that
    end together from writing an outdated list last."""
    from cleanup.report import html
    data = data_dir or paths.data_dir()
    os.makedirs(data, exist_ok=True)
    fd = os.open(os.path.join(data, ".index.lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        atomic_write(os.path.join(data, INDEX), html.index_page(history(data), data).encode("utf-8"))
    finally:
        os.close(fd)


def housekeeping(data_dir=None):
    """After a run: prune by the retention settings, rebuild index.html. A
    problem here is only a warning; the run itself is already saved."""
    try:
        _, _, failed = prune(data_dir)
        for run, reason in failed:
            print(f"clean: warning: could not prune the old run {run.name}: {reason}",
                  file=sys.stderr)
    except OSError as e:
        print(f"clean: warning: could not prune old runs: {why(e)}", file=sys.stderr)
    try:
        rebuild_index(data_dir)
    except OSError as e:
        print(f"clean: warning: could not update the history page: {why(e)}", file=sys.stderr)


# ---------------------------------------------------------------- the lock

def lock(what, data_dir=None):
    """Make sure this is the only clean deleting or moving anything right now.
    Returns an open file descriptor: the lock lasts until it is closed or the
    process ends (however it ends, so a crash never leaves it stuck)."""
    data = data_dir or paths.data_dir()
    try:
        os.makedirs(data, exist_ok=True)
        fd = os.open(os.path.join(data, LOCK), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
    except OSError as e:
        raise UsageError(f"cannot use the data folder {printable(data)}: {why(e)} "
                         f"(the setting paths.data chooses another folder)")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        holder = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
        os.close(fd)
        if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise UsageError(f"cannot lock the data folder {printable(data)}: {why(e)}")
        raise UsageError(f"another clean is deleting right now "
                         f"({printable(holder) or 'details unknown'}). Only one may "
                         f"delete at a time; try again when it has finished.")
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"process {os.getpid()}: {what}\n".encode("utf-8", "surrogateescape"), 0)
    return fd
