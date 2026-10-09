"""
What `clean space` shows: the headline, summary.txt (and the terminal's
shorter copy) and report.html, all made from the run's data.json.gz document
(commands/space.py builds it), so the dashboard can show any saved run the
same way.

The folder table in the document holds the folders worth showing (1 MB or
more, and every root), parents first:

    [parent, name, disk, size, files, folders, flags, own disk, own files, look]

disk is what the folder takes on the disk with everything inside it; own disk
and own files are its files directly inside.
"""

import heapq
import os

from cleanup.fsutil import human, printable
from cleanup.report import data
from cleanup.report.html import (_callout, _footer, _header, _issues_table, _stats, esc,
                                 page as _page)
from cleanup.report.text import RULE, TERMINAL_ISSUES, _issues, _saved, duration, plural, when
from cleanup.space import EXCLUDED, MOUNTS, RECENT_SECONDS, UNREADABLE

PARENT, NAME, DISK, SIZE, FILES, DIRS, FLAGS, OWN, OWN_FILES, LOOK = range(10)
BAR = 12                    # characters in a terminal bar
EXPAND = 0.10               # a folder this share of its root's size is opened in the terminal
DEEP_KIDS = 5               # folders shown inside an opened one
SUMMARY_TOP = 50            # folders and files in summary.txt
TERMINAL_ITEMS = 5          # clutter items listed per kind in the terminal
SUMMARY_ITEMS = 200         # ... in summary.txt
TREE_NODES = 2000           # folders in the report page's tree
TREE_KIDS = 60              # folders shown inside one folder there
PAGE_ITEMS = 2000           # items per clutter list on the page
MAX_INDENT = 24             # deeper folders in the tree are indented no further

CLUTTER = (("zero", "Zero-byte files", "zero-byte file"),
           ("broken", "Broken symlinks", "broken symlink"),
           ("temp", "Temp leftovers", "temp leftover"),
           ("empty", "Empty folders", "empty folder"))


# ---------------------------------------------------------------- reading the document

class Doc:
    """Helpers over a space document: children, paths, sums."""

    def __init__(self, doc):
        self.doc = doc
        self.folders = doc.get("folders") or []
        self.kids = [[] for _ in self.folders]
        for n, f in enumerate(self.folders):
            if f[PARENT] >= 0:
                self.kids[f[PARENT]].append(n)
        for k in self.kids:
            k.sort(key=lambda n: (-self.folders[n][DISK], self.folders[n][NAME]))
        self.big = {}
        for n, name, disk, size, mtime in doc.get("big") or []:
            self.big.setdefault(n, []).append((name, disk, size, mtime))
        self._paths = {}
        self.dirs = data.paths_of(doc.get("dirs") or [])

    def path(self, n):
        p = self._paths.get(n)
        if p is None:
            chain = []
            while n >= 0 and n not in self._paths:
                chain.append(n)
                n = self.folders[n][PARENT]
            base = self._paths.get(n) if n >= 0 else None
            for m in reversed(chain):
                f = self.folders[m]
                base = f[NAME] if f[PARENT] < 0 else os.path.join(base, f[NAME])
                self._paths[m] = base
            p = base
        return p

    def roots(self):
        return [n for n, f in enumerate(self.folders) if f[PARENT] < 0]

    def item_path(self, item):
        return os.path.join(self.dirs[item[0]], item[1])

    def more(self, n, hidden=()):
        """(bytes, folders in all) inside folder n that its shown sub-folders
        and its own files don't account for: the `hidden` ones of its
        sub-folders, with everything inside them, and those too small to be
        in the table."""
        f = self.folders[n]
        shown = [k for k in self.kids[n] if k not in hidden] if hidden else self.kids[n]
        nbytes = f[DISK] - f[OWN] - sum(self.folders[k][DISK] for k in shown)
        count = f[DIRS] - sum(1 + self.folders[k][DIRS] for k in shown)
        return (max(nbytes, 0), count) if count > 0 else (0, 0)


