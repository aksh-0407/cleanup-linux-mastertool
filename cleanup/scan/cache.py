"""
The hash cache (~/.cache/clean/hashes.sqlite): file hashes from earlier scans,
so a re-scan doesn't read unchanged files again.

A file is identified by its drive (filesystem UUID) and its path inside that
drive, so the cache still applies after the drive is mounted somewhere else.
A filesystem without a UUID (tmpfs, network shares) uses the full path. An
entry is used only while the file's size and modification time (to the
nanosecond) are what they were when it was hashed. Files changed in the last
few seconds aren't stored: a change within the same clock tick would go
unnoticed.

The cache only speeds up finding duplicates. Removing anything (phase 3)
re-reads the bytes, never trusting a stored hash. With --media it also keeps
the photo and song tags read from a file (`tags`, '' when the file had none
worth using), so they aren't parsed again.

Any problem with the cache file gives one warning, and the scan goes on
without it. It is safe to delete at any time.

Upkeep: each entry notes when a scan last met its file (`seen`). When the
file grows past cache.max_mb, the entries met longest ago go first and the
file is compacted (VACUUM); `clean cache prune` also drops entries of files
that are gone, and `clean cache clear` deletes the file.
"""

import os
import sqlite3
import time

from cleanup import paths
from cleanup.fsutil import printable, why

