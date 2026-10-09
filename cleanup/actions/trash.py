"""
The Trash: the freedesktop.org one, which the Files app, `gio trash` and
every desktop share, so things clean moves there can be seen and restored
from any of them.

    ~/.local/share/Trash/        for files on the same drive as your home
        files/photo.jpg          the item itself, moved here in one rename
        info/photo.jpg.trashinfo where it came from, and when
    /media/me/DRIVE/.Trash-1000/ for files on another drive: the same,
                                 at the top of that drive

An item always stays on its own drive: a move to the Trash is a rename, so
even a folder of 100 GB goes in one step, and nothing is ever copied and then
deleted. Where a drive has no usable Trash (read-only, no permission), the
move fails with the reason; it never falls back to deleting.

The .trashinfo file is written first, and only if no file of that name
exists (O_EXCL), then the item is renamed in under the same name: two
programs trashing at once can't take each other's name, and a crash in
between leaves a harmless info file without an item.
"""

import os
import re
import shutil
import stat
import time
from datetime import datetime
from urllib.parse import quote_from_bytes, unquote_to_bytes

from cleanup import volumes
from cleanup.fsutil import is_within, printable, rename_noreplace, why

INFO_SUFFIX = ".trashinfo"
MAX_TRIES = 10000

# Filesystems that never hold a Trash worth looking at.
_PSEUDO = frozenset((
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "securityfs", "debugfs",
    "tracefs", "pstore", "bpf", "mqueue", "hugetlbfs", "configfs", "fusectl", "autofs",
    "binfmt_misc", "efivarfs", "nsfs", "squashfs", "rpc_pipefs", "fuse.portal",
    "fuse.gvfsd-fuse"))
_SKIP_POINTS = ("/proc", "/sys", "/dev", "/snap", "/boot/efi")


class TrashError(Exception):
    """This item can't be moved to the Trash: the message says why."""


def home_trash_path():
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(os.path.abspath(data), "Trash")


class Trash:
    """One Trash folder: files/ and info/. `top` is the folder that relative
    paths in its .trashinfo files start from (the drive's mount point), or
    None for the home Trash, which stores full paths."""

    def __init__(self, path, top, mount=None):
        self.path = path
        self.top = top
        self.mount = mount          # volumes.Mount of its drive, when known
        self.files = os.path.join(path, "files")
        self.info = os.path.join(path, "info")

    def __repr__(self):
        return f"Trash({self.path!r})"

    @property
    def is_home(self):
        return self.top is None

    # ------------------------------------------------------------ putting

    def put(self, path, before_move=None):
        """Move `path` (a file or a whole folder) here. Returns its Entry.
        Raises TrashError, or OSError from the rename itself.

        before_move(entry) runs once the name in the Trash is reserved (its
        .trashinfo written), just before the move: a log written there
        always names where the item went, even if the process dies next."""
        st = os.lstat(path)
        here = os.lstat(self.files)
        if st.st_dev != here.st_dev:
            raise TrashError(f"its Trash ({printable(self.path)}) is on another drive")
        original = os.path.abspath(path)
        stored = original
        if self.top is not None:
            if not is_within(original, self.top):
                raise TrashError(f"it is not on the drive of {printable(self.path)}")
            stored = os.path.relpath(original, self.top)
        text = ("[Trash Info]\nPath=" + quote_from_bytes(os.fsencode(stored), safe="/")
                + "\nDeletionDate=" + datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "\n")
        base = os.path.basename(original.rstrip("/"))
        for n in range(1, MAX_TRIES):
            name = _fit(base, n, self.info)
            info = os.path.join(self.info, name + INFO_SUFFIX)
            try:
                fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            except FileExistsError:
                continue
            try:
                os.write(fd, text.encode("utf-8"))
                os.fsync(fd)
            except OSError:
                os.close(fd)
                _unlink_quietly(info)
                raise
            os.close(fd)
            target = os.path.join(self.files, name)
            entry = Entry(self, name, original, text_date(text))
            try:
                if before_move is not None:
                    before_move(entry)
                rename_noreplace(original, target)
            except FileExistsError:
                if os.path.lexists(original):
                    _unlink_quietly(info)    # an orphan item holds the name: next one
                    continue
                _unlink_quietly(info)
                raise
            except BaseException:
                _unlink_quietly(info)
                raise
            return entry
        raise TrashError("no free name in the Trash")

    # ------------------------------------------------------------ reading

    def entries(self):
        """Every item in this Trash (and info files whose item is missing,
        with .missing set), oldest first."""
        try:
            names = os.listdir(self.info)
        except OSError:
            return []
        out = []
        for info_name in names:
            if not info_name.endswith(INFO_SUFFIX) or info_name == INFO_SUFFIX:
                continue
            name = info_name[:-len(INFO_SUFFIX)]
            try:
                with open(os.path.join(self.info, info_name), "rb") as f:
                    raw = f.read(65536)
            except OSError:
                continue
            path, date = parse_info(raw)
            if path is None:
                continue
            if not path.startswith("/"):
                if self.top is None:
                    continue    # the home Trash stores full paths only
                path = os.path.normpath(os.path.join(self.top, path))
            out.append(Entry(self, name, path, date))
        out.sort(key=lambda e: (e.deleted or "", e.name))
        return out

    def entry(self, name):
        """The item called `name` in files/, or None."""
        for e in self.entries():
            if e.name == name:
                return e
        return None