def reclaim(doc):
    """{what: bytes} that clean knows can go, by the commands it lists."""
    out = {"caches": sum(c["frees"] for c in doc.get("caches") or []),
           "system": sum(c["frees"] for c in doc.get("system_caches") or []),
           "trash": sum(t["bytes"] for t in doc.get("trash") or []),
           "temp": (doc.get("clutter") or {}).get("temp", {}).get("bytes", 0)}
    return out


def headline(doc):
    if doc.get("stopped"):
        return doc["stopped"][0].upper() + doc["stopped"][1:]
    t = doc["total"]
    roots = doc["roots"]
    where = (printable(_tilde(roots[0], os.path.expanduser("~")))
             + (f" and {len(roots) - 1} more" if len(roots) > 1 else ""))
    r = reclaim(doc)
    could = r["caches"] + r["system"] + r["trash"] + r["temp"]
    text = f"{human(t['disk'])} in {where}"
    if could:
        text += f"; about {human(could)} could be freed (caches, Trash, temp files)"
    return text


def totals(doc):
    if doc.get("stopped"):
        return {}
    t = doc["total"]
    r = reclaim(doc)
    c = doc["clutter"]
    return {"disk": t["disk"], "files": t["files"], "folders": t["dirs"],
            "caches": r["caches"] + r["system"], "trash": r["trash"],
            "clutter": sum(c[k]["count"] for k, _, _ in CLUTTER),
            "errors": doc.get("errors_total", 0)}


# ---------------------------------------------------------------- text

def _bar(share):
    share = max(0.0, min(1.0, share))
    eighths = int(round(share * BAR * 8))
    full, part = divmod(eighths, 8)
    return ("█" * full + ("▏▎▍▌▋▊▉"[part - 1] if part else "")).ljust(BAR)


def _size(n):
    return f"{human(n):>9}"


def _flags_text(f):
    bits = []
    if f[FLAGS] & UNREADABLE:
        bits.append("some of it unreadable: at least this")
    if f[FLAGS] & MOUNTS:
        bits.append("another drive inside, not counted")
    if f[FLAGS] & EXCLUDED:
        bits.append("some of it left out by --exclude")
    return f"  ({'; '.join(bits)})" if bits else ""


def _tree_lines(d, top, deep, home):
    lines = []
    for r in d.roots():
        root = d.folders[r]
        lines.append(f"  {_size(root[DISK])}  {'':{BAR}}  {printable(_tilde(root[NAME], home))}"
                     + _flags_text(root))
        out = []

        def walk(n, depth):
            f = d.folders[n]
            kids = d.kids[n]
            limit = top if depth == 1 else DEEP_KIDS
            for k in kids[:limit]:
                kf = d.folders[k]
                share = kf[DISK] / f[DISK] if f[DISK] else 0
                out.append(f"  {_size(kf[DISK])}  {_bar(share)}  {'  ' * depth}"
                           f"{printable(kf[NAME])}/" + _flags_text(kf))
                if depth < deep and root[DISK] and kf[DISK] >= EXPAND * root[DISK] and d.kids[k]:
                    walk(k, depth + 1)
            nbytes, nmore = d.more(n, kids[limit:])
            if nmore > 0:
                out.append(f"  {_size(nbytes)}  {'':{BAR}}  {'  ' * depth}"
                           f"+ {plural(nmore, 'more folder')}")
            if f[OWN_FILES] and (depth == 1 or f[OWN] >= EXPAND * root[DISK]):
                out.append(f"  {_size(f[OWN])}  {'':{BAR}}  {'  ' * depth}"
                           f"{plural(f[OWN_FILES], 'file')} directly here")

        walk(r, 1)
        lines += out
    return lines


def _cmd_lines(e, indent):
    lines = []
    cmds = e.get("commands") or []
    for c in cmds[:5]:
        lines.append(f"{indent}$ {c}")
    if len(cmds) > 5:
        lines.append(f"{indent}  ... and {len(cmds) - 5} more of the same kind (in the report)")
    if e.get("note"):
        lines.append(f"{indent}{e['note']}")
    return lines


