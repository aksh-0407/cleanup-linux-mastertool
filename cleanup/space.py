"""
The space engine behind `clean space`: where the space goes under some
folders. It only reads: it lists folders and looks at each entry once
(lstat), and changes nothing.

Each PATH is walked without leaving its drive: another drive mounted inside
is listed, not counted (name it as a PATH of its own to include it). Sizes
are what files take on the disk (st_blocks), so a sparse file counts what it
really uses, and a file with several names (hard links) counts once, in the
first folder it is met in.

Along the way it notes clutter: zero-byte files, broken symlinks, temp
leftovers (*.tmp, ~$*, .~lock.*) and empty folders. Nothing is looked for
inside a Trash, inside .git and the like (programs need them as they are),
or inside clean's own data. Clutter inside the folders of programs and
packages (hidden folders, site-packages, node_modules, a conda or Python
environment) and marker files such as __init__.py or .gitkeep are counted
but not listed: they belong to the program that made them.
"""

import errno
import heapq
import os
import stat as _stat
import time

from cleanup.errors import Cancelled
from cleanup.fsutil import CANCEL, human, printable, why
from cleanup.scan.filters import is_trash

KEEP_MIN = 1 << 20          # folders this big or bigger are kept for the report
MAX_FOLDERS = 50_000        # ... at most: the bar rises if more qualify
TOP_FILES = 500             # biggest files kept
FOLDER_FILES = 10           # biggest files kept per folder, for the drill-down
FOLDER_FILE_MIN = 1 << 20   # ... if at least this big
LIST_MAX = 2000             # items kept per clutter list

# Folder flags (a folder's flags include those of everything inside it).
UNREADABLE = 1      # something could not be read: its size is at least this
MOUNTS = 2          # another drive is mounted inside (not counted)
EXCLUDED = 4        # something was left out by --exclude (not counted)

# How a folder is looked at for clutter.
LOOK = 0            # listed
APP = 1             # counted, not listed: a program's or package's own files
QUIET = 2           # not looked at: Trash, .git and the like, clean's data

# Empty files that mean something to the program that made them.
MARKERS = frozenset((
    "__init__.py", "__init__.pyi", "py.typed", "REQUESTED", "LOCK", "lock", "CACHEDIR.TAG",
    "nodejs-installed", "COLCON_IGNORE", "AMENT_IGNORE", "CATKIN_IGNORE"))
# Folders holding a program's or package's own files.
PACKAGE_DIRS = frozenset((
    "site-packages", "dist-packages", "node_modules", "__pycache__", "bower_components",
    "vendor", "snap", "build", "CMakeFiles"))
# A folder holding one of these is a whole environment (conda, venv).
ENV_SIGNS = ("conda-meta", "pyvenv.cfg")

TEMP_SUFFIXES = (".tmp", ".temp")
TEMP_PREFIXES = ("~$", ".~lock.")
RECENT_SECONDS = 24 * 3600   # a temp file changed this recently may be in use


def is_temp(name):
    low = name.lower()
    return low.endswith(TEMP_SUFFIXES) or name.startswith(TEMP_PREFIXES)


class Clutter:
    """One kind of clutter: how many, how big, and the first LIST_MAX that
    are listed (those outside program and package folders)."""

    def __init__(self):
        self.count = 0
        self.bytes = 0
        self.unlisted = 0       # in program or package folders, or markers
        self.items = []         # (folder index, name, extra...)
        self.more = 0           # listable but beyond LIST_MAX

    def add(self, listed, size, item):
        self.count += 1
        self.bytes += size
        if not listed:
            self.unlisted += 1
        elif len(self.items) < LIST_MAX:
            self.items.append(item)
        else:
            self.more += 1


class _NoWatch:
    def ok(self):
        pass

    def failed(self, err):
        pass


