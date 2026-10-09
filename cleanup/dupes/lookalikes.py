"""
Look-alikes (`clean dupes --media`): the same photo or song in files whose
bytes differ, so they are not duplicates. Examples are a re-saved or edited
photo, a HEIC and its JPEG export, or one song ripped twice at different
bitrates. dupes/media.py reads the tags and says what counts as the same.

Look-alikes are only ever reported. Nothing picks them for removal: a file
goes only when you tick it, and never the last version of the photo or song
(selection.py). A removal checks the file and a version that stays, as for
duplicates, so every version gets its full hash here.

Candidates are files with a photo or song extension, one name per file on
disk, not empty, and visible unless --hidden. Their tags come from the hash
cache when the file is unchanged, else from reading the file's headers; a
file that can't be read is left out (and counted), it isn't an error of the
scan.
"""

from cleanup.dupes import media
from cleanup.dupes.files import FULL_DIGEST
from cleanup.errors import Cancelled
from cleanup.fsutil import CANCEL, human, ordered_map
from cleanup.scan.hashing import FileChanged, read_hash


class Lookalike:
    """One set of files showing the same photo or song: `files` best first,
    `letter[j]` names each distinct content (A, B, ...), `best` the files of
    the version that looks best (empty when it is a tie)."""

    __slots__ = ("id", "kind", "files", "tags", "letter", "best", "label")

    def __init__(self, kind, files, tags, letter, best, label):
        self.id = 0
        self.kind = kind
        self.files = files
        self.tags = tags
        self.letter = letter
        self.best = best
        self.label = label