NAME = "hashes.sqlite"
VERSION = 3             # 2 added `seen`, 3 `tags`; older files are upgraded in place
SIDE_FILES = ("-wal", "-shm", "-journal")
FLUSH_SECONDS = 2.0     # pending rows are committed this often
RACY_SECONDS = 3        # files changed more recently than this aren't stored
LOOKUP_BATCH = 50000
SEEN_STEP = 3600        # `seen` is renewed at most this often
TRIM_TO = 0.8           # trimming leaves the file at this share of the limit

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hashes (
    vol   TEXT NOT NULL,        -- filesystem UUID, '' when it has none
    path  BLOB NOT NULL,        -- path inside it (full path when vol is '')
    size  INTEGER NOT NULL,
    mtime INTEGER NOT NULL,     -- st_mtime_ns
    head  BLOB,                 -- blake2b-128 of the first 4 KiB (big files)
    full  BLOB,                 -- blake2b-512 of the whole file
    seen  INTEGER NOT NULL DEFAULT 0,   -- when a scan last met it (Unix time)
    tags  TEXT,                 -- photo / song tags (media.encode), '' for none
    PRIMARY KEY (vol, path)
) WITHOUT ROWID;
"""


# A new value replaces the stored one; a value not given (None) keeps the
# stored one while the file is unchanged, so hashes and tags written at
# different times end up in one entry.
_KEEP = ("CASE WHEN hashes.size = excluded.size AND hashes.mtime = excluded.mtime "
         "THEN coalesce(excluded.{0}, hashes.{0}) ELSE excluded.{0} END")
_UPSERT = ("INSERT INTO hashes (vol, path, size, mtime, head, full, seen, tags) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (vol, path) DO UPDATE SET "
           + ", ".join(f"{c} = {_KEEP.format(c)}" for c in ("head", "full", "tags"))
           + ", size = excluded.size, mtime = excluded.mtime, seen = excluded.seen")


def default_path():
    return os.path.join(paths.cache_dir(), NAME)


class Cache:
    """One open cache for one scan. `warning` is set (and the cache turned
    off) if anything goes wrong with the file."""

    def __init__(self, path=None):
        self.path = path or default_path()
        self.db = None
        self.warning = None
        self.pending = {}           # file number -> [vol, path, size, mtime, head, full,
                                    #                 seen, tags]
        self.last_flush = time.monotonic()
        self.hits = 0
        self.stored = 0
        self.pruned = 0
        self.trimmed = 0
        self._keys = None
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(_SCHEMA)
            row = self.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            if row is None:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)",
                                (str(VERSION),))
            elif row[0] in ("1", "2"):
                self.db.execute("BEGIN")
                if row[0] == "1":
                    self.db.execute("ALTER TABLE hashes ADD COLUMN seen INTEGER NOT NULL "
                                    "DEFAULT 0")
                self.db.execute("ALTER TABLE hashes ADD COLUMN tags TEXT")
                self.db.execute("UPDATE meta SET value = ? WHERE key = 'version'",
                                (str(VERSION),))
                self.db.execute("COMMIT")
            elif row[0] != str(VERSION):
                raise sqlite3.DatabaseError(f"it was written by another version "
                                            f"({row[0]})")
        except (sqlite3.Error, OSError) as e:
            self._fail(e)

    @property
    def active(self):
        return self.db is not None

    def _fail(self, err):
        if self.warning is None:
            reason = why(err) if isinstance(err, OSError) else str(err)
            self.warning = (f"the hash cache {printable(paths.tilde(self.path))} can't be "
                            f"used ({reason}); this scan reads every file it compares. "
                            f"Deleting the file fixes it: rm "
                            f"{printable(paths.tilde(self.path))}*")
        self.pending.clear()
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
        self.db = None

    # ------------------------------------------------------------ keys

    def attach(self, tree, volumes):
        """Work out every scanned folder's (drive, path inside) once."""
        self._keys = FolderKeys(tree, volumes)
        self.tree = tree

    def key(self, j):
        return self._keys.file_key(j)

    # ------------------------------------------------------------ reading

    def lookup(self, files):
        """{file number: (head, full)} for the files whose entry still
        matches their size and modification time. head or full may be None."""
        found = {}
        for j, head, full in self._select(files, "h.head, h.full",
                                          "(h.head IS NOT NULL OR h.full IS NOT NULL)"):
            found[j] = (head, full)
        return found

    def lookup_tags(self, files):
        """{file number: tags text} for the files whose entry has tags and
        still matches their size and modification time."""
        return {j: tags for j, tags in self._select(files, "h.tags", "h.tags IS NOT NULL")}

    def _select(self, files, columns, where):
        """(file number, *columns) of the entries of `files` that match
        `where` and the file's size and modification time; renews `seen`."""
        if not self.active:
            return []
        tree = self.tree
        now = int(time.time())
        found = []
        try:
            db = self.db
            db.execute("CREATE TEMP TABLE IF NOT EXISTS want "
                       "(n INTEGER PRIMARY KEY, vol TEXT, path BLOB)")
            files = list(files)
            for start in range(0, len(files), LOOKUP_BATCH):
                batch = files[start:start + LOOKUP_BATCH]
                db.execute("DELETE FROM want")
                db.execute("BEGIN")
                db.executemany("INSERT INTO want VALUES (?, ?, ?)",
                               ((j,) + self.key(j) for j in batch))
                db.execute("COMMIT")
                rows = db.execute(f"SELECT want.n, h.size, h.mtime, h.seen, {columns} "
                                  f"FROM want JOIN hashes h ON h.vol = want.vol "
                                  f"AND h.path = want.path WHERE {where}")
                renew = []              # hits whose `seen` is over SEEN_STEP old
                for row in rows:
                    j = row[0]
                    if row[1] == tree.size[j] and row[2] == tree.mtime[j]:
                        found.append((j,) + tuple(row[4:]))
                        if row[3] < now - SEEN_STEP:
                            renew.append(j)
                if renew:
                    db.execute("BEGIN")
                    db.executemany("UPDATE hashes SET seen = ? WHERE vol = ? AND path = ?",
                                   ((now,) + self.key(j) for j in renew))
                    db.execute("COMMIT")
            db.execute("DELETE FROM want")
        except sqlite3.Error as e:
            self._fail(e)
            return []
        return found

    # ------------------------------------------------------------ writing

    def put(self, j, head, full):
        """Remember file j's hashes (written at the next flush)."""
        row = self._row(j)
        if row is not None:
            row[4], row[5] = head, full

    def put_tags(self, j, tags):
        """Remember file j's photo / song tags (media.encode text)."""
        row = self._row(j)
        if row is not None:
            row[7] = tags

    def _row(self, j):
        """File j's pending row, or None when it isn't stored (no cache, or
        the file changed too recently to trust its time)."""
        if not self.active:
            return None
        row = self.pending.get(j)
        if row is not None:
            return row
        tree = self.tree
        mtime = tree.mtime[j]
        now = time.time_ns()
        if mtime > now or now - mtime < RACY_SECONDS * 1_000_000_000:
            return None
        vol, path = self.key(j)
        row = self.pending[j] = [vol, path, tree.size[j], mtime, None, None,
                                 now // 1_000_000_000, None]
        return row

    def flush(self, force=False):
        """Commit pending rows, at most every FLUSH_SECONDS unless forced."""
        if not self.active or not self.pending:
            return
        if not force and time.monotonic() - self.last_flush < FLUSH_SECONDS:
            return
        rows = list(self.pending.values())
        self.pending.clear()
        self.last_flush = time.monotonic()
        try:
            self.db.execute("BEGIN")
            self.db.executemany(_UPSERT, rows)
            self.db.execute("COMMIT")
            self.stored += len(rows)
        except sqlite3.Error as e:
            self._fail(e)

    # ------------------------------------------------------------ pruning

    def prune(self):
        """After a complete scan: drop entries for files that are gone or
        changed in the folders this scan read, and for folders under its
        paths that no longer exist (deleted, renamed). Entries in folders it
        didn't enter (excluded, beyond --max-depth, other drives) are kept,
        and so are files a filter left out but that are still unchanged."""
        if not self.active:
            return
        keys = self._keys
        tree = self.tree
        try:
            db = self.db
            db.execute("CREATE TEMP TABLE IF NOT EXISTS seen (vol TEXT, path BLOB, "
                       "PRIMARY KEY (vol, path)) WITHOUT ROWID")
            db.execute("DELETE FROM seen")
            db.execute("BEGIN")
            db.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?)",
                           (self.key(j) for j in range(tree.nfiles_total)))
            db.execute("COMMIT")
            scanned = keys.scanned_folders()
            stale = []
            for vol, prefix, real in keys.regions():
                if not os.path.isdir(os.fsdecode(real)):
                    continue            # the drive went away: decide nothing
                missing = {}            # folder -> it no longer exists
                low, high = prefix + b"/", prefix + b"0"   # "0" sorts right after "/"
                rows = db.execute(
                    "SELECT h.path, h.size, h.mtime FROM hashes h WHERE h.vol = ? "
                    "AND h.path >= ? AND h.path < ? AND NOT EXISTS (SELECT 1 FROM "
                    "seen s WHERE s.vol = h.vol AND s.path = h.path)",
                    (vol, low, high)).fetchall()
                for path, size, mtime in rows:
                    folder = path.rpartition(b"/")[0]
                    if (vol, folder) not in scanned:
                        gone = missing.get(folder)
                        if gone is None:
                            gone = missing[folder] = _missing(real + folder[len(prefix):])
                        if gone:
                            stale.append((vol, path))
                        continue        # else a folder this scan didn't read
                    try:
                        st = os.lstat(os.fsdecode(real + path[len(prefix):]))
                        if st.st_size == size and st.st_mtime_ns == mtime:
                            continue    # still there, only left out by a filter
                    except OSError:
                        pass
                    stale.append((vol, path))
            if stale:
                db.execute("BEGIN")
                db.executemany("DELETE FROM hashes WHERE vol = ? AND path = ?", stale)
                db.execute("COMMIT")
            db.execute("DROP TABLE seen")
            self.pruned = len(stale)
        except (sqlite3.Error, ValueError) as e:
            self._fail(e)

    # ------------------------------------------------------------ upkeep

    def trim(self, max_bytes):
        """If the file is bigger than max_bytes (0: no limit), drop the
        entries met longest ago until it would fit in TRIM_TO of that, and
        compact it. Returns how many entries went."""
        if not self.active or max_bytes <= 0:
            return 0
        self.flush(force=True)
        if file_size(self.path) <= max_bytes:
            return 0
        try:
            db = self.db
            rows = db.execute("SELECT count(*) FROM hashes").fetchone()[0]
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            page = db.execute("PRAGMA page_size").fetchone()[0]
            per_row = max(1.0, pages * page / max(rows, 1))
            drop = rows - int(max_bytes * TRIM_TO / per_row)
            if drop > 0:
                db.execute("BEGIN")
                db.execute("DELETE FROM hashes WHERE (vol, path) IN (SELECT vol, path FROM "
                           "hashes ORDER BY seen, vol, path LIMIT ?)", (drop,))
                db.execute("COMMIT")
            self.compact()
            self.trimmed += max(drop, 0)
            return max(drop, 0)
        except sqlite3.Error as e:
            self._fail(e)
            return 0

    def compact(self):
        """VACUUM: give the space of dropped entries back to the disk."""
        if not self.active:
            return
        try:
            self.flush(force=True)
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:
            self._fail(e)

    def prune_gone(self, volumes, progress=None):
        """Drop the entries of files that are gone or changed, on drives that
        are connected now. An entry on a drive that isn't connected stays: it
        is good again once the drive is back. Returns (dropped, kept because
        their drive isn't connected)."""
        if not self.active:
            return 0, 0
        try:
            stale, offline, looked = [], 0, 0
            where = {}          # (vol, folder inside) -> real folder (bytes) or None
            for vol, path, size, mtime in self.db.execute(
                    "SELECT vol, path, size, mtime FROM hashes").fetchall():
                looked += 1
                if progress is not None and progress.due():
                    progress.show(f"checking: {looked:,} cached files")
                folder, _, name = path.rpartition(b"/")
                real = where.get((vol, folder), False)
                if real is False:
                    real = where[(vol, folder)] = _where(vol, folder, volumes)
                if real is None:
                    offline += 1
                    continue
                if real is _GONE:
                    stale.append((vol, path))
                    continue
                try:
                    st = os.lstat(real + b"/" + name)
                except (FileNotFoundError, NotADirectoryError):
                    stale.append((vol, path))
                    continue
                except OSError:
                    continue        # can't tell: keep it
                if st.st_size != size or st.st_mtime_ns != mtime:
                    stale.append((vol, path))
            if stale:
                self.db.execute("BEGIN")
                self.db.executemany("DELETE FROM hashes WHERE vol = ? AND path = ?", stale)
                self.db.execute("COMMIT")
            return len(stale), offline
        except (sqlite3.Error, ValueError) as e:
            self._fail(e)
            return 0, 0

    def close(self):
        if self.db is not None:
            try:
                self.flush(force=True)
                if self.db is not None:
                    self.db.close()
            except sqlite3.Error:
                pass
            self.db = None


