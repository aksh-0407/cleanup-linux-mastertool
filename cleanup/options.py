"""
Command-line option types and the options shared by every command that walks
folder trees. Defaults come from the settings (see settings.py), so
`clean config set dupes.min_size 1M` changes what `--min-size` defaults to.
"""

import argparse

from cleanup import paths
from cleanup import settings as _settings
from cleanup.fsutil import DEFAULT_EXCLUDES, human, size_from_text


def positive_int(text):
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {text!r}")
    return n


def whole_number(text):
    """0 or more."""
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a whole number (0 or more), not {text!r}")
    return n


def percent(text):
    try:
        n = int(text.rstrip("%"))
    except ValueError:
        n = 0
    if not 1 <= n <= 100:
        raise argparse.ArgumentTypeError(f"must be a percentage from 1 to 100, not {text!r}")
    return n


def parse_size(text):
    """'4096' -> 4096, '500K' -> 512000, '1.5G' -> 1610612736 (1K = 1024)."""
    try:
        return size_from_text(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def setting(name):
    """The current value of setting `name`: an option's default."""
    return _settings.current().get(name)


def shown(name, settings=None):
    """`name`'s current value for a help text, e.g. 'default: 1; setting empty.jobs'."""
    settings = settings or _settings.current()
    value = settings.get(name)
    if _settings.KEYS[name].kind == "size":
        text = human(value) if value >= 1024 else str(value)
    else:
        text = _settings.format_value(_settings.KEYS[name], value)
    return f"default: {text}; setting {name}"


def add_walk_options(p, jobs_key, jobs_help):
    """Options shared by every subcommand that walks a folder tree."""
    s = _settings.current()
    one_fs = s.get("scan.one_file_system")
    defaults = s.get("scan.default_excludes")
    extra = s.get("scan.exclude")
    p.add_argument("-x", "--one-file-system", dest="one_file_system",
                   action="store_true", default=one_fs,
                   help="stay on the drive of each PATH: don't go into other "
                        "drives mounted inside it"
                        + (" (on by default; setting scan.one_file_system)" if one_fs
                           else ""))
    p.add_argument("--no-one-file-system", dest="one_file_system",
                   action="store_false", help=argparse.SUPPRESS if not one_fs else
                   "do go into other drives mounted inside PATH")
    p.add_argument("-e", "--exclude", action="append", default=[], metavar="PATTERN",
                   help="skip files and folders matching PATTERN (a shell glob "
                        "such as 'node_modules' or '*.tmp', matched against the "
                        "name; a pattern containing '/' is matched against the "
                        "full path). Repeatable."
                        + (f" Always skipped too (setting scan.exclude): "
                           f"{', '.join(extra)}." if extra else ""))
    p.add_argument("--no-default-excludes", dest="default_excludes",
                   action="store_false", default=defaults,
                   help="also go into " + ", ".join(DEFAULT_EXCLUDES) +
                        " (skipped by default because programs need them intact)")
    p.add_argument("--default-excludes", dest="default_excludes", action="store_true",
                   help=argparse.SUPPRESS if defaults else
                   "skip " + ", ".join(DEFAULT_EXCLUDES) +
                   " (off by default here; setting scan.default_excludes)")
    p.add_argument("-j", "--jobs", type=positive_int, default=s.get(jobs_key),
                   metavar="N", help=f"{jobs_help} ({shown(jobs_key, s)})")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="no progress line and no per-item listing; summary only")


def excludes(args):
    """The exclude patterns for this run: the scan.exclude setting plus -e."""
    return list(dict.fromkeys(_settings.current().get("scan.exclude") + args.exclude))


class _Removed(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(f"{option_string} was removed in clean 3: every run is now saved "
                     f"in {paths.tilde(paths.data_dir())}/runs, with its report and lists "
                     f"(`clean show` prints the last one, `clean history` lists them)")


def add_removed_options(p, *flags):
    """Options of clean 2 that no longer exist: hidden from --help, but say
    where things went instead of a bare "unrecognized arguments"."""
    for flag in flags:
        p.add_argument(flag, nargs="?", action=_Removed, help=argparse.SUPPRESS)
