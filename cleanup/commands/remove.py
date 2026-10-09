"""
`clean remove [RUN]`: act on the results of a `clean dupes` run. The rules
(cleanup/selection.py) choose what goes and prove that a copy of everything
stays; each item is then checked again on disk and moved to the Trash,
deleted, merged or replaced with hard links (cleanup/actions/remove.py).

The run's actions.tsv names every step before it happens, which is what
`clean undo` reads. `clean dupes ... --remove` comes here too, straight
after its scan.
"""

import argparse
import os
import sys

from cleanup import choices as _choices
from cleanup import runs, selection
from cleanup import settings as _settings
from cleanup.actions import remove as _remove
from cleanup.dupes import manifest as _manifest
from cleanup.errors import UsageError
from cleanup.fsutil import Progress, is_within, printable, why
from cleanup.report import data, removal
from cleanup.report.text import command_line, plural
from cleanup.safety import DriveWatch, StopFlag, check_delete_allowed, confirm
from cleanup.volumes import Volumes

EXAMPLES = """\
examples:
  clean remove -n                    what removing the newest scan's copies does
  clean remove                       move it to the Trash (asks first)
  clean remove --keep-in ~/Pictures  never touch the copies in ~/Pictures
  clean remove --remove-from /media/$USER/USB/backup
                                     only remove copies in there
  clean remove 3 --rank newest       act on run 3 of `clean history`; keep the
                                     newest copy of each file
  clean remove --merge               also merge diverged folders into the copy
                                     that stays
  clean undo                         changed your mind: put it all back

What goes: what you chose in the dashboard (`clean ui`), if you did and no
rules are given here. Otherwise, by default: in each family of folders that
are copies of each other, every copy except #1 of the report (the most
complete; --rank-folders lets --rank choose); folders whose every file exists
elsewhere; and in each group of identical files left over, every copy but one,
the one kept chosen by --rank. Diverged folders (each has files of its own)
stay unless --merge. Look-alikes (`clean dupes --media`) go only if you tick
them in the dashboard, and never the last version of a photo or song.

Before anything is touched, every copy that goes is read again in full and
must still be exactly what the scan saw, and so must a copy that stays: what
changed since the scan is skipped. A folder holding anything new is skipped
whole. The last copy of a file can never be removed, whatever the rules say.

Things go to the Trash of their own drive (setting remove.method), which the
Files app shows too; `clean undo` puts them back and `clean trash empty`
frees the space for good. --permanent deletes instead: no undo.

Exit status: 0 done; 1 something was skipped or failed (listed), or the
drive went away; 2 bad arguments or refused; 130 interrupted.
"""


# The note that says the dashboard's choices are used (the dashboard itself
# leaves it out: there it goes without saying).
CHOICES_NOTE = ("using the choices made in the dashboard (`clean ui`); --no-choices "
                "ignores them")


def _rank(text):
    try:
        selection.parse_rank([text])
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--rank {e}")
    return text


def add_parser(subparsers):
    p = subparsers.add_parser(
        "remove",
        help="remove the duplicates a `clean dupes` run found (to the Trash)",
        description="Remove the duplicates found by a `clean dupes` run: move them to the\n"
                    "Trash (or delete them), keeping one copy of everything. Each file is\n"
                    "checked again first; anything changed since the scan is skipped.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("run", nargs="?", metavar="RUN",
                   help="the `clean dupes` run to act on: a number from `clean history`, "
                        "the start of its name, or last (default: the newest dupes run)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show what would be removed, and save it as a run; touch nothing")
    add_options(p)
    p.add_argument("--no-choices", action="store_true",
                   help="ignore what was chosen for this run in the dashboard (`clean "
                        "ui`) and go by the rules alone")
    # The dashboard passes the fingerprint of the plan it showed: if the plan
    # is not the same any more, nothing is removed.
    p.add_argument("--plan-digest", metavar="HEX", help=argparse.SUPPRESS)
    p.add_argument("-q", "--quiet", action="store_true",
                   help="no progress line and no list of items; summary only")
    p.set_defaults(func=run)
    return p


RULE_FLAGS = (("keep_in", "--keep-in"), ("remove_from", "--remove-from"), ("rank", "--rank"),
              ("rank_folders", "--rank-folders"), ("only", "--only"), ("merge", "--merge"))


