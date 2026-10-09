"""
Choosing what to remove from the results of a duplicate scan, and proving
that the choice never removes the last copy of anything.

    rules = Rules(keep_in=["/home/me/Pictures"], rank=parse_rank(["oldest"]))
    plan = select(manifest, rules)
    plan.items      what to remove (folders first, outermost first, then files)
    plan.blocked    what the rules picked but may not be removed, with why
    plan.survivors(content)   the copies that stay, to check before acting

The defaults follow the report: each family keeps its anchor (#1), and its
identical, renamed and partial copies go; covered folders go; diverged
copies stay (or are merged into the anchor, with --merge); in each file
group that is left, one copy stays, chosen by the rank rules.

A family doesn't keep a folder that goes anyway, inside one that another
family or a covered folder removes: it keeps the folder with the same files
in the copy that stays there, else another of its copies, else the deepest
folder in that copy holding all its files.

Rules:
  keep_in       folders whose contents are never removed (reference
                folders): a copy there is kept first, and a folder that holds
                one is never removed whole.
  remove_from   when given, only copies inside these folders are removed.
  rank          which copy of a file group to keep, the first rule deciding:
                first-root (in the order the scan was given its folders),
                oldest, newest, shortest (path), largest-folder (the folder
                with the most files), path-contains=TEXT.
  only          "folders" or "files": only that kind of result.
  merge         merge diverged copies into the anchor instead of keeping them.
  base          "rules" (the defaults above) or "none": nothing is picked
                but what `choices` asks for.
  per_group     "all" (every copy of a file group but one) or "one" (only
                the copy the rank rules like least).
  exts          when given, only files with these extensions are picked
                from the file groups.
  choices       the dashboard's per-item choices, which win over the rules:
                {"d<folder row>" or "f<file number>": "keep" or "remove"}.
                A kept folder or file is never removed, nor anything holding
                it; a removed one is picked even where the rules wouldn't
                (but never inside a keep_in folder). For a diverged folder,
                remove means merge it into the anchor where that is possible.

Look-alikes (`clean dupes --media`: the same photo or song in files whose
bytes differ) are never picked by the rules, only by a choice to remove one.

Validation then goes through the picks, outermost folders first, and keeps
for every content at least one file on disk (an inode) that nothing removes:
a pick that would take the last one is blocked. Two names of one hard-linked
file count as one: the other name never counts as a copy that survives. The
one exception is a file chosen to go that has look-alikes: it may take the
last copy of its content while another version of the photo or song stays,
so a look-alike set always keeps at least one version.
"""

import hashlib

from cleanup.actions.verify import MOUNTED_HERE, mounted_inside
from cleanup.fsutil import is_within

RANKS = ("first-root", "oldest", "newest", "shortest", "largest-folder", "path-contains")
DEFAULT_RANK = ["first-root", "oldest", "shortest"]

REDUNDANT = ("identical", "renamed", "partial")
CANDIDATE = ("identical", "renamed")     # may be kept instead of the anchor
BASES = ("rules", "none")
PER_GROUP = ("all", "one")
CHOICES = ("keep", "remove")


def parse_rank(items):
    """['oldest', 'path-contains=Camera'] -> [('oldest', None),
    ('path-contains', 'camera')]; an item may hold several rules separated
    by commas. Raises ValueError for anything else."""
    out = []
    for item in (part for text in items for part in text.split(",")):
        name, sep, arg = item.strip().partition("=")
        name = name.strip().lower()
        if name not in RANKS:
            raise ValueError(f"has an unknown rank rule {item.strip()!r}; the rules are "
                             + ", ".join(r + ("=TEXT" if r == "path-contains" else "")
                                         for r in RANKS))
        if (name == "path-contains") != bool(sep and arg):
            raise ValueError("needs a text after path-contains=, as in path-contains=Camera"
                             if name == "path-contains" else f"has {name}={arg}, but {name} "
                             "takes no value")
        out.append((name, arg.lower() if sep else None))
    return out


