"""
Folder relations: which folders are copies of each other, whole or in part.

Every untainted folder (see scan/tree.py) gets a *content fingerprint*: the
multiset of the contents of all files below it, ignoring names and layout
(a sum of random 128-bit numbers per content, with the file count and bytes).
Folders with the same fingerprint, checked exactly, form a *family*:

    identical   same names and contents (junk ignored), compared with a
                Merkle signature of the tree
    renamed     same files under other names or in other subfolders

A family member inside a copy that is already redundant is left out, so a
copied tree is reported once, at the top, and not again for each subfolder.
Then, walking down from each root, every other folder may be:

    partial     all its files are in one other folder (the lowest one that
                holds them all), which becomes the family's anchor. A family's
                anchor may itself be a partial copy of a bigger folder: then
                the whole family joins that folder's, all as partial copies
    covered     all its files exist elsewhere, scattered over several places
                (mostly in places that are not more of a copy than it is)
    diverged    it shares at least --similar % of its contents with another
                folder, and both have files of their own (keep both). Each
                side is the lowest folder that holds all the shared files on
                its side, so a pair is never named one level too high (which
                would send a merge to the wrong place)

A folder that is partial or covered is judged on all of its files; results
found inside it earlier (a subfolder that is an identical copy of another)
become part of it, so they are not reported twice. Likewise, copies between
the two folders of a diverged pair are part of the pair.

Of copies that are equal, the one kept is in the first root given, then
inside a folder a bigger family keeps (so that folder isn't hollowed out),
then in the more complete surroundings (Main/a, not Old/a, when all of Old is
in Main), then the one whose files are least deep.

A folder is only called redundant (identical, renamed, partial, covered) if
every content it holds would still have a copy after ALL redundant folders
reported so far were removed. Folders that are copies of each other in a
circle are therefore never all reported: the last copy of anything is never
inside a redundant folder.

Space counts each file once (hard links: once per file, not per name), and a
folder only frees a file's space if all of that file's names are inside it.
"""

import itertools
import random
from bisect import bisect_left, insort
from collections import Counter, defaultdict

from cleanup.scan.filters import category_of
from cleanup.scan.tree import HIDDEN, TAINT

NOISE = 50              # content held by more folders than this says nothing
CONTAINED_RATIO = 10    # a partial copy holds at least 1/10 of its anchor's files
SIMILAR_BUDGET = 2000   # folder comparisons spent looking for diverged copies
SIMILAR_PER_FOLDER = 20
SURROUNDINGS_MAX = 16   # copies of one folder compared by their surroundings
COVERED_MARGIN = 0.25   # how much more of a copy a covered folder's copies may sit in

IDENTICAL, RENAMED, PARTIAL, DIVERGED = "identical", "renamed", "partial", "diverged"
REDUNDANT_ROLES = (IDENTICAL, RENAMED, PARTIAL)


class Member:
    """One non-anchor folder of a family."""

    def __init__(self, folder, role, shared=None):
        self.folder = folder
        self.role = role
        self.shared = shared        # diverged: fraction of distinct contents in common
        self.freed = 0
        self.diff = None


class Family:
    """An anchor folder (the one to keep) and the folders related to it."""

    def __init__(self, anchor):
        self.anchor = anchor
        self.members = []
        self.id = None
        self.freed = 0

    @property
    def kind(self):
        roles = {m.role for m in self.members}
        return roles.pop() if len(roles) == 1 else "mixed"


class Covered:
    """A folder whose every file has a copy somewhere else."""

    def __init__(self, folder):
        self.folder = folder
        self.id = None
        self.freed = 0
        self.where = []             # [(folder, files there)], most first


class Group:
    """A duplicate file group, as reported."""

    __slots__ = ("cid", "files", "size", "full", "explained", "folded", "category", "id")

    def __init__(self, cid, files, size, full, explained, folded, category):
        self.cid = cid
        self.files = files          # file numbers, one per inode
        self.size = size
        self.full = full            # raw blake2b-512
        self.explained = explained  # per file: "F3" / "C7" / ""
        self.folded = folded        # every copy but one is in a redundant folder
        self.category = category
        self.id = None