def _cache_lines(entries, home):
    lines = []
    for e in entries:
        where = ", ".join(_tilde(p, home) for p in e["paths"][:3])
        if len(e["paths"]) > 3:
            where += f" (+{len(e['paths']) - 3} more)"
        lines.append(f"  {_size(e['disk'])}  {e['label']}  {printable(where)}")
        if e.get("shared"):
            lines.append(f"             frees about {human(e['frees'])}: {human(e['shared'])} of "
                         f"it is shared with other folders (hard links)")
        if e.get("partial"):
            lines.append("             some of it could not be read: at least this much")
        lines += _cmd_lines(e, "             ")
    return lines


def _tilde(path, home):
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def _recent(doc, item):
    return doc.get("now", 0) - item[3] < RECENT_SECONDS


def _clutter_item(kind, d, item, home):
    path = printable(_tilde(d.item_path(item), home))
    if kind == "broken":
        return f"{path} -> {printable(item[2])}  ({item[3]})"
    if kind == "temp":
        return (f"{human(item[2]):>9}  {path}"
                + ("  (changed in the last day: may be in use)" if _recent(d.doc, item) else ""))
    if kind == "empty":
        return f"{path}/" + (f"  (+{item[2]:,} inside)" if item[2] else "")
    return path


def summary(doc, run=None, terminal=False, top=None):
    """summary.txt, or with terminal=True the shorter copy for the terminal."""
    top = top or (15 if terminal else SUMMARY_TOP)
    lines = [RULE]
    for i, root in enumerate(doc["roots"]):
        lines.append(f"{'Looked at:' if i == 0 else '':<17}{printable(root)}")
    if doc.get("stopped"):
        lines.append(f"Stopped:         {doc['stopped']}")
        _saved(lines, run)
        lines.append(RULE)
        return "\n".join(lines) + "\n"
    home = os.path.expanduser("~")
    d = Doc(doc)
    t = doc["total"]
    lines.append(f"On disk:         {human(t['disk'])} in {plural(t['files'], 'file')} and "
                 f"{plural(t['dirs'], 'folder')}  (took {duration(doc.get('seconds'))})")
    links = doc.get("links") or {}
    if links.get("names"):
        lines.append(f"Hard links:      {plural(links['names'], 'extra name')} of files "
                     f"counted once ({human(links['bytes'])} not counted again)")
    if doc.get("excluded"):
        lines.append(f"Left out:        {plural(doc['excluded'], 'entry', 'entries')} matched "
                     f"--exclude (not counted)")
    if doc.get("mounts"):
        lines.append(f"Other drives:    {len(doc['mounts']):,} mounted inside, not counted "
                     f"(name one as a PATH to include it)")
    r = reclaim(doc)
    parts = [(r["caches"], "caches"), (r["system"], "system caches"),
             (r["trash"], "Trash"), (r["temp"], "temp leftovers")]
    could = sum(n for n, _ in parts)
    if could:
        lines.append(f"Could be freed:  about {human(could)}: "
                     + ", ".join(f"{label} {human(n)}" for n, label in parts if n))
    lines.append(f"Problems:        {doc.get('errors_total', 0):,}")
    _saved(lines, run)

    drives = doc.get("drives") or []
    if drives:
        lines += ["", "Drives"]
        for dr in drives:
            share = dr["used"] / dr["total"] if dr["total"] else 0
            lines.append(f"  {_bar(share)}  {human(dr['free']):>9} free of {human(dr['total'])}"
                         f"  {dr['name']}  {printable(dr['point'])}"
                         + ("  (read-only)" if dr.get("ro") else ""))

    lines += ["", "Biggest folders  (the bar: share of the folder above)"]
    lines += _tree_lines(d, top, 3 if terminal else 4, home)

    files = [f for f in doc.get("top") or [] if f[2] > 0]
    if files:
        lines += ["", "Biggest files"]
        for item in files[:top]:
            lines.append(f"  {_size(item[2])}  {printable(_tilde(d.item_path(item), home))}")

    lines += ["", "Caches  (programs make them again; clean never clears them itself)"]
    if doc.get("caches"):
        lines += _cache_lines(doc["caches"], home)
    else:
        lines.append("  None found in these folders.")
    system = doc.get("system_caches")
    if system:
        lines += ["", "System caches  (the commands need sudo: clean prints them, never runs them)"]
        lines += _cache_lines(system, home)

    lines += ["", "Trash"]
    warn = doc.get("trash_warn_gb") or 0
    trashes = doc.get("trash") or []
    if not trashes:
        lines.append("  There is no Trash on any connected drive yet.")
    for tr in trashes:
        lines.append(f"  {_size(tr['bytes'])}  {tr['where']}: "
                     + (plural(tr["items"], "item") if tr["items"] else "empty"))
        if warn and tr["bytes"] > warn * 1024 ** 3:
            lines.append(f"             more than trash.warn_gb ({warn} GB): that space is only "
                         f"free once it is emptied")
    if any(tr["items"] for tr in trashes):
        lines.append("  What is there: clean trash list -v    empty it for good: clean trash empty")

    lines += ["", "Clutter"]
    c = doc["clutter"]
    cap = TERMINAL_ITEMS if terminal else SUMMARY_ITEMS
    for kind, title, one in CLUTTER:
        k = c[kind]
        line = f"  {title + ':':<17}{k['count']:,}"
        if k.get("bytes"):
            line += f", {human(k['bytes'])}"
        if k["unlisted"]:
            line += (f"  ({k['unlisted']:,} inside program and package folders: theirs, "
                     f"not listed)")
        lines.append(line)
        for item in k["items"][:cap]:
            lines.append(f"      {_clutter_item(kind, d, item, home)}")
        more = len(k["items"]) - cap + k["more"]
        if more > 0:
            lines.append(f"      ... and {more:,} more"
                         + (" (the report lists them: clean show --open)" if terminal else ""))
    if c["empty"]["items"]:
        lines.append("  Empty folders: clean empty PATH lists them in full; --delete removes them.")
    if any(_recent(doc, item) for item in c["temp"]["items"]):
        lines.append("  A temp leftover changed in the last day may still be in use: close the "
                     "program that made it first.")

    lines += ["", "Duplicates"]
    du = doc.get("dupes")
    if du:
        lines.append(f"  Newest scan of these folders: {du['name']} ({when(du['started'])})")
        if du.get("headline"):
            lines.append(f"  {du['headline']}")
        lines.append(f"  See it: clean show {du['name']}    choose what goes: clean ui")
    else:
        lines.append("  No duplicates scan of these folders yet: clean dupes "
                     + " ".join(_quote(printable(p)) for p in doc["roots"]))

    own = doc.get("own")
    if own:
        lines += ["", "clean's own data"]
        lines.append(f"  Runs:          {plural(own['runs'], 'run')}, {human(own['data_bytes'])}"
                     f" in {printable(_tilde(own['data_dir'], home))}")
        if own["keep_days"]:
            lines.append(f"  Retention:     runs older than {own['keep_days']} days go, but the "
                         f"newest {own['keep_runs']} always stay"
                         + (f"; {_prunable(own)}" if own["prunable"] else ""))
        else:
            lines.append("  Retention:     every run is kept (history.keep_days is 0)")
        what, upkeep = _cache_text(own)
        lines.append(f"  Hash cache:    {what}")
        if upkeep:
            lines.append(f"                 {upkeep}")

    errors = [(d.item_path(e), e[2]) for e in doc.get("errors") or []]
    if errors:
        lines += ["", "Problems (sizes there are at least what could be read):"]
        _issues(lines, errors, TERMINAL_ISSUES if terminal else None)
        hidden = doc.get("errors_total", 0) - len(errors)
        if hidden > 0:
            lines.append(f"  ... and {hidden:,} more")
    lines += ["", "Nothing was deleted: clean space only looks."]
    lines.append(RULE)
    return "\n".join(lines) + "\n"