_GONE = object()


def _where(vol, folder, volumes):
    """Where the cached `folder` (bytes; inside drive `vol`, or a full path
    when vol is '') is now: its real path as bytes, _GONE if the drive is
    there but the folder isn't, or None if the drive isn't connected."""
    if vol:
        if volumes is None:
            return None
        inside = os.fsdecode(folder) or "/"
        if not volumes.by_uuid(vol):
            return None
        real = volumes.resolve(vol, inside)
        if real is None:
            # The drive is connected but this part of it isn't visible (only
            # a sub-folder is mounted): it can't be checked, so keep it.
            return None
        real = os.fsencode(real)
    else:
        real = folder or b"/"
    try:
        if os.path.isdir(real):
            return real.rstrip(b"/") or b""
        os.lstat(real)
        return _GONE            # there, but not a folder any more
    except FileNotFoundError:
        return _GONE
    except NotADirectoryError:
        return _GONE
    except OSError:
        return None             # can't tell


def file_size(path):
    """Bytes of the cache file with its write-ahead log and other side files."""
    total = 0
    for p in (path,) + tuple(path + s for s in SIDE_FILES):
        try:
            total += os.stat(p).st_size
        except OSError:
            pass
    return total


def info(path=None):
    """What the cache file holds, read-only: {path, exists, bytes, entries,
    drives: {vol: entries}, oldest, newest (seen, Unix time), version,
    error}."""
    path = path or default_path()
    out = {"path": path, "exists": os.path.exists(path), "bytes": file_size(path),
           "entries": 0, "drives": {}, "oldest": None, "newest": None, "version": None,
           "error": None}
    if not out["exists"]:
        return out
    try:
        # Query-only rather than mode=ro: a read-only connection leaves empty
        # -wal and -shm files behind, this one tidies them when it closes.
        db = sqlite3.connect(f"file:{_uri(path)}?mode=rw", uri=True, timeout=5)
        try:
            db.execute("PRAGMA query_only=1")
            row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            out["version"] = row[0] if row else None
            for vol, n in db.execute("SELECT vol, count(*) FROM hashes GROUP BY vol"):
                out["drives"][vol] = n
                out["entries"] += n
            if out["version"] in ("2", str(VERSION)):
                oldest, newest = db.execute("SELECT min(seen), max(seen) FROM hashes").fetchone()
                out["oldest"], out["newest"] = oldest, newest
        finally:
            db.close()
    except sqlite3.Error as e:
        out["error"] = str(e)
    return out


