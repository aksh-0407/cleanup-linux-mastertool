"""
`clean undo [RUN]`: put back what a `clean remove` run moved to the Trash,
merged or replaced with hard links, reading its actions.tsv newest step
first (cleanup/actions/undo.py). Nothing is ever overwritten.
"""

import argparse
import os
import sys

from cleanup import runs
from cleanup.actions import remove as _remove
from cleanup.actions import undo as _undo
from cleanup.errors import UsageError
from cleanup.fsutil import is_within, printable
from cleanup.report import data, restore
from cleanup.safety import DriveWatch, StopFlag, confirm
from cleanup.volumes import Volumes

EXAMPLES = """\
examples:
  clean undo -n               what undoing the newest removal would put back
  clean undo                  put it back (asks first)
  clean undo 2                undo run 2 of `clean history`

Everything goes back exactly where it was, from the Trash of its drive. If
something new has been put at a path since, that item stays in the Trash and
is listed: nothing is ever overwritten. Folders a --merge moved files into
get them back out, and copies a merge made on another drive are deleted once
the originals are back. Undo can be run again: what is back already is
skipped. A removal made with --permanent can't be undone.

Exit status: 0 everything is back; 1 something couldn't come back (listed);
2 bad arguments or refused; 130 interrupted.
"""


def add_parser(subparsers):
    p = subparsers.add_parser(
        "undo",
        help="put back what a `clean remove` run moved to the Trash",
        description="Put back what a `clean remove` run moved to the Trash (or merged, or\n"
                    "replaced with hard links), exactly where it was. Nothing is ever\n"
                    "overwritten.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("run", nargs="?", metavar="RUN",
                   help="the `clean remove` run to undo: a number from `clean history`, the "
                        "start of its name, or last (default: the newest removal that "
                        "isn't undone yet)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show what would come back, and save it as a run; touch nothing")
    p.add_argument("-y", "--yes", action="store_true",
                   help="don't ask before going ahead (needed when there is no terminal "
                        "to ask on)")
    p.add_argument("-q", "--quiet", action="store_true", help="summary only")
    p.set_defaults(func=run)
    return p


def run(args):
    if not args.dry_run and not args.yes:
        try:
            tty = os.isatty(0)
        except OSError:
            tty = False
        if not tty:
            raise UsageError("there is no terminal to ask on before putting things back: "
                             "add -y to go ahead without asking (or -n to see what would "
                             "come back)")
    source = _source(args.run)
    try:
        vols = Volumes()
    except (OSError, ValueError):
        vols = None
    moves, unavailable, notes = _locate(source, vols)
    info = restore.new_info(source, _kinds(source), notes, args.dry_run)
    try:
        planned = _undo.Undoer(source, moves, dry_run=True, unavailable=unavailable).go()
    except OSError as e:
        raise UsageError(f"cannot read what run {source.name} did ({source.file(runs.ACTIONS)}):"
                         f" {e.strerror or e}")
    restore.add_outcomes(info, planned)
    out = sys.stdout
    for note in notes:
        print(f"Note: {note}", file=out)
    if not info["rows"]:
        print(f"Nothing to undo: run {source.name} removed nothing.", file=out)
        return 0
    if not args.quiet or args.dry_run:
        print(restore.preview(info, cap=None if args.dry_run else restore.PREVIEW),
              end="", file=out)
    if not args.dry_run:
        if not restore.to_do(info):
            print("Nothing to put back.", file=out)
            return 0 if restore.complete(info) else 1
        if not confirm(restore.question(info), args.yes):
            print("Nothing was moved.", file=out)
            return 2
    held = None if args.dry_run else runs.lock(f"clean undo {source.name}")
    try:
        roots = [r for r in source.roots if r not in unavailable]
        present = [moves.get(r, r) for r in roots if os.path.isdir(moves.get(r, r))]
        undo_run = runs.start("undo", "dry-run" if args.dry_run else "undo", present)
        undo_run.meta.update(source=source.name)
        undo_run.save()
        with runs.recording(undo_run):
            if args.dry_run:
                return _finish(undo_run, info, "done", 0 if restore.complete(info) else 1,
                               args.quiet)
            return _undo_now(args, undo_run, source, moves, unavailable, present, info)
    finally:
        if held is not None:
            os.close(held)