class Finder:
    def __init__(self, tree, matcher, cache=None, jobs=1, progress=None, watch=None,
                 hidden=False):
        self.tree = tree
        self.matcher = matcher
        self.cache = cache if cache is not None and cache.active else None
        self.jobs = jobs
        self.progress = progress
        self.watch = watch
        self.hidden = hidden
        self.candidates = 0         # photos and songs looked at
        self.tagged = 0             # of them, those with tags to go by
        self.tags_read = 0          # files whose headers were read (not cached)
        self.tags_cached = 0
        self.hashed = 0             # look-alikes read whole for their hash
        self.hash_bytes = 0
        self.unreadable = 0         # left out: couldn't be read
        self.extra_full = {}        # content -> full hash, for contents with no copy
        self.groups = []

    def run(self):
        tree, m = self.tree, self.matcher
        cands = []
        for j in range(tree.nfiles_total):
            if m.rep[j] != j or m.cid[j] <= 0:
                continue
            name = tree.fname[j]
            stem, dot, ext = name.rpartition(".")
            if not (dot and stem) or ext.lower() not in media.EXTS:
                continue
            if self.hidden or tree.file_visible(j):
                cands.append(j)
        self.candidates = len(cands)
        tags = self._tags(cands)
        self.tagged = len(tags)
        sets = [s for s in media.group(sorted(tags.items()))
                if len({m.cid[j] for j in s}) > 1]
        sets = self._hash(sets)
        self.groups = [self._make(s, tags) for s in sets]
        self.groups.sort(key=lambda g: (-self._spare(g), g.files[0]))
        for k, g in enumerate(self.groups, 1):
            g.id = k
        return self.groups

    # ------------------------------------------------------------ reading

    def _tags(self, cands):
        """{file: tags} for the candidates that have tags to go by."""
        tree, cache = self.tree, self.cache
        out = {}
        todo = cands
        if cache is not None:
            known = cache.lookup_tags(cands)
            todo = []
            for j in cands:
                text = known.get(j)
                if text is None:
                    todo.append(j)
                    continue
                self.tags_cached += 1
                t = media.decode(text)
                if t is not None:
                    out[j] = t
        progress = self.progress
        total = len(todo)

        def work(j):
            if CANCEL.is_set():
                raise Cancelled
            try:
                return media.read(tree.file_path(j), tree.size[j], tree.mtime[j])
            except OSError as e:
                return e

        for n, (j, t) in enumerate(zip(todo, ordered_map(work, todo, self.jobs)), 1):
            if isinstance(t, OSError):
                self.unreadable += 1
                if self.watch is not None:
                    self.watch.failed(t)
            else:
                if self.watch is not None:
                    self.watch.ok()
                self.tags_read += 1
                if t is not None:
                    out[j] = t
                if cache is not None:
                    cache.put_tags(j, media.encode(t))
                    cache.flush()
            if progress is not None and progress.due():
                progress.show(f"reading tags: {n:,} of {total:,} photos and songs")
        return out

    def _hash(self, sets):
        """The sets, each file with a full hash for its content (from the
        matcher, the cache or a read); files that fail are left out."""
        tree, m, cache = self.tree, self.matcher, self.cache
        need = sorted({j for s in sets for j in s
                       if m.cid[j] not in m.full and m.cid[j] not in self.extra_full})
        # one file per content is enough
        by_cid = {}
        for j in need:
            by_cid.setdefault(m.cid[j], j)
        need = sorted(by_cid.values())
        got = {}
        if cache is not None and need:
            for j, (_, full) in cache.lookup(need).items():
                if full is not None:
                    got[j] = full
        todo = [j for j in need if j not in got]
        progress = self.progress
        if progress is not None:
            progress.total_bytes = sum(tree.size[j] for j in todo)
            progress.bytes = 0

        def work(j):
            try:
                return read_hash(tree.file_path(j), tree.size[j], tree.mtime[j],
                                 tree.size[j], FULL_DIGEST, progress)
            except FileChanged:
                return None
            except OSError as e:
                return e

        bad = set()
        for j, h in zip(todo, ordered_map(work, todo, self.jobs)):
            if isinstance(h, bytes):
                if self.watch is not None:
                    self.watch.ok()
                got[j] = h
                self.hashed += 1
                self.hash_bytes += tree.size[j]
                if cache is not None:
                    cache.put(j, None, h)
                    cache.flush()
            else:
                bad.add(m.cid[j])
                if isinstance(h, OSError):
                    self.unreadable += 1
                    if self.watch is not None:
                        self.watch.failed(h)
            if progress is not None and progress.due():
                progress.show(f"reading look-alikes: {human(progress.bytes)} of "
                              f"{human(progress.total_bytes)}")
        for j, h in got.items():
            self.extra_full[m.cid[j]] = h
        if cache is not None:
            cache.flush(force=True)
        out = []
        for s in sets:
            s = [j for j in s if m.cid[j] not in bad]
            if len({m.cid[j] for j in s}) > 1:
                out.append(s)
        return out

    # ------------------------------------------------------------ results

    def _make(self, files, tags):
        tree, cid = self.tree, self.matcher.cid
        quality = {}
        for j in files:
            q = media.quality(tags[j], tree.size[j])
            c = cid[j]
            if c not in quality or q > quality[c]:
                quality[c] = q
        order = sorted(quality, key=lambda c: quality[c], reverse=True)
        rank = {c: k for k, c in enumerate(order)}
        files = sorted(files, key=lambda j: (rank[cid[j]], tree.file_path(j)))
        letter = {j: _letter(rank[cid[j]]) for j in files}
        top = order[0]
        best = set()
        if len(order) < 2 or quality[order[1]] < quality[top]:
            best = {j for j in files if cid[j] == top}
        t = tags[files[0]]
        return Lookalike(t["kind"], files, {j: tags[j] for j in files}, letter, best,
                         _label(t))

    def _spare(self, g):
        """Bytes in the versions other than the first: what the set could
        give back, at most."""
        first = self.matcher.cid[g.files[0]]
        return sum(self.tree.size[j] for j in g.files if self.matcher.cid[j] != first)

    def contents(self):
        """{content: full hash} of every look-alike, for the manifest."""
        full = self.matcher.full
        return {self.matcher.cid[j]: full.get(self.matcher.cid[j])
                or self.extra_full[self.matcher.cid[j]]
                for g in self.groups for j in g.files}

    def stats(self):
        return {"candidates": self.candidates, "tagged": self.tagged,
                "read": self.tags_read, "cached": self.tags_cached,
                "hashed": self.hashed, "hash_bytes": self.hash_bytes,
                "unreadable": self.unreadable}


def _letter(k):
    """0 -> A, 25 -> Z, 26 -> AA, ..."""
    out = ""
    k += 1
    while k:
        k, r = divmod(k - 1, 26)
        out = chr(65 + r) + out
    return out


def _label(t):
    """What the set shows: 'Canon EOS 5D · 2019-06-12 10:15:30' or
    'Artist – Title'."""
    if t["kind"] == "photo":
        make, model = t.get("make") or "", t.get("model") or ""
        camera = model if make and model.lower().startswith(make.split()[0].lower()) \
            else " ".join(x for x in (make, model) if x)
        return f"{camera} · {t['taken']}"
    return f"{t['artist']} – {t['title']}"


def collect(tree, finder):
    """The look-alikes as plain data for the reports:
    [{id, kind, label, files: [{path, size, mtime, letter, best, detail}],
      versions, bytes, spare}]."""
    out = []
    for g in finder.groups:
        files = [{"path": tree.file_path(j), "size": tree.size[j], "mtime": tree.mtime[j],
                  "letter": g.letter[j], "best": j in g.best,
                  "detail": media.describe(g.tags[j])} for j in g.files]
        out.append({"id": g.id, "kind": g.kind, "label": g.label, "files": files,
                    "versions": len(set(g.letter.values())),
                    "bytes": sum(f["size"] for f in files),
                    "spare": finder._spare(g)})
    return out