def _quote(text):
    import shlex
    return shlex.quote(text)


# ---------------------------------------------------------------- html

def _hbar(share, dim=False):
    # A <meter>, not a styled box: the dashboard's pages allow no inline styles.
    if share is None:
        return '<span class="bar"></span>'
    value = round(max(0.0, min(1.0, share)) * 1000)
    return (f'<meter class="bar{" dim" if dim else ""}" min="0" max="1000" value="{value}" '
            f'aria-hidden="true"></meter>')


def _notes_html(f):
    bits = []
    if f[FLAGS] & UNREADABLE:
        bits.append("partly unreadable")
    if f[FLAGS] & MOUNTS:
        bits.append("drive inside not counted")
    if f[FLAGS] & EXCLUDED:
        bits.append("some left out")
    return "".join(f' <span class="tag">{esc(b)}</span>' for b in bits)


def _row(name_html, share, nbytes, extra, cls="", depth=0):
    indent = f" d{min(depth, MAX_INDENT)}" if depth else ""
    return (f'<span class="node-row{(" " + cls) if cls else ""}">'
            f'<span class="nm{indent}">{name_html}</span>{_hbar(share, bool(cls))}'
            f'<span class="sz">{esc(human(nbytes))}</span>'
            f'<span class="fl">{esc(extra)}</span></span>')


