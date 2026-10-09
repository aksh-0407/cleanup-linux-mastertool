"""
Duplicate files: byte-identical files, in one folder or across several.

Two files are "the same" iff they have identical bytes: same size AND same full
content hash. Names, locations and dates are ignored unless --same-name or
--same-date asks for them to match too. Speed on large drives comes from a
cheapest-test-first funnel, so full reads only happen for real candidates:

    stage 1  group by file SIZE            (from the scan, no reads)
             -> a unique size cannot have a duplicate; drop it.
    stage 2  hash the first HEAD_BYTES     (one small read per file)
             -> a unique head hash cannot be a duplicate; drop it.
             -> a file no bigger than HEAD_BYTES is now fully hashed: done.
    stage 3  hash the FULL content         (full read, only for survivors)
             -> identical full hash = byte-identical duplicates.

Hashes from the cache (scan/cache.py) are used instead of reading when the
file's size and modification time haven't changed. Files are read in scan
order, which on a spinning drive keeps the head moving mostly one way.

The result is a content id (cid) for every file: 0 for empty files, -1 for
files that couldn't be read (their folder is then tainted, see scan/tree.py),
and the same positive number for files with the same content. A hard link
(one file under several names) is one file: every name gets the cid, but it
is never its own duplicate and deleting one name frees nothing.
"""

from collections import defaultdict

from cleanup.fsutil import human, ordered_map, why
from cleanup.scan.hashing import HEAD_BYTES, FileChanged, read_hash
from cleanup.scan.tree import UNREADABLE

HEAD_DIGEST = 16        # bytes of the head hash (blake2b-128)
FULL_DIGEST = 64        # bytes of the full hash (blake2b-512)


