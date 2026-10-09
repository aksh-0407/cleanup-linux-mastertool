"""
A `clean dupes` run as the dashboard shows it, for choosing what goes.

The run's results (data.json.gz, what the report shows) are joined to its
manifest (manifest.json.gz, what `clean remove` acts on), found on the drives
as they are mounted now, and put through the same selection code as `clean
remove`, with the choices saved for the run (selection.json). Each card then
says, for every folder and file in it, whether it goes and why, or why it
stays.

A box on the page is named the way a choice is saved: "d12" is folder row 12
of the manifest, "f345" file 345. Every name is shown through html.esc().
"""

import hashlib
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime

from cleanup import choices as _choices
from cleanup import runs, selection
from cleanup import settings as _settings
from cleanup.commands import remove as _remove_cmd
from cleanup.dupes import manifest as _manifest
from cleanup.fsutil import human, is_within
from cleanup.report import data as _data
from cleanup.report import html as H
from cleanup.report.removal import DONE
from cleanup.report.text import KIND_TEXT, member_note, plural
from cleanup.volumes import MOUNTINFO, Volumes

esc = H.esc

PER_PAGE = 50
MAX_MEMBERS = 100       # folders listed per card
MAX_COPIES = 100        # files listed per file group
TABS = (("families", "Folder families"), ("covered", "Covered folders"),
        ("groups", "File groups"), ("lookalikes", "Look-alikes"))
TAB_OF = {"F": "families", "C": "covered", "G": "groups", "L": "lookalikes"}
SORTS = (("space", "most space first"), ("count", "most copies first"),
         ("path", "by path"), ("date", "newest first"))
STATUS = (("", "any"), ("selected", "something goes"), ("kept", "nothing goes"),
          ("blocked", "blocked"), ("chosen", "changed by you"),
          ("undecided", "as suggested"))
DATES = (("", "any time"), ("30", "changed in the last 30 days"),
         ("365", "changed in the last year"), ("-365", "unchanged for a year or more"),
         ("-1825", "unchanged for 5 years or more"))
SIZES = H.SIZE_STEPS

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class ViewError(Exception):
    """The run can't be shown for choosing (a message for the page)."""


def day(ns):
    """'7 Oct 2026' for a time in nanoseconds."""
    try:
        d = datetime.fromtimestamp(ns / 1e9)
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def moment(ns):
    try:
        d = datetime.fromtimestamp(ns / 1e9)
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}, {d:%H:%M:%S}"


