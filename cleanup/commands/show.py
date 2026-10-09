"""
`clean show`: print a saved run's summary again, or open its report page.
"""

import argparse
import os
import shutil
import subprocess
import sys

from cleanup import runs
from cleanup.errors import UsageError
from cleanup.fsutil import printable
from cleanup.report.text import command_line, when

EXAMPLES = """\
examples:
  clean show                the last run's summary
  clean show 3              run 3 from `clean history`
  clean show --open         open the last run's report page in the browser
  clean show 2026-10-07     the run whose name starts with that (if only one does)
  ls "$(clean show --path)" the last run's folder

RUN is `last` (the default), a number from `clean history` (1 = newest), the
start of a run's name, or the path of a run folder.
"""

WHY = {
    "done": "It finished, but its summary.txt is missing or unreadable.",
    "running": "It is still running.",
    "unfinished": "It stopped without saying how: it was killed (kill -9), crashed, "
                  "or the computer went off.",
    "interrupted": "It was interrupted (Ctrl-C, kill, or the terminal was closed).",
    "drive-gone": "It stopped because a drive went away or failed.",
    "failed": "It failed.",
}


def add_parser(subparsers):
    p = subparsers.add_parser(
        "show",
        help="print a saved run's summary, or open its report",
        description="Print the summary of a saved run again, or open its report page.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("run", nargs="?", default="last", metavar="RUN",
                   help="which run (default: the last one)")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--open", action="store_true",
                     help="open its report page in the browser instead")
    how.add_argument("--path", action="store_true",
                     help="print the run's folder instead")
    p.set_defaults(func=run)
    return p


def open_path(path):
    """Open a file or folder in the desktop's default program, without waiting
    for it or letting it write into this terminal."""
    opener = shutil.which("xdg-open")
    if opener is None:
        raise UsageError(f"cannot open {printable(path)}: xdg-open is not installed. "
                         f"Open it yourself, e.g. in a browser.")
    subprocess.Popen([opener, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"Opening {printable(path)}")


def run(args):
    found = runs.resolve(args.run)
    if args.path:
        print(printable(found.folder))
        return 0
    if args.open:
        page = found.file("report.html")
        open_path(page if os.path.isfile(page) else found.folder)
        return 0
    number = f" (#{found.number} in clean history)" if found.number else ""
    status = found.status
    print(f"Run {found.name}{number}: {status}")
    try:
        with open(found.file("summary.txt"), encoding="utf-8", errors="replace") as f:
            summary = f.read()
    except OSError:
        summary = None
    if summary is not None and status not in ("running", "unfinished"):
        # (summary.txt is clean's own, but it is a file anyone could edit)
        sys.stdout.write("\n".join(printable(line) for line in summary.split("\n")))
        return 0
    _explain(found, status)
    return 0


def _explain(found, status):
    """A run with no summary: say what is known from run.json."""
    m = found.meta
    if not m:
        print("Its run.json is missing or unreadable, so little is known about it.")
    else:
        argv = [a for a in m.get("argv", []) if isinstance(a, str)]
        print(f"Command: {command_line(argv)}")
        print(f"Started: {when(found.started_ts)}")
        for root in found.roots:
            print(f"Path:    {printable(root)}")
    print(WHY.get(status, ""))
    if isinstance(m.get("error"), str):
        print(f"Error:   {printable(m['error'])}")
    if status == "running" and isinstance(m.get("pid"), int):
        print(f"Process: {m['pid']}")
    tsv = next((n for n in ("folders.tsv", "files.tsv") if found.has(n)), None)
    if tsv:
        print(f"What it recorded before then: {printable(found.file(tsv))}")
    print(f"Folder:  {printable(found.folder)}")
