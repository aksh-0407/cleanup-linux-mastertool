"""
The checks `clean remove` makes just before it touches anything:

- the copy about to go is still exactly what the scan saw: the same kind of
  file, size and modification time, and its whole content is read again and
  must hash the same (a cached hash is never trusted for this);
- a folder about to go holds nothing the scan didn't see (junk aside);
- some other copy of each content is still there and verified the same way,
  and is a different file on disk, not a second view of the same one (a bind
  mount or a hard link would vanish together with it).
"""

import errno
import hashlib
import os
import stat

from cleanup.fsutil import why
from cleanup.volumes import mount_points

FULL_DIGEST = 64            # blake2b-512, as the scan's full hashes
CHUNK = 1 << 20


class Gone(Exception):
    """It isn't there any more (removed by an earlier run, or by hand)."""


class Bad(Exception):
    """It isn't what the scan saw; the message says how."""


class Stopped(Exception):
    """Ctrl-C (or kill) while reading: the item is left as it is."""


def _kind(mode):
    if stat.S_ISDIR(mode):
        return "a folder"
    if stat.S_ISLNK(mode):
        return "a symlink"
    if stat.S_ISREG(mode):
        return "a file"
    return "a special file"


def read_file(path, size, mtime_ns, want=None, stop=None):
    """Read the whole file at `path` and return (stat, hex digest).

    Raises Gone if nothing is there, and Bad if it isn't a regular file of
    `size` bytes last modified at `mtime_ns`, or its content doesn't hash to
    `want` (hex) when that is given. Symlinks are never followed. Other
    OSErrors (unreadable, the drive failing) are raised as they are."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Gone
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Bad("it is now a symlink")
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Bad(f"it is now {_kind(st.st_mode)}")
        if st.st_size != size:
            raise Bad(f"its size changed since the scan ({size} -> {st.st_size} bytes)")
        if st.st_mtime_ns != mtime_ns:
            raise Bad("it was modified since the scan")
        h = hashlib.blake2b(digest_size=FULL_DIGEST)
        big = size > CHUNK
        if big:
            _advise(fd, "SEQUENTIAL")
        buf = bytearray(min(max(size, 1), CHUNK))
        view = memoryview(buf)
        seen = 0
        while True:
            if stop is not None and stop():
                raise Stopped
            n = os.readv(fd, [view])
            if not n:
                break
            h.update(view[:n])
            seen += n
        if big:
            _advise(fd, "DONTNEED")     # don't push everything else out of RAM
        after = os.fstat(fd)
        if seen != size or after.st_size != size or after.st_mtime_ns != mtime_ns:
            raise Bad("it changed while being checked")
    finally:
        os.close(fd)
    digest = h.hexdigest()
    if want is not None and digest != want:
        raise Bad("its content changed since the scan, though its size and date did not")
    return st, digest


def _advise(fd, what):
    try:
        os.posix_fadvise(fd, 0, 0, getattr(os, "POSIX_FADV_" + what))
    except (AttributeError, OSError):
        pass


def same_file(a, b):
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


MOUNTED_HERE = ("it is a mount point (a drive, or another folder, is mounted here): it is "
                "never removed as a whole")


def mounted_inside(point):
    return f"it holds a mount point, {point}: it is never removed as a whole"


class Folder:
    """A folder as it is now, checked against the scan: `present` [(file,
    path, stat, digest)] verified, `gone` [file] no longer there, `junk`
    [path] junk files (removed with the folder), `dirs` [path] deepest
    first, `linked` [(file, path, kept copy)] already hard links to a copy
    that stays (only when asked)."""

    def __init__(self):
        self.present, self.gone, self.junk, self.dirs = [], [], [], []
        self.linked = []
        self.top = None             # the folder's own stat


def check_folder(m, d, path, stop=None, linked=None):
    """List folder `d` (at `path`) again, all the way down, and check it
    against the manifest `m`. Raises Gone, Bad (something new appeared, or a
    file changed, or something is mounted at or inside it) or Stopped. With
    `linked(file, path, stat)`, a file it returns a kept copy for is already
    a hard link to that copy: it goes in `linked` instead of being checked
    against the scan."""
    try:
        top = os.lstat(path)
    except FileNotFoundError:
        raise Gone
    if not stat.S_ISDIR(top.st_mode):
        raise Bad(f"it is now {_kind(top.st_mode)}")
    mounts = mount_points()
    if os.path.ismount(path) or path.rstrip("/") in mounts:
        raise Bad(MOUNTED_HERE)
    inside = next((p for p in sorted(mounts) if p.startswith(path.rstrip("/") + "/")), None)
    if inside is not None:
        raise Bad(mounted_inside(inside))
    base = m.paths[d].rstrip("/") + "/"
    dirs = {m.paths[k][len(base):] if k != d else "" for k in m.subtree(d)}
    files = {m.file_path(f)[len(base):]: f for f in m.files_in(d)}
    junk = {n.lower() for n in m.junk}
    out = Folder()
    out.top = top
    found = []
    stack = [("", path)]
    while stack:
        rel, where = stack.pop()
        out.dirs.append(where)
        try:
            entries = list(os.scandir(where))
        except FileNotFoundError:
            if rel:
                continue                # a subfolder went: its files show as gone
            raise Gone
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev != top.st_dev:
                    raise Bad(mounted_inside(e.path))
                if r not in dirs:
                    raise Bad(f"{r}/ is new since the scan")
                stack.append((r, e.path))
            elif stat.S_ISREG(st.st_mode) and r in files:
                found.append((files[r], e.path, st))
            elif stat.S_ISREG(st.st_mode) and e.name.lower() in junk:
                out.junk.append(e.path)
            elif stat.S_ISREG(st.st_mode):
                raise Bad(f"{r} is new since the scan")
            else:
                raise Bad(f"{r} is {_kind(st.st_mode)} the scan didn't see")
    seen = {f for f, _, _ in found}
    out.gone = [f for f in files.values() if f not in seen]
    for f, p, st in sorted(found, key=lambda x: x[1]):
        keep = linked(f, p, st) if linked is not None else None
        if keep is not None:
            out.linked.append((f, p, keep))
            continue
        entry = m.files[f]
        want = m.hashes[entry[4]] if entry[4] else None
        try:
            st, digest = read_file(p, entry[2], entry[3], want, stop)
        except Gone:
            out.gone.append(f)
            continue
        except Bad as e:
            raise Bad(f"{p[len(path):].lstrip('/')}: {e}")
        out.present.append((f, p, st, digest))
    out.dirs.sort(key=lambda p: p.count("/"), reverse=True)
    return out


class Survivors:
    """Finds, for a content, a copy that stays and checks it as above. A
    verified copy is remembered for the rest of the run; one that fails is
    never tried again."""

    def __init__(self, m, plan, stop=None, unavailable=()):
        self.m = m
        self.plan = plan
        self.stop = stop
        self.unavailable = tuple(r.rstrip("/") + "/" for r in unavailable)
        self.good = {}          # content -> [(file, path, stat)]
        self.failed = {}        # file -> why it can't be relied on

    def check(self, c, f):
        """(path, stat) of survivor `f` of content `c` if it verifies, else
        None; why it doesn't is kept in self.failed[f]."""
        for g, path, st in self.good.get(c, ()):
            if g == f:
                return path, st
        if f in self.failed:
            return None
        path = self.m.file_path(f)
        if self.unavailable and path.startswith(self.unavailable):
            self.failed[f] = "its drive is not connected"
            return None
        entry = self.m.files[f]
        try:
            st, _ = read_file(path, entry[2], entry[3], self.m.hashes[c], self.stop)
        except Gone:
            self.failed[f] = f"{path} is gone"
            return None
        except Bad as e:
            self.failed[f] = f"{path}: {e}"
            return None
        except OSError as e:
            self.failed[f] = f"{path}: {why(e)}"
            return None
        self.good.setdefault(c, []).append((f, path, st))
        return path, st

    def find(self, c, avoid, same_dev=None):
        """(file, path, stat) of a verified copy of content `c` that is none
        of the files `avoid` [(dev, ino)] and, with same_dev, is on that
        device. Raises Bad when there is none."""
        for f, path, st in self.good.get(c, ()):
            if (st.st_dev, st.st_ino) not in avoid and same_dev in (None, st.st_dev):
                return f, path, st
        last, elsewhere = None, False
        for f in self.plan.survivors(c):
            got = self.check(c, f)
            if got is None:
                last = self.failed[f]
                continue
            path, st = got
            if (st.st_dev, st.st_ino) in avoid:
                last = f"{path} is the same file on disk, seen through another path"
                continue
            if same_dev not in (None, st.st_dev):
                elsewhere = True
                continue
            return f, path, st
        name = os.path.basename(self.m.file_path(self.m.copies[c][0])) if self.m.copies[c] else "?"
        if elsewhere:
            raise Bad(f"the copies of {name} that stay are on another drive, and a hard link "
                      f"can't reach another drive" + (f" ({last})" if last else ""))
        raise Bad(f"no verified copy of {name} remains" + (f" ({last})" if last else ""))

    def linked_to(self, c, path, st):
        """(file, path, stat) of the verified copy of content `c` that stays
        and is the very file `st` at `path` under another name (a hard link
        to it, as an earlier --link run leaves), or None. The same name seen
        through a bind mount doesn't count: it would go along with `path`."""
        if not stat.S_ISREG(st.st_mode) or st.st_nlink < 2:
            return None
        for f in self.plan.survivors(c):
            other = self.m.file_path(f)
            try:
                if not same_file(os.lstat(other), st) or _same_entry(path, other):
                    continue
            except OSError:
                continue
            got = self.check(c, f)
            return None if got is None else (f, got[0], got[1])
        return None


def _same_entry(a, b):
    """Whether paths `a` and `b` are one directory entry (one name in one
    folder), reached through different mounts."""
    if os.path.basename(a) != os.path.basename(b):
        return False
    return same_file(os.lstat(os.path.dirname(a) or "."), os.lstat(os.path.dirname(b) or "."))