def add_options(p, prefix=""):
    """The rules and how to remove, shared with `clean dupes --remove`."""
    s = _settings.current()
    rules = p.add_argument_group(
        prefix + "what to remove", "The last copy of anything is never removed, whatever "
                                   "these say.")
    rules.add_argument("--keep-in", action="append", default=[], metavar="DIR",
                       help="never remove anything inside DIR (a reference folder); "
                            "its copies are kept first. Repeatable")
    rules.add_argument("--remove-from", action="append", default=[], metavar="DIR",
                       help="only remove copies inside DIR. Repeatable")
    rules.add_argument("--rank", action="append", type=_rank, metavar="RULES",
                       help="which copy of a file stays, the first rule deciding, "
                            "comma-separated: " + ", ".join(
                                r + ("=TEXT" if r == "path-contains" else "")
                                for r in selection.RANKS)
                            + f" (default: {','.join(s.get('remove.rank'))}; setting "
                              "remove.rank)")
    rules.add_argument("--rank-folders", action="store_true",
                       help="let --rank also choose which copy of a folder stays (by "
                            "default the most complete one, #1 in the report)")
    rules.add_argument("--only", choices=("files", "folders"),
                       help="act only on folder results, or only on single files")
    rules.add_argument("--merge", action="store_true",
                       help="also merge each diverged folder into the copy that stays: "
                            "its files found nowhere there move in, then it goes")
    rules.add_argument("--conflicts", choices=_remove.CONFLICTS, default="both",
                       help="--merge: when a file's name is taken by a different file: "
                            "keep both, the moved one as \"name (from B).ext\" (both, the "
                            "default); leave it, and the folder, where it is (skip); or "
                            "keep the newer, the other going to the Trash (newer)")
    how = p.add_argument_group(prefix + "how")
    method = how.add_mutually_exclusive_group()
    default = s.get("remove.method")
    method.add_argument("--permanent", dest="method", action="store_const",
                        const="permanent", default=default,
                        help="delete for good instead of moving to the Trash: no undo"
                        + (" (the default; setting remove.method)" if default == "permanent"
                           else ""))
    method.add_argument("--trash", dest="method", action="store_const", const="trash",
                        help="move to the Trash"
                        + (" (the default; setting remove.method)" if default == "trash"
                           else ""))
    how.add_argument("--link", action="store_true",
                     help="replace each copy with a hard link to the one that stays, "
                          "instead of removing it: the space is freed and every path "
                          "still works, but editing one then edits all. Not on FAT or "
                          "exFAT drives")
    how.add_argument("-y", "--yes", action="store_true",
                     help="don't ask before going ahead (needed when there is no "
                          "terminal to ask on)")
    how.add_argument("--force", action="store_true",
                     help="allow removing inside your whole home folder or system "
                          "folders such as /usr (dangerous)")


def precheck(args):
    """Refuse bad combinations before any work (for dupes --remove: before
    the scan)."""
    if args.link and args.merge:
        raise UsageError("--link and --merge can't be used together: run them one after "
                         "the other")
    if args.conflicts != "both" and not args.merge:
        raise UsageError("--conflicts only matters with --merge")
    _check_method(args, args.merge, args.conflicts, "--conflicts newer")
    if not getattr(args, "dry_run", False) and not args.yes:
        try:
            tty = os.isatty(0)
        except OSError:
            tty = False
        if not tty:
            raise UsageError("there is no terminal to ask on before removing: add -y to "
                             "go ahead without asking (or -n to see what would go)")


def _check_method(args, merge, conflicts, what):
    if args.link and merge:
        raise UsageError("the choices made in the dashboard merge folders, which --link "
                         "can't do: remove without --link, or merge nothing")
    if conflicts == "newer" and args.method == "permanent":
        raise UsageError(f"{what} moves the older file to the Trash, so it can't be used "
                         f"with --permanent")


def run(args):
    precheck(args)
    source = _source(args.run)
    return act(args, source)


