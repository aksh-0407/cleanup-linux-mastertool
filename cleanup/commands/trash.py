"""
`clean trash [list|restore|empty]`: the Trash of every drive, the one the
Files app shows: what is in it (and which `clean remove` run put it there),
putting things back, and emptying it for good, all of it or only part.
"""

import argparse
import os
import sys

from cleanup import runs
from cleanup import settings as _settings
from cleanup.actions import remove as _remove
from cleanup.actions import trash as _trash
from cleanup.actions.undo import Undoer
from cleanup.commands import undo as _undo_cmd
from cleanup.errors import UsageError
from cleanup.fsutil import human, is_within, printable, why
from cleanup.report import html as _html
from cleanup.report.text import RULE, TERMINAL_ISSUES, plural
from cleanup.actions.verify import Bad
from cleanup.safety import StopFlag, confirm
from cleanup.volumes import Volumes

EXAMPLES = """\
examples:
  clean trash                           what is in the Trash of each drive
  clean trash list -v                   ...item by item
  clean trash restore ~/Pictures/x.jpg  put that back where it was
  clean trash empty --run 2             delete for good what removal run 2 moved
                                        there (its undo is then gone too)
  clean trash empty --older-than 30     what has been there for 30 days or more
  clean trash empty --drive /media/$USER/USB    only that drive's Trash
  clean trash empty                     everything in every Trash (asks first)

The Trash is the one the Files app shows: on the drive of your home folder it
is ~/.local/share/Trash, on other drives .Trash-{uid} at their top. Emptying
deletes for good; restoring never overwrites anything.

Exit status: 0 done; 1 something couldn't be restored or deleted (listed);
2 bad arguments or refused; 130 interrupted.
"""


def add_parser(subparsers):
    p = subparsers.add_parser(
        "trash",
        help="see, restore from, or empty the Trash of each drive",
        description="See what is in the Trash of each drive, put things back, or empty it\n"
                    "for good (all of it, one drive, what one removal put there, or what\n"
                    "has been there for a while).",
        epilog=EXAMPLES.replace("{uid}", str(os.getuid())),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="action", metavar="<action>")
    ls = sub.add_parser("list", help="what is in the Trash (the default)",
                        description="List what is in the Trash of each drive: how much, and "
                                    "what clean's removals put there.")
    _common(ls)
    ls.add_argument("-v", "--verbose", action="store_true", help="list every item")
    ls.set_defaults(func=do_list)
    rs = sub.add_parser("restore", help="put items back where they were",
                        description="Put items back from the Trash where they were. Nothing "
                                    "is overwritten: an item whose place is taken stays in "
                                    "the Trash, and is listed.")
    rs.add_argument("paths", nargs="+", metavar="PATH",
                    help="where the item was (a folder also brings back everything that "
                         "was inside it)")
    rs.add_argument("-n", "--dry-run", action="store_true", help="only show what would "
                                                                 "come back")
    rs.add_argument("-y", "--yes", action="store_true", help="don't ask first")
    rs.set_defaults(func=do_restore)
    em = sub.add_parser("empty", help="delete what is in the Trash for good",
                        description="Delete what is in the Trash for good, freeing its "
                                    "space: all of it, or only one drive's, one removal's, or "
                                    "what has been there a while. A removal emptied this way "
                                    "can't be undone.")
    _common(em)
    em.add_argument("--older-than", type=_days, metavar="DAYS",
                    help="only what has been in the Trash at least DAYS days")
    em.add_argument("--run", metavar="RUN",
                    help="only what this `clean remove` run moved there")
    em.add_argument("-n", "--dry-run", action="store_true", help="only show what would go")
    em.add_argument("-y", "--yes", action="store_true", help="don't ask first")
    em.set_defaults(func=do_empty)
    _common(p)
    p.add_argument("-v", "--verbose", action="store_true", help="list every item")
    p.set_defaults(func=do_list, action=None)
    return p


def _common(p):
    p.add_argument("--drive", metavar="DRIVE",
                   help="only the Trash of this drive: a folder on it, its name or UUID, "
                        "or home (the drive of your home folder)")


