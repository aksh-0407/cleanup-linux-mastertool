"""
`clean cache`: the hash cache's size and upkeep. The cache only saves time:
deleting it never loses anything but the next scan's speed.
"""

import argparse
import os
import sys

from cleanup import paths
from cleanup import settings as _settings
from cleanup.fsutil import Progress, human, printable
from cleanup.report.text import plural, when
from cleanup.safety import confirm
from cleanup.scan import cache as _cache
from cleanup.volumes import Volumes

EXAMPLES = """\
examples:
  clean cache             how big it is and what it holds
  clean cache prune       drop the files that are gone or changed, then compact it
  clean cache clear       delete it (asks first; -y doesn't); the next scan reads every file

The cache remembers each file's hash by drive, path, size and modification
time, so a re-scan does not read unchanged files again. A file's entry is
only used while all of these still match, and a removal re-reads every file
anyway, so a stale entry can't cause a wrong result: it only takes space.
After each finished duplicates scan, the entries of files that scan found
gone are dropped, and once the file is bigger than cache.max_mb the entries
used longest ago go. Entries on drives that aren't connected are kept.
"""


def add_parser(subparsers):
    p = subparsers.add_parser(
        "cache",
        help="the hash cache: size, prune, clear",
        description="Show the hash cache (which makes re-scans fast), drop the\n"
                    "entries of files that are gone, or delete it.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("action", nargs="?", choices=("info", "prune", "clear"), default="info",
                   help="info (the default), prune or clear")
    p.add_argument("-y", "--yes", action="store_true", help="with clear: don't ask first")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress line")
    p.set_defaults(func=run)
    return p


def run(args):
    path = _cache.default_path()
    if args.action == "prune":
        return prune(path, args.quiet)
    if args.action == "clear":
        return clear_cmd(path, args.yes)
    return show(path)


def _limit():
    mb = _settings.current().get("cache.max_mb")
    return f"limit {mb} MB (cache.max_mb)" if mb else "no size limit (cache.max_mb is 0)"


def show(path):
    i = _cache.info(path)
    print(f"Hash cache:      {printable(paths.tilde(path))}")
    if not i["exists"]:
        print("Not created yet: clean dupes makes it.")
        return 0
    print(f"Size:            {human(i['bytes'])}, {_limit()}")
    if i["error"]:
        print(f"Can't read it:   {i['error']}\n"
              f"                 clean cache clear deletes it; the next scan makes a new one")
        return 1
    print(f"Remembers:       {plural(i['entries'], 'file')}")
    if i["drives"]:
        vols = Volumes()
        for vol, n in sorted(i["drives"].items(), key=lambda kv: -kv[1]):
            if vol:
                where = f"drive {vol}"
                name = vols.name(vol)
                if name != vol:
                    where += f" ({name})"
                where += "" if vols.by_uuid(vol) else ", not connected"
            else:
                where = "folders on no named drive"
            print(f"  {n:>12,}  {printable(where)}")
    if i["oldest"]:
        print(f"Least recent:    {when(i['oldest'])} (the entries used longest ago go "
              f"first when it is over the limit)")
    if not _settings.current().get("cache.enabled"):
        print("Turned off:      cache.enabled is false, so scans don't use it")
    print("Upkeep:          clean cache prune  ·  delete it: clean cache clear")
    return 0


def prune(path, quiet=False):
    if not os.path.exists(path):
        print("There is no hash cache yet: nothing to prune.")
        return 0
    before = _cache.file_size(path)
    c = _cache.Cache(path)
    progress = Progress(quiet)
    try:
        dropped, offline = c.prune_gone(Volumes(), progress)
        progress.clear()
        mb = _settings.current().get("cache.max_mb")
        trimmed = c.trim(mb * 1024 * 1024) if mb else 0
        if not trimmed:
            c.compact()
    finally:
        progress.clear()
        c.close()
    if c.warning:
        print(f"clean: error: {c.warning}", file=sys.stderr)
        return 1
    after = _cache.file_size(path)
    print(f"Dropped {plural(dropped, 'entry', 'entries')} of files that are gone or changed"
          + (f", and {trimmed:,} used longest ago to keep it under {mb} MB" if trimmed else "")
          + ".")
    if offline:
        print(f"Kept {plural(offline, 'entry', 'entries')} on drives that aren't connected: "
              f"they are good again once the drive is back.")
    print(f"Compacted: {human(before)} -> {human(after)}.")
    return 0


def clear_cmd(path, yes):
    size = _cache.file_size(path)
    if not size and not os.path.exists(path):
        print("There is no hash cache: nothing to delete.")
        return 0
    print(f"The hash cache {printable(paths.tilde(path))} takes {human(size)}. Without it "
          f"the next duplicates scan reads every file again.")
    if not confirm("Delete it?", yes):
        print("Nothing was deleted.")
        return 2
    freed, failed = _cache.clear(path)
    for p, err in failed:
        print(f"clean: error: could not delete {printable(p)}: {err}", file=sys.stderr)
    print(f"Deleted the hash cache: {human(freed)} freed.")
    return 1 if failed else 0
