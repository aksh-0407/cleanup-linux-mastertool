"""
How a family member differs from its anchor, file by file:

    only_here    in the member only
    only_there   in the anchor only (both list whole folders once, under
                 "folders", as [folder, files])
    changed      the same path in both, different contents
    moved        the same contents at another path (renamed or moved), as
                 [path in the anchor, path in the member]; files that moved
                 with their folder are listed once per folder under
                 "folders", as [folder in the anchor, in the member, files]

Paths are relative to each folder. Contents are compared by content id
(dupes/files.py), so this needs no reads. Long lists are cut at CAP entries;
the counts are always complete.
"""

from collections import Counter, defaultdict

CAP = 10000


def relative_files(tree, cid, folder):
    """{path relative to folder: content id} of every file below it."""
    paths = tree.paths()
    base = paths[folder].rstrip("/")
    cut = len(base) + 1
    out = {}
    for j in tree.files_of(folder):
        d = paths[tree.fdir[j]][cut:]
        out[(d + "/" if d else "") + tree.fname[j]] = cid[j]
    return out


def diff(tree, cid, anchor, member, anchor_files=None):
    """The differences between `member` and `anchor` (folder numbers).
    `anchor_files` may be given when one anchor is compared many times."""
    there = relative_files(tree, cid, anchor) if anchor_files is None else anchor_files
    here = relative_files(tree, cid, member)
    changed, only_here = [], []
    for rel, c in here.items():
        other = there.get(rel)
        if other is None:
            only_here.append(rel)
        elif other != c:
            changed.append(rel)
    only_there = [rel for rel in there if rel not in here]
    # The same content at different paths: moved or renamed (empty files
    # are all alike, so they are never paired up).
    lost, found = defaultdict(list), defaultdict(list)
    for rel in only_there:
        if there[rel] > 0:
            lost[there[rel]].append(rel)
    for rel in only_here:
        if here[rel] > 0:
            found[here[rel]].append(rel)
    moved, paired = [], set()
    for c, olds in lost.items():
        news = found.get(c)
        if not news:
            continue
        for old, new in zip(sorted(olds), sorted(news)):
            moved.append([old, new])
            paired.add(("there", old))
            paired.add(("here", new))
    only_here = [r for r in only_here if ("here", r) not in paired]
    only_there = [r for r in only_there if ("there", r) not in paired]
    out = {"changed": _capped(changed)}
    for name, only, every in (("only_here", only_here, here), ("only_there", only_there, there)):
        folders, single = _whole_folders(only, every)
        out[name] = dict(_capped(single), count=len(only), folders=folders[:CAP])
    folders, single = _moved_folders(moved)
    out["moved"] = dict(_capped(single), count=len(moved), folders=folders[:CAP])
    return out


def _whole_folders(only, every):
    """The outermost folders all of whose files (two or more) are in `only`,
    biggest first: ([[folder, files]], the paths in no such folder).
    `every`: all the paths on that side."""
    def folders_of(rel):
        k = rel.find("/")
        while k >= 0:
            yield rel[:k]
            k = rel.find("/", k + 1)
    if not only:
        return [], []
    total, hit = Counter(), Counter()
    for rel in every:
        total.update(folders_of(rel))
    for rel in only:
        hit.update(folders_of(rel))
    whole = {d for d, n in hit.items() if n >= 2 and n == total[d]}
    found, single = Counter(), []
    for rel in only:
        top = next((d for d in folders_of(rel) if d in whole), None)
        if top is None:
            single.append(rel)
        else:
            found[top] += 1
    folders = sorted(([d, n] for d, n in found.items()), key=lambda f: (-f[1], f[0]))
    return folders, single


def _moved_folders(moved):
    """Moves that keep the end of the path (P/a/x.jpg -> B/P/a/x.jpg) as one
    entry per pair of folders, biggest first: ([[old, new, files]], the
    other moves). The folder names are as short as can be, but take in the
    first folder they share when one side would be the top itself."""
    groups = defaultdict(list)
    single = []
    for old, new in moved:
        o, n = old.split("/"), new.split("/")
        k = 0
        while k < min(len(o), len(n)) and o[-1 - k] == n[-1 - k]:
            k += 1
        if k == 0:
            single.append([old, new])      # the file itself was renamed
        else:
            groups["/".join(o[:-k]), "/".join(n[:-k])].append((old, new, o[-k:]))
    folders = []
    for (a, b), items in groups.items():
        if len(items) < 2:
            single.extend([old, new] for old, new, _ in items)
            continue
        firsts = {rest[0] for _, _, rest in items if len(rest) > 1}
        if (not a or not b) and len(firsts) == 1 and all(len(r) > 1 for _, _, r in items):
            top = firsts.pop()
            a, b = (a + "/" if a else "") + top, (b + "/" if b else "") + top
        folders.append([a, b, len(items)])
    folders.sort(key=lambda f: (-f[2], f[0], f[1]))
    return folders, single


def _capped(items):
    items = sorted(items)
    return {"count": len(items), "items": items[:CAP]}


def shared_counts(tree, cid, anchor, member):
    """(contents in both, in the member only, in the anchor only), counting
    distinct contents."""
    a = {cid[j] for j in tree.files_of(anchor) if cid[j] > 0}
    m = {cid[j] for j in tree.files_of(member) if cid[j] > 0}
    return len(a & m), len(m - a), len(a - m)