def check_rank(value):
    """Validate the remove.rank setting: returns it normalised."""
    return list(dict.fromkeys(n if a is None else f"{n}={a}" for n, a in parse_rank(value)))


def ext_of(name):
    """"jpg" for "IMG_1.JPG"; "" for "README" or ".bashrc"."""
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem else ""


def parse_choices(choices):
    """{"d12": "keep", "f7": "remove"} -> ({12: "keep"}, {7: "remove"}).
    Raises ValueError for anything else."""
    dirs, files = {}, {}
    if not isinstance(choices, (dict, type(None))):
        raise ValueError("has choices that are not a JSON object")
    for key, value in (choices or {}).items():
        if (not isinstance(key, str) or len(key) < 2 or key[0] not in "df"
                or not (key[1:].isascii() and key[1:].isdigit()) or value not in CHOICES):
            raise ValueError(f"has a choice {key!r}: {value!r}, which is not one clean makes")
        (dirs if key[0] == "d" else files)[int(key[1:])] = value
    return dirs, files


class Rules:
    def __init__(self, keep_in=(), remove_from=(), rank=None, family_rank=False,
                 only=None, merge=False, mounts=(), base="rules", per_group="all",
                 exts=(), choices=None):
        self.keep_in = [p.rstrip("/") or "/" for p in keep_in]
        self.mounts = [p.rstrip("/") or "/" for p in mounts]   # where drives are mounted
        self.remove_from = [p.rstrip("/") or "/" for p in remove_from]
        self.rank = list(rank) if rank is not None else parse_rank(DEFAULT_RANK)
        self.family_rank = family_rank      # rank also picks which family copy stays
        self.only = only
        self.merge = merge
        self.base = base
        self.per_group = per_group
        self.exts = {e.lower().lstrip(".") for e in exts}
        self.choices = dict(choices or {})


class Item:
    """One thing to remove (or merge)."""

    __slots__ = ("kind", "target", "path", "why", "ref", "into", "order", "chosen")

    def __init__(self, kind, target, path, why, ref, into=None, chosen=False):
        self.kind = kind            # "folder", "file" or "merge"
        self.target = target        # folder row, or file number
        self.path = path
        self.why = why              # "identical copy of /a/b"
        self.ref = ref              # ("F", 3): family 3; ("C", 5); ("G", 7); ("L", 2)
        self.into = into            # merge: the folder row it goes into
        self.order = 0
        self.chosen = chosen        # picked by a choice, not by the rules

    def __repr__(self):
        return f"Item({self.kind} {self.path!r})"


class Plan:
    def __init__(self, manifest):
        self.m = manifest
        self.items = []
        self.blocked = []           # [(Item, reason)]
        self.removed_keys = set()   # inode keys of every file that goes
        self.keepers = {}           # family id -> the folder row that stays
        self.group_keep = {}        # group id -> the file the rules keep
        self.versions = {}          # file -> the look-alike that stays for it,
                                    # when it takes the last copy of its content
        self._survivors = {}

    def files_of(self, item):
        """The files an item removes (for a merge: the folder merged away)."""
        if item.kind == "file":
            return [item.target]
        return self.m.files_in(item.target)

    def survivors(self, content):
        """Copies of `content` that no item removes: the copies an item
        removing it relies on, in the order to check them."""
        out = self._survivors.get(content)
        if out is None:
            m = self.m
            out = [f for f in m.copies[content] if m.inode_key(f) not in self.removed_keys]
            out.sort(key=lambda f: (m.top_of[m.files[f][0]], m.file_path(f)))
            self._survivors[content] = out
        return out

    def totals(self):
        """(bytes, files, folders) the items remove, each file on disk once;
        merged folders not counted."""
        seen, size, files, folders = set(), 0, 0, 0
        for it in self.items:
            if it.kind == "merge":
                continue
            folders += it.kind == "folder"
            for f in self.files_of(it):
                key = self.m.inode_key(f)
                if key not in seen:
                    seen.add(key)
                    size += self.m.files[f][2]
                    files += 1
        return size, files, folders