def _days(text):
    try:
        n = float(text.rstrip("d"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--older-than wants a number of days, not {text!r}")
    if n < 0:
        raise argparse.ArgumentTypeError("--older-than can't be negative")
    return n


# ---------------------------------------------------------------- finding

def _volumes():
    try:
        return Volumes()
    except (OSError, ValueError):
        return None


def _trashes(vols, drive):
    """The Trashes to look at: all of them, or those of --drive."""
    found = _trash.all_trashes(vols)
    if drive is None:
        return found
    if drive == "home":
        return [t for t in found if t.is_home]
    path = os.path.abspath(os.path.expanduser(drive))
    if os.path.exists(path):
        real = os.path.realpath(path)
        home = [t for t in found if t.is_home]
        dev = os.stat(real).st_dev
        out = [t for t in found if not t.is_home and _same_drive(t, dev)]
        if home and _same_drive(home[0], dev):
            out.insert(0, home[0])
        if not out:
            raise UsageError(f"--drive {printable(drive)}: that drive has no Trash")
        return out
    if vols is not None:
        points = {m.point for m in vols.mounts
                  if drive in (m.uuid, m.label) or os.path.basename(m.point) == drive}
        out = [t for t in found if not t.is_home and t.top in points]
        if out:
            return out
    raise UsageError(f"--drive {printable(drive)}: no connected drive with a Trash is called "
                     f"that (give a folder on it, its name, or home)")


def _same_drive(t, dev):
    try:
        return os.stat(t.files).st_dev == dev
    except OSError:
        return False


def _where(t, vols):
    if t.is_home:
        return "the drive of your home folder"
    name = None
    if t.mount is not None and t.mount.uuid and vols is not None:
        name = vols.name(t.mount.uuid) or t.mount.uuid
    return f"drive {name} ({printable(t.top)})" if name else f"the drive at {printable(t.top)}"


def _origins():
    """realpath of a Trash item -> the `clean remove` run that put it there."""
    out = {}
    for r in runs.history():
        if r.command != "remove" or not r.has(runs.ACTIONS):
            continue
        try:
            rows = _remove.read_log(r.file(runs.ACTIONS))
        except OSError:
            continue
        for row in rows:
            if row[2] == "trash" and row[4]:
                out.setdefault(os.path.realpath(row[4]), r.name)
    return out


def _run_entries(spec, vols):
    """(items run `spec` moved to the Trash that are still there, the run,
    how many of its items couldn't be checked because their drive is not
    connected)."""
    src = runs.resolve(spec)
    if src.command != "remove" or not src.has(runs.ACTIONS):
        raise UsageError(f"run {src.name} is not a `clean remove` run that moved things to "
                         f"the Trash")
    if src.meta.get("mode") != "trash":
        raise UsageError(f"run {src.name} didn't move anything to the Trash "
                         f"({src.label})")
    moves, unavailable, _ = _undo_cmd._locate(src, vols)
    u = Undoer(src, moves)
    out, seen, offline = [], set(), 0
    try:
        rows = _remove.read_log(src.file(runs.ACTIONS))
    except OSError as e:
        raise UsageError(f"cannot read what run {src.name} did: {why(e)}")
    for row in reversed(rows):              # newest first: only that one is real
        if row[2] != "trash":
            continue
        path, to = u.here(row[3]), u.here(row[4])
        if path in seen:
            continue
        seen.add(path)
        if any(is_within(row[4], x) for x in unavailable):
            offline += 1
            continue
        try:
            t, name, original, date = u.entry(to)
            if not os.path.lexists(os.path.join(t.files, name)):
                continue
            if u.untrash(path, to, check_only=True, in_the_way_ok=True)[0] == "ok":
                out.append(_trash.Entry(t, name, original, date))
        except (Bad, OSError):
            continue
    return out, src, offline


def _orphans(t):
    """Items in files/ with no record in info/ (the Files app empties them
    too); shown by their place in the Trash, with no date."""
    try:
        names = os.listdir(t.files)
    except OSError:
        return []
    return [_trash.Entry(t, n, os.path.join(t.files, n), None) for n in sorted(names)
            if not os.path.lexists(os.path.join(t.info, n + _trash.INFO_SUFFIX))]


def _date(e):
    return (e.deleted or "unknown date").replace("T", " ")[:16]


def overview(vols=None):
    """Each Trash: {where, path, drive, home, items, bytes, files}, items
    counting those without a record too (the Files app empties them as
    well)."""
    out = []
    for t in _trash.all_trashes(vols):
        entries = t.entries() + _orphans(t)
        total = files = 0
        for e in entries:
            size, n = e.size()
            total += size
            files += n
        out.append({"where": _where(t, vols), "path": t.path,
                    "drive": "home" if t.is_home else t.top, "home": t.is_home,
                    "items": len(entries), "bytes": total, "files": files})
    return out


def too_full(nbytes):
    """True when a Trash this big is past the trash.warn_gb setting."""
    limit = _settings.current().get("trash.warn_gb")
    return bool(limit) and nbytes > limit * 1024 ** 3


def warning_text(t):
    """The warning for an overview entry past trash.warn_gb, or ''."""
    if not too_full(t["bytes"]):
        return ""
    limit = _settings.current().get("trash.warn_gb")
    return (f"the Trash of {t['where']} holds {human(t['bytes'])}, more than "
            f"trash.warn_gb ({limit} GB): that space is only free once it is emptied")


# ---------------------------------------------------------------- list

def do_list(args):
    vols = _volumes()
    trashes = _trashes(vols, args.drive)
    origins = _origins()
    out = sys.stdout
    grand, grand_n = 0, 0
    if not trashes:
        print("There is no Trash yet on any connected drive.", file=out)
        return 0
    for t in trashes:
        entries = [e for e in t.entries() if not e.missing]
        sizes = [e.size() for e in entries]
        total = sum(s for s, _ in sizes)
        ours = sum(1 for e in entries if os.path.realpath(e.file) in origins)
        grand += total
        grand_n += len(entries)
        print(f"Trash of {_where(t, vols)}: {printable(t.path)}", file=out)
        line = f"  {plural(len(entries), 'item')}, {human(total)}"
        if ours:
            line += f"; {ours:,} put there by `clean remove`"
        print(line, file=out)
        warn = warning_text({"where": _where(t, vols), "bytes": total})
        if warn:
            print(f"  Warning: {warn[0].upper() + warn[1:]}.", file=out)
        if args.verbose:
            for e, (size, files) in zip(entries, sizes):
                run = origins.get(os.path.realpath(e.file))
                print(f"    {_date(e)}  {human(size):>9}  {printable(e.original)}"
                      + (f"/  ({plural(files, 'file')})" if os.path.isdir(e.file)
                         and not os.path.islink(e.file) else "")
                      + (f"  [run {run}]" if run else ""), file=out)
    print(f"In all: {plural(grand_n, 'item')}, {human(grand)}.", file=out)
    if grand_n:
        print("Empty it for good: clean trash empty (--older-than DAYS, --drive DRIVE or "
              "--run RUN for only part)", file=out)
    return 0


# ---------------------------------------------------------------- restore

def do_restore(args):
    _ask_check(args)
    vols = _volumes()
    entries = [e for t in _trash.all_trashes(vols) for e in t.entries() if not e.missing]
    out = sys.stdout
    picks, unmatched = {}, []
    for given in args.paths:
        path = os.path.abspath(os.path.expanduser(given))
        hits = [e for e in entries if is_within(e.original, path)]
        if not hits:
            unmatched.append(given)
        for e in hits:          # the newest item from each place
            best = picks.get(e.original)
            if best is None or (e.deleted or "") > (best.deleted or ""):
                picks[e.original] = e
    for given in unmatched:
        path = os.path.abspath(os.path.expanduser(given))
        holder = [e for e in entries if is_within(path, e.original)]
        if holder:
            print(f"Not in the Trash by itself: {printable(given)} is inside "
                  f"{printable(holder[-1].original)}, which is; restore that", file=out)
        else:
            print(f"Not in the Trash: {printable(given)}", file=out)
    if not picks:
        return 1
    # Outer folders first, so what was trashed from inside them goes back in.
    todo = sorted(picks.values(), key=lambda e: e.original)
    blocked = [e for e in todo if os.path.lexists(e.original)]
    ready = [e for e in todo if e not in blocked]
    for e in ready:
        print(f"  {'would come back' if args.dry_run else 'comes back'}: "
              f"{printable(e.original)}  (in the Trash since {_date(e)})", file=out)
    for e in blocked:
        print(f"  in the way: {printable(e.original)} exists again, so its item stays in "
              f"the Trash ({printable(e.file)})", file=out)
    if args.dry_run or not ready:
        return 0 if not (blocked or unmatched) else 1
    if not confirm(f"Put back {plural(len(ready), 'item')}?", args.yes):
        print("Nothing was moved.", file=out)
        return 2
    held = runs.lock("clean trash restore")
    failed = 0
    try:
        for e in ready:
            try:
                e.restore()
            except FileExistsError:
                print(f"  in the way: {printable(e.original)} (nothing was overwritten)",
                      file=out)
                failed += 1
            except OSError as err:
                print(f"  failed: {printable(e.original)}: {why(err)}", file=out)
                failed += 1
    finally:
        os.close(held)
    print(f"Put back {plural(len(ready) - failed, 'item')}.", file=out)
    return 1 if failed or blocked or unmatched else 0


def _ask_check(args):
    if args.dry_run or args.yes:
        return
    try:
        tty = os.isatty(0)
    except OSError:
        tty = False
    if not tty:
        raise UsageError("there is no terminal to ask on: add -y to go ahead without "
                         "asking (or -n to see what would happen)")


# ---------------------------------------------------------------- empty

def do_empty(args):
    _ask_check(args)
    vols = _volumes()
    trashes = _trashes(vols, args.drive)
    source, offline = None, 0
    if args.run is not None:
        picked, source, offline = _run_entries(args.run, vols)
        homes = {os.path.realpath(t.path) for t in trashes}
        entries = [e for e in picked if os.path.realpath(e.trash.path) in homes]
    else:
        entries = [e for t in trashes for e in t.entries() + _orphans(t)]
    undated = 0
    if args.older_than is not None:
        keep = []
        for e in entries:
            age = _trash.age_days(e)
            if age is None:
                undated += 1
            elif age >= args.older_than:
                keep.append(e)
        entries = keep
    out = sys.stdout
    if offline:
        print(f"Note: {plural(offline, 'item')} of run {source.name} went to the Trash of a "
              f"drive that is not connected; they are left alone.", file=out)
    if not entries:
        print("Nothing to empty" + (f" (run {source.name} has nothing left in the Trash)"
                                    if source else "") + ".", file=out)
        if source is not None and not offline and not args.dry_run:
            _mark_emptied(source, None, vols)
        if undated:
            print(f"({plural(undated, 'item')} with no date in the Trash left alone.)",
                  file=out)
        return 0
    sizes = {id(e): e.size() for e in entries}
    total = sum(s for s, _ in sizes.values())
    files = sum(n for _, n in sizes.values())
    what = f"{plural(len(entries), 'item')} ({plural(files, 'file')}, {human(total)})"
    if args.dry_run:
        print(f"Would delete for good: {what}", file=out)
        for e in entries:
            print(f"  {_date(e)}  {human(sizes[id(e)][0]):>9}  {printable(e.original)}",
                  file=out)
        return 0
    if not confirm(f"Delete {what} from the Trash for good? This can't be undone.",
                   args.yes):
        print("Nothing was deleted.", file=out)
        return 2
    held = runs.lock("clean trash empty")
    try:
        run = runs.start("trash", "empty", sorted({e.trash.path for e in entries}))
        run.meta.update(source=source.name if source else None, older_than=args.older_than)
        run.save()
        with runs.recording(run):
            return _empty_now(run, entries, sizes, source, vols)
    finally:
        os.close(held)


def _empty_now(run, entries, sizes, source, vols):
    try:
        log = _remove.Log(run.file(runs.ACTIONS))
    except _remove.LogError as e:
        raise UsageError(f"{e}; nothing was deleted")
    done, failed, stopped = [], [], False
    try:
        with StopFlag() as flag:
            for step, e in enumerate(entries, 1):
                if flag.hit:
                    stopped = True
                    break
                log.row(step, "empty", e.file, e.original)
                try:
                    e.delete()
                    done.append(e)
                except OSError as err:
                    failed.append((e, why(err)))
    finally:
        log.close()
    freed = sum(sizes[id(e)][0] for e in done)
    head = f"deleted {plural(len(done), 'item')} from the Trash for good ({human(freed)})"
    if failed:
        head += f" · {len(failed):,} failed"
    if stopped:
        head = "interrupted: " + head + f" · {len(entries) - len(done) - len(failed):,} not done"
    status = "interrupted" if stopped else "done"
    code = 130 if stopped else (1 if failed else 0)
    run.conclude(status, code, head, {"items": len(entries), "deleted": len(done),
                                      "failed": len(failed), "freed": freed})
    marked = _mark_emptied(source, run, vols)
    issues = [f"  - {printable(e.original)}\n      {msg}" for e, msg in failed]
    head_lines = [f"Failed ({len(failed):,}):"] if failed else []

    def tail():
        lines = [RULE, "Mode:            EMPTY THE TRASH (deleted for good, no undo)",
                 f"Emptied:         {head}"]
        if source is not None:
            lines.append(f"From:            what run {source.name} put in the Trash")
        if marked:
            lines.append("No undo left:    " + ", ".join(f"run {n}" for n in marked)
                         + " (nothing of it is in the Trash any more)")
        lines.append(f"Run:             {run.name}")
        lines.append("Report:          " + ("NOT SAVED (see the warnings)"
                                            if "report.html" in run.unsaved
                                            else printable(run.file("report.html"))))
        if stopped:
            lines += ["", "INTERRUPTED: run the same command again to finish."]
        return lines + [RULE]

    warnings = run.complete([("report.html", lambda: _page(run, head, done, failed, sizes)),
                             ("summary.txt",
                              lambda: "\n".join(head_lines + issues + tail()) + "\n")])
    shown = issues[:TERMINAL_ISSUES]
    if len(issues) > len(shown):
        shown.append(f"  ... and {len(issues) - len(shown):,} more (all in summary.txt)")
    print("\n".join(head_lines + shown + tail()))
    return code if code or not warnings else 1


def _mark_emptied(source, run, vols=None):
    """Removals none of whose items is in the Trash any more can't be undone:
    record that (keeps_undo then lets their log go). Only `source` if given.
    Returns their names."""
    marked = []
    vols = vols or _volumes()
    if vols is None:
        return marked           # can't tell which drives are connected
    for r in runs.history():
        if not runs.keeps_undo(r) or r.meta.get("mode") != "trash" or r.status == "running":
            continue
        if source is not None and r.name != source.name:
            continue
        try:
            rows = _remove.read_log(r.file(runs.ACTIONS))
            if any(row[2] in ("move", "copy") for row in rows):
                continue        # a merge: what it moved can still be moved back
            left, _, offline = _run_entries(r.name, vols)
        except (OSError, UsageError):
            continue
        if left or offline:
            continue
        r.meta["emptied_by"] = run.name if run is not None else "the Trash"
        try:
            r.save()
            marked.append(r.name)
        except OSError:
            pass
    return marked


def _page(run, head, done, failed, sizes):
    body = [_html._header(run, "Empty the Trash", "permanent"),
            _html._callout(run, head, "Deleted for good: there is no undo."),
            _html._stats([(*_html._count(len(done), "item deleted", "items deleted"), False),
                          (human(sum(sizes[id(e)][0] for e in done)), "freed", False)]
                         + ([(f"{len(failed):,}", "failed", True)] if failed else [])),
            _html._issues_table("Failed", [(e.original, msg) for e, msg in failed]),
            _html._issues_table("Deleted", [(e.original, f"in the Trash since {_date(e)} · "
                                             f"{human(sizes[id(e)][0])}") for e in done]),
            _html._footer(run, ("summary.txt", "actions.tsv", "run.json"))]
    return _html.page(f"Empty the Trash · {run.name}", "".join(body))
