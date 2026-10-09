"""
The `clean` command line: options, signals, and dispatch to the commands.
"""

import argparse
import os
import signal
import sys

from cleanup import __version__, paths
from cleanup import settings as _settings
from cleanup.commands import (cache, config, dupes, empty, history, remove, show, space, trash,
                              ui, undo)
from cleanup.errors import UsageError

EPILOG = """\
examples:
  clean empty "/media/$USER/Backup Drive/Photos"            list empty folders
  clean empty "/media/$USER/Backup Drive/Photos" --delete   delete them
  clean dupes "/media/$USER/Backup Drive" /media/$USER/USB  duplicates on both
  clean dupes ~/Music --media   also the same song in other formats or bitrates
  clean remove -n         what removing the last scan's extra copies would do
  clean remove            do it: to the Trash (asks first)
  clean undo              put it all back
  clean trash             what is in the Trash of each drive
  clean space             where the space in your home folder goes, and what can go
  clean history           past runs, newest first
  clean show --open       the last run's report page
  clean ui                the dashboard: all of this in the browser
  clean config            see and change the defaults

Safety: `empty` only ever calls rmdir, which cannot remove a folder that holds
anything, and lists instead of deleting unless you pass --delete. `dupes` only
reports, unless --remove. `remove` moves extra copies to the Trash (--permanent to delete them),
re-checking every file and the copy that stays just before, and never removes
the last copy of anything; `clean undo` puts it back. `space` only looks: it
lists caches with each program's own command to clear them. Nothing follows
symlinks.

Every run is saved, with its summary, a report page and the full lists, in
  {data}/runs
`clean history` lists them, and this page shows them in a browser:
  {data}/index.html
More: clean <command> --help, and {readme}
"""

README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="clean",
        description="Find duplicates and empty folders, see where the space goes, and\n"
                    "clean up safely: to the Trash, re-checked first, with undo.",
        epilog=EPILOG.format(data=paths.tilde(paths.data_dir()), readme=paths.tilde(README)),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"clean {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    empty.add_parser(sub)
    dupes.add_parser(sub)
    remove.add_parser(sub)
    undo.add_parser(sub)
    trash.add_parser(sub)
    space.add_parser(sub)
    history.add_parser(sub)
    show.add_parser(sub)
    ui.add_parser(sub)
    cache.add_parser(sub)
    config.add_parser(sub)
    return parser


def _stop(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    # `kill`, `timeout` or a closed terminal stop the run the same clean way as
    # Ctrl-C (no half-written report, an exact delete log). Left alone if the
    # signal is ignored, e.g. under nohup.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, _stop)

    # A file name that isn't valid UTF-8 must never crash the output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    # A broken settings file or a bad CLEAN_* variable never stops a command;
    # the built-in default is used and this says so. (`config edit` checks
    # the file after editing instead.)
    if (args.command, getattr(args, "action", None)) != ("config", "edit"):
        for warning in _settings.current().check_all():
            print(f"clean: warning: {warning}", file=sys.stderr)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except UsageError as e:
        print(f"clean: error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Output piped into something like `head` that stopped reading: exit
        # quietly, and keep Python from complaining again while flushing.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