def mount_signature():
    """Changes whenever a drive is mounted or unmounted."""
    try:
        with open(MOUNTINFO, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


class Card:
    __slots__ = ("kind", "id", "freed", "count", "path", "types", "loc", "text", "rows",
                 "files", "folded", "src", "_date")

    def __init__(self, kind, cid, src):
        self.kind = kind            # "F", "C", "G" or "L"
        self.id = cid               # "F1", "C2", "G7", "L3"
        self.src = src              # the result as data.json.gz has it
        self.rows = []              # its folders (manifest rows; None if not found)
        self.files = []             # its files (file groups, look-alikes)
        self.freed = 0
        self.count = 0
        self.path = ""
        self.types = ()
        self.loc = set()
        self.text = ""
        self.folded = False
        self._date = None

    def refs(self):
        return ([f"d{r}" for r in self.rows if r is not None]
                + [f"f{f}" for f in self.files if f is not None])


class Results:
    """A dupes run's results joined to its manifest, located on the drives
    as they are now."""

    def __init__(self, run):
        self.run = run
        if run.command != "dupes":
            raise ViewError(f"run {run.name} is not a `clean dupes` run")
        if not run.has(_manifest.NAME) or not run.has(_data.NAME):
            raise ViewError(f"run {run.name} has no results to choose from (it didn't "
                            f"finish, or an older clean made it)")
        try:
            self.data = _data.read(run.file(_data.NAME))
            m = _manifest.Manifest.read(run.file(_manifest.NAME))
        except (OSError, ValueError, KeyError, TypeError, EOFError) as e:
            raise ViewError(f"the results of run {run.name} can't be read: {e}")
        row_of = {p: d for d, p in enumerate(m.paths)}      # as the scan saw them
        try:
            vols = Volumes()
        except (OSError, ValueError):
            vols = None
        self.vols = vols
        self.mounts = [x.point for x in vols.mounts] if vols is not None else []
        moves, self.unavailable, self.vanished, self.notes = _remove_cmd._locate(run, m, vols)
        if moves:
            m.relocate(moves)
        self.m, self.moves = m, moves
        self.lock = threading.Lock()
        self._state = (None, None)
        self._latest = {}
        dpath = _data.paths_of(self.data.get("dirs", []))
        self.places = H._Places(m.roots)

        def row(i):
            return row_of.get(dpath[i]) if isinstance(i, int) and 0 <= i < len(dpath) else None

        def where(i):
            return _choices.moved(dpath[i], moves)

        self.cards = {k: [] for k, _ in TABS}
        for f in self.data.get("families", []):
            c = Card("F", str(f["id"]), f)
            c.rows = [row(f["anchor"])] + [row(x["folder"]) for x in f["members"]]
            paths = [where(f["anchor"])] + [where(x["folder"]) for x in f["members"]]
            self._fill(c, f["freed"], len(paths), paths, f.get("types", []))
            self.cards["families"].append(c)
        for cv in self.data.get("covered", []):
            c = Card("C", str(cv["id"]), cv)
            c.rows = [row(cv["folder"])]
            path = where(cv["folder"])
            self._fill(c, cv["freed"], cv["where_total"], [path], cv.get("types", []),
                       [where(p) for p, _ in cv.get("where", [])])
            self.cards["covered"].append(c)
        groups = {gid: files for gid, _, files in m.groups}
        for g in self.data.get("groups", []):
            c = Card("G", f"G{g['id']}", g)
            files = groups.get(g["id"], [])
            names = [_choices.moved(os.path.join(dpath[d], n), moves) for d, n in g["files"]]
            if len(files) != len(names) or any(m.file_path(f) != p for f, p in zip(files, names)):
                by_path = {m.file_path(f): f for f in files}
                files = [by_path.get(p) for p in names]
            c.files = list(files)
            c.folded = bool(g.get("folded"))
            n = len(names)
            self._fill(c, g["size"] * (n - 1), n, names, [g["category"]])
            self.cards["groups"].append(c)
        sets = {lid: files for lid, _, files in getattr(m, "lookalikes", [])}
        for g in self.data.get("lookalikes", []):
            c = Card("L", f"L{g['id']}", g)
            names = [_choices.moved(os.path.join(dpath[x[0]], x[1]), moves) for x in g["files"]]
            by_path = {m.file_path(f): f for f in sets.get(g["id"], [])}
            c.files = [by_path.get(p) for p in names]
            self._fill(c, g["spare"], len(names), names,
                       ["Images" if g["kind"] == "photo" else "Audio"], [g["label"]])
            self.cards["lookalikes"].append(c)
        self.by_id = {c.id: c for cards in self.cards.values() for c in cards}
        media = (self.data.get("options") or {}).get("media")
        self.tabs = [(k, t) for k, t in TABS
                     if k != "lookalikes" or media or self.cards[k]]

    def _fill(self, c, freed, count, paths, types, extra=()):
        c.freed, c.count = freed, count
        c.path = paths[0] if paths else ""
        c.types = tuple(types)
        for p in paths:
            c.loc.update(self.places.of(p))
        c.text = "\n".join(list(paths) + list(extra)).lower()

    # ------------------------------------------------------------ facts

    def latest(self, d):
        """The newest modification time (ns) of a file in folder row d."""
        out = self._latest.get(d)
        if out is None:
            m = self.m
            out = self._latest[d] = max((m.files[f][3] for f in m.files_in(d)), default=0)
        return out

    def card_date(self, c):
        if c._date is None:
            if c.kind in ("G", "L"):
                c._date = max((self.m.files[f][3] for f in c.files if f is not None), default=0)
            else:
                c._date = max((self.latest(r) for r in c.rows if r is not None), default=0)
        return c._date

    def path_of(self, ref):
        """The path of a box ("d12" or "f3") in this run, or None."""
        if not isinstance(ref, str) or len(ref) < 2 or ref[0] not in "df" \
                or not (ref[1:].isascii() and ref[1:].isdigit()):
            return None
        n = int(ref[1:])
        if ref[0] == "d":
            return self.m.paths[n] if n < len(self.m.rows) else None
        return self.m.file_path(n) if n < len(self.m.files) else None

    def card_of_ref(self, ref):
        for c in self.by_id.values():
            if ref in c.refs():
                return c
        return None

    # ------------------------------------------------------------ choices

    def saved(self):
        """(the choices saved for the run, a problem with them or None)."""
        try:
            return _choices.load(self.run) or _choices.Saved(), None
        except ValueError as e:
            return _choices.Saved(), str(e)

    def state(self, saved=None):
        """The plan for the saved choices (cached until they change)."""
        problem = None
        if saved is None:
            saved, problem = self.saved()
        rank = _settings.current().get("remove.rank")
        key = (repr(saved.rules), repr(sorted(saved.choices.items())), repr(rank),
               _done_key(self.run))
        cached_key, cached = self._state
        if cached_key == key:
            cached.saved, cached.problem = saved, problem
            return cached
        st = State(self, saved, rank)
        st.problem = problem
        self._state = (key, st)
        return st


class State:
    """The plan for one set of choices, indexed by box."""

    def __init__(self, res, saved, rank):
        m = res.m
        self.res, self.saved, self.problem = res, saved, None
        try:
            rules = saved.to_rules(rank, res.mounts, res.moves)
        except ValueError as e:
            self.problem = f"the saved rules can't be used ({e}); the suggestions are shown"
            rules = _choices.Saved().to_rules(rank, res.mounts, res.moves)
        plan = selection.select(m, rules)
        missing = [r for r in res.vanished if not any(is_within(r, it.path) for it in plan.items)]
        self.unavailable = res.unavailable + missing
        off = [it for it in plan.items if any(is_within(it.path, r) for r in self.unavailable)]
        if off:
            gone = set(map(id, off))
            plan.items = [it for it in plan.items if id(it) not in gone]
        self.plan, self.off_items = plan, off
        self.digest = selection.digest(plan)
        self.go, self.blocked, self.off, self.under = {}, {}, {}, {}
        for it in plan.items:
            self.go[ref_of(it)] = it
            if it.kind != "file":
                for k in m.subtree(it.target):
                    if k != it.target:
                        self.under.setdefault(k, it.target)
        for it, reason in plan.blocked:
            self.blocked.setdefault(ref_of(it), reason)
        for it in off:
            lost = any(is_within(it.path, r) for r in missing)
            self.off[ref_of(it)] = ("it is not there any more" if lost
                                    else "its drive is not connected")
        self.done = done_paths(res.run)

    # ------------------------------------------------------------ one box

    def of(self, ref):
        """(state, text) of a box: go, merge, with (goes with a folder that
        goes), blocked, off, done (removed by an earlier removal), keep or
        kept (the copy the rules keep)."""
        m = self.res.m
        path = self.res.path_of(ref)
        if path is not None and path in self.done:
            return "done", self.done[path]
        it = self.go.get(ref)
        if it is not None:
            if it.kind == "merge":
                return "merge", f"merged into {m.paths[it.into]}, then goes"
            return "go", it.why
        reason = self.blocked.get(ref)
        if reason is not None:
            return "blocked", reason
        reason = self.off.get(ref)
        if reason is not None:
            return "off", reason
        n = int(ref[1:])
        d = n if ref[0] == "d" else m.files[n][0]
        outer = self.go_row(d)
        if outer is not None:
            return "with", f"goes with {m.paths[outer]}"
        if ref[0] == "d" and n in self.plan.keepers.values():
            return "kept", "the copy that stays"
        if ref[0] == "f" and n in self.plan.group_keep.values():
            return "kept", "the copy that stays"
        return "keep", ""

    def go_row(self, d):
        """The folder that goes which d is (or is in), or None."""
        it = self.go.get(f"d{d}")
        if it is not None and it.kind != "merge":
            return d
        top = self.under.get(d)
        if top is not None and self.go[f"d{top}"].kind != "merge":
            return top
        return None

    def card_flags(self, card):
        states = [self.of(r)[0] for r in card.refs()]
        return {"selected": any(s in ("go", "merge") for s in states),
                "blocked": "blocked" in states,
                "chosen": any(r in self.saved.choices for r in card.refs())}

    # ------------------------------------------------------------ totals

    def footer(self):
        plan, m = self.plan, self.res.m
        items = [it for it in plan.items if it.path not in self.done]
        seen, size, files, folders, merges = set(), 0, 0, 0, 0
        for it in items:
            if it.kind == "merge":
                merges += 1
                continue
            folders += it.kind == "folder"
            for f in plan.files_of(it):
                key = m.inode_key(f)
                if key not in seen:
                    seen.add(key)
                    size += m.files[f][2]
                    files += 1
        n = len(items)
        bits = [f"Selected {plural(n, 'item')}"]
        if n:
            bits.append(human(size))
        if merges:
            bits.append(f"{plural(merges, 'merge')}")
        if plan.blocked:
            bits.append(f"{len(plan.blocked):,} blocked")
        if self.off_items:
            bits.append(f"{len(self.off_items):,} on drives not connected")
        done = len(plan.items) - n
        if done:
            bits.append(f"{done:,} removed already")
        saved = self.saved
        how = ("as suggested" if saved.is_default() else
               f"{plural(len(saved.choices), 'choice')} of yours" if saved.choices else
               "your rules")
        return {"items": n, "bytes": size, "files": files, "folders": folders - 0,
                "merges": merges, "blocked": len(plan.blocked), "off": len(self.off_items),
                "done": done, "chosen": len(saved.choices), "updated": saved.updated,
                "text": " · ".join(bits), "how": how, "digest": self.digest,
                "problem": self.problem}


def ref_of(it):
    return ("f" if it.kind == "file" else "d") + str(it.target)


# ---------------------------------------------------------------- earlier removals

_done_cache = {}
_done_lock = threading.Lock()


def _removals(run):
    """The removal runs made from `run` (newest first)."""
    data_dir = os.path.dirname(os.path.dirname(run.folder))
    return [r for r in runs.history(data_dir)
            if r.command == "remove" and r.meta.get("source") == run.name
            and r.meta.get("mode") != "dry-run"]


def _done_key(run):
    out = []
    for r in _removals(run):
        try:
            st = os.stat(r.file(_data.NAME))
            out.append((r.name, st.st_mtime_ns, repr(r.meta.get("undone_by"))))
        except OSError:
            out.append((r.name, 0, ""))
    return tuple(out)


def done_paths(run):
    """{path: "removed on <date> (run name)"} for what removals made from
    `run` did and no undo has put back."""
    key = (run.folder, _done_key(run))
    with _done_lock:
        hit = _done_cache.get(run.folder)
        if hit is not None and hit[0] == key:
            return hit[1]
    out = {}
    for r in reversed(_removals(run)):
        undone = r.meta.get("undone_by")
        if isinstance(undone, dict):
            continue        # put back (all of it, or some: then it is looked at again)
        try:
            d = _data.read(r.file(_data.NAME))
            cols = d["columns"]
            at = {c: i for i, c in enumerate(cols)}
            when = day(r.started_ts * 1e9)
            for row in d["items"]:
                if row[at["status"]] in DONE:
                    verb = "merged" if row[at["status"]] == "merged" else (
                        "linked" if row[at["status"]] == "linked" else "removed")
                    out[row[at["path"]]] = f"{verb} on {when} ({r.name})"
        except (OSError, ValueError, KeyError, TypeError, IndexError, EOFError):
            continue
    with _done_lock:
        _done_cache[run.folder] = (key, out)
    return out


# ---------------------------------------------------------------- the cache

class Cache:
    """The last few runs loaded (a big manifest takes a while to read)."""

    def __init__(self, size=2):
        self.size = size
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, run):
        try:
            key = (run.folder, os.stat(run.file(_manifest.NAME)).st_mtime_ns,
                   os.stat(run.file(_data.NAME)).st_mtime_ns, mount_signature())
        except OSError:
            raise ViewError(f"run {run.name} has no results to choose from (it didn't "
                            f"finish, or an older clean made it)")
        with self.lock:
            hit = self.items.get(run.folder)
            if hit is not None and hit[0] == key:
                self.items.move_to_end(run.folder)
                return hit[1]
        res = Results(run)
        with self.lock:
            self.items[run.folder] = (key, res)
            self.items.move_to_end(run.folder)
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return res

    def forget(self, run=None):
        with self.lock:
            if run is None:
                self.items.clear()
            else:
                self.items.pop(run.folder, None)


