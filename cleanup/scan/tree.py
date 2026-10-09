"""
The scanner: every folder and file under one or more roots, held in compact
parallel lists rather than one object per file, so a drive with 700,000 files
fits in a few hundred MB.

Folders are numbered in depth-first order (preorder), children by name, so a
folder's whole subtree is the range [i, end[i]) of folder numbers and its
files are one contiguous range of file numbers too: "everything inside X" is
a slice, never a search.

A folder is *tainted* when it holds something the scan can't vouch for: an
excluded entry, a symlink, a device or pipe, something unreadable, another
drive's mount point (with -x), a file left out by a filter, or subfolders
beyond --max-depth. A tainted folder (and every folder above it) is never
called a copy of anything as a whole; its files are still compared one by
one. Junk files (Thumbs.db, .DS_Store, ...) are simply ignored and don't taint.
Hidden files and folders are scanned like any other.
"""

import os
import stat
from collections import Counter

from cleanup.errors import Cancelled
from cleanup.fsutil import CANCEL, ordered_map, printable, why
from cleanup.scan.filters import is_trash

# Taint bits (a folder's own reasons; ancestors inherit them in dupes/folders.py)
EXCLUDED = 1        # an entry skipped by -e, the default excludes or the Trash
SYMLINK = 2
SPECIAL = 4         # device, pipe, socket
UNREADABLE = 8      # a folder or file that could not be read
MOUNT = 16          # another drive mounted inside, not entered (-x)
FILTERED = 32       # a file left out by --type, --min-size, --name, ...
DEPTH = 64          # subfolders not entered (--max-depth)
TAINT = 127
HIDDEN = 128        # not a taint: the folder or one above it starts with "."

TAINT_NAMES = ((EXCLUDED, "an excluded entry"), (SYMLINK, "a symlink"),
               (SPECIAL, "a device, pipe or socket"), (UNREADABLE, "something unreadable"),
               (MOUNT, "another drive mounted inside"), (FILTERED, "files left out by the filters"),
               (DEPTH, "folders beyond --max-depth"))

# What was left out, for the summary ("Skipped: 12 junk files, 3 symlinks").
SKIP_LABELS = (("junk", "junk file", "junk files"), ("excluded", "excluded entry", "excluded entries"),
               ("trash", "Trash folder", "Trash folders"), ("filtered", "filtered-out file",
                                                            "filtered-out files"),
               ("symlinks", "symlink", "symlinks"), ("special", "device/pipe/socket",
                                                     "devices/pipes/sockets"),
               ("mounts", "other drive", "other drives"), ("too_deep", "folder beyond --max-depth",
                                                          "folders beyond --max-depth"))


class Tree:
    """The scan result. Folder i: parent[i] (-1 for a root), name[i] (a root
    holds its full path), depth[i], root[i] (which root, in the order given),
    flags[i], ffirst[i] / nfiles[i] (its own files), end[i] (one past its
    subtree). File j: fdir[j], fname[j], size[j], mtime[j] (ns), ino[j],
    dev[j] (index into devs), nlink[j]."""

    def __init__(self, roots):
        self.roots = list(roots)
        self.parent, self.name, self.depth, self.root = [], [], [], []
        self.flags, self.ffirst, self.nfiles, self.end = [], [], [], []
        self.fdir, self.fname, self.size, self.mtime = [], [], [], []
        self.ino, self.dev, self.nlink = [], [], []
        self.devs = []              # st_dev values; files store an index
        self.skipped = Counter()
        self.errors = []            # [(path, message)]
        self.root_index = []        # folder number of each root
        self._paths = None

    @property
    def nfolders(self):
        return len(self.parent)

    @property
    def nfiles_total(self):
        return len(self.fdir)

    def files_of(self, i):
        """range of the file numbers anywhere inside folder i."""
        end = self.end[i]
        return range(self.ffirst[i], self.ffirst[end] if end < len(self.ffirst) else len(self.fdir))

    def path(self, i):
        """Folder i's full path."""
        if self._paths is not None:
            return self._paths[i]
        parts = []
        while i >= 0:
            parts.append(self.name[i])
            i = self.parent[i]
        return "/".join(reversed(parts))

    def paths(self):
        """Every folder's full path (built once, then kept)."""
        if self._paths is None:
            out = []
            for i, p in enumerate(self.parent):
                out.append(self.name[i] if p < 0 else
                           (out[p].rstrip("/") + "/" + self.name[i]))
            self._paths = out
        return self._paths

    def file_path(self, j):
        return self.path(self.fdir[j]).rstrip("/") + "/" + self.fname[j]

    def visible(self, i):
        return not self.flags[i] & HIDDEN

    def file_visible(self, j):
        return not (self.flags[self.fdir[j]] & HIDDEN) and not self.fname[j].startswith(".")

    def taint_reasons(self, flags):
        return [text for bit, text in TAINT_NAMES if flags & bit]