def select(m, rules):
    """The plan for manifest `m` under `rules`."""
    return _Selector(m, rules).run()


def digest(plan):
    """A short fingerprint of what a plan removes, merges and where: the same
    plan always gives the same text, any change a different one."""
    h = hashlib.sha256()
    m = plan.m
    for it in plan.items:
        into = m.paths[it.into] if it.into is not None else ""
        h.update(f"{it.kind}\0{it.path}\0{into}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()[:24]


class _Selector:
    def __init__(self, m, rules):
        self.m = m
        self.r = rules
        self.plan = Plan(m)
        self._cands = {}            # family id -> its candidate keepers, best first
        self._contents = {}         # folder row -> contents()
        self.auto = rules.base != "none"    # the rules pick, not only the choices
        self.ov_dirs, self.ov_files = parse_choices(rules.choices)
        self.ov_dirs = {d: v for d, v in self.ov_dirs.items() if 0 <= d < len(m.rows)}
        self.ov_files = {f: v for f, v in self.ov_files.items() if 0 <= f < len(m.files)}
        # folder rows that stay: never removed whole, nor what holds them
        self.keep_rows = {d for d, v in self.ov_dirs.items() if v == "keep"}
        self.kept_files = {}        # folder row -> a file in it chosen to keep
        for f, v in self.ov_files.items():
            if v == "keep":
                self.kept_files.setdefault(m.files[f][0], f)

    # ------------------------------------------------------------ path tests

    def protected(self, path, folder=False):
        """Inside a keep-in folder; for a folder, also: holds one."""
        for k in self.r.keep_in:
            if is_within(path, k) or (folder and is_within(k, path)):
                return True
        return False

    def removable_here(self, path):
        return not self.r.remove_from or any(is_within(path, d) for d in self.r.remove_from)

    def protected_why(self, path, folder):
        """Why a pick the choices asked for may not go, if keep_in says so."""
        for k in self.r.keep_in:
            if path.rstrip("/") == k:
                return "it is a keep-in folder: its copies are always kept"
            if is_within(path, k):
                return f"it is in {k}, whose copies are always kept"
            if folder and is_within(k, path):
                return f"it holds {k}, whose copies are always kept"
        return None

    # ------------------------------------------------------------ rank keys

    def file_key(self, f):
        m = self.m
        d, _, _, mtime = m.files[f][:4]
        path = m.file_path(f)
        out = []
        for name, arg in self.r.rank:
            if name == "first-root":
                out.append(m.top_of[d])
            elif name == "oldest":
                out.append(mtime)
            elif name == "newest":
                out.append(-mtime)
            elif name == "shortest":
                out.append((m.depth[d], len(path)))
            elif name == "largest-folder":
                out.append(-m.dir_files[d])
            else:
                out.append(0 if arg in path.lower() else 1)
        return out

    def folder_key(self, d):
        m = self.m
        files = m.files_in(d)
        latest = max((m.files[f][3] for f in files), default=0)
        path = m.paths[d]
        out = []
        for name, arg in self.r.rank:
            if name == "first-root":
                out.append(m.top_of[d])
            elif name == "oldest":
                out.append(latest)
            elif name == "newest":
                out.append(-latest)
            elif name == "shortest":
                out.append((m.depth[d], len(path)))
            elif name == "largest-folder":
                out.append(-len(files))
            else:
                out.append(0 if arg in path.lower() else 1)
        return out

    # ------------------------------------------------------------ picking

    def run(self):
        m, r = self.m, self.r
        picks = []
        if r.only != "files":
            picks += self.families()
            for cid, d in m.covered:
                path = m.paths[d]
                o = self.ov_dirs.get(d)
                if o == "keep" or (o is None and self.protected(path, folder=True)):
                    self.keep_rows.add(d)
                elif o == "remove" or (self.auto and self.removable_here(path)):
                    picks.append(Item("folder", d, path, "every file has a copy elsewhere",
                                      ("C", cid), chosen=o == "remove"))
        else:
            for fid, anchor, members in m.families:
                self.keep_rows.add(self.keeper(fid, anchor, members)[0])
        for i, it in enumerate(picks):
            it.order = i
        picks.sort(key=lambda it: (m.depth[it.target], it.order))
        self.validate(picks)
        if r.only != "folders":
            self.validate(self.groups())
            self.validate(self.lookalikes())
        return self.plan

    def keeper(self, fid, anchor, members, doomed=None):
        """(the folder of a family that stays, its role). A folder that goes
        with one another family removes is passed over: for its twin in the
        copy that family keeps, else for the next one. `doomed` is what
        would go, from would_go()."""
        m = self.m
        cands = self._cands.get(fid)
        if cands is None:
            def pref(c):
                d, role = c
                path = m.paths[d]
                # A copy chosen to stay stays, but doesn't displace the
                # one the rules keep: a choice changes only its own box.
                return (self.ov_dirs.get(d) == "remove",
                        not self.protected(path, folder=True), self.removable_here(path),
                        self.folder_key(d) if self.r.family_rank else [],
                        role != "anchor", path)
            cands = [(anchor, "anchor")] + [(d, role) for d, role in members
                                             if role in CANDIDATE]
            cands = self._cands[fid] = sorted(cands, key=pref)
        if not doomed:
            return cands[0]
        holder = None
        for d, role in cands:
            hit = self.doomed_by(d, ("F", fid), doomed)
            if hit is None:
                return d, role
            if hit[1] is None:
                continue
            twin, held = self.stays_for(d, hit[0], hit[1], doomed)
            if twin is not None:
                same = role != "renamed" and self.layout(twin) == self.layout(d)
                return twin, "identical" if same else "renamed"
            holder = holder or held
        if holder is not None:
            return holder, "holder"         # every file of the family is in it
        return cands[0]

    @staticmethod
    def others(anchor, members, keep, keep_role):
        """A family's members but the one kept, with their roles to it."""
        others = [(d, role) for d, role in members if d != keep]
        if keep != anchor:
            # Roles are relative to the anchor; to a renamed keeper, the
            # anchor and its identical copies are renamed copies too, and a
            # folder holding the anchor's files holds those of all but the
            # diverged ones.
            others.insert(0, (anchor, "identical"))
            if keep_role == "renamed":
                others = [(d, "renamed" if role == "identical" else role)
                          for d, role in others]
            elif keep_role == "holder":
                others = [(d, role if role == "diverged" else "partial")
                          for d, role in others]
        return others

    def goes(self, d):
        path = self.m.paths[d]
        return not self.protected(path, folder=True) and self.removable_here(path)

    def picked(self, d, role):
        """Is a family member or covered folder picked, choices first?"""
        o = self.ov_dirs.get(d)
        if o is not None:
            return o == "remove"
        if role == "diverged":                  # merged away, then gone
            return self.auto and self.r.merge and self.goes(d)
        return self.auto and role in REDUNDANT + ("covered",) and self.goes(d)

    def would_go(self, keepers):
        """{folder: (who removes it, the folder that stays in its place or
        None)}: the folders the families, covered folders and choices would
        remove with these keepers, before validation."""
        # (a folder chosen to go goes whatever its families say)
        out = {d: (("U", d), None) for d, o in self.ov_dirs.items() if o == "remove"}
        for (fid, anchor, members), (keep, role) in zip(self.m.families, keepers):
            for d, r in self.others(anchor, members, keep, role):
                if self.picked(d, r):
                    out.setdefault(d, (("F", fid), keep))
        for cid, d in self.m.covered:
            if self.picked(d, "covered"):
                out.setdefault(d, (("C", cid), None))
        return out

    def doomed_by(self, d, who, doomed):
        """(the folder d would go with, the folder kept in its place), when
        something other than `who` removes d or a folder above it."""
        while d >= 0:
            hit = doomed.get(d)
            if hit is not None and hit[0] != who:
                return d, hit[1]
            d = self.m.parent[d]
        return None

    def stays_for(self, d, top, keep, doomed):
        """Folders in `keep` that nothing removes, for d, which sits in `top`,
        a copy of `keep` that goes: (one with the same files as d, the
        deepest one with a copy of each), either None if there is none."""
        m = self.m
        want = self.contents(d)
        names = []
        while d != top:
            names.append(m.rows[d][1])
            d = m.parent[d]
        at = keep                           # first: the same place in keep
        for name in reversed(names):
            at = next((k for k in m.subdirs[at] if m.rows[k][1] == name), None)
            if at is None:
                break
        if (at is not None and self.contents(at) == want
                and self.doomed_by(at, None, doomed) is None):
            return at, at
        need = set(want) - {0}
        twins, holders, stack = [], [], [keep]
        while stack:                        # only where every file has a copy
            k = stack.pop()
            if not need <= set(self.contents(k)) or self.doomed_by(k, None, doomed):
                continue
            stack.extend(m.subdirs[k])
            holders.append(k)
            if self.contents(k) == want:
                twins.append(k)
        twin = min(twins, key=lambda k: (m.depth[k], m.paths[k]), default=None)
        held = min(holders, key=lambda k: (-m.depth[k], m.paths[k]), default=None)
        return twin, held

    def contents(self, d):
        """The content ids of every file in d, sorted."""
        out = self._contents.get(d)
        if out is None:
            out = self._contents[d] = tuple(sorted(self.m.files[f][4]
                                                   for f in self.m.files_in(d)))
        return out

    def layout(self, d):
        """Every file in d as (path below d, content id), sorted."""
        m = self.m
        cut = len(m.paths[d].rstrip("/")) + 1
        return sorted((m.file_path(f)[cut:], m.files[f][4]) for f in m.files_in(d))

    def families(self):
        m = self.m
        fams = m.families
        keepers = [self.keeper(fid, anchor, members) for fid, anchor, members in fams]
        # A keeper inside a folder another family removes would block that
        # removal; choose again until the choices settle.
        for _ in range(len(fams)):
            doomed = self.would_go(keepers)
            again = [self.keeper(fid, anchor, members, doomed)
                     for fid, anchor, members in fams]
            if again == keepers:
                break
            keepers = again
        doomed = self.would_go(keepers)
        picks = []
        for (fid, anchor, members), (keep, keep_role) in zip(fams, keepers):
            self.plan.keepers[fid] = keep
            keep_path = m.paths[keep]
            last = None
            if self.ov_dirs.get(keep) == "remove":
                # Every copy that could stay was chosen to go: validation
                # takes the others first, so that if one must stay, it is
                # this one.
                last = Item("folder", keep, keep_path, "you chose to remove it", ("F", fid),
                            chosen=True)
            elif self.auto or self.ov_dirs.get(keep) == "keep":
                self.keep_rows.add(keep)
            for d, role in self.others(anchor, members, keep, keep_role):
                path = m.paths[d]
                o = self.ov_dirs.get(d)
                if o == "keep":
                    self.keep_rows.add(d)
                    continue
                if role == "diverged":
                    mergeable = (keep == anchor and not self.protected(path, True)
                                 and not m.inside(d, anchor) and not m.inside(anchor, d)
                                 and self.doomed_by(anchor, None, doomed) is None)
                    if mergeable and (o == "remove" or (self.auto and self.r.merge
                                                        and self.removable_here(path))):
                        picks.append(Item("merge", d, path, f"diverged copy of {keep_path}",
                                          ("F", fid), into=anchor, chosen=o == "remove"))
                    elif o == "remove":
                        picks.append(Item("folder", d, path, f"diverged copy of {keep_path}",
                                          ("F", fid), chosen=True))
                    elif self.auto or self.protected(path, True):
                        self.keep_rows.add(d)
                    continue
                what = {"identical": "identical copy of", "renamed": "same files as",
                        "partial": "all its files are in"}[role]
                if o == "remove" or (self.auto and self.goes(d)):
                    picks.append(Item("folder", d, path, f"{what} {keep_path}", ("F", fid),
                                      chosen=o == "remove"))
                elif self.auto or self.protected(path, True):
                    self.keep_rows.add(d)
            if last is not None:
                picks.append(last)
        return picks

    def groups(self):
        """File picks from the duplicate groups, after the folders: in each,
        the copies not already going with a folder, minus the one to keep,
        and minus those in folders that stay (a kept folder is not hollowed
        out, unless --remove-from names it)."""
        m, plan = self.m, self.plan
        gone_rows = set()
        for it in plan.items:
            if it.kind != "file":
                gone_rows.update(m.subtree(it.target))
        kept_rows = set()
        for d in self.keep_rows:
            kept_rows.update(m.subtree(d))
        picks = []
        hollow = bool(self.r.remove_from)   # copies in a kept folder may go too
        ov, exts = self.ov_files, self.r.exts
        for gid, folded, files in m.groups:
            left = [f for f in files if m.files[f][0] not in gone_rows]
            if len(left) < 2 and not any(ov.get(f) == "remove" for f in left):
                continue
            paths = {f: m.file_path(f) for f in left}

            def pref(f):
                return (ov.get(f) == "remove",
                        not self.protected(paths[f]), self.removable_here(paths[f]),
                        m.files[f][0] not in kept_rows, self.file_key(f), paths[f])
            ranked = sorted(left, key=pref)
            keep = ranked[0]
            self.plan.group_keep[gid] = keep
            chosen, auto = [], []
            for f in ranked:
                o = ov.get(f)
                if o == "remove":
                    chosen.append(f)
                elif (o is None and self.auto and f != keep and not self.protected(paths[f])
                        and self.removable_here(paths[f])
                        and (m.files[f][0] not in kept_rows or hollow)
                        and (not exts or ext_of(m.files[f][1]) in exts)):
                    auto.append(f)
            if self.r.per_group == "one":
                auto = auto[-1:]
            # Worst first, so if every copy was chosen to go, the one the
            # rules like best is the one validation keeps.
            for f in sorted(chosen + auto, key=pref, reverse=True):
                other = keep if f != keep else next((g for g in ranked if g != f), None)
                why = f"copy of {paths[other]}" if other is not None else "you chose to remove it"
                picks.append(Item("file", f, paths[f], why, ("G", gid), chosen=f in chosen))
        return picks

    def lookalikes(self):
        """File picks from the look-alikes: only files chosen to go, and only
        those no duplicate group has decided on already."""
        m, plan = self.m, self.plan
        in_groups = {f for _, _, files in m.groups for f in files}
        picks = []
        for lid, kind, files in m.lookalikes:
            # worst first: when every version is ticked, the best one stays
            for f in reversed(files):
                if (self.ov_files.get(f) != "remove" or f in in_groups
                        or m.inode_key(f) in plan.removed_keys):
                    continue
                others = m.versions(f)
                why = (f"same {kind} as {m.file_path(others[0])} (not byte-identical)"
                       if others else "you chose to remove it")
                picks.append(Item("file", f, m.file_path(f), why, ("L", lid), chosen=True))
        return picks

    # ------------------------------------------------------------ validation

    def validate(self, picks):
        """Accept picks in order while each leaves a copy of everything."""
        m, plan = self.m, self.plan
        alive = self.alive
        gone_rows = set()
        for it in plan.items:
            if it.kind != "file":
                gone_rows.update(m.subtree(it.target))
        intos = [it for it in plan.items if it.kind == "merge"]
        for it in picks:
            if it.kind == "file":
                if m.files[it.target][0] in gone_rows:
                    continue                    # goes with its folder already
            elif it.target in gone_rows:
                continue
            else:
                if it.kind == "merge" and it.into in gone_rows:
                    self.block(it, f"{m.paths[it.into]}, which it would be merged into, goes")
                    continue
                taker = next((x for x in intos if m.inside(x.into, it.target)), None)
                if taker is not None:
                    self.block(it, f"{m.paths[taker.into]} in it takes in what is only in "
                                   f"{taker.path}")
                    continue
                held = self.holds_kept(it.target)
                if held is not None:
                    path, chosen = held
                    self.block(it, f"it holds {path}, which "
                                   + ("you chose to keep" if chosen else "is kept"))
                    continue
                mount = self.mount_in(it.path)
                if mount is not None:
                    self.block(it, MOUNTED_HERE if mount == it.path.rstrip("/")
                               else mounted_inside(mount))
                    continue
            if it.chosen:
                reason = self.protected_why(it.path, it.kind != "file")
                if reason:
                    self.block(it, reason)
                    continue
            files = plan.files_of(it)
            moved = set()
            if it.kind == "merge":
                into = {m.files[f][4] for f in m.files_in(it.into)}
                moved = {m.files[f][4] for f in files} - into
            need = {}
            for f in files:
                c = m.files[f][4]
                if c and c not in moved:
                    need.setdefault(c, set()).add(m.inode_key(f))
            lost = next((c for c, keys in need.items() if not (alive(c) - keys)), None)
            if lost is not None and it.kind == "file" and it.chosen and m.versions(it.target):
                other = self.version_left(it.target)
                if other is None:
                    self.block(it, "it is the last version left: every look-alike of it goes")
                    continue
                plan.versions[it.target] = other
                kind = m.lookalikes[m.lookalike_of[it.target]][1]
                it.why = (f"same {kind} as {m.file_path(other)}, which stays "
                          f"(not byte-identical)")
                lost = None
            if lost is not None:
                self.block(it, self.last_copy(it, lost))
                continue
            for c, keys in need.items():
                alive(c).difference_update(keys)
            for f in files:
                if m.files[f][4] in moved:  # its new place is not counted on
                    alive(m.files[f][4]).discard(m.inode_key(f))
            plan.removed_keys.update(m.inode_key(f) for f in files)
            plan.items.append(it)
            if it.kind == "merge":
                intos.append(it)
            if it.kind != "file":
                gone_rows.update(m.subtree(it.target))

    def block(self, it, reason):
        self.plan.blocked.append((it, reason))
        if it.kind != "file":
            self.keep_rows.add(it.target)   # stays whole: its files stay too

    _alive = None

    def alive(self, c):
        """Inode keys holding content c that nothing removes yet."""
        if self._alive is None:
            self._alive = {}
        s = self._alive.get(c)
        if s is None:
            s = self._alive[c] = {self.m.inode_key(f) for f in self.m.copies[c]}
        return s

    def version_left(self, f):
        """A file of another version of f's photo or song that nothing
        removes, or None."""
        m = self.m
        for g in m.versions(f):
            c = m.files[g][4]
            left = self.alive(c)
            if left:
                return next((h for h in m.copies[c] if m.inode_key(h) in left), g)
        return None

    def mount_in(self, path):
        """A mount point that is `path` or inside it, or None."""
        return next((p for p in self.r.mounts if p != "/" and is_within(p, path)), None)

    def holds_kept(self, d):
        """(the path of a kept folder or a file chosen to keep that is d or
        inside it, whether a choice keeps it), or None."""
        for k in self.m.subtree(d):
            if k in self.keep_rows:
                return self.m.paths[k], self.ov_dirs.get(k) == "keep"
            f = self.kept_files.get(k)
            if f is not None:
                return self.m.file_path(f), True
        return None

    def last_copy(self, it, c):
        m = self.m
        f = next(f for f in self.plan.files_of(it) if m.files[f][4] == c)
        if it.kind == "file":
            return "it is the last copy left"
        return f"it holds the last copy left of {m.file_path(f)}"