# ---------------------------------------------------------------- filtering

def query(res, st, q):
    """The cards of one tab that pass the filters, sorted: (cards, total)."""
    tab = q.get("tab") if q.get("tab") in dict(TABS) else "families"
    cards = res.cards[tab]
    one = q.get("card")
    if one:                     # a link to one result ("F3" next to a file)
        c = res.by_id.get(one)
        if c is not None:
            tab = TAB_OF[c.kind]
            return tab, [c], len(res.cards[tab])
    if tab == "groups" and q.get("folded") != "1":
        cards = [c for c in cards if not c.folded]
    words = [w for w in (q.get("q") or "").lower().split() if w]
    typ = q.get("type") or ""
    try:
        size = int(q.get("size") or 0)
    except ValueError:
        size = 0
    try:
        loc = int(q.get("loc") or 0)
    except ValueError:
        loc = 0
    status = q.get("status") or ""
    try:
        days = int(q.get("date") or 0)
    except ValueError:
        days = 0
    now = time.time() * 1e9
    out = []
    for c in cards:
        if size and c.freed < size:
            continue
        if typ and typ not in c.types:
            continue
        if loc and loc not in c.loc:
            continue
        if words and not all(w in c.text for w in words):
            continue
        if days:
            age = now - res.card_date(c)
            if (days > 0 and age > days * 86400e9) or (days < 0 and age < -days * 86400e9):
                continue
        if status:
            f = st.card_flags(c)
            if status == "selected" and not f["selected"]:
                continue
            if status == "kept" and f["selected"]:
                continue
            if status == "blocked" and not f["blocked"]:
                continue
            if status == "chosen" and not f["chosen"]:
                continue
            if status == "undecided" and f["chosen"]:
                continue
        out.append(c)
    how = q.get("sort") or "space"
    if how == "count":
        out.sort(key=lambda c: -c.count)
    elif how == "path":
        out.sort(key=lambda c: c.path)
    elif how == "date":
        out.sort(key=lambda c: -res.card_date(c))
    return tab, out, len(cards)


