"""
Filesystem helpers shared by every command: which folders may be scanned,
exclusion rules, safe path printing, sizes, the progress line, and Ctrl-C
handling for worker threads.
"""

import ctypes
import errno
import fnmatch
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from cleanup.errors import UsageError

# Folders that are never entered (and so never cleaned or reported) unless
# --no-default-excludes is given. Their empty subfolders mean something to the
# program that owns them:
#   .git .hg .svn .bzr  version-control metadata. git stops recognising a repo
#                       whose .git/refs is gone, and after `git gc` that folder
#                       holds only empty subfolders, so the empty-folder
#                       cascade would remove it.
#   lost+found          fsck's recovery folder on ext2/3/4.
#   .stfolder           Syncthing's folder marker; syncing stops without it.
DEFAULT_EXCLUDES = (".git", ".hg", ".svn", ".bzr", "lost+found", ".stfolder")

# Never scanned at all: kernel pseudo-filesystems. Reading them is pointless and
# some entries block forever.
NEVER_SCAN = ("/proc", "/sys", "/dev")

# Set when the main thread gets Ctrl-C, so worker threads stop promptly instead
# of the process hanging until their whole subtree is finished.
CANCEL = threading.Event()


# ---------------------------------------------------------------- paths

def is_within(path, parent):
    """True if `path` is `parent` or anywhere inside it (both absolute)."""
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def resolve_roots(raw_paths, refuse_symlinks):
    """Turn command-line paths into canonical folders to walk.

    Returns (roots, given): `given` is every requested folder, `roots` is the
    subset to actually walk, with duplicates and folders nested inside another
    requested folder dropped (walking the outer one covers them).
    """
    given = []
    for raw in raw_paths:
        path = os.path.abspath(os.path.expanduser(raw))
        if refuse_symlinks and os.path.islink(path):
            raise UsageError(f"{printable(path)} is a symlink; to clean the folder it "
                             f"points to, pass that instead: "
                             f"{printable(os.path.realpath(path))}")
        real = os.path.realpath(path)
        if not os.path.exists(real):
            raise UsageError(f"no such folder: {printable(path)}")
        if not os.path.isdir(real):
            raise UsageError(f"not a folder: {printable(path)}")
        if real == "/" or any(is_within(real, d) for d in NEVER_SCAN):
            raise UsageError(f"refusing to scan {printable(real)}: pick a folder or "
                             f"a drive, e.g. /media/{os.environ.get('USER', 'you')}/...")
        given.append(real)

    # In the order given (the first one named is preferred for keeping, when
    # copies are found), each once, without folders inside another one.
    unique = list(dict.fromkeys(given))
    roots = [p for p in unique if not any(r != p and is_within(p, r) for r in unique)]
    return roots, given


class Excluder:
    """Decides which names/paths are skipped: --exclude patterns plus defaults,
    and `skip`: exact paths that are never scanned (clean's own data folder)."""

    def __init__(self, patterns, use_defaults, skip=()):
        patterns = list(patterns) + (list(DEFAULT_EXCLUDES) if use_defaults else [])
        self.skip = set(skip)
        self.names = set()      # plain names: one set lookup per entry
        self.name_globs = []
        self.path_globs = []
        for pat in patterns:
            if "/" in pat:
                self.path_globs.append(os.path.abspath(os.path.expanduser(pat)).rstrip("/") or "/")
            elif any(c in pat for c in "*?["):
                self.name_globs.append(pat)
            else:
                self.names.add(pat)

    def __call__(self, name, path):
        if name in self.names or path in self.skip:
            return True
        for pat in self.name_globs:
            if fnmatch.fnmatchcase(name, pat):
                return True
        for pat in self.path_globs:
            if fnmatch.fnmatchcase(path, pat):
                return True
        return False


# ---------------------------------------------------------------- output