def _uri(path):
    from urllib.parse import quote
    return quote(os.path.abspath(path))


def clear(path=None):
    """Delete the cache file and its side files. Returns (bytes freed,
    [(path, error)])."""
    path = path or default_path()
    freed, failed = 0, []
    for p in (path,) + tuple(path + s for s in SIDE_FILES):
        try:
            size = os.lstat(p).st_size
            os.unlink(p)
            freed += size
        except FileNotFoundError:
            continue
        except OSError as e:
            failed.append((p, why(e)))
    return freed, failed


def _missing(path):
    """Folder `path` (bytes) certainly doesn't exist."""
    try:
        os.lstat(os.fsdecode(path))
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        pass
    return False


class FolderKeys:
    """Each folder's cache key prefix: (drive UUID or '', path inside it as
    bytes). Computed top-down: a folder is on its parent's drive unless it is
    itself a mount point."""

    def __init__(self, tree, volumes):
        self.tree = tree
        folder_paths = tree.paths()
        points = {m.point for m in volumes.mounts} if volumes else set()
        n = tree.nfolders
        self.vol = [""] * n
        self.prefix = [b""] * n
        self._regions = []      # (vol, prefix, real path prefix) per scanned region
        mount = [None] * n
        parent = tree.parent
        for i in range(n):
            path = folder_paths[i]
            p = parent[i]
            if p < 0 or path in points:
                uuid, inside, m = volumes.locate(path) if volumes else (None, path, None)
                mount[i] = m
                if uuid:
                    self.vol[i] = uuid
                    self.prefix[i] = os.fsencode(inside.rstrip("/"))
                else:
                    self.prefix[i] = os.fsencode(path.rstrip("/"))
                self._regions.append((self.vol[i], self.prefix[i],
                                      os.fsencode(path.rstrip("/"))))
            else:
                mount[i] = mount[p]
                self.vol[i] = self.vol[p]
                self.prefix[i] = self.prefix[p] + b"/" + os.fsencode(tree.name[i])

    def file_key(self, j):
        tree = self.tree
        d = tree.fdir[j]
        return self.vol[d], self.prefix[d] + b"/" + os.fsencode(tree.fname[j])

    def regions(self):
        return self._regions

    def scanned_folders(self):
        """(vol, prefix) of the folders whose listing was read completely."""
        from cleanup.scan.tree import UNREADABLE
        tree = self.tree
        return {(self.vol[i], self.prefix[i]) for i in range(tree.nfolders)
                if not tree.flags[i] & UNREADABLE}