def saved_choices(args, source):
    """What was chosen in the dashboard for `source`, if it is to be used:
    only when no rules are given here (both together would be unclear)."""
    if getattr(args, "no_choices", True):     # also `clean dupes --remove`
        return None
    try:
        saved = _choices.load(source)
    except ValueError as e:
        raise UsageError(f"the choices saved in the dashboard for run {source.name} can't be "
                         f"used: {e}. Choose again in `clean ui`, or add --no-choices to go "
                         f"by the rules alone.")
    if saved is None or saved.is_default():
        return None
    given = [flag for attr, flag in RULE_FLAGS if getattr(args, attr)]
    given += ["--conflicts"] if args.conflicts != "both" else []
    if given:
        raise UsageError(f"run {source.name} has choices you made in the dashboard (`clean "
                         f"ui`), and {', '.join(given)} would mix with them: add "
                         f"--no-choices to go by the rules given here alone, or leave out "
                         f"{', '.join(given)} to use the choices")
    _check_method(args, saved.rules["merge"], saved.rules["conflicts"],
                  "the dashboard's choice of keeping the newer file on a merge")
    return saved


def _source(spec):
    """The dupes run to act on."""
    if spec is None:
        for r in runs.history():
            if r.command == "dupes" and r.has(_manifest.NAME):
                return r
        raise UsageError("there is no finished `clean dupes` run to act on: run "
                         "`clean dupes PATH...` first")
    r = runs.resolve(spec)
    if r.command != "dupes":
        raise UsageError(f"run {r.name} is a `clean {r.command}` run; `clean remove` acts "
                         f"on the results of a `clean dupes` run")
    if not r.has(_manifest.NAME):
        raise UsageError(f"run {r.name} has no results to act on (it didn't finish, or "
                         f"it was made by an older clean): run `clean dupes` again")
    return r


def go_ahead(args, source):
    """The command that does what a dry run showed: the same scan, rules and
    method, whether the dry run was `clean remove -n` or `clean dupes
    --remove -n` (so a newer scan or another folder doesn't change it)."""
    argv = ["remove", source.name]
    argv += ["--no-choices"] if getattr(args, "no_choices", False) else []
    for flag, dirs in (("--keep-in", args.keep_in), ("--remove-from", args.remove_from)):
        for d in dirs:
            argv += [flag, os.path.abspath(os.path.expanduser(d))]
    for r in args.rank or []:
        argv += ["--rank", r]
    argv += ["--rank-folders"] if args.rank_folders else []
    argv += ["--only", args.only] if args.only else []
    argv += ["--merge"] if args.merge else []
    argv += ["--conflicts", args.conflicts] if args.conflicts != "both" else []
    if args.method == "permanent":
        argv.append("--permanent")
    elif _settings.current().get("remove.method") == "permanent":
        argv.append("--trash")
    argv += ["--link"] if args.link else []
    argv += ["--force"] if args.force else []
    argv += ["-q"] if args.quiet else []
    return command_line(argv)


