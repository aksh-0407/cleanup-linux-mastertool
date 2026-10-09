"""
`clean history`: the saved runs, newest first; pruning old ones.
"""

import argparse
import shutil
import sys

from cleanup import paths, runs
from cleanup import settings as _settings
from cleanup.commands.show import open_path
from cleanup.fsutil import printable, why
from cleanup.report.text import plural, when
from cleanup.safety import UsageError, confirm

EXAMPLES = """\
examples:
  clean history                   the newest 20 runs
  clean history --all             every run
  clean show 3                    run 3's summary again (clean show 3 --open: its page)
  clean history --open            the same list as a page in the browser
  clean history --prune --dry-run which old runs the retention settings would remove
  clean history --prune           remove them now (this also happens after every run)
  clean history --delete 3        delete run 3 (asks first; -y doesn't)

Retention: runs older than history.keep_days days are removed, but the newest
history.keep_runs always stay, and so does any run a removal can still be
undone from. history.keep_days 0 keeps every run. Deleting a run's folder by
hand is fine too.
"""


def add_parser(subparsers):
    p = subparsers.add_parser(
        "history",
        help="list saved runs (1 = newest); prune old ones",
        description="List the saved runs, newest first. The numbers work with "
                    "`clean show`.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-n", type=_count, default=20, metavar="N",
                   help="how many runs to list (default: 20)")
    p.add_argument("--all", action="store_true", help="list every run")
    p.add_argument("--open", action="store_true",
                   help="open the history page (index.html) in the browser")
    p.add_argument("--prune", action="store_true",
                   help="remove old runs now, by the retention settings")
    p.add_argument("--dry-run", action="store_true",
                   help="with --prune: only list what would be removed")
    p.add_argument("--delete", metavar="RUN",
                   help="delete one run: its number, a start of its name, or last")
    p.add_argument("-y", "--yes", action="store_true",
                   help="with --delete: don't ask first")
    p.set_defaults(func=run)
    return p


def _count(text):
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {text!r}")
    return n


def run(args):
    data = paths.data_dir()
    if args.delete is not None:
        if args.prune or args.dry_run or args.open:
            raise UsageError("--delete can't be combined with --prune, --dry-run or --open")
        return delete(data, runs.resolve(args.delete, data), args.yes)
    if args.dry_run and not args.prune:
        args.prune = True   # "history --dry-run" can only mean the prune preview
    code = _prune(data, args.dry_run) if args.prune else 0
    try:
        runs.rebuild_index(data)
    except OSError:
        pass        # nothing saved yet in an unwritable place; the list still prints
    if args.open:
        open_path(f"{data}/{runs.INDEX}")
        return code
    if not args.prune:
        _list(runs.history(data), args, data)
    return code


def _list(found, args, data):
    if not found:
        print("No runs yet: `clean empty PATH` and `clean dupes PATH` each save one.")
        print(f"They are kept in {printable(data)}/{runs.RUNS}.")
        return
    shown = found if args.all else found[:args.n]
    rows = [("#", "When", "What", "Status", "Result")]
    for r in shown:
        roots = r.roots
        result = r.headline or {"running": "still running",
                                "unfinished": "stopped without finishing"}.get(r.status, "")
        if roots:
            result += (" · " if result else "") + printable(roots[0])
            if len(roots) > 1:
                result += f" (+{len(roots) - 1} more)"
        rows.append((str(r.number), when(r.started_ts), r.label, r.status, result))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    width = shutil.get_terminal_size((10**6, 24)).columns if sys.stdout.isatty() else 10**6
    for row in rows:
        line = (f"{row[0]:>{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  "
                f"{row[3]:<{widths[3]}}  {row[4]}")
        if len(line) > width:
            line = line[:max(width - 1, 0)] + "…"
        print(line)
    print()
    if len(shown) < len(found):
        print(f"The newest {len(shown)} of {len(found):,} runs (--all lists every one).")
    print("A run's summary: clean show N    its page: clean show N --open")
    print(f"All runs as a page: {printable(data)}/{runs.INDEX}  (clean history --open)")


def delete_check(run):
    """Why `run` can't be deleted now (None if it can), and a warning to show
    before deleting it (or None)."""
    if run.status == "running":
        return f"run {run.name} is still running", None
    warn = None
    if runs.keeps_undo(run):
        warn = ("it is the record `clean undo` uses to bring back what this removal put "
                "in the Trash: once it is deleted, that can only be restored by hand "
                "(`clean trash restore`)")
    return None, warn


def delete(data, run, yes):
    refused, warn = delete_check(run)
    if refused:
        raise UsageError(f"{refused}: it can't be deleted until it ends")
    print(f"Run {run.number}: {run.name}  {when(run.started_ts)}  {run.label}  "
          f"{printable(run.headline or run.status)}")
    if warn:
        print(f"Note: {warn}.")
    if not confirm("Delete this run's folder and everything in it?", yes):
        print("Nothing was deleted.")
        return 2
    try:
        runs.delete_run(run, data)
    except OSError as e:
        print(f"clean: error: could not delete {run.name}: {why(e)}", file=sys.stderr)
        return 1
    try:
        runs.rebuild_index(data)
    except OSError:
        pass
    print(f"Deleted run {run.name}.")
    return 0


def _prune(data, dry_run):
    s = _settings.current()
    days, keep = s.get("history.keep_days"), s.get("history.keep_runs")
    if not days:
        print("history.keep_days is 0, so runs are kept until you delete them; "
              "nothing to prune.")
        return 0
    removed, kept, failed = runs.prune(data, days, keep, dry_run=dry_run)
    rule = f"older than {plural(days, 'day')}" + (f", and not among the newest {keep:,}"
                                                   if keep else "")
    if not removed and not failed:
        print(f"Nothing to prune (a run goes once it is {rule}).")
    else:
        print(f"{'Would remove' if dry_run else 'Removed'} {plural(len(removed), 'run')} ({rule}):")
        for r in removed:
            print(f"  {r.name}  {printable(r.headline)}")
    for r, reason in kept:
        print(f"Kept although old: {r.name} ({reason})")
    for r, reason in failed:
        print(f"clean: error: could not remove {r.name}: {reason}", file=sys.stderr)
    return 1 if failed else 0