def _pick(d, budget, tops):
    """The folders to show in the page's tree: the biggest first, each with
    its parent, at most `budget`."""
    chosen = set(tops)
    heap = []
    for r in tops:
        for k in d.kids[r][:TREE_KIDS]:
            heapq.heappush(heap, (-d.folders[k][DISK], k))
    while heap and len(chosen) < budget:
        _, n = heapq.heappop(heap)
        chosen.add(n)
        for k in d.kids[n][:TREE_KIDS]:
            heapq.heappush(heap, (-d.folders[k][DISK], k))
    return chosen


def tree_html(d, budget=TREE_NODES, open_depth=1, start=None, focus=None):
    """The folders as a tree that opens folder by folder (no script needed).
    start: only that folder (its number) and what is inside it. focus(n): a
    link to show folder n on its own, offered where the budget cut the tree
    short (the dashboard has it; the static page shows the budget's worth)."""
    tops = [start] if start is not None else d.roots()
    chosen = _pick(d, budget, tops)
    home = os.path.expanduser("~")
    out = ['<div class="tree">']
    # An explicit stack, not recursion: folders can nest deeper than Python's limit.
    for top in tops:
        stack = [("open", top, 0, None)]
        while stack:
            what, n, depth, share = stack.pop()
            if what == "close":
                out.append("</details>")
                continue
            if what == "html":
                out.append(n)
                continue
            f = d.folders[n]
            name = _tilde(d.path(n), home) if n == top else f[NAME] + "/"
            name_html = esc(name) + _notes_html(f)
            files = f"{f[FILES]:,} files" if f[FILES] != 1 else "1 file"
            kids = [k for k in d.kids[n] if k in chosen]
            inner = [("open", k, depth + 1, d.folders[k][DISK] / f[DISK] if f[DISK] else 0)
                     for k in kids]
            tail = []
            cut = [k for k in d.kids[n] if k not in chosen]
            nbytes, nmore = d.more(n, set(cut))
            if nmore:
                label = esc(f"+ {plural(nmore, 'more folder')}")
                if focus and cut:
                    label = f'<a href="{esc(focus(n))}">{label}</a>'
                tail.append(_row(label, nbytes / f[DISK] if f[DISK] else 0, nbytes, "", "rest",
                                 depth + 1))
            for fname, disk, size, mtime in d.big.get(n, []):
                tail.append(_row(esc(fname), disk / f[DISK] if f[DISK] else 0, disk, "",
                                 "file", depth + 1))
            others = f[OWN] - sum(b[1] for b in d.big.get(n, []))
            nothers = f[OWN_FILES] - len(d.big.get(n, []))
            if nothers > 0 and others > 0:
                tail.append(_row(esc(f"+ {plural(nothers, 'smaller file')} directly here"),
                                 others / f[DISK] if f[DISK] else 0, others, "", "rest",
                                 depth + 1))
            row = _row(name_html, share, f[DISK], files, "", depth)
            if not inner and not tail:
                out.append(f'<div class="node leaf">{row}</div>')
                continue
            opened = " open" if depth < open_depth else ""
            out.append(f'<details class="node"{opened}><summary>{row}</summary>')
            stack.append(("close", None, 0, 0))
            if tail:
                stack.append(("html", "".join(tail), 0, 0))
            for item in reversed(inner):
                stack.append(item)
    out.append("</div>")
    return "".join(out)