# Control characters would break a TSV row or a terminal line (or, for ESC,
# drive the terminal), so they are shown as escapes.
_CONTROL = {c: f"\\x{c:02x}" for c in (*range(0x20), 0x7F)}
_CONTROL.update({ord("\t"): "\\t", ord("\n"): "\\n", ord("\r"): "\\r"})
_CONTROL.update({c: f"\\u{c:04x}" for c in range(0x80, 0xA0)})  # C1 controls
# Bidirectional-text controls could make a name display as something else
# ("evil\u202egpj.exe" shows as "evilexe.jpg"), and line/paragraph separators
# would split a path over two lines.
_CONTROL.update({c: f"\\u{c:04x}" for c in (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F),
                                            *range(0x2066, 0x206A), 0x2028, 0x2029)})


def printable(path):
    """`path` made safe for a terminal line or a TSV cell.

    Bytes that aren't valid UTF-8 (Linux allows them in names) become \\xNN and
    control characters become \\t, \\n, \\xNN (\\uNNNN for U+0080-U+009F and
    for the bidi and line-separator controls), so one path is always one line
    and shows as what it is.
    """
    if path.isprintable():
        return path
    text = path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    return text.translate(_CONTROL)


# ---------------------------------------------------------------- files

def atomic_write(path, data):
    """Replace `path` with `data` (bytes) in one step: readers see the old file
    or the new one, never half of it, even if the power goes in between."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".clean-", suffix=".tmp")
    try:
        umask = os.umask(0)
        os.umask(umask)
        os.fchmod(fd, 0o666 & ~umask)  # mkstemp makes it owner-only
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # the rename itself is done; this only hardens it against power loss


_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_renameat2 = None


def rename_noreplace(src, dst):
    """Rename src to dst, never replacing anything already at dst: raises
    FileExistsError instead (os.rename would silently overwrite a file).
    renameat2(RENAME_NOREPLACE) checks and renames in one step; where the
    kernel, C library or filesystem can't do that, a check just before
    os.rename() is the fallback (a tiny window instead of none)."""
    global _renameat2
    if _renameat2 is None:
        try:
            fn = ctypes.CDLL(None, use_errno=True).renameat2
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_uint]
            fn.restype = ctypes.c_int
            _renameat2 = fn
        except (AttributeError, OSError):
            _renameat2 = False
    if _renameat2:
        if _renameat2(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst),
                      _RENAME_NOREPLACE) == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(err, os.strerror(err), src, None, dst)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
    os.rename(src, dst)


# actions.tsv cells: like printable(), but exactly reversible, since `clean
# undo` reads the paths back. Backslash doubles; bytes that aren't UTF-8 are
# \xNN (80-ff); control characters are \t \n \r or \xNN (00-7f); C1 and
# bidi controls are \uNNNN.
_ESCAPE = {c: f"\\x{c:02x}" for c in (*range(0x20), 0x7f)}
_ESCAPE.update({0x5c: "\\\\", 0x09: "\\t", 0x0a: "\\n", 0x0d: "\\r"})
_ESCAPE.update({c: f"\\x{c - 0xdc00:02x}" for c in range(0xdc80, 0xdd00)})
_ESCAPE.update({c: f"\\u{c:04x}" for c in (*range(0x80, 0xa0), 0x061C, 0x200E, 0x200F,
                                            *range(0x202A, 0x202F), *range(0x2066, 0x206A),
                                            0x2028, 0x2029)})
_UNESCAPE = re.compile(r"\\(?:(\\)|([tnr])|x([0-9a-f]{2})|u([0-9a-f]{4}))")


def cell(text):
    """`text` as one TSV cell that uncell() turns back into exactly `text`."""
    return text.translate(_ESCAPE)


def uncell(text):
    def one(m):
        if m.group(1):
            return "\\"
        if m.group(2):
            return {"t": "\t", "n": "\n", "r": "\r"}[m.group(2)]
        if m.group(3):
            n = int(m.group(3), 16)
            return chr(n) if n < 0x80 else chr(0xdc00 + n)
        return chr(int(m.group(4), 16))
    return _UNESCAPE.sub(one, text)


_SURROGATE = re.compile("[\ud800-\udfff]")


def json_text(value, indent=None):
    """JSON for `value`, readable UTF-8, in which names that aren't valid UTF-8
    still round-trip exactly: Python holds their bytes as lone surrogates
    (\\udcff for the byte 0xff), which are written as \\u escapes."""
    seps = None if indent else (",", ":")
    text = json.dumps(value, ensure_ascii=False, indent=indent, separators=seps)
    return _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def why(err):
    """Short human reason for an OSError."""
    return err.strerror or str(err)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", re.IGNORECASE)


def size_from_text(text):
    """'4096' -> 4096, '500K' -> 512000, '1.5G' -> 1610612736 (1K = 1024).
    Raises ValueError for anything else."""
    m = _SIZE_RE.fullmatch(text)
    if not m:
        raise ValueError(f"invalid size {text!r} (examples: 0, 4096, 500K, 10M, 2G)")
    unit = m.group(2).lower()
    return int(float(m.group(1)) * 1024 ** " kmgt".index(unit or " "))


class Progress:
    """One self-overwriting status line on stderr. Silent unless stderr is a
    terminal, so pipes and log files only ever get the real output.

    With CLEAN_PROGRESS=lines (how `clean ui` runs its jobs) each update is a
    line of its own instead, starting with LINE_MARK, and clearing it is an
    empty one, so the dashboard can tell them from the real output."""

    LINE_MARK = "\x01P "

    def __init__(self, quiet):
        self.lines = os.environ.get("CLEAN_PROGRESS") == "lines"
        self.enabled = not quiet and (self.lines or sys.stderr.isatty())
        self.count = 0          # shared counter the walkers bump (display only)
        self.bytes = 0
        self.total_bytes = 0
        self._next = 0.0
        self._shown = False
        self._lock = threading.Lock()

    def due(self):
        """Cheap check, called per item: is it time to redraw?"""
        return self.enabled and time.monotonic() >= self._next

    def show(self, text):
        if not self.enabled:
            return
        with self._lock:
            self._next = time.monotonic() + 0.1
            if self.lines:
                sys.stderr.write(self.LINE_MARK + text.replace("\n", " ") + "\n")
                sys.stderr.flush()
                self._shown = True
                return
            width = shutil.get_terminal_size().columns - 1
            if width < 1:               # a terminal that doesn't know its size
                width = 79
            sys.stderr.write("\r\033[K" + text[:width])
            sys.stderr.flush()
            self._shown = True

    def clear(self):
        with self._lock:
            if self._shown:
                sys.stderr.write(self.LINE_MARK + "\n" if self.lines else "\r\033[K")
                sys.stderr.flush()
                self._shown = False


# ---------------------------------------------------------------- threads

def parallel_map(fn, items, jobs):
    """[fn(x) for x in items], on `jobs` threads when jobs > 1. Results come back
    in input order. On Ctrl-C the workers are told to stop and are waited for,
    so no thread is left touching the disk after this returns."""
    if jobs <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    ex = ThreadPoolExecutor(max_workers=jobs)
    try:
        return list(ex.map(fn, items))
    except BaseException:
        CANCEL.set()
        raise
    finally:
        try:
            ex.shutdown(wait=True, cancel_futures=True)
        except TypeError:  # Python < 3.9: queued items start, see CANCEL, quit
            ex.shutdown(wait=True)


def ordered_map(fn, items, jobs, window=None):
    """fn(x) for each x in items, as a generator yielding results in input
    order, on `jobs` threads when jobs > 1. At most `window` calls run or wait
    ahead of the consumer, so results don't pile up in memory. An error in fn
    (or Ctrl-C in the consumer) stops the workers and is waited for, as in
    parallel_map."""
    if jobs <= 1:
        for x in items:
            yield fn(x)
        return
    window = window or jobs * 4
    ex = ThreadPoolExecutor(max_workers=jobs)
    pending = deque()
    try:
        for x in items:
            pending.append(ex.submit(fn, x))
            if len(pending) >= window:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()
    except GeneratorExit:
        raise       # the consumer stopped early: not an error
    except BaseException:
        CANCEL.set()
        raise
    finally:
        for f in pending:
            f.cancel()
        ex.shutdown(wait=True)