class Entry:
    """One item in a Trash."""

    def __init__(self, trash, name, original, deleted):
        self.trash = trash
        self.name = name                # its name in files/
        self.original = original        # where it came from (full path)
        self.deleted = deleted          # "2026-10-07T21:15:00", or None

    def __repr__(self):
        return f"Entry({self.name!r} from {self.original!r})"

    @property
    def file(self):
        return os.path.join(self.trash.files, self.name)

    @property
    def info(self):
        return os.path.join(self.trash.info, self.name + INFO_SUFFIX)

    @property
    def missing(self):
        return not os.path.lexists(self.file)

    @property
    def deleted_ts(self):
        try:
            return datetime.strptime(self.deleted, "%Y-%m-%dT%H:%M:%S").timestamp()
        except (TypeError, ValueError):
            return None

    def size(self):
        """(bytes, files) of the item; each file on disk counted once."""
        return tree_size(self.file)

    def restore(self, to=None):
        """Move the item back to where it came from (or to `to`), creating
        missing parent folders. Never replaces anything: FileExistsError if
        something is there again. Returns the path it went to."""
        dest = to or self.original
        if os.path.lexists(dest):
            raise FileExistsError(17, "something is there again", dest)
        os.lstat(self.file)     # gone (with its drive?): no folders are made for it
        if not os.path.isdir(os.path.dirname(dest)):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        rename_noreplace(self.file, dest)
        _unlink_quietly(self.info)
        return dest

    def delete(self):
        """Delete the item for good, then its info file."""
        if os.path.lexists(self.file):
            remove_tree(self.file)
        _unlink_quietly(self.info)


# ---------------------------------------------------------------- finding

def for_path(path, vols=None, create=True):
    """The Trash that `path` goes to: the home Trash for a file on the same
    drive as it, else the Trash at the top of the file's drive. Creates it
    when needed (and `create`). Raises TrashError when there is none."""
    st = os.lstat(path)
    home = home_trash_path()
    probe = home
    while not os.path.lexists(probe):
        probe = os.path.dirname(probe)
    try:
        home_dev = os.stat(probe).st_dev
    except OSError:
        home_dev = None
    if st.st_dev == home_dev:
        try:
            if create:
                os.makedirs(os.path.dirname(home), exist_ok=True)
                _make_trash(home)
        except OSError as e:
            raise TrashError(f"cannot use {printable(home)}: {why(e)}")
        return Trash(home, None)
    vols = vols or volumes.Volumes()
    mount = vols.find_mount(os.path.realpath(os.path.dirname(os.path.abspath(path))))
    if mount is None:
        raise TrashError("could not tell which drive it is on")
    top = mount.point
    errors = []
    shared = os.path.join(top, ".Trash")
    uid = str(os.getuid())
    try:
        sst = os.lstat(shared)
    except FileNotFoundError:
        sst = None
    except OSError as e:
        sst = None
        errors.append(f"{printable(shared)}: {why(e)}")
    if sst is not None:
        if stat.S_ISDIR(sst.st_mode) and sst.st_mode & stat.S_ISVTX:
            try:
                t = Trash(os.path.join(shared, uid), top, mount)
                if create:
                    _make_trash(t.path)
                if _usable(t, st.st_dev):
                    return t
            except OSError as e:
                errors.append(f"{printable(os.path.join(shared, uid))}: {why(e)}")
        # (a .Trash that is a link, a file or not sticky must not be used)
    t = Trash(os.path.join(top, f".Trash-{uid}"), top, mount)
    try:
        if create:
            _make_trash(t.path)
        if _usable(t, st.st_dev):
            return t
        errors.append(f"{printable(t.path)} is not usable")
    except OSError as e:
        errors.append(f"cannot create {printable(t.path)}: {why(e)}")
    raise TrashError("this drive has no usable Trash (" + "; ".join(errors) + ")")