class _Part:
    """One subtree's folders and files, numbered from 0; grafted into the
    Tree afterwards. Folder 0's parent is -1 (it hangs under the caller)."""

    def __init__(self):
        self.parent, self.name, self.depth, self.flags = [], [], [], []
        self.ffirst, self.nfiles = [], []
        self.fname, self.size, self.mtime, self.ino, self.dev, self.nlink = [], [], [], [], [], []
        self.skipped = Counter()
        self.errors = []
        self.devs_index = {}        # st_dev -> index in this part


class Scanner:
    """Settings shared by the walker threads (read-only) and progress."""

    def __init__(self, excluded, one_file_system, filters, junk, max_depth, progress, watch):
        self.excluded = excluded            # fsutil.Excluder
        self.one_file_system = one_file_system
        self.filters = filters              # scan.filters.Filters
        self.junk = junk                    # scan.filters.Junk
        self.max_depth = max_depth          # None: no limit
        self.progress = progress
        self.watch = watch                  # stops the run if the drive goes away

    def _list(self, path, depth, dev, flags, part):
        """Read one folder's files into `part`; returns its subfolders as
        [(name, path, dev, flags)] sorted by name, its own taint and how many
        files it holds."""
        files, subdirs = [], []
        own = 0
        try:
            with os.scandir(path) as it:
                entries = list(it)
            self.watch.ok()
        except OSError as e:
            part.errors.append((path, f"cannot read folder: {why(e)}"))
            self.watch.failed(e)
            return [], UNREADABLE, 0
        enter = self.max_depth is None or depth < self.max_depth
        hidden = flags & HIDDEN
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if is_trash(name, entry.path):
                        part.skipped["trash"] += 1
                        own |= EXCLUDED
                        continue
                    if self.excluded(name, entry.path):
                        part.skipped["excluded"] += 1
                        own |= EXCLUDED
                        continue
                    if not enter:
                        part.skipped["too_deep"] += 1
                        own |= DEPTH
                        continue
                    child_dev = dev
                    if self.one_file_system:
                        child_dev = entry.stat(follow_symlinks=False).st_dev
                        if child_dev != dev:
                            part.skipped["mounts"] += 1
                            own |= MOUNT
                            continue
                    subdirs.append((name, entry.path, child_dev,
                                    HIDDEN if hidden or name.startswith(".") else 0))
                elif entry.is_file(follow_symlinks=False):
                    if self.junk(name):
                        part.skipped["junk"] += 1
                        continue
                    if self.excluded(name, entry.path):
                        part.skipped["excluded"] += 1
                        own |= EXCLUDED
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if st.st_size and not self.filters.file_ok(name, st.st_size, st.st_mtime_ns):
                        part.skipped["filtered"] += 1
                        own |= FILTERED
                        continue
                    files.append((name, st))
                elif entry.is_symlink():
                    part.skipped["symlinks"] += 1
                    own |= SYMLINK
                else:
                    # A device, pipe or socket; or something that vanished
                    # between the listing and this check.
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode):
                        raise OSError(0, "changed while being scanned")
                    part.skipped["special"] += 1
                    own |= SPECIAL
            except FileNotFoundError:
                continue    # deleted while being scanned: it simply isn't there
            except OSError as e:
                part.errors.append((entry.path, f"cannot inspect: {why(e)}"))
                own |= UNREADABLE
                if e.errno:
                    self.watch.failed(e)
        files.sort(key=lambda f: f[0])
        devs = part.devs_index
        for name, st in files:
            part.fname.append(name)
            part.size.append(st.st_size)
            part.mtime.append(st.st_mtime_ns)
            part.ino.append(st.st_ino)
            d = devs.get(st.st_dev)
            if d is None:
                d = devs[st.st_dev] = len(devs)
            part.dev.append(d)
            part.nlink.append(st.st_nlink)
        subdirs.sort(key=lambda s: s[0])
        return subdirs, own, len(files)

    def walk(self, start):
        """Every folder and file under `start` = (name, path, depth, dev,
        flags), as a _Part. Explicit stack in preorder, so any depth works and
        a subtree's folders and files come out contiguous."""
        name, path, depth, dev, flags = start
        part = _Part()
        stack = [(-1, name, path, depth, dev, flags)]
        progress = self.progress
        while stack:
            if CANCEL.is_set():
                raise Cancelled
            parent, name, path, depth, dev, flags = stack.pop()
            i = len(part.parent)
            part.parent.append(parent)
            part.name.append(name)
            part.depth.append(depth)
            part.ffirst.append(len(part.fname))
            subdirs, own, count = self._list(path, depth, dev, flags, part)
            part.flags.append(flags | own)
            part.nfiles.append(count)
            for sub in reversed(subdirs):
                cname, cpath, cdev, cflags = sub
                stack.append((i, cname, cpath, depth + 1, cdev, cflags))
            progress.count += 1
            if progress.due():
                progress.show(f"scanning: {progress.count:,} folders  {printable(path)}")
        return part

    def scan(self, roots, jobs):
        """Scan every root (in the order given); returns a Tree. Each root's
        top-level subfolders are walked in parallel with jobs > 1."""
        tree = Tree(roots)
        for r, root in enumerate(roots):
            st = os.stat(root)
            # The root's own entries first, then each subfolder as a shard.
            top = _Part()
            subdirs, own, count = self._list(root, 0, st.st_dev, 0, top)
            top.parent.append(-1)
            top.name.append(root)
            top.depth.append(0)
            top.flags.append(own)
            top.ffirst.append(0)
            top.nfiles.append(count)
            rindex = self._graft(tree, top, -1, r)
            tree.root_index.append(rindex)
            starts = [(name, path, 1, dev, flags) for name, path, dev, flags in subdirs]
            for part in ordered_map(self.walk, starts, jobs, window=jobs * 2):
                self._graft(tree, part, rindex, r)
        _ends(tree)
        return tree

    @staticmethod
    def _graft(tree, part, parent, root):
        """Append `part` to `tree` under folder `parent`; returns the number
        its first folder got."""
        base, fbase = len(tree.parent), len(tree.fdir)
        devmap = {}
        for st_dev, d in part.devs_index.items():
            try:
                devmap[d] = tree.devs.index(st_dev)
            except ValueError:
                devmap[d] = len(tree.devs)
                tree.devs.append(st_dev)
        tree.parent.extend(parent if p < 0 else p + base for p in part.parent)
        tree.name.extend(part.name)
        tree.depth.extend(part.depth)
        tree.root.extend([root] * len(part.parent))
        tree.flags.extend(part.flags)
        tree.ffirst.extend(f + fbase for f in part.ffirst)
        tree.nfiles.extend(part.nfiles)
        for i, n in enumerate(part.nfiles):
            tree.fdir.extend([base + i] * n)
        tree.fname.extend(part.fname)
        tree.size.extend(part.size)
        tree.mtime.extend(part.mtime)
        tree.ino.extend(part.ino)
        tree.dev.extend(devmap[d] for d in part.dev)
        tree.nlink.extend(part.nlink)
        tree.skipped.update(part.skipped)
        tree.errors.extend(part.errors)
        return base


def _ends(tree):
    """end[i] = one past the last folder of i's subtree. Children come after
    their parent in preorder, so one backwards pass adds up subtree sizes."""
    n = tree.nfolders
    count = [1] * n
    parent = tree.parent
    for i in range(n - 1, -1, -1):
        p = parent[i]
        if p >= 0:
            count[p] += count[i]
    tree.end = [i + count[i] for i in range(n)]