class Prepared:
    """What `clean remove` would do, worked out but not done yet (also what
    the dashboard shows before asking)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def prepare(args, source):
    """Read the scan, find where its drives are now, choose: a Prepared with
    m, vols, plan, info, notes, unavailable, missing, off, merge, conflicts,
    saved (the dashboard's choices, if used) and digest."""
    try:
        m = _manifest.Manifest.read(source.file(_manifest.NAME))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise UsageError(f"cannot read the results of run {source.name}: "
                         f"{why(e) if isinstance(e, OSError) else e}")
    saved = saved_choices(args, source)
    try:
        vols = Volumes()
    except (OSError, ValueError):
        vols = None
    moves, unavailable, vanished, notes = _locate(source, m, vols)
    if moves:
        m.relocate(moves)
    if saved is not None:
        notes.insert(0, CHOICES_NOTE)
        try:
            rules = saved.to_rules(_settings.current().get("remove.rank"),
                                   [x.point for x in vols.mounts] if vols is not None else (),
                                   moves)
        except ValueError as e:
            raise UsageError(f"the dashboard's choices for run {source.name} can't be used: "
                             f"{e}")
        merge, conflicts = saved.rules["merge"], saved.rules["conflicts"]
    else:
        rules = _rules(args, m, vols)
        merge, conflicts = args.merge, args.conflicts
    plan = selection.select(m, rules)
    # A scanned folder that is not there any more: if the plan removes it
    # whole, an earlier run did, and what was in it is simply already gone.
    # Otherwise what was in it is left alone.
    missing = [r for r in vanished if not any(is_within(r, it.path) for it in plan.items)]
    notes += [f"{printable(r)} is not there any more: what was in it is left alone"
              for r in missing]
    unavailable = unavailable + missing
    if plan.versions:
        notes.append(f"{plural(len(plan.versions), 'look-alike')} chosen to go: not copies, "
                     "so their bytes differ from the version of the photo or song that stays "
                     "(another format, an edit, other quality)")
    info = removal.new_info(source, m, plan, args, unavailable, notes, missing)
    info["merge"], info["conflicts"] = merge, conflicts
    if info["dry_run"]:
        info["go_ahead"] = go_ahead(args, source)
    off = [it for it in plan.items if any(is_within(it.path, r) for r in unavailable)]
    if off:
        plan.items = [it for it in plan.items if it not in off]
        info["missing"] = sum(1 for it in off if any(is_within(it.path, r) for r in missing))
        info["offline"] = len(off) - info["missing"]
    return Prepared(m=m, vols=vols, plan=plan, info=info, notes=notes, unavailable=unavailable,
                    missing=missing, off=off, merge=merge, conflicts=conflicts, saved=saved,
                    digest=selection.digest(plan))


def busy_roots(m, plan, unavailable):
    """The scanned folders the plan removes or merges anything in."""
    roots = [r for r in m.roots if r not in unavailable]
    return sorted({r for r in roots
                   if any(is_within(it.path, r) or (it.into is not None
                                                    and is_within(m.paths[it.into], r))
                          for it in plan.items)})


def act(args, source):
    """Plan, show, confirm, then remove. Returns the exit code."""
    quiet = args.quiet
    p = prepare(args, source)
    m, vols, plan, info = p.m, p.vols, p.plan, p.info
    notes, unavailable, missing, off = p.notes, p.unavailable, p.missing, p.off
    confirmed = getattr(args, "plan_digest", None)
    if confirmed is not None and confirmed != p.digest:
        raise UsageError("what would be removed is not what was confirmed in the dashboard "
                         "any more (the choices, the files or the drives changed since): "
                         "nothing was removed. Look at it again and confirm anew.")

    out = sys.stdout
    for note in notes:
        print(f"Note: {note}", file=out)
    if not plan.items:
        print(removal.nothing_text(info), end="", file=out)
        if off:
            why_ = [f"the drive of {printable(r)} is not connected"
                    for r in unavailable if r not in missing]
            why_ += [f"{printable(r)} is not there any more" for r in missing]
            raise UsageError(("nothing can be removed while " if info["offline"] else
                              "nothing can be removed: ") + "; ".join(why_))
        return 0
    info["rows"] = removal.plan_rows(m, plan)
    if not quiet or args.dry_run:
        print(removal.plan_text(info, cap=None if args.dry_run else removal.PREVIEW),
              end="", file=out)

    roots = [r for r in m.roots if r not in unavailable]
    if not args.dry_run:
        for root in busy_roots(m, plan, unavailable):
            check_delete_allowed(root, args.force)
        if not confirm(removal.question(info), args.yes):
            print("Nothing was removed.", file=out)
            return 2
    mode = "dry-run" if args.dry_run else args.method
    held = None if args.dry_run else runs.lock(
        f"clean remove {source.name}" + (" --permanent" if mode == "permanent" else ""))
    try:
        run = runs.start("remove", mode, roots)
        run.meta.update(source=source.name, method=args.method, link=args.link,
                        merge=p.merge, conflicts=p.conflicts, choices=p.saved is not None)
        run.save()
        with runs.recording(run):
            if args.dry_run:
                return _finish(run, info, "done", 0, quiet)
            return _remove_now(args, run, m, plan, vols, unavailable, info, p.conflicts)
    finally:
        if held is not None:
            os.close(held)


def _remove_now(args, run, m, plan, vols, unavailable, info, conflicts):
    progress = Progress(args.quiet)
    present = [r for r in m.roots if r not in unavailable and os.path.isdir(r)]
    try:
        log = _remove.Log(run.file(runs.ACTIONS))
    except _remove.LogError as e:
        info["log_error"] = str(e)
        print(f"clean: error: {e}\nNothing was removed: without its log, it couldn't be "
              f"undone.", file=sys.stderr)
        return _finish(run, info, "failed", 1, args.quiet)
    remover = None
    try:
        with StopFlag() as flag:
            remover = _remove.Remover(
                m, plan, log, method=args.method, link=args.link, conflicts=conflicts,
                stop=lambda: flag.hit, watch=DriveWatch(present), vols=vols,
                unavailable=unavailable, progress=progress)
            try:
                remover.run()
            except _remove.LogError as e:
                info["log_error"] = str(e)
    finally:
        log.close()
        progress.clear()
    removal.add_results(info, remover)
    res = info["result"]
    if info.get("log_error"):
        print(f"clean: error: {info['log_error']}\nStopped: the log `clean undo` relies on "
              f"can't be written.", file=sys.stderr)
        status, code = "failed", 1
    elif remover.stopped:
        status, code = "interrupted", 130
    elif remover.fatal:
        status, code = "drive-gone", 1
    elif any(res[k] for k in ("skipped", "failed", "partial", "not-done")):
        status, code = "done", 1
    else:
        status, code = "done", 0
    return _finish(run, info, status, code, args.quiet)


def _finish(run, info, status, code, quiet):
    info["status"] = status
    run.conclude(status, code, removal.headline(info), removal.totals(info),
                 error=info.get("fatal") or info.get("log_error"))
    warnings = run.complete([("report.html", lambda: removal.page(run, info)),
                             (data.NAME, lambda: data.encode(removal.data_of(info))),
                             ("summary.txt", lambda: removal.summary(info, run))])
    print(removal.summary(info, run, terminal=True, quiet=quiet), end="")
    return code if code or not warnings else 1


# ---------------------------------------------------------------- set-up

def _locate(source, m, vols):
    """Where each scanned folder is now: (moves {old: new}, unavailable
    [paths on drives that aren't connected], vanished [paths that are not
    there any more], notes). A drive is known by its filesystem UUID, so one
    mounted somewhere else is still found, and another drive now mounted at
    the old place is never mistaken for it."""
    moves, unavailable, vanished, notes = {}, [], [], []
    known = {r.get("path"): r for r in source.meta.get("roots") or []
             if isinstance(r, dict) and isinstance(r.get("path"), str)}
    for root in m.roots:
        entry = known.get(root) or {}
        uuid, inside = entry.get("uuid"), entry.get("inside")
        now = root
        if uuid and isinstance(inside, str) and vols is not None:
            now = vols.resolve(uuid, inside)
            if now is None:
                unavailable.append(root)
                name = entry.get("drive") or uuid
                notes.append(f"drive {name}, where {printable(root)} was scanned, is not "
                             f"connected: what is on it is left alone")
                continue
        if now != root:
            moves[root] = now
        if not os.path.isdir(now):
            vanished.append(now)
        elif now != root:
            notes.append(f"{printable(root)} is now at {printable(now)} (the same drive, "
                         f"mounted somewhere else)")
    unavailable = [moves.get(r, r) for r in unavailable]
    return moves, unavailable, vanished, notes


def _folders(given, m, flag):
    out = []
    for p in given:
        path = os.path.abspath(os.path.expanduser(p))
        real = os.path.realpath(path)
        if not any(is_within(x, r) or is_within(r, x) for r in m.roots for x in (path, real)):
            raise UsageError(f"{flag} {printable(p)}: that is not inside any folder the scan "
                             f"looked at (" + ", ".join(printable(r) for r in m.roots) + ")")
        out.append(path if any(is_within(path, r) or is_within(r, path) for r in m.roots)
                   else real)
    return out


def _rules(args, m, vols=None):
    s = _settings.current()
    rank = args.rank if args.rank else s.get("remove.rank")
    try:
        parsed = selection.parse_rank(rank)
    except ValueError as e:
        raise UsageError(f"--rank {e}")
    return selection.Rules(keep_in=_folders(args.keep_in, m, "--keep-in"),
                           remove_from=_folders(args.remove_from, m, "--remove-from"),
                           rank=parsed, family_rank=args.rank_folders, only=args.only,
                           merge=args.merge,
                           mounts=[x.point for x in vols.mounts] if vols is not None else ())