def drives_html(drives):
    if not drives:
        return '<div class="empty-state">No disks found.</div>'
    out = ['<ul class="rows drives">']
    for dr in drives:
        total, used = dr["total"], dr["used"]
        pct = used * 100 // total if total else 0
        out.append(
            f'<li class="drive"><div class="drive-line"><b>{esc(dr["name"])}</b> '
            f'<span class="path muted">{esc(dr["point"])}</span> <span class="tag">'
            f'{esc(dr["fstype"])}</span>'
            + (' <span class="tag">read-only</span>' if dr.get("ro") else "")
            + f'<span class="drive-free">{esc(human(dr["free"]))} free of '
              f'{esc(human(total))}</span></div><meter min="0" max="{total}" value="{used}" '
              f'low="{total * 7 // 10}" high="{total * 9 // 10}" optimum="0" '
              f'aria-label="{pct}% used">{pct}%</meter></li>')
    out.append("</ul>")
    return "".join(out)


def caches_html(entries, home, sudo=False):
    out = ['<ul class="rows caches">']
    for e in entries:
        where = "".join(f'<div class="path muted">{esc(_tilde(p, home))}</div>'
                        for p in e["paths"][:4])
        if len(e["paths"]) > 4:
            where += f'<div class="muted">and {len(e["paths"]) - 4:,} more</div>'
        facts = [f"{plural(e['files'], 'file')}"]
        if e.get("shared"):
            facts.append(f"frees about {human(e['frees'])}: {human(e['shared'])} is shared "
                         f"with other folders (hard links)")
        if e.get("partial"):
            facts.append("partly unreadable: at least this much")
        cmds = e.get("commands") or []
        how = ""
        if cmds:
            shown = cmds[:20]
            how = "".join(f'<div><span class="faint">$</span> <code>{esc(c)}</code></div>'
                          for c in shown)
            if len(cmds) > len(shown):
                how += f'<div class="muted">and {len(cmds) - len(shown):,} more like it</div>'
        if e.get("note"):
            how += f'<div class="muted">{esc(e["note"])}</div>'
        out.append(f'<li class="cache"><div class="cache-line"><b>{esc(e["label"])}</b>'
                   f'<span class="cache-size">{esc(human(e["disk"]))}</span></div>'
                   f'{where}<div class="muted small">{esc(" · ".join(facts))}</div>'
                   f'<div class="how">{how}</div></li>')
    out.append("</ul>")
    return "".join(out)


def trash_html(trashes, warn_gb, button=None):
    """button(trash): extra HTML for a Trash's row (the dashboard's Empty)."""
    if not trashes:
        return '<div class="empty-state">There is no Trash on any connected drive yet.</div>'
    out = ['<ul class="rows">']
    for t in trashes:
        warn = warn_gb and t["bytes"] > warn_gb * 1024 ** 3
        out.append(f'<li><div class="cache-line"><b>{esc(t["where"])}</b>'
                   f'<span class="cache-size">{esc(human(t["bytes"]))}</span></div>'
                   f'<div class="path muted">{esc(t["path"])}</div>'
                   f'<div class="muted small">'
                   f'{esc(plural(t["items"], "item") if t["items"] else "empty")}</div>'
                   + (button(t) if button else "")
                   + (f'<p class="note">More than trash.warn_gb ({warn_gb} GB): that space is '
                      f'only free once the Trash is emptied.</p>' if warn else "")
                   + "</li>")
    out.append("</ul>")
    return "".join(out)