def _make_trash(path):
    """Create a Trash folder and its files/ and info/, owner-only. Refuses
    any part that exists but is a symlink or not a folder."""
    for p in (path, os.path.join(path, "files"), os.path.join(path, "info")):
        try:
            os.mkdir(p, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(p)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(20, "exists but is not a folder (or is a symlink)", p)


def _usable(t, dev):
    """Is t a real Trash folder of ours, on device `dev`?"""
    try:
        for p in (t.path, t.files, t.info):
            st = os.lstat(p)
            if not stat.S_ISDIR(st.st_mode) or st.st_dev != dev:
                return False
        return os.lstat(t.path).st_uid == os.getuid()
    except OSError:
        return False


def all_trashes(vols=None):
    """Every Trash that exists now: the home one, and those at the top of
    each mounted drive (yours: .Trash-UID and .Trash/UID)."""
    out, seen = [], set()
    home = home_trash_path()
    if os.path.isdir(os.path.join(home, "info")):
        out.append(Trash(home, None))
        seen.add(os.path.realpath(home))
    try:
        vols = vols or volumes.Volumes()
    except (OSError, ValueError):
        return out
    uid = str(os.getuid())
    for m in vols.mounts:
        if (m.fstype in _PSEUDO or any(is_within(m.point, p) for p in _SKIP_POINTS)
                or vols.find_mount(m.point) is not m):
            continue
        for path in (os.path.join(m.point, ".Trash", uid), os.path.join(m.point, f".Trash-{uid}")):
            try:
                st = os.lstat(path)
                info = os.lstat(os.path.join(path, "info"))
            except OSError:
                continue
            real = os.path.realpath(path)
            if stat.S_ISDIR(st.st_mode) and stat.S_ISDIR(info.st_mode) and real not in seen:
                seen.add(real)
                out.append(Trash(path, m.point, m))
    return out


# ---------------------------------------------------------------- helpers

def parse_info(raw):
    """(full or relative path, date) from the bytes of a .trashinfo file;
    (None, None) if it isn't one."""
    text = raw.decode("utf-8", "replace")
    if not re.search(r"^\[Trash Info\]\s*$", text, re.M):
        return None, None
    path = date = None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
            continue
        if section != "[Trash Info]" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "Path" and path is None:
            path = os.fsdecode(unquote_to_bytes(value))
        elif key == "DeletionDate" and date is None:
            date = value
    return (path or None), date


def text_date(text):
    m = re.search(r"^DeletionDate=(.*)$", text, re.M)
    return m.group(1) if m else None


def _fit(base, n, folder):
    """The n-th name to try for `base` in a Trash: base, base.2, base.3...,
    shortened (keeping its extension) so that it and its .trashinfo file
    fit in a file name."""
    suffix = "" if n == 1 else f".{n}"
    try:
        limit = os.pathconf(folder, "PC_NAME_MAX")
    except (OSError, ValueError):
        limit = 255
    room = limit - len(INFO_SUFFIX.encode()) - len(suffix.encode())
    raw = os.fsencode(base)
    if len(raw) <= room:
        return base + suffix
    stem, ext = os.path.splitext(base)
    ext_raw = os.fsencode(ext)
    if len(ext_raw) > room // 2:
        ext_raw = b""
    stem_raw = os.fsencode(stem)
    cut = stem_raw[:room - len(ext_raw)]
    try:
        stem_raw.decode("utf-8")
        # Don't cut a character in half (exFAT refuses invalid names).
        cut = cut.decode("utf-8", "ignore").encode("utf-8")
    except UnicodeDecodeError:
        pass
    return os.fsdecode(cut + ext_raw) + suffix


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def tree_size(path):
    """(bytes, files) under path, without following symlinks; a file with
    several names inside counts once."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1
    total, count, seen = 0, 0, set()
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                fst = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if fst.st_nlink > 1:
                key = (fst.st_dev, fst.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            total += fst.st_size
            count += 1
    return total, count


def remove_tree(path):
    """Delete a file or a whole folder, like `rm -rf`: read-only folders
    inside are made writable first. Never follows symlinks."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return

    def retry(func, p, exc_info):
        err = exc_info[1]
        if not isinstance(err, PermissionError):
            raise err
        parent = os.path.dirname(p)
        os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
        if os.path.isdir(p) and not os.path.islink(p):
            os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | stat.S_IRWXU)
            shutil.rmtree(p, onerror=retry)
        else:
            func(p)
    if not os.access(path, os.W_OK | os.X_OK):
        os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
    shutil.rmtree(path, onerror=retry)


def age_days(entry, now=None):
    ts = entry.deleted_ts
    return None if ts is None else ((now or time.time()) - ts) / 86400
