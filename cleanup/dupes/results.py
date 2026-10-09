"""
One duplicate scan's results as plain data (full paths as text, numbers),
from which the summary, the report page, files.tsv, folders.tsv and
data.json.gz are all made.

    families   [{id, kind, anchor, files, bytes, freed, types, members}]
               members: [{path, role, files, bytes, freed, shared, diff}]
    covered    [{id, path, files, bytes, freed, types, where, where_total}]
    groups     [{id, size, hash, category, folded, files, explained}]

`files` and `bytes` count a folder's non-empty files; `freed` is what removing
it would free (each file once, hard links only when every name goes). A
member's diff (dupes/diff.py) is None for identical copies.
"""

from collections import Counter

from cleanup.dupes import diff as _diff
from cleanup.dupes.folders import DIVERGED, IDENTICAL, PARTIAL, RENAMED
from cleanup.scan.filters import category_of

ROLE_ORDER = {IDENTICAL: 0, RENAMED: 1, PARTIAL: 2, DIVERGED: 3}


def collect(tree, matcher, rel):
    paths = tree.paths()
    cid = matcher.cid
    families = []
    for fam in rel.families:
        anchor_files = None
        members = []
        for m in sorted(fam.members, key=lambda m: (ROLE_ORDER[m.role], m.folder)):
            d = None
            if m.role != IDENTICAL:
                if anchor_files is None:
                    anchor_files = _diff.relative_files(tree, cid, fam.anchor)
                d = _diff.diff(tree, cid, fam.anchor, m.folder, anchor_files)
            members.append({"path": paths[m.folder], "role": m.role,
                            "files": rel.nfile[m.folder], "bytes": rel.nbytes[m.folder],
                            "freed": m.freed, "shared": m.shared, "diff": d})
        families.append({"id": fam.id, "kind": fam.kind, "anchor": paths[fam.anchor],
                         "files": rel.nfile[fam.anchor], "bytes": rel.nbytes[fam.anchor],
                         "freed": fam.freed, "types": types_in(tree, cid, fam.anchor),
                         "members": members})
    covered = [{"id": c.id, "path": paths[c.folder], "files": rel.nfile[c.folder],
                "bytes": rel.nbytes[c.folder], "freed": c.freed,
                "types": types_in(tree, cid, c.folder),
                "where": [[paths[f], n] for f, n in c.where], "where_total": c.where_total}
               for c in rel.covered]
    groups = [{"id": g.id, "size": g.size, "hash": g.full.hex(), "category": g.category,
               "folded": g.folded, "files": [tree.file_path(j) for j in g.files],
               "explained": g.explained}
              for g in rel.groups]
    return {"families": families, "covered": covered, "groups": groups,
            "totals": rel.totals()}


def types_in(tree, cid, folder):
    """The kinds of files in a folder ('Images', ...), most bytes first."""
    by = Counter()
    for j in tree.files_of(folder):
        if cid[j] > 0:
            by[category_of(tree.fname[j])] += tree.size[j]
    return [k for k, _ in by.most_common()]
