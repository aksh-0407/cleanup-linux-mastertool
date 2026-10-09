"""
manifest.json.gz: what a duplicate scan saw, in enough detail for `clean
remove` to act on its results later without scanning again, and to check
first that nothing has changed since.

    {"format": 1, "roots": [...], "junk": [...],
     "dirs": [[parent, name]],       folder table, as in data.json.gz (a top
                                     entry holds a root's full path)
     "dir_files": [n],               files the scan saw directly in each folder
     "listed": [dir],                folders listed completely: every folder
                                     of a result; all their subfolders are in
                                     the table and all their files in "files"
     "files": [[dir, name, size, mtime_ns, content, inode]],
     "hashes": [hex or null],        per content: its blake2b-512, or null
                                     when it has no copy (never read whole)
                                     and is no look-alike
     "families": [[id, anchor, [[dir, role]]]],
     "covered": [[id, dir]],
     "groups": [[id, folded, [file]]],
     "lookalikes": [[id, kind, [file]]]}   with --media: files showing the
                                     same photo or song ("photo", "song"),
                                     best version first

Content 0 is the empty file. `inode` is 0, or a number shared by the names
of one hard-linked file. "files" holds every file inside a listed folder,
and every name of every content that has a copy anywhere or is a
look-alike, hidden ones included: the copies a removal can fall back on.
"""

from cleanup.report import data

FORMAT = 1
NAME = "manifest.json.gz"


def build(tree, matcher, rel, junk_names, lookalikes=None):
    """The manifest of one finished scan, as plain data for data.encode().
    `lookalikes` is the finished lookalikes.Finder of a --media scan."""
    cid, rep, full = matcher.cid, matcher.rep, matcher.full
    if lookalikes is not None and lookalikes.groups:
        full = dict(full)
        full.update(lookalikes.contents())
    row = {}                    # tree folder -> row in "dirs"
    rows, dir_files = [], []

    def add_dir(i):
        chain = []
        while i >= 0 and i not in row:
            chain.append(i)
            i = tree.parent[i]
        for k in reversed(chain):
            p = tree.parent[k]
            row[k] = len(rows)
            rows.append([row[p] if p >= 0 else -1, tree.name[k]])
            dir_files.append(tree.nfiles[k])
        return row[chain[0]] if chain else row[i]

    for r in tree.root_index:
        add_dir(r)
    tops = [fam.anchor for fam in rel.families]
    tops += [m.folder for fam in rel.families for m in fam.members]
    tops += [c.folder for c in rel.covered]
    wanted = set()              # file numbers
    listed = []
    for top in sorted(set(tops)):
        listed.append(add_dir(top))
        for k in range(top, tree.end[top]):
            add_dir(k)
        wanted.update(tree.files_of(top))
    for j in range(tree.nfiles_total):
        if cid[j] > 0 and cid[j] in full:
            wanted.add(j)

    content = {0: 0}
    hashes = [None]
    inode = {}
    files, index = [], {}
    for j in sorted(wanted):
        c = cid[j]
        if c not in content:
            content[c] = len(hashes)
            hashes.append(full[c].hex() if c in full else None)
        node = 0
        if tree.nlink[j] > 1 and tree.size[j]:
            node = inode.setdefault(rep[j], len(inode) + 1)
        index[j] = len(files)
        files.append([add_dir(tree.fdir[j]), tree.fname[j], tree.size[j], tree.mtime[j],
                      content[c], node])
    return {
        "format": FORMAT, "roots": tree.roots, "junk": list(junk_names),
        "dirs": rows, "dir_files": dir_files, "listed": listed,
        "files": files, "hashes": hashes,
        "families": [[fam.id, row[fam.anchor], [[row[m.folder], m.role] for m in fam.members]]
                     for fam in rel.families],
        "covered": [[c.id, row[c.folder]] for c in rel.covered],
        "groups": [[g.id, g.folded, [index[j] for j in g.files]] for g in rel.groups],
        "lookalikes": [[g.id, g.kind, [index[j] for j in g.files]]
                       for g in (lookalikes.groups if lookalikes is not None else ())],
    }


class Manifest:
    """A loaded manifest, with the lookups removal needs."""

    def __init__(self, obj):
        if not isinstance(obj, dict) or obj.get("format") != FORMAT:
            raise ValueError("not a manifest this version of clean can read")
        self.roots = list(obj["roots"])
        self.junk = obj.get("junk", [])
        self.rows = obj["dirs"]
        self.dir_files = obj["dir_files"]
        self.listed = obj["listed"]
        self.files = obj["files"]
        self.hashes = obj["hashes"]
        self.families = obj["families"]
        self.covered = obj["covered"]
        self.groups = obj["groups"]
        self.lookalikes = obj.get("lookalikes", [])
        self.lookalike_of = {}      # file -> its look-alike set (position in lookalikes)
        for k, (_, _, files) in enumerate(self.lookalikes):
            for f in files:
                self.lookalike_of[f] = k
        self.parent = [p for p, _ in self.rows]
        self.paths = data.paths_of(self.rows)
        n = len(self.rows)
        self.subdirs = [[] for _ in range(n)]
        for d, p in enumerate(self.parent):
            if p >= 0:
                self.subdirs[p].append(d)
        self.dir_of_file = [[] for _ in range(n)]
        self.copies = [[] for _ in range(len(self.hashes))]
        for f, entry in enumerate(self.files):
            self.dir_of_file[entry[0]].append(f)
            self.copies[entry[4]].append(f)
        self.top_of = []            # each folder's root (by its position in roots)
        for d, p in enumerate(self.parent):
            self.top_of.append(self.roots.index(self.rows[d][1])
                               if p < 0 and self.rows[d][1] in self.roots
                               else (self.top_of[p] if p >= 0 else len(self.roots)))
        self.depth = []
        for d, p in enumerate(self.parent):
            self.depth.append(self.depth[p] + 1 if p >= 0 else 0)

    @classmethod
    def read(cls, path):
        return cls(data.read(path))

    def relocate(self, moves):
        """Paths under the old root `old` are now under `new`, for each
        (old, new) in moves: the drive was mounted somewhere else."""
        moves = dict(moves)
        for d, (p, name) in enumerate(self.rows):
            if p < 0 and name in moves:
                self.rows[d] = [p, moves[name]]
        self.roots = [moves.get(r, r) for r in self.roots]
        self.paths = data.paths_of(self.rows)

    # ------------------------------------------------------------ lookups

    def file_path(self, f):
        entry = self.files[f]
        return self.paths[entry[0]].rstrip("/") + "/" + entry[1]

    def subtree(self, d):
        """Folder d and every folder below it."""
        out, stack = [], [d]
        while stack:
            k = stack.pop()
            out.append(k)
            stack.extend(self.subdirs[k])
        return out

    def files_in(self, d):
        """Every file anywhere inside folder d."""
        return [f for k in self.subtree(d) for f in self.dir_of_file[k]]

    def inside(self, d, top):
        """Is folder d top or inside it?"""
        while d >= 0:
            if d == top:
                return True
            d = self.parent[d]
        return False

    def versions(self, f):
        """The other versions of file f's photo or song: one file of each
        other content in its look-alike set, best first; [] for none."""
        k = self.lookalike_of.get(f)
        if k is None:
            return []
        own = self.files[f][4]
        out, seen = [], {own}
        for g in self.lookalikes[k][2]:
            c = self.files[g][4]
            if c not in seen:
                seen.add(c)
                out.append(g)
        return out

    def inode_key(self, f):
        """One key per file on disk: hard-linked names share it."""
        node = self.files[f][5]
        return ("i", node) if node else ("f", f)