def clutter_html(doc, d, home, cap=PAGE_ITEMS):
    c = doc["clutter"]
    out = []
    for kind, title, one in CLUTTER:
        k = c[kind]
        if not k["count"]:
            continue
        head = f"{k['count']:,}" + (f" · {human(k['bytes'])}" if k.get("bytes") else "")
        note = (f'<span class="muted"> · {k["unlisted"]:,} inside program and package folders, '
                f'not listed</span>' if k["unlisted"] else "")
        if not k["items"]:
            out.append(f'<li><b>{esc(title)}</b> <span class="muted">{esc(head)}</span>{note}'
                       '</li>')
            continue
        rows = "".join(f'<li class="path">{esc(_clutter_item(kind, d, item, home))}</li>'
                       for item in k["items"][:cap])
        more = len(k["items"]) - cap + k["more"]
        if more > 0:
            rows += f'<li class="more">... and {more:,} more</li>'
        out.append(f'<li><details class="block"><summary><b>{esc(title)}</b> '
                   f'<span class="muted">{esc(head)}</span></summary>{note}<ul>{rows}</ul>'
                   f'</details></li>')
    hints = []
    if c["empty"]["items"]:
        roots = " ".join(_quote(p) for p in doc["roots"])
        hints.append(f"Empty folders: <code>clean empty {esc(roots)}</code> lists them in "
                     "full, and with <code>--delete</code> removes them.")
    if any(_recent(doc, item) for item in c["temp"]["items"]):
        hints.append("A temp leftover changed in the last day may still be in use: close the "
                     "program that made it first.")
    if c["zero"]["items"] or c["broken"]["items"]:
        hints.append("Zero-byte files and broken symlinks are listed, not removed: some "
                     "programs need them (a lock, a marker).")
    if not out:
        return '<div class="empty-state">None found.</div>'
    return ('<ul class="rows clutter">' + "".join(out) + "</ul>"
            + "".join(f'<p class="hint">{h}</p>' for h in hints))


def _prunable(own):
    n = own["prunable"]
    if own.get("at_end"):
        return f"{plural(n, 'older run')} {'goes' if n == 1 else 'go'} as this run ends"
    return f"{n:,} would go now: clean history --prune"


def _cache_text(own):
    """(what the hash cache is, how to keep it small or None)."""
    ca = own["cache"]
    if not ca["exists"]:
        return "not created yet (clean dupes makes it)", None
    if ca["error"]:
        return (f"{human(ca['bytes'])}, can't be read ({ca['error']})",
                "clean cache clear deletes it; the next scan makes a new one")
    limit = f"limit {own['cache_max_mb']} MB" if own["cache_max_mb"] else "no size limit"
    return (f"{human(ca['bytes'])}, {plural(ca['entries'], 'file')} remembered ({limit})"
            + ("" if ca["enabled"] else "; turned off (cache.enabled)"),
            "clean cache prune drops files that are gone, clean cache clear deletes it")


def own_html(own, home):
    if not own:
        return ""
    rows = [("Runs", f"{plural(own['runs'], 'run')}, {human(own['data_bytes'])} in "
                     f"{_tilde(own['data_dir'], home)}")]
    if own["keep_days"]:
        text = (f"older than {own['keep_days']} days go, but the newest {own['keep_runs']} "
                f"always stay")
        if own["prunable"]:
            text += f"; {_prunable(own)}"
        rows.append(("Retention", text))
    else:
        rows.append(("Retention", "every run is kept (history.keep_days is 0)"))
    what, upkeep = _cache_text(own)
    rows.append(("Hash cache", what + (f"; {upkeep}" if upkeep else "")))
    body = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    return f'<dl class="facts">{body}</dl>'


def dupes_html(doc, link=None, scan=None):
    """link(run name): where a dupes run opens; scan: a page that starts a
    duplicates scan of these folders (the dashboard's)."""
    du = doc.get("dupes")
    if du:
        target = link(du["name"]) if link else f"../{du['name']}/report.html"
        return (f'<p>Newest scan of these folders: <a href="{esc(target)}">{esc(du["name"])}'
                f'</a> <span class="muted">({esc(when(du["started"]))})</span></p>'
                + (f'<p class="muted">{esc(du["headline"])}</p>' if du.get("headline") else ""))
    roots = " ".join(_quote(p) for p in doc["roots"])
    return (f'<p class="muted">No duplicates scan of these folders yet: '
            f'<code>clean dupes {esc(roots)}</code></p>'
            + (f'<p class="acts"><a class="button" href="{esc(scan)}">Scan these for '
               f'duplicates</a></p>' if scan else ""))


def stats_html(doc):
    t = doc["total"]
    r = reclaim(doc)
    clutter_n = sum(doc["clutter"][k]["count"] for k, _, _ in CLUTTER)
    return _stats([
        (human(t["disk"]), "on disk", False),
        (f"{t['files']:,}", "files", False),
        (f"{t['dirs']:,}", "folders", False),
        (human(r["caches"] + r["system"]), "in caches", False),
        (human(r["trash"]), "in the Trash", False),
        (f"{clutter_n:,}", "clutter", False),
        (f"{doc.get('errors_total', 0):,}", "problems", bool(doc.get("errors_total")))])