def page_of(q, n):
    try:
        p = max(1, int(q.get("page") or 1))
    except ValueError:
        p = 1
    pages = max(1, (n + PER_PAGE - 1) // PER_PAGE)
    return min(p, pages), pages


# ---------------------------------------------------------------- rendering

STATE_LABEL = {"go": "goes", "merge": "merged", "with": "goes", "blocked": "blocked",
               "off": "left alone", "done": "done", "keep": "stays", "kept": "stays"}


def _box(ref, state, label):
    checked = " checked" if state in ("go", "merge", "with") else ""
    disabled = " disabled" if state in ("with", "off", "done") else ""
    return (f'<label class="box" title="{esc(label)}"><input type="checkbox" '
            f'data-ref="{esc(ref)}"{checked}{disabled} aria-label="{esc(label)}"></label>')


def _state_line(st, ref):
    state, text = st.of(ref)
    chosen = st.saved.choices.get(ref)
    bits = [f'<span class="st st-{state}">{esc(STATE_LABEL[state])}</span>']
    if text:
        bits.append(f'<span class="why">{esc(text)}</span>')
    if chosen:
        bits.append(f'<span class="mine">your choice: {esc(chosen)}</span>'
                    f'<button type="button" class="link" data-act="unset" data-ref="{esc(ref)}">'
                    'back to the suggestion</button>')
    return state, '<div class="state">' + " ".join(bits) + "</div>"


def _folder_acts(ref, ignore=True):
    out = (f'<button type="button" class="small" data-act="open" data-ref="{esc(ref)}">'
           'Open folder</button>')
    if ignore:
        out += (f'<button type="button" class="small" data-act="ignore" data-ref="{esc(ref)}">'
                'Ignore this folder</button>')
    return f'<div class="acts">{out}</div>'


def _member(res, st, row, role, facts, note="", diff_html=""):
    if row is None:
        return (f'<li class="member pick"><span></span><span class="role">'
                f'<span class="badge r-{esc(role)}">{esc(H.ROLE_LABEL[role])}</span></span>'
                f'<div><div class="muted">(not found in the manifest)</div></div></li>')
    ref = f"d{row}"
    path = res.m.paths[row]
    state, line = _state_line(st, ref)
    label = f"remove {path}"
    latest = res.latest(row)
    if latest:
        facts += f" · newest file {day(latest)}"
    return (f'<li class="member pick s-{state}">{_box(ref, state, label)}'
            f'<span class="role"><span class="badge r-{esc(role)}">{esc(H.ROLE_LABEL[role])}'
            f'</span></span><div><div class="path">{esc(path)}</div>'
            f'<div class="facts">{esc(facts)}</div>'
            + (f'<div class="note">{esc(note)}</div>' if note else "")
            + line + diff_html + _folder_acts(ref) + "</div></li>")


def _card_buttons(card):
    return (f'<span class="card-acts"><button type="button" class="small" data-act="card-reset" '
            f'data-card="{esc(card.id)}">As suggested</button>'
            f'<button type="button" class="small" data-act="card-keep" data-card="{esc(card.id)}">'
            'Keep all</button></span>')


def _family(res, st, c):
    f = c.src
    members = f["members"]
    head = H._card_head(c.id, [
        f'<span class="big">{esc(plural(len(members) + 1, "folder"))}</span>',
        f'<span class="tag">{esc(KIND_TEXT.get(f["kind"], f["kind"]))}</span>',
        f'<span class="muted">{esc(", ".join(f["types"][:3]))}</span>' if f["types"] else "",
        _card_buttons(c)], f["freed"])
    rows = [_member(res, st, c.rows[0], "keep", H._folder_facts(f["files"], f["bytes"]),
                    "the most complete copy; the others are compared with it")]
    for x, row in list(zip(members, c.rows[1:]))[:MAX_MEMBERS]:
        rows.append(_member(res, st, row, x["role"],
                            H._folder_facts(x["files"], x["bytes"], x["freed"]),
                            member_note(x), H._diff_html(x.get("diff"))))
    if len(members) > MAX_MEMBERS:
        rows.append(f'<li class="more">... and {len(members) - MAX_MEMBERS:,} more folders '
                    f'(folders.tsv has all)</li>')
    return (f'<section class="panel card" id="{esc(c.id)}">{head}<ul class="rows">'
            + "".join(rows) + "</ul></section>\n")


def _covered(res, st, c):
    cv = c.src
    head = H._card_head(c.id, [
        f'<span class="big">{esc(plural(cv["files"], "file"))} · {esc(human(cv["bytes"]))}</span>',
        f'<span class="muted">copies in {esc(plural(cv["where_total"], "place"))}</span>',
        _card_buttons(c)], cv["freed"])
    where = "".join(f'<li><span class="path">{esc(_choices.moved(p, res.moves))}</span> '
                    f'<span class="muted">({esc(plural(n, "file"))})</span></li>'
                    for p, n in _where_paths(res, cv))
    more = cv["where_total"] - len(cv["where"])
    if more > 0:
        where += f'<li class="muted">... and {more:,} more places</li>'
    member = _member(res, st, c.rows[0], "covered",
                     H._folder_facts(cv["files"], cv["bytes"], cv["freed"]),
                     "every file in it also exists elsewhere:")
    return (f'<section class="panel card" id="{esc(c.id)}">{head}<ul class="rows">{member}'
            f'<li class="where"><ul class="rows">{where}</ul></li></ul></section>\n')


def _where_paths(res, cv):
    dpath = res._dpaths if hasattr(res, "_dpaths") else None
    if dpath is None:
        dpath = res._dpaths = _data.paths_of(res.data.get("dirs", []))
    return [(dpath[d], n) for d, n in cv["where"]]


PREVIEWABLE = None      # filled in by the server: the extensions it previews


def _group(res, st, c):
    g = c.src
    m = res.m
    n = len(c.files)
    head = H._card_head(c.id, [
        f'<span class="big">{n} copies × {esc(human(g["size"]))}</span>',
        f'<span class="tag">{esc(g["category"])}</span>',
        f'<span class="hash mono" title="blake2b {esc(g["hash"])}">{esc(g["hash"][:16])}</span>',
        _card_buttons(c)], g["size"] * (n - 1))
    rows = []
    for f, ex in list(zip(c.files, g["explained"]))[:MAX_COPIES]:
        if f is None:
            rows.append('<li class="muted">(a copy not found in the manifest)</li>')
            continue
        ref = f"f{f}"
        path = m.file_path(f)
        state, line = _state_line(st, ref)
        d, name, size, mtime = m.files[f][:4]
        tag = (f'<a class="ex" href="#{esc(ex)}" data-act="goto" data-card="{esc(ex)}" '
               f'title="this folder is in result {esc(ex)}">{esc(ex)}</a>' if ex else "")
        preview = _preview_button(f, name)
        details = (f'<details class="info"><summary>Details</summary><dl class="facts">'
                   f'<dt>Size</dt><dd>{esc(human(size))} ({size:,} bytes)</dd>'
                   f'<dt>Modified</dt><dd>{esc(moment(mtime))}</dd>'
                   f'<dt>Folder</dt><dd class="path">{esc(m.paths[d])}</dd>'
                   f'<dt>Content</dt><dd class="mono hash">blake2b {esc(g["hash"])}</dd>'
                   + ('<dt>Hard links</dt><dd>other names of this file are in the list</dd>'
                      if m.files[f][5] else "")
                   + "</dl></details>")
        rows.append(
            f'<li class="copy pick s-{state}">{_box(ref, state, "remove " + path)}<div>'
            f'<span class="path">{esc(path)}</span>{tag}'
            f'<div class="facts">{esc(human(size))} · modified {esc(day(mtime))}</div>{line}'
            f'<div class="acts">{preview}<button type="button" class="small" data-act="open" '
            f'data-ref="{esc(ref)}">Open folder</button>{details}</div></div></li>')
    if n > MAX_COPIES:
        rows.append(f'<li class="more">... and {n - MAX_COPIES:,} more (files.tsv has all)</li>')
    return (f'<section class="panel card" id="{esc(c.id)}">{head}<ul class="rows">'
            + "".join(rows) + "</ul></section>\n")


def _preview_button(f, name):
    if PREVIEWABLE is None or selection.ext_of(name) not in PREVIEWABLE:
        return ""
    return (f'<button type="button" class="small" data-act="preview" '
            f'data-file="{f}" data-name="{esc(name)}">Preview</button>')


def _lookalike(res, st, c):
    g = c.src
    m = res.m
    head = H.lookalike_head(c.id, g, _card_buttons(c))
    rows = []
    for f, x in list(zip(c.files, g["files"]))[:MAX_COPIES]:
        if f is None:
            rows.append('<li class="muted">(a file not found in the manifest)</li>')
            continue
        ref = f"f{f}"
        path = m.file_path(f)
        state, line = _state_line(st, ref)
        d, name, size, mtime = m.files[f][:4]
        letter, best, detail = x[4], x[5], x[6]
        badge, facts = H.lookalike_facts(letter, best, detail, size, f"modified {day(mtime)}")
        details = (f'<details class="info"><summary>Details</summary><dl class="facts">'
                   f'<dt>Version</dt><dd>{esc(letter)}: {esc(detail)}</dd>'
                   f'<dt>Size</dt><dd>{esc(human(size))} ({size:,} bytes)</dd>'
                   f'<dt>Modified</dt><dd>{esc(moment(mtime))}</dd>'
                   f'<dt>Folder</dt><dd class="path">{esc(m.paths[d])}</dd>'
                   + (f'<dt>Content</dt><dd class="mono hash">blake2b {esc(m.hashes[m.files[f][4]])}'
                      f'</dd>' if m.hashes[m.files[f][4]] else "")
                   + "</dl></details>")
        rows.append(
            f'<li class="copy pick s-{state}">{_box(ref, state, "remove " + path)}<div>'
            f'{badge}<span class="path">{esc(path)}</span>'
            f'<div class="facts">{facts}</div>{line}'
            f'<div class="acts">{_preview_button(f, name)}<button type="button" class="small" '
            f'data-act="open" data-ref="{esc(ref)}">Open folder</button>{details}</div></div></li>')
    if len(c.files) > MAX_COPIES:
        rows.append(f'<li class="more">... and {len(c.files) - MAX_COPIES:,} more</li>')
    return (f'<section class="panel card" id="{esc(c.id)}">{head}<ul class="rows">'
            + "".join(rows) + "</ul></section>\n")


LOOKALIKE_HINT = (
    '<p class="hint">The same photo (camera, time taken, size) or song (artist, title, '
    'length) in files whose bytes differ, such as an edited copy or another format. Nothing '
    'here is ticked for you: tick a file to remove it. The last version always stays.</p>')

RENDER = {"F": _family, "C": _covered, "G": _group, "L": _lookalike}


def results(res, q):
    """The cards for a query, as {html, shown, total, page, pages, footer}."""
    with res.lock:
        st = res.state()
        tab, cards, total = query(res, st, q)
        page, pages = page_of(q, len(cards))
        shown = cards[(page - 1) * PER_PAGE: page * PER_PAGE]
        if not shown:
            body = ('<div class="panel"><div class="empty-state">'
                    + ("None found." if not total else "Nothing matches these filters.")
                    + "</div></div>")
        else:
            body = "".join(RENDER[c.kind](res, st, c) for c in shown)
        if tab == "lookalikes":
            body = LOOKALIKE_HINT + body
        counts = {k: len(v) for k, v in res.cards.items()}
        folded = sum(1 for c in res.cards["groups"] if c.folded)
        counts["groups"] -= folded
        return {"tab": tab, "html": body, "shown": len(cards), "total": total,
                "page": page, "pages": pages, "footer": st.footer(),
                "counts": counts, "folded": folded}


def type_options(res):
    cats = sorted({t for cards in res.cards.values() for c in cards for t in c.types})
    return "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cats)
