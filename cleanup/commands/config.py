"""
`clean config`: show and change the settings (see cleanup/settings.py).
"""

import argparse
import difflib
import os
import shlex
import shutil
import subprocess
import sys
import textwrap

from cleanup import paths
from cleanup import settings as S
from cleanup.errors import UsageError
from cleanup.fsutil import printable

EXAMPLES = """\
examples:
  clean config                                  every setting, its value and where it comes from
  clean config set dupes.min_size 1M            ignore files under 1 MB from now on
  clean config add scan.exclude node_modules '*.tmp'
  clean config remove scan.exclude '*.tmp'
  clean config get scan.exclude                 the value alone, for scripts
  clean config reset dupes.min_size             back to the built-in default
  clean config edit                             open the settings file in your editor

A command-line option always wins for that run. Next come environment
variables, named after the setting: CLEAN_DUPES_MIN_SIZE=1M clean dupes ...
(list settings take comma-separated items there). Then the settings file, then
the built-in default.
"""


def add_parser(subparsers):
    p = subparsers.add_parser(
        "config",
        help="show or change settings (defaults for every command)",
        description="Show or change clean's settings: the defaults every command uses.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="action", metavar="<action>")
    sub.add_parser("list", help="every setting with its value, source and meaning (default)")
    g = sub.add_parser("get", help="print one setting's value (lists: one item per line)")
    g.add_argument("key", metavar="KEY", help="a setting's name, e.g. dupes.min_size (`clean config` lists them)")
    s = sub.add_parser("set", help="save a value in the settings file")
    s.add_argument("key", metavar="KEY", help="a setting's name, e.g. dupes.min_size (`clean config` lists them)")
    s.add_argument("values", nargs="+", metavar="VALUE",
                   help="the new value; list settings take any number of items")
    a = sub.add_parser("add", help="add items to a list setting")
    a.add_argument("key", metavar="KEY", help="a list setting's name, e.g. scan.exclude")
    a.add_argument("values", nargs="+", metavar="ITEM", help="the items to add")
    r = sub.add_parser("remove", help="remove items from a list setting")
    r.add_argument("key", metavar="KEY", help="a list setting's name, e.g. scan.exclude")
    r.add_argument("values", nargs="+", metavar="ITEM", help="the items to remove")
    z = sub.add_parser("reset", help="back to the built-in default")
    z.add_argument("keys", nargs="*", metavar="KEY", help="the settings to reset")
    z.add_argument("--all", action="store_true", help="reset every setting")
    sub.add_parser("path", help="print where the settings file and the data folder are")
    sub.add_parser("edit", help="open the settings file in $VISUAL / $EDITOR")
    p.set_defaults(func=run)
    return p


def run(args):
    action = args.action or "list"
    settings = S.current()
    try:
        return ACTIONS[action](settings, args)
    except S.SettingsError as e:
        raise UsageError(str(e))


def _key(name):
    key = S.KEYS.get(name)
    if key is None:
        close = difflib.get_close_matches(name, S.KEYS, n=1)
        hint = f" (did you mean {close[0]}?)" if close else ""
        raise UsageError(f"unknown setting {name!r}{hint}; `clean config` lists them all")
    return key


def _parse(key, items):
    try:
        return S.parse_text(key, items)
    except ValueError as e:
        raise UsageError(f"{key.name} {e}")


def _shadowed(settings, key):
    """Warn when an environment variable hides what was just saved."""
    if key.env in settings.env:
        print(f"Note: {key.env} is set in the environment and wins over the "
              f"settings file while it is set.", file=sys.stderr)


def _saved(settings, key):
    value = settings.file_values.get(key.name, key.default)
    print(f"{key.name} = {S.format_value(key, value)}")
    _shadowed(settings, key)
    return 0


def do_list(settings, args):
    exists = "" if settings.exists else "  (not created yet; all defaults)"
    print(f"Settings file: {printable(paths.tilde(settings.path))}{exists}")
    print(f"Data folder:   {printable(paths.tilde(paths.data_dir(settings)))}")
    width = max(len(n) for n in S.KEYS)
    for name, key in S.KEYS.items():
        value = settings.get(name)
        source = settings.source(name)
        print()
        print(f"{name:<{width}}  {printable(S.format_value(key, value))}")
        detail = f"[{source}]"
        if source != "default":
            detail += f" default: {S.format_value(key, key.default)}"
        for line in textwrap.wrap(f"{key.help} {detail}", 76):
            print(f"    {line}")
    return 0


def do_get(settings, args):
    key = _key(args.key)
    value = settings.get(key.name)
    for item in (value if key.kind == "list" else [S.text_value(key, value)]):
        print(printable(item))
    return 0


def do_set(settings, args):
    key = _key(args.key)
    settings.set(key.name, _parse(key, args.values))
    return _saved(settings, key)


def do_add(settings, args):
    key = _key(args.key)
    if key.kind != "list":
        raise UsageError(f"{key.name} is not a list; use `clean config set`")
    items = _parse(key, args.values)
    current = settings.file_values.get(key.name, key.default)
    settings.set(key.name, list(dict.fromkeys(current + items)))
    return _saved(settings, key)


def do_remove(settings, args):
    key = _key(args.key)
    if key.kind != "list":
        raise UsageError(f"{key.name} is not a list; use `clean config reset`")
    current = settings.file_values.get(key.name, key.default)
    missing = [v for v in args.values if v not in current]
    for v in missing:
        print(f"clean: warning: {v!r} is not in {key.name}", file=sys.stderr)
    settings.set(key.name, [v for v in current if v not in args.values])
    return _saved(settings, key)


def do_reset(settings, args):
    if args.all == bool(args.keys):
        raise UsageError("name the settings to reset, or use --all")
    keys = list(S.KEYS.values()) if args.all else [_key(n) for n in args.keys]
    settings.reset([k.name for k in keys])
    for key in keys:
        print(f"{key.name} = {S.format_value(key, key.default)}  (default)")
        _shadowed(settings, key)
    return 0


def do_path(settings, args):
    print(f"settings: {printable(settings.path)}")
    print(f"data:     {printable(paths.data_dir(settings))}")
    return 0


def _editor():
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var, "").strip()
        if value:
            try:
                return shlex.split(value)
            except ValueError as e:
                raise UsageError(f"cannot understand ${var} ({printable(value)!r}): {e}")
    for name in ("nano", "vi"):
        if shutil.which(name):
            return [name]
    raise UsageError("no editor found: set $EDITOR (e.g. export EDITOR=nano)")


def do_edit(settings, args):
    editor = _editor()
    if not os.path.exists(settings.path):
        S.atomic_write(settings.path, b"{\n}\n")
    try:
        status = subprocess.call(editor + [settings.path])
    except OSError as e:
        raise UsageError(f"cannot start the editor {editor[0]!r}: {e.strerror or e}")
    if status != 0:
        print(f"clean: warning: the editor exited with status {status}", file=sys.stderr)
    after = S.Settings(settings.path, settings.env)
    if after.check_all():
        for w in after.warnings:
            print(f"clean: warning: {w}", file=sys.stderr)
        return 1
    print(f"Settings OK: {printable(paths.tilde(settings.path))}")
    return 0


ACTIONS = {"list": do_list, "get": do_get, "set": do_set, "add": do_add,
           "remove": do_remove, "reset": do_reset, "path": do_path, "edit": do_edit}