def files_html(doc, d, home):
    files = [f for f in doc.get("top") or [] if f[2] > 0]
    if not files:
        return ""
    rows = "".join(f'<tr><td class="path">{esc(_tilde(d.item_path(f), home))}</td>'
                   f'<td class="n">{esc(human(f[2]))}</td></tr>' for f in files)
    return (f'<h2>Biggest files <span class="count">{len(files):,}</span></h2>'
            '<table class="num files"><thead><tr><th>File</th><th class="n">On disk'
            f'</th></tr></thead><tbody>{rows}</tbody></table>\n')


def sections(doc, d, dupes_link=None, focus=None, start=None, trash_extra="", dupes_scan=None):
    """The page's sections after its head and headline, shared by report.html
    and the dashboard's page of a run (which passes its own links: focus(n)
    shows folder n alone, focus(None) the whole tree)."""
    home = os.path.expanduser("~")
    body = [stats_html(doc), f'<h2>Drives</h2>{drives_html(doc.get("drives") or [])}\n']
    crumbs = ""
    if start is not None and focus:
        chain = []
        n = d.folders[start][PARENT]
        while n >= 0:
            chain.append(n)
            n = d.folders[n][PARENT]
        links = [f'<a href="{esc(focus(None))}">All</a>']
        links += [f'<a href="{esc(focus(m))}">'
                  f'{esc(_tilde(d.path(m), home) if d.folders[m][PARENT] < 0 else d.folders[m][NAME])}'
                  "</a>" for m in reversed(chain)]
        crumbs = '<p class="crumbs">' + " / ".join(links) + "</p>"
    body.append('<h2 id="folders">Folders</h2><p class="muted">Sizes are what the files take on '
                'the disk; a file with several names counts once. The bar is the share of the '
                'folder above. Click a folder to open it.</p>\n' + crumbs
                + tree_html(d, start=start, focus=focus) + "\n")
    body.append(files_html(doc, d, home))
    body.append('<h2 id="caches">Caches</h2><p class="muted">Programs make these again as needed. clean '
                'never clears them itself: only the program knows what is still in use, so '
                'each comes with its own command.</p>')
    body.append(caches_html(doc["caches"], home) if doc.get("caches")
                else '<div class="empty-state">None found in these folders.</div>')
    if doc.get("system_caches"):
        body.append('<h2>System caches</h2><p class="muted">These commands need sudo: clean '
                    'prints them and never runs them.</p>'
                    + caches_html(doc["system_caches"], home, sudo=True))
    body.append('<h2 id="trash">Trash</h2>' + trash_html(doc.get("trash") or [], doc.get("trash_warn_gb")))
    if any(tr["items"] for tr in doc.get("trash") or []):
        body.append('<p class="hint">What is there: <code>clean trash list -v</code> · empty it '
                    'for good: <code>clean trash empty</code>' + trash_extra + '</p>')
    body.append('<h2 id="clutter">Clutter</h2>' + clutter_html(doc, d, home))
    body.append('<h2>Duplicates</h2>' + dupes_html(doc, dupes_link, dupes_scan))
    body.append("<h2>clean's own data</h2>" + own_html(doc.get("own"), home))
    errors = [(d.item_path(e), e[2]) for e in doc.get("errors") or []]
    body.append(_issues_table("Problems", errors, " (all in summary.txt)"))
    return "".join(body)


def page(run, doc):
    body = [_header(run, "Space", "read-only")]
    if doc.get("stopped"):
        body.append(_callout(run, headline(doc), "The scan did not finish, so there are no "
                                                 "results."))
    else:
        body.append(_callout(run, headline(doc), "Read-only: nothing was deleted. What can go "
                                                 "is listed with the command for it."))
        body.append(sections(doc, Doc(doc)))
    body.append(_footer(run, ("summary.txt", "data.json.gz", "run.json")))
    return _page(f"Space · {run.name}", "".join(body))