def _undo_now(args, run, source, moves, unavailable, present, info):
    try:
        log = _remove.Log(run.file(runs.ACTIONS))
    except _remove.LogError as e:
        info["log_error"] = str(e)
        print(f"clean: error: {e}\nNothing was moved.", file=sys.stderr)
        return _finish(run, info, "failed", 1, args.quiet)
    undoer = None
    try:
        with StopFlag() as flag:
            undoer = _undo.Undoer(source, moves, stop=lambda: flag.hit,
                                  watch=DriveWatch(present), log=log, unavailable=unavailable)
            try:
                undoer.go()
            except _remove.LogError as e:
                info["log_error"] = str(e)
    finally:
        log.close()
    restore.add_outcomes(info, undoer.results)
    info["stopped"], info["fatal"] = undoer.stopped, undoer.fatal
    if info["log_error"]:
        print(f"clean: error: {info['log_error']}\nStopped: the log of what was done "
              f"can't be written.", file=sys.stderr)
        status, code = "failed", 1
    elif undoer.stopped:
        status, code = "interrupted", 130
    elif undoer.fatal:
        status, code = "drive-gone", 1
    else:
        status, code = "done", 0 if restore.complete(info) else 1
    source.meta["undone_by"] = {"run": run.name, "full": restore.complete(info)}
    try:
        source.save()
    except OSError:
        pass
    return _finish(run, info, status, code, args.quiet)


def _finish(run, info, status, code, quiet):
    info["status"] = status
    run.conclude(status, code, restore.headline(info), restore.totals(info),
                 error=info.get("fatal") or info.get("log_error"))
    warnings = run.complete([("report.html", lambda: restore.page(run, info)),
                             (data.NAME, lambda: data.encode(restore.data_of(info))),
                             ("summary.txt", lambda: restore.summary(info, run))])
    print(restore.summary(info, run, terminal=True), end="")
    return code if code or not warnings else 1


# ---------------------------------------------------------------- set-up

def _source(spec):
    """The removal to undo."""
    if spec is None:
        for r in runs.history():
            if runs.keeps_undo(r) and r.status != "running":
                return r
        raise UsageError("there is no removal left to undo (`clean history` lists the runs; "
                         "a removal made with --permanent, undone already, or whose items "
                         "were emptied from the Trash since can't be undone)")
    r = runs.resolve(spec)
    if r.command != "remove":
        raise UsageError(f"run {r.name} is a `clean {r.command}` run; `clean undo` undoes "
                         f"`clean remove` runs")
    mode = r.meta.get("mode")
    if mode == "dry-run":
        raise UsageError(f"run {r.name} was a dry run (-n): nothing was removed, so there "
                         f"is nothing to undo")
    if mode == "permanent":
        raise UsageError(f"run {r.name} deleted for good (--permanent): there is nothing to "
                         f"bring back")
    if r.status == "running":
        raise UsageError(f"run {r.name} is still running; undo it once it has finished")
    if not r.has(runs.ACTIONS):
        raise UsageError(f"run {r.name} has no log of what it did ({runs.ACTIONS}), so it "
                         f"can't be undone")
    return r


def _kinds(source):
    """step -> (kind, path) of each item of the removal, from its saved
    results (for naming what comes back); {} if they can't be read."""
    try:
        d = data.read(source.file(data.NAME))
        cols = d["columns"]
        i_step, i_kind, i_path = cols.index("step"), cols.index("kind"), cols.index("path")
        return {row[i_step]: (row[i_kind], row[i_path]) for row in d["items"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {}


def _locate(source, vols):
    """Where the removal's drives are now: (moves {old: new} for its folders
    and the drives' mount points, unavailable [old paths], notes)."""
    moves, unavailable, notes = {}, [], []
    for entry in source.meta.get("roots") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path, uuid, inside = entry["path"], entry.get("uuid"), entry.get("inside")
        mount = entry.get("mount")
        if not (uuid and isinstance(inside, str) and vols is not None):
            continue
        now = vols.resolve(uuid, inside)
        if now is None:
            unavailable.append(path)
            if isinstance(mount, str) and mount != "/":
                unavailable.append(mount)
            notes.append(f"drive {entry.get('drive') or uuid}, where {printable(path)} was, "
                         f"is not connected: what came from it can't come back until it is")
            continue
        if now == path:
            continue
        moves[path] = now
        notes.append(f"{printable(path)} is now at {printable(now)} (the same drive, "
                     f"mounted somewhere else)")
        if isinstance(mount, str) and is_within(path, mount):
            rel = path[len(mount.rstrip("/")):]
            if rel and now.endswith(rel):
                moves[mount] = now[:-len(rel)] or "/"
            elif not rel:
                moves[mount] = now
    return moves, sorted(set(unavailable)), notes