class Scan:
    """One read-only walk over some folders, and what it found."""

    def __init__(self, excluded, quiet_paths=(), quiet_names=(), progress=None, watch=None,
                 watch_paths=(), keep_paths=()):
        self.excluded = excluded            # fsutil.Excluder: left out, not counted
        self.quiet_paths = set(quiet_paths)  # clean's own folders
        self.quiet_names = set(quiet_names)  # .git and the like
        self.keep_paths = set(keep_paths)   # never called empty (Desktop, Templates...)
        self.progress = progress
        self.watch = watch or _NoWatch()    # safety.DriveWatch
        self.watch_paths = set(watch_paths)  # folders whose size is asked for (caches)
        self.watched = {}                   # path -> folder index
        # per folder, in walk order: a folder's parent comes before it
        self.parent, self.name, self.flags, self.look = [], [], [], []
        self.disk, self.size, self.files = [], [], []   # of its files only
        self.solo, self.shared = [], []     # bytes in files with one name / several
        self.content = []                   # something in it keeps it from being empty
        self.big = {}                       # folder -> its biggest files
        self.roots = []                     # (path, folder index, st_dev)
        self.root_paths = set()
        self.top = []                       # heap of (disk, size, folder, name, mtime)
        self.linked = set()                 # (dev, ino) of files with several names
        self.link_names = 0                 # names not counted again
        self.link_bytes = 0
        self.mounts = []                    # (path, folder index)
        self.excluded_n = 0
        self.errors = []                    # (path, message)
        self.zero = Clutter()
        self.broken = Clutter()
        self.temp = Clutter()
        self.seconds = 0.0
        self.now = time.time()
        self._seen_folders = 0
        self._bytes = 0

    # ------------------------------------------------------------ walking

    def run(self, roots):
        start = time.monotonic()
        self.root_paths = set(roots)
        try:
            for root in roots:
                self._walk(root)
        finally:
            self.seconds = time.monotonic() - start
        self._totals()

    def _new(self, parent, name, look):
        i = len(self.parent)
        self.parent.append(parent)
        self.name.append(name)
        self.flags.append(0)
        self.look.append(look)
        self.disk.append(0)
        self.size.append(0)
        self.files.append(0)
        self.solo.append(0)
        self.shared.append(0)
        self.content.append(False)
        return i

    def _walk(self, root):
        st = os.lstat(root)
        look = QUIET if root in self.quiet_paths else LOOK
        i = self._new(-1, root, look)
        self.disk[i] += st.st_blocks * 512
        self.roots.append((root, i, st.st_dev))
        if root in self.watch_paths:
            self.watched[root] = i
        stack = [(i, root)]
        dev = st.st_dev
        while stack:
            if CANCEL.is_set():
                raise Cancelled
            i, path = stack.pop()
            self._list(i, path, dev, stack)

    def _list(self, i, path, dev, stack):
        look = self.look[i]
        disk = size = nfiles = solo = shared = 0
        content = path in self.keep_paths
        big = []
        kids = []
        files = []          # (name, st, entry path) for clutter, decided after the listing
        env = False
        try:
            with os.scandir(path) as it:
                for entry in it:
                    name = entry.name
                    try:
                        if self.excluded(name, entry.path):
                            self.flags[i] |= EXCLUDED
                            self.excluded_n += 1
                            content = True
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        content = True
                        self.flags[i] |= UNREADABLE
                        self.errors.append((entry.path, f"cannot inspect: {why(e)}"))
                        self.watch.failed(e)
                        continue
                    if name in ENV_SIGNS:
                        env = True
                    if is_dir:
                        if st.st_dev != dev:
                            content = True
                            if entry.path in self.root_paths:
                                continue    # a drive walked as a root of its own
                            self.flags[i] |= MOUNTS
                            self.mounts.append((entry.path, i))
                        else:
                            kids.append((name, entry.path, st.st_blocks * 512))
                        continue
                    content = True
                    nfiles += 1
                    blocks = st.st_blocks * 512
                    if st.st_nlink > 1:
                        shared += blocks
                        key = (st.st_dev, st.st_ino)
                        if key in self.linked:
                            self.link_names += 1
                            self.link_bytes += blocks
                            continue    # counted already, under another name
                        self.linked.add(key)
                    else:
                        solo += blocks
                    disk += blocks
                    size += st.st_size
                    if _stat.S_ISREG(st.st_mode):
                        if blocks >= FOLDER_FILE_MIN:
                            big.append((blocks, st.st_size, name, st.st_mtime))
                        top = self.top
                        if len(top) < TOP_FILES:
                            heapq.heappush(top, (blocks, st.st_size, i, name, st.st_mtime))
                        elif blocks > top[0][0]:
                            heapq.heapreplace(top, (blocks, st.st_size, i, name, st.st_mtime))
                    if look != QUIET:
                        files.append((name, st, entry.path))
            self.watch.ok()
        except OSError as e:
            content = True
            self.flags[i] |= UNREADABLE
            self.errors.append((path, f"cannot read folder: {why(e)}"))
            self.watch.failed(e)
        self.disk[i] += disk
        self.size[i] += size
        self.files[i] += nfiles
        self.solo[i] += solo
        self.shared[i] += shared
        self._bytes += disk
        if big:
            big.sort(reverse=True)
            self.big[i] = big[:FOLDER_FILES]
        if env and look == LOOK:
            look = self.look[i] = APP
        for name, st, full in files:
            self._clutter(i, look, name, st, full)
        for name, full, blocks in kids:
            kid_look = look
            if look != QUIET:
                if (full in self.quiet_paths or name in self.quiet_names
                        or is_trash(name, full)):
                    kid_look = QUIET
                    content = True      # what `clean empty` leaves alone keeps this folder
                elif name.startswith(".") or name in PACKAGE_DIRS:
                    kid_look = APP
            j = self._new(i, name, kid_look)
            self.disk[j] += blocks
            self._bytes += blocks
            if full in self.watch_paths:
                self.watched[full] = j
            stack.append((j, full))
        self.content[i] = content
        self._seen_folders += 1
        progress = self.progress
        if progress is not None:
            progress.count = self._seen_folders
            if progress.due():
                progress.show(f"scanning: {self._seen_folders:,} folders, "
                              f"{human(self._bytes)}  {printable(path)}")

    def _clutter(self, i, look, name, st, full):
        listed = look == LOOK
        mode = st.st_mode
        if _stat.S_ISLNK(mode):
            try:
                os.stat(full)
                return
            except OSError as e:
                if e.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                    return      # can't tell (no permission): not called broken
                reason = "a loop" if e.errno == errno.ELOOP else "missing"
            try:
                target = os.readlink(full)
            except OSError:
                target = "?"
            self.broken.add(listed, 0, (i, name, target, reason))
            return
        if not _stat.S_ISREG(mode):
            return
        if is_temp(name):
            self.temp.add(listed, st.st_blocks * 512,
                          (i, name, st.st_blocks * 512, st.st_mtime))
        elif st.st_size == 0:
            self.zero.add(listed and name not in MARKERS and not name.startswith("."), 0,
                          (i, name))

    # ------------------------------------------------------------ results

    def _totals(self):
        """Sizes, counts and flags of each folder with everything inside it,
        and which folders are empty (counting empty folders inside as
        nothing), children before parents."""
        n = len(self.parent)
        self.tdisk = list(self.disk)
        self.tsize = list(self.size)
        self.tfiles = list(self.files)
        self.tsolo = list(self.solo)
        self.tshared = list(self.shared)
        self.tdirs = [0] * n
        self.tflags = list(self.flags)
        self.empty = [False] * n
        self.inner = [0] * n         # empty folders inside an empty folder
        blocked = [False] * n        # has a folder inside that isn't empty
        parent = self.parent
        for i in range(n - 1, -1, -1):
            p = parent[i]
            empty = (p >= 0 and not self.content[i] and not blocked[i]
                     and self.look[i] != QUIET)
            self.empty[i] = empty
            if p < 0:
                continue
            self.tdisk[p] += self.tdisk[i]
            self.tsize[p] += self.tsize[i]
            self.tfiles[p] += self.tfiles[i]
            self.tsolo[p] += self.tsolo[i]
            self.tshared[p] += self.tshared[i]
            self.tdirs[p] += self.tdirs[i] + 1
            self.tflags[p] |= self.tflags[i]
            if empty:
                self.inner[p] += self.inner[i] + 1
            else:
                blocked[p] = True

    def path(self, i):
        parts = []
        while True:
            p = self.parent[i]
            if p < 0:
                break
            parts.append(self.name[i])
            i = p
        parts.append(self.name[i])
        return os.path.join(*reversed(parts))

    def empty_folders(self):
        """(count, unlisted count, [(outermost empty folder, empty folders
        inside it)] up to LIST_MAX, listable ones not shown)."""
        count = unlisted = more = 0
        out = []
        for i, empty in enumerate(self.empty):
            if not empty:
                continue
            count += 1
            if self.empty[self.parent[i]]:
                continue        # inside an outermost one
            if self.look[i] != LOOK:
                unlisted += self.inner[i] + 1
            elif len(out) < LIST_MAX:
                out.append((i, self.inner[i]))
            else:
                more += 1
        return count, unlisted, out, more

    def kept(self):
        """The folders worth keeping for the report: every root, and every
        folder of KEEP_MIN or more (at most about MAX_FOLDERS: the bar rises
        if more qualify). A folder's parent is never smaller than it, so the
        kept folders always reach up to a root."""
        bar = KEEP_MIN
        big = [d for d in self.tdisk if d >= bar]
        if len(big) > MAX_FOLDERS:
            bar = sorted(big, reverse=True)[MAX_FOLDERS] + 1
        self.keep_bar = bar
        return [i for i in range(len(self.parent)) if self.parent[i] < 0 or self.tdisk[i] >= bar]

    def top_files(self):
        """[(disk, size, folder, name, mtime)], biggest first."""
        return sorted(self.top, reverse=True)

    def total(self):
        return sum(self.tdisk[i] for _, i, _ in self.roots)