class Relations:
    """Folder families, covered folders and file groups of one scan."""

    def __init__(self, tree, matcher, similar_pct=80, hidden=False, seed=0):
        self.tree = tree
        self.matcher = matcher
        self.cid = matcher.cid
        self.pct = similar_pct
        self.hidden = hidden
        self.rng = random.Random(seed)
        self.families = []
        self.covered = []
        self.groups = []
        self.budget = SIMILAR_BUDGET
        self._within_memo = None

    def run(self):
        self._aggregate()
        self._locations()
        self._exact_families()
        self._walk()
        self._finish()
        self._file_groups()
        return self

    # ------------------------------------------------------------ per folder

    def _aggregate(self):
        """Bottom-up: taint, file count, bytes and fingerprint of each subtree."""
        tree, cid = self.tree, self.cid
        n = tree.nfolders
        top = max(cid) if cid else 0
        rand = self.rng.getrandbits
        weight = [0] + [rand(128) for _ in range(top)]
        taint = [f & TAINT for f in tree.flags]
        nfile = [0] * n
        nbytes = [0] * n
        fsum = [0] * n
        fdir, size = tree.fdir, tree.size
        for j in range(tree.nfiles_total):
            c = cid[j]
            if c > 0:
                d = fdir[j]
                nfile[d] += 1
                nbytes[d] += size[j]
                fsum[d] += weight[c]
        parent = tree.parent
        for i in range(n - 1, -1, -1):
            p = parent[i]
            if p >= 0:
                taint[p] |= taint[i]
                nfile[p] += nfile[i]
                nbytes[p] += nbytes[i]
                fsum[p] += fsum[i]
        mask = (1 << 128) - 1
        self.fp = [s & mask for s in fsum]
        self.taint, self.nfile, self.nbytes = taint, nfile, nbytes
        self.gone = [None] * n          # result that removes this whole folder
        self.member_of = {}             # folder -> Family it is a member of
        self.anchors = {}               # folder -> Family it anchors

    def _locations(self):
        """Where each repeated content is (folders, sorted), and the raw
        coverage count: per folder, how many of its files have a copy
        outside it."""
        tree, cid = self.tree, self.cid
        count = [0] * ((max(cid) if cid else 0) + 1)
        for c in cid:
            if c > 0:
                count[c] += 1
        self.alive = count              # names of each content not yet in a redundant folder
        names = {}
        fdir = tree.fdir
        for j in range(tree.nfiles_total):
            c = cid[j]
            if c > 0 and count[c] > 1:
                lst = names.get(c)
                if lst is None:
                    names[c] = [j]
                else:
                    lst.append(j)
        self.names = names              # content -> file numbers (every name)
        self.where = {c: [fdir[j] for j in js] for c, js in names.items()}
        end, parent = tree.end, tree.parent
        cov = [0] * tree.nfolders
        for c, folders in self.where.items():
            x = folders[0]
            for f in folders:
                cov[f] += 1
                while x >= 0 and not (x <= f < end[x]):
                    x = parent[x]
            if x >= 0:
                cov[x] -= len(folders)
        for i in range(tree.nfolders - 1, -1, -1):
            p = parent[i]
            if p >= 0:
                cov[p] += cov[i]
        self.cov = cov
        # How much of a copy a folder's surroundings are: the lowest share of
        # files with a copy outside, over the folder and the folders above it
        # (not the roots: they hold everything).
        nfile, copied = self.nfile, [1.0] * tree.nfolders
        for i in range(tree.nfolders):
            p = parent[i]
            if p >= 0:
                share = cov[i] / nfile[i] if nfile[i] else 1.0
                copied[i] = min(copied[p], share)
        self.copied = copied

    # ------------------------------------------------------------ helpers

    def _visible(self, i):
        return self.hidden or not self.tree.flags[i] & HIDDEN

    def _is_gone(self, x):
        gone, parent = self.gone, self.tree.parent
        while x >= 0:
            if gone[x] is not None:
                return True
            x = parent[x]
        return False

    def _alive_below(self, f, top):
        """Folder f (inside alive folder `top`) is not in a redundant folder."""
        gone, parent = self.gone, self.tree.parent
        while f != top:
            if gone[f] is not None:
                return False
            f = parent[f]
        return True

    def _content(self, b):
        """Counter of the contents in folder b, skipping redundant subfolders."""
        return self._contents(b)[0]

    def _contents(self, b):
        """(live, every): Counters of the contents in folder b, without and
        with its redundant subfolders (the same object when it has none)."""
        tree, cid, gone = self.tree, self.cid, self.gone
        ffirst, nfiles, end = tree.ffirst, tree.nfiles, tree.end
        out = Counter()
        skipped = False
        k, stop = b, end[b]
        while k < stop:
            if k != b and gone[k] is not None:
                skipped = True
                k = end[k]
                continue
            for j in range(ffirst[k], ffirst[k] + nfiles[k]):
                c = cid[j]
                if c > 0:
                    out[c] += 1
            k += 1
        if not skipped:
            return out, out
        every = Counter(c for c in (cid[j] for j in tree.files_of(b)) if c > 0)
        return out, every

    def _accept(self, b, result, content=None):
        """Mark folder b redundant (removed by `result`) if every content in
        it keeps a copy outside it and outside every redundant folder."""
        content = self._content(b) if content is None else content
        alive = self.alive
        for c, k in content.items():
            if alive[c] - k < 1:
                return False
        for c, k in content.items():
            alive[c] -= k
        self.gone[b] = result
        return True

    def _children(self, i):
        end = self.tree.end
        c, stop = i + 1, end[i]
        while c < stop:
            yield c
            c = end[c]

    def signature(self, i, memo):
        """Merkle signature of folder i: names, kinds and contents of
        everything below it (junk is never stored, so it doesn't count)."""
        import hashlib
        tree, cid = self.tree, self.cid
        if i in memo:
            return memo[i]
        for k in range(tree.end[i] - 1, i - 1, -1):
            if k in memo:
                continue
            entries = []
            for j in range(tree.ffirst[k], tree.ffirst[k] + tree.nfiles[k]):
                entries.append((tree.fname[j], 0, cid[j].to_bytes(8, "little", signed=True)))
            for c in self._children(k):
                entries.append((tree.name[c], 1, memo[c]))
            entries.sort()
            h = hashlib.blake2b(digest_size=16)
            for name, kind, value in entries:
                h.update(name.encode("utf-8", "surrogateescape") + b"\0" +
                         bytes((kind,)) + value)
            memo[k] = h.digest()
        return memo[i]

    # ------------------------------------------------------------ families

    def _exact_families(self):
        tree, nfile, taint = self.tree, self.nfile, self.taint
        end, depth = tree.end, tree.depth
        by_fp = defaultdict(list)
        files_depth = {}        # how deep a folder's files really are (below wrappers)
        for i in range(tree.nfolders):
            if nfile[i] and not taint[i] and self._visible(i):
                by_fp[(self.fp[i], nfile[i], self.nbytes[i])].append(i)
        candidates = []
        for members in by_fp.values():
            if len(members) < 2:
                continue
            kept, open_ = [], []
            for m in members:               # preorder: a folder before its subfolders
                while open_ and end[open_[-1]] <= m:
                    open_.pop()
                if open_:
                    # Same content as a folder above it (that folder only
                    # wraps this one): a chain, reported at the top.
                    files_depth[open_[0]] = depth[m]
                    continue
                open_.append(m)
                kept.append(m)
                files_depth[m] = depth[m]
            if len(kept) < 2:
                continue
            exact = defaultdict(list)
            for m in kept:
                exact[tuple(sorted(c for c in (self.cid[j] for j in tree.files_of(m))
                                   if c > 0))].append(m)
            candidates.extend(ms for ms in exact.values() if len(ms) >= 2)
        del by_fp
        # Biggest first: a copy that holds another copy (with other content)
        # has more files, so it is taken first and the copies inside it are
        # dropped, never reported again on their own.
        candidates.sort(key=lambda ms: (-nfile[ms[0]], -self.nbytes[ms[0]],
                                        min(depth[m] for m in ms), ms[0]))
        memo = {}
        for ms in candidates:
            ms = [m for m in ms if not self._is_gone(m)]
            if len(ms) < 2:
                continue
            # Keep the copy in the first root given; then one inside a folder
            # an earlier (bigger) family keeps, so that folder isn't hollowed
            # out (Pictures/2019 rather than Phone dump/, when Pictures is
            # kept over Backup/Pictures); then the one in the more complete
            # surroundings (Main/a rather than Old/a when all of Old is in
            # Main, so that Old is one partial copy and not split up); then
            # the one whose files are least deep: Photos/ rather than
            # Backup/Photos/ when Backup holds nothing else.
            lesser = self._in_lesser_surroundings(ms)
            anchor = min(ms, key=lambda m: (tree.root[m], not self._in_kept(m), m in lesser,
                                            files_depth[m], depth[m], m))
            fam = Family(anchor)
            anchor_sig = self.signature(anchor, memo)
            for m in ms:
                if m == anchor or not self._accept(m, fam):
                    continue
                role = IDENTICAL if self.signature(m, memo) == anchor_sig else RENAMED
                fam.members.append(Member(m, role))
                self.member_of[m] = fam
            if fam.members:
                self.anchors[anchor] = fam
                self.families.append(fam)
            # Signatures are only needed within this family's members.
            memo.clear()
        self._within_memo = None

    def _in_kept(self, m):
        """m is inside the anchor of a family found already."""
        parent, anchors = self.tree.parent, self.anchors
        p = parent[m]
        while p >= 0:
            if p in anchors:
                return True
            p = parent[p]
        return False

    def _in_lesser_surroundings(self, ms):
        """The copies whose parent folder is all found in the parent of
        another copy (and not the other way round)."""
        parent = self.tree.parent
        out = set()
        if len(ms) > SURROUNDINGS_MAX:
            return out
        for a in ms:
            pa = parent[a]
            if pa < 0:
                continue
            for b in ms:
                pb = parent[b]
                if pb < 0 or pb == pa:
                    continue
                if self._within(pa, pb) and not self._within(pb, pa):
                    out.add(a)
                    break
        return out

    def _within(self, p, q):
        """Every content below folder p has a copy below folder q."""
        memo = self._within_memo
        if memo is None:
            memo = self._within_memo = {}
        r = memo.get((p, q))
        if r is None:
            r = memo[(p, q)] = self._check_within(p, q)
        return r

    def _check_within(self, p, q):
        stop, cid, where = self.tree.end[q], self.cid, self.where
        if q <= p < stop:
            return True
        for j in self.tree.files_of(p):
            c = cid[j]
            if c <= 0:
                continue
            folders = where.get(c)
            if folders is None:
                return False        # found nowhere else
            k = bisect_left(folders, q)
            if k == len(folders) or folders[k] >= stop:
                return False
        return True

    # ------------------------------------------------------------ the walk

    def _walk(self):
        """Top-down: each folder that is not yet part of a result may be a
        partial copy, covered elsewhere, or a diverged copy. A folder that is
        reported is not looked into."""
        tree = self.tree
        n, end = tree.nfolders, tree.end
        self.anchor_list = sorted(self.anchors)
        self.pair_sides = set()         # both folders of each diverged pair
        i = 0
        while i < n:
            if self.gone[i] is not None or i in self.pair_sides:
                i = end[i]
                continue
            if self._candidate(i) and self.cov[i] == self.nfile[i]:
                live, every = self._contents(i)
                if every and (self._contained(i, live, every) or
                              self._covered(i, live, every)):
                    i = end[i]
                    continue
            elif self._contained_anchor(i):
                live, every = self._contents(i)
                if every and self._contained(i, live, every):
                    i = end[i]
                    continue
            elif self._may_diverge(i):
                if self._similar(i):
                    i = end[i]
                    continue
            i += 1
        # A family found after a pair may still link its two sides.
        for fam in list(self.families):
            for m in fam.members:
                if m.role == DIVERGED:
                    self._absorb_pair(fam.anchor, m.folder)

    def _holds_anchor(self, i, lo):
        """Some anchor among folders lo .. end[i] - 1: lo = i counts i
        itself, lo = i + 1 only the folders below it."""
        a = self.anchor_list
        k = bisect_left(a, lo)
        return k < len(a) and a[k] < self.tree.end[i]

    def _add_anchor(self, x, fam):
        self.anchors[x] = fam
        self.families.append(fam)
        insort(self.anchor_list, x)

    def _drop_anchor(self, x):
        fam = self.anchors.pop(x)
        self.families.remove(fam)
        del self.anchor_list[bisect_left(self.anchor_list, x)]
        return fam

    def _absorb(self, b):
        """b was just found redundant: results inside it (subfolders that are
        copies of folders elsewhere, or diverged ones) become part of it.
        There are no anchors inside b (see _candidate), only members."""
        gone, member_of = self.gone, self.member_of
        for k in range(b + 1, self.tree.end[b]):
            fam = member_of.pop(k, None)
            if fam is not None:
                fam.members = [m for m in fam.members if m.folder != k]
                if not fam.members:
                    self._drop_anchor(fam.anchor)
            elif isinstance(gone[k], Covered):
                self.covered.remove(gone[k])
            gone[k] = None

    def _candidate(self, i):
        return (self.nfile[i] and not self.taint[i] and self._visible(i)
                and i not in self.member_of and not self._holds_anchor(i, i))

    def _may_diverge(self, i):
        """i has files of its own but shares enough to be a diverged copy.
        It may hold anchors (both sides of a pair are kept)."""
        return (self.budget > 0 and self.nfile[i] and not self.taint[i] and self._visible(i)
                and i not in self.member_of and i not in self.anchors
                and self.nfile[i] * self.pct <= self.cov[i] * 100 < self.nfile[i] * 100)

    def _contained_anchor(self, i):
        """i anchors copies of itself (or of part of it), with nothing kept
        below it: the whole family may be a partial copy of a bigger folder."""
        fam = self.anchors.get(i)
        return (fam is not None and i not in self.member_of and not self.taint[i]
                and self._visible(i) and self.cov[i] == self.nfile[i]
                and all(m.role in REDUNDANT_ROLES for m in fam.members)
                and not self._holds_anchor(i, i + 1))

    def _contains(self, x, contents):
        """Folder x (alive) holds a live copy of every content in `contents`."""
        end, where = self.tree.end, self.where
        for c in contents:
            folders = where[c]
            k = bisect_left(folders, x)
            stop = end[x]
            while k < len(folders) and folders[k] < stop:
                if self._alive_below(folders[k], x):
                    break
                k += 1
            else:
                return False
            if k >= len(folders) or folders[k] >= stop:
                return False
        return True

    def _contained(self, b, live, every):
        """b is a partial copy: one other folder holds all of its files
        (`every`: all of them; `live`: those not in a redundant subfolder)."""
        end, parent = self.tree.end, self.tree.parent
        contents = sorted(every, key=lambda c: len(self.where[c]))
        seed = contents[0]
        if len(self.where[seed]) > NOISE:
            return False
        best, tried = None, set()
        for f in self.where[seed]:
            if b <= f < end[b] or self._is_gone(f):
                continue
            x = f
            while x >= 0 and not (x <= b < end[x]):     # never a folder holding b
                if x in tried:
                    break
                tried.add(x)
                if self._contains(x, contents):
                    if best is None or self.nfile[x] < self.nfile[best]:
                        best = x
                    break
                x = parent[x]
        if best is None or best in self.member_of or not self._visible(best):
            return False
        if self.nfile[best] > sum(every.values()) * CONTAINED_RATIO:
            return False            # a few files inside a big folder: "covered" says it better
        fam = self.anchors.get(best) or Family(best)
        if not self._accept(b, fam, live):
            return False
        fam.members.append(Member(b, PARTIAL))
        self.member_of[b] = fam
        if best not in self.anchors:
            self._add_anchor(best, fam)
        self._absorb(b)
        if b in self.anchors:
            # b anchored copies of itself (or partial copies of it): all of
            # them are in `best` too, so they join its family.
            old = self._drop_anchor(b)
            for m in old.members:
                m.role, m.shared = PARTIAL, None
                fam.members.append(m)
                self.member_of[m.folder] = fam
                self.gone[m.folder] = fam
        return True

    def _covered(self, b, live, every):
        end, copied = self.tree.end, self.copied
        where = Counter()
        # Most of b's files must have a copy in a place that is no more a
        # copy than b is: a folder of originals whose files were also put
        # into folders of copies (Downloads/x, with other things) is not
        # the one to call redundant.
        limit = copied[b] + COVERED_MARGIN
        backed = 0
        for c, k in every.items():
            ok = False
            for f in self.where[c]:
                if not (b <= f < end[b]) and not self._is_gone(f):
                    where[f] += 1
                    ok = ok or copied[f] <= limit
            if ok:
                backed += k
        if backed * 2 < sum(every.values()):
            return False
        cov = Covered(b)
        if not self._accept(b, cov, live):
            return False
        self._absorb(b)
        cov.where = where.most_common(5)
        cov.where_total = len(where)
        self.covered.append(cov)
        return True

    def _similar(self, b):
        """b and another folder share at least pct % of their distinct
        contents, and each has files of its own: a diverged copy."""
        end, parent, nfile = self.tree.end, self.tree.parent, self.nfile
        p = self.pct / 100
        mine = set(self._contents(b)[1])
        if len(mine) < 2:
            return False
        # The rarest contents that b shares with some other place (one held
        # twice inside b, and nowhere else, leads nowhere).
        seeds = sorted((c for c in mine if c in self.where and len(self.where[c]) <= NOISE
                        and not all(b <= f < end[b] for f in self.where[c])),
                       key=lambda c: len(self.where[c]))[:3]
        candidates = []
        seen = set()
        for c in seeds:
            for f in self.where[c]:
                if b <= f < end[b] or self._is_gone(f):
                    continue
                x = f
                while x >= 0 and not (x <= b < end[x]) and nfile[x] * p <= len(mine) * 4:
                    if x not in seen:
                        seen.add(x)
                        if nfile[x] >= len(mine) * p:
                            candidates.append(x)
                    x = parent[x]
        best, best_j = None, 0.0
        for x in candidates[:SIMILAR_PER_FOLDER]:
            if (self.taint[x] or not self._visible(x) or x in self.member_of
                    or self._is_gone(x)):
                continue
            if self.budget <= 0:
                break
            self.budget -= 1
            theirs = set(self._contents(x)[1])
            if not theirs - mine:
                continue            # x is a partial copy of b; found when the walk gets there
            shared = mine & theirs
            j = len(shared) / len(mine | theirs)
            if j < p or j <= best_j:
                continue
            # What they share must not all sit in one subfolder of either:
            # that subfolder is the copy (b's is reached later in the walk,
            # x's is a candidate of its own).
            if self._holder(b, shared) != b or self._holder(x, shared) != x:
                continue
            best, best_j = x, j
        if best is None:
            return False
        if best in self.anchors:
            fam, member = self.anchors[best], b
        else:
            bigger = best if self.nfile[best] > self.nfile[b] else b
            member = b if bigger == best else best
            fam = Family(bigger)
            self._add_anchor(bigger, fam)
        fam.members.append(Member(member, DIVERGED, best_j))
        self.member_of[member] = fam
        self.pair_sides.update((fam.anchor, member))
        self._absorb_pair(fam.anchor, member)
        return True

    def _holder(self, x, contents):
        """The lowest folder inside x (x itself at most) that holds all the
        files of x with one of `contents`."""
        end, parent, where = self.tree.end, self.tree.parent, self.where
        lo = hi = None
        for c in contents:
            folders = where[c]
            k = bisect_left(folders, x)
            if k < len(folders) and folders[k] < end[x]:
                lo = folders[k] if lo is None else min(lo, folders[k])
                hi = max(hi if hi is not None else -1,
                         folders[bisect_left(folders, end[x]) - 1])
        if lo is None:
            return x
        y = lo
        while not hi < end[y]:
            y = parent[y]
        return y

    def _absorb_pair(self, a, b):
        """a and b were found to be a diverged pair: copies between them
        (b/sub identical to a/sub) are part of that, not results of their
        own. Each member whose anchor is on the other side leaves its family
        and counts as kept again; copies of folders elsewhere stay."""
        alive, inside, end = self.alive, self._inside_of, self.tree.end
        families = {}
        for k in itertools.chain(range(a, end[a]), range(b, end[b])):
            fam = self.member_of.get(k) or self.anchors.get(k)
            if fam is not None:
                families[id(fam)] = fam
        for fam in families.values():
            side = inside(fam.anchor, a, b)
            if side is None:
                continue
            other = b if side == a else a
            keep = []
            for m in fam.members:
                if m.role == DIVERGED or inside(m.folder, a, b) != other:
                    keep.append(m)
                    continue
                for c, k in self._content(m.folder).items():
                    alive[c] += k
                self.gone[m.folder] = None
                del self.member_of[m.folder]
            fam.members = keep
            if not keep:
                self._drop_anchor(fam.anchor)

    def _anchors_in(self, x):
        a = self.anchor_list
        k = bisect_left(a, x)
        stop = self.tree.end[x]
        while k < len(a) and a[k] < stop:
            yield a[k]
            k += 1

    def _inside_of(self, f, a, b):
        """a or b, whichever folder f is in (None for neither)."""
        end = self.tree.end
        return a if a <= f < end[a] else b if b <= f < end[b] else None

    # ------------------------------------------------------------ totals

    def _finish(self):
        """Freed space per result, the space all of them free together, and
        result numbers (biggest first)."""
        tree = self.tree
        n = tree.nfolders
        inside = [None] * n             # the redundant folder each folder is in
        for i in range(n):
            g = self.gone[i]
            if g is None and tree.parent[i] >= 0:
                g = inside[tree.parent[i]]
            inside[i] = g
        self.inside = inside
        # Folders of diverged pairs: their shared files are explained by the
        # family card too (kept on both sides, to be merged).
        kept = [None] * n
        for fam in self.families:
            for m in fam.members:
                if m.role == DIVERGED:
                    kept[fam.anchor] = kept[m.folder] = fam
        for i in range(n):
            if kept[i] is None and tree.parent[i] >= 0:
                kept[i] = kept[tree.parent[i]]
        self.diverged_in = kept
        names_seen = Counter()
        rep = self.matcher.rep
        for j in range(tree.nfiles_total):
            if tree.nlink[j] > 1:
                names_seen[rep[j]] += 1
        self.names_seen = names_seen
        for fam in self.families:
            for m in fam.members:
                if m.role in REDUNDANT_ROLES:
                    m.freed = self._freed(m.folder)
                    fam.freed += m.freed
        for cov in self.covered:
            cov.freed = self._freed(cov.folder)
        self.families.sort(key=lambda f: (-f.freed, -self.nbytes[f.anchor], f.anchor))
        for k, fam in enumerate(self.families, 1):
            fam.id = f"F{k}"
        self.covered.sort(key=lambda c: (-c.freed, c.folder))
        for k, cov in enumerate(self.covered, 1):
            cov.id = f"C{k}"
        # Together: a file is freed when every name of it is in some redundant folder.
        freed, in_names = 0, Counter()
        for j in range(tree.nfiles_total):
            if tree.size[j] and inside[tree.fdir[j]] is not None:
                if tree.nlink[j] == 1:
                    freed += tree.size[j]
                else:
                    in_names[rep[j]] += 1
        for r, k in in_names.items():
            if k >= tree.nlink[r]:
                freed += tree.size[r]
        self.freed_total = freed

    def _freed(self, b):
        """Bytes freed by removing folder b (minus parts already counted for
        other results): files whose every name is inside."""
        tree, gone, end = self.tree, self.gone, self.tree.end
        rep = self.matcher.rep
        total, linked = 0, Counter()
        k, stop = b, end[b]
        while k < stop:
            if k != b and gone[k] is not None:
                k = end[k]
                continue
            for j in range(tree.ffirst[k], tree.ffirst[k] + tree.nfiles[k]):
                if not tree.size[j]:
                    continue
                if tree.nlink[j] == 1:
                    total += tree.size[j]
                else:
                    linked[rep[j]] += 1
            k += 1
        for r, count in linked.items():
            if count >= tree.nlink[r]:
                total += tree.size[r]
        return total

    def result_of(self, folder):
        """The id ("F3", "C7") of the result that removes this folder, or of
        the diverged family it belongs to; "" for neither."""
        r = self.inside[folder] or self.diverged_in[folder]
        return r.id if r is not None else ""

    # ------------------------------------------------------------ file groups

    def _file_groups(self):
        """Each duplicate content as a group of files, one name per file.
        Groups whose copies all but one sit in redundant folders are
        *folded*: the folder results already say everything about them."""
        tree, matcher = self.tree, self.matcher
        rep, inside = matcher.rep, self.inside
        out = []
        for c, files in matcher.groups():
            # One name per inode: a visible one where there is one.
            chosen = {}
            alive = {}                  # inode -> diverged family it is in (or None)
            for j in self.names.get(c, files):
                r = rep[j]
                visible = self.hidden or tree.file_visible(j)
                if inside[tree.fdir[j]] is None:
                    alive[r] = self.diverged_in[tree.fdir[j]]
                if visible and r not in chosen:
                    chosen[r] = j
            if len(chosen) < 2:
                continue
            listed = sorted(chosen.values())
            left = [alive[r] for r in chosen if r in alive]
            # Folded: at most one copy is left once the redundant folders go,
            # or the copies left are all in the two sides of one diverged pair.
            folded = len(left) <= 1 or (left[0] is not None and
                                        all(f is left[0] for f in left))
            explained = [self.result_of(tree.fdir[j]) for j in listed]
            out.append(Group(c, listed, tree.size[listed[0]], matcher.full[c], explained,
                             folded, category_of(tree.fname[listed[0]])))
        out.sort(key=lambda g: (-(g.size * (len(g.files) - 1)), g.files[0]))
        for k, g in enumerate(out, 1):
            g.id = k
        self.groups = out

    def totals(self):
        """Counts and sizes for the summary."""
        groups = self.groups
        redundant = sum(len(g.files) - 1 for g in groups)
        reclaim = sum(g.size * (len(g.files) - 1) for g in groups)
        cats = {}
        for g in groups:
            t = cats.setdefault(g.category, [0, 0, 0])
            t[0] += 1
            t[1] += len(g.files) - 1
            t[2] += g.size * (len(g.files) - 1)
        return {
            "groups": len(groups),
            "groups_shown": sum(1 for g in groups if not g.folded),
            "redundant": redundant,
            "reclaimable": reclaim,
            "families": len(self.families),
            "family_freed": sum(f.freed for f in self.families),
            "covered": len(self.covered),
            "covered_freed": sum(c.freed for c in self.covered),
            "folders_freed": self.freed_total,
            "categories": cats,
        }