class Matcher:
    """Assigns content ids to the files of a scanned Tree."""

    def __init__(self, tree, same_name=False, same_date=False, cache=None, jobs=1,
                 progress=None, watch=None):
        self.tree = tree
        self.same_name = same_name
        self.same_date = same_date
        self.cache = cache if cache is not None and cache.active else None
        self.jobs = jobs
        self.progress = progress
        self.watch = watch
        n = tree.nfiles_total
        self.cid = [0] * n
        self.rep = list(range(n))       # each name -> the first name of its inode
        self.full = {}                  # cid -> full hash (raw) of every duplicate cid
        self.hard_links = 0             # extra names of files seen more than once
        self.files_read = 0             # files read (once each, even if read twice)
        self.bytes_read = 0
        self.cache_hits = 0             # files compared without reading them at all
        self.unreadable = 0
        self._next = 1
        self._known = {}                # file -> [head, full] from the cache or a read
        self._head_read = set()         # big files whose head was read (not cached)

    # ------------------------------------------------------------ the funnel

    def run(self):
        tree = self.tree
        size, nlink = tree.size, tree.nlink
        # Hard links: one representative per (device, inode).
        seen = {}
        reps = []
        for j in range(tree.nfiles_total):
            if not size[j]:
                continue
            if nlink[j] > 1:
                key = (tree.dev[j], tree.ino[j])
                first = seen.get(key)
                if first is not None:
                    self.rep[j] = first
                    self.hard_links += 1
                    continue
                seen[key] = j
            reps.append(j)
        del seen

        # Stage 1: by size (and name / date when asked).
        by_key = defaultdict(list)
        for j in reps:
            by_key[self._key(j)].append(j)
        candidates = []
        for members in by_key.values():
            if len(members) == 1:
                self.cid[members[0]] = self._new()
            else:
                candidates.extend(members)
        del by_key
        candidates.sort()

        if self.cache:
            for j, (head, full) in self.cache.lookup(candidates).items():
                self._known[j] = [head, full]

        # Stage 2: the first 4 KiB (all of a small file).
        heads = self._hash(candidates, head=True)
        groups = defaultdict(list)
        for j in candidates:
            h = heads.get(j)
            if h is not None:
                groups[(self._key(j), h)].append(j)
        need_full = []
        for (key, h), members in groups.items():
            if len(members) == 1:
                self.cid[members[0]] = self._new()
            elif self.tree.size[members[0]] <= HEAD_BYTES:
                self._same(members, h)
            else:
                need_full.extend(members)
        del groups, heads
        need_full.sort()

        # Stage 3: the whole file.
        if self.progress is not None:
            self.progress.total_bytes = sum(size[j] for j in need_full
                                            if self._known.get(j, (None, None))[1] is None)
            self.progress.bytes = 0
        fulls = self._hash(need_full, head=False)
        groups = defaultdict(list)
        for j in need_full:
            h = fulls.get(j)
            if h is not None:
                groups[(self._key(j), h)].append(j)
        for (key, h), members in groups.items():
            if len(members) == 1:
                self.cid[members[0]] = self._new()
            else:
                self._same(members, h)
        del groups, fulls
        if self.cache:
            self.cache.flush(force=True)

        for j in range(tree.nfiles_total):
            r = self.rep[j]
            if r != j:
                self.cid[j] = self.cid[r]
        self._known = {}
        self._head_read = set()
        return self.cid

    def _key(self, j):
        tree = self.tree
        key = (tree.size[j],)
        if self.same_name:
            key += (tree.fname[j].casefold(),)
        if self.same_date:
            key += (tree.mtime[j] // 1_000_000_000,)
        return key

    def _new(self):
        c = self._next
        self._next += 1
        return c

    def _same(self, members, full):
        c = self._new()
        self.full[c] = full
        for j in members:
            self.cid[j] = c

    # ------------------------------------------------------------ reading

    def _hash(self, files, head):
        """{file: digest} for the files that could be read; the others get
        cid -1 and an error. A small file's "head" is its full hash."""
        tree, known, cache = self.tree, self._known, self.cache
        out = {}
        todo = []
        for j in files:
            small = tree.size[j] <= HEAD_BYTES
            k = known.get(j)
            have = None
            if k is not None:
                have = k[1] if (small or not head) else k[0]
            if have is not None:
                out[j] = have
                if head:
                    self.cache_hits += 1
            else:
                todo.append(j)
        progress = self.progress
        if progress is not None:
            progress.count = 0
        total = len(todo)

        def work(j):
            size = tree.size[j]
            small = size <= HEAD_BYTES
            nbytes = size if (small or not head) else HEAD_BYTES
            dsize = HEAD_DIGEST if (head and not small) else FULL_DIGEST
            try:
                return read_hash(tree.file_path(j), size, tree.mtime[j], nbytes, dsize,
                                 progress if not head else None)
            except FileChanged:
                return "changed while being compared; skipped"
            except OSError as e:
                return e

        for j, result in zip(todo, ordered_map(work, todo, self.jobs)):
            if isinstance(result, bytes):
                if self.watch is not None:
                    self.watch.ok()
                out[j] = result
                size = tree.size[j]
                small = size <= HEAD_BYTES
                if head:
                    self.files_read += 1
                    if not small:
                        self._head_read.add(j)
                elif j not in self._head_read:
                    # its head came from the cache, but it had to be read after all
                    self.files_read += 1
                    self.cache_hits -= 1
                self.bytes_read += size if (small or not head) else min(size, HEAD_BYTES)
                k = known.setdefault(j, [None, None])
                if head and not small:
                    k[0] = result
                else:
                    k[1] = result
                if cache:
                    cache.put(j, k[0], k[1])
                    cache.flush()
            else:
                self._unreadable(j, result)
            if progress is not None:
                progress.count += 1
                if head and progress.due():
                    progress.show(f"checking: {progress.count:,} of {total:,} same-size files")
                elif not head and progress.due():
                    progress.show(f"comparing: {human(progress.bytes)} of "
                                  f"{human(progress.total_bytes)} read")
        return out

    def _unreadable(self, j, err):
        tree = self.tree
        self.cid[j] = -1
        self.unreadable += 1
        tree.flags[tree.fdir[j]] |= UNREADABLE
        if isinstance(err, OSError):
            tree.errors.append((tree.file_path(j), f"cannot read: {why(err)}"))
            if self.watch is not None:
                self.watch.failed(err)
        else:
            tree.errors.append((tree.file_path(j), err))

    # ------------------------------------------------------------ results

    def groups(self):
        """Duplicate groups: [(cid, [file numbers])], one name per file
        (hard links listed once), in the order of their first file."""
        members = defaultdict(list)
        cid, rep = self.cid, self.rep
        for j in range(self.tree.nfiles_total):
            c = cid[j]
            if c > 0 and c in self.full and rep[j] == j:
                members[c].append(j)
        return [(c, m) for c, m in members.items() if len(m) > 1]
