"""
Settings: every default the commands use, in one place.

Each setting has a name like `dupes.min_size`, a type, a built-in default and
a one-line description. A value can come from, highest priority first:

    1. a command-line option             clean dupes --min-size 1M ...
    2. an environment variable           CLEAN_DUPES_MIN_SIZE=1M
    3. the settings file                 ~/.config/clean/settings.json
    4. the built-in default

The settings file is a flat JSON object ({"dupes.min_size": 1048576, ...}),
written by `clean config set` (atomically) or edited by hand. A file that
can't be read, or a bad value in it, gives a warning and falls back to the
default; it never stops a command.
"""

import json
import os

from cleanup import paths
from cleanup.selection import DEFAULT_RANK, RANKS, check_rank
from cleanup.scan.filters import JUNK_NAMES, TYPES
from cleanup.fsutil import atomic_write, human, printable, size_from_text, why  # noqa: F401


class Key:
    """One setting: name, kind (int, size, bool, list, path, word), default,
    help; `minimum` and `maximum` for whole numbers, `choices` for a word or
    for lists whose items must be one of a few words, `check` for any other
    rule (it returns the value normalised or raises ValueError)."""

    def __init__(self, name, kind, default, help, minimum=1, maximum=None, choices=None,
                 check=None):
        self.name = name
        self.kind = kind
        self.default = default
        self.help = help
        self.minimum = minimum
        self.maximum = maximum
        self.choices = choices
        self.check = check

    @property
    def env(self):
        return "CLEAN_" + self.name.upper().replace(".", "_")


KEYS = {k.name: k for k in (
    Key("empty.jobs", "int", 1,
        "Parallel folder walkers for `clean empty`. Keep 1 on USB hard drives, "
        "where more is slower; higher only helps on SSD/NVMe."),
    Key("dupes.jobs", "int", 1,
        "Parallel workers for the `clean dupes` walk and hashing. Keep 1 on USB "
        "hard drives; higher only helps on SSD/NVMe."),
    Key("dupes.min_size", "size", 0,
        "`clean dupes` skips files smaller than this; a folder holding one is then "
        "not compared as a whole. Empty files are never duplicates anyway."),
    Key("dupes.max_size", "size", 0,
        "`clean dupes` skips files bigger than this (0: no limit); a folder holding "
        "one is then not compared as a whole."),
    Key("dupes.types", "list", [],
        "Only these kinds of files, from: " + ", ".join(TYPES) + " (empty: every "
        "file). A folder holding other files is then not compared as a whole.",
        choices=tuple(TYPES)),
    Key("dupes.hidden", "bool", False,
        "Also list hidden files and folders (names starting with \".\") in the "
        "results. They are always compared as part of the folder they are in."),
    Key("dupes.same_name", "bool", False,
        "Files are duplicates only if their names match too (ignoring case); "
        "the content always has to."),
    Key("dupes.same_date", "bool", False,
        "Files are duplicates only if their modification times match too (to "
        "the second); the content always has to."),
    Key("dupes.media", "bool", False,
        "Also look for look-alikes: the same photo or song in files whose bytes "
        "differ (same capture time, camera and dimensions; or same artist, title "
        "and length). Only reported; never picked for removal."),
    Key("dupes.similar_pct", "int", 80,
        "Two folders sharing at least this percentage of their files are "
        "reported as diverged copies (copies that went on separately).",
        minimum=1, maximum=100),
    Key("junk.names", "list", list(JUNK_NAMES),
        "Files that are ignored everywhere (matched ignoring case): system "
        "clutter that is not worth keeping, so folders differing only in these "
        "still count as copies."),
    Key("cache.enabled", "bool", True,
        "Remember file hashes in the cache folder, so a re-scan does not read "
        "unchanged files again. The cache is safe to delete at any time."),
    Key("cache.max_mb", "int", 500,
        "When the hash cache grows past this many MB, the entries for files "
        "scanned longest ago are dropped and the file is compacted (0: no "
        "limit). `clean cache` shows its size.", minimum=0),
    Key("trash.warn_gb", "int", 10,
        "`clean space`, `clean trash` and the dashboard warn when a drive's "
        "Trash holds more than this many GB (0: never warn).", minimum=0),
    Key("space.system_caches", "bool", True,
        "`clean space` also measures the system's own caches (logs, downloaded "
        "updates, old snap versions) when a scanned folder is on the system "
        "disk. It only measures them and prints the command to clear them."),
    Key("scan.exclude", "list", [],
        "Names, globs or full paths that every scan skips, as if given with -e. "
        "`clean empty` never removes them."),
    Key("scan.default_excludes", "bool", True,
        "Skip .git, .hg, .svn, .bzr, lost+found and .stfolder, which programs "
        "need intact (--no-default-excludes turns it off for one run)."),
    Key("scan.one_file_system", "bool", False,
        "Never go into other drives mounted inside a scanned folder, as with -x "
        "(--no-one-file-system turns it off for one run)."),
    Key("remove.method", "word", "trash",
        "What `clean remove` does with the copies it removes: trash (moved to "
        "the drive's Trash, so `clean undo` can bring them back) or permanent "
        "(deleted outright; --permanent for one run).", choices=("trash", "permanent")),
    Key("remove.rank", "list", list(DEFAULT_RANK),
        "Which copy of a file `clean remove` keeps when rules don't decide, "
        "first rule first: " + ", ".join(r + ("=TEXT" if r == "path-contains" else "")
                                          for r in RANKS)
        + ". first-root is the location named first on the scan's command line.",
        check=check_rank),
    Key("history.keep_days", "int", 90,
        "Runs older than this many days are removed from the history, except "
        "the newest history.keep_runs. 0 keeps every run forever.", minimum=0),
    Key("history.keep_runs", "int", 30,
        "The newest this many runs are always kept, however old. 0: no minimum.",
        minimum=0),
    Key("paths.data", "path", "",
        "Folder for the run history (reports, lists, index.html). Empty means "
        "~/.local/share/clean."),
    Key("ui.port", "int", 0,
        "Port `clean ui` listens on, on 127.0.0.1 only (0: a free one, chosen "
        "each time).", minimum=0, maximum=65535),
    Key("ui.open_browser", "bool", True,
        "`clean ui` opens the dashboard in the browser (--no-browser only prints "
        "its address)."),
    Key("ui.preview_max_mb", "int", 200,
        "The dashboard previews files up to this many MB (pictures, sound, "
        "video, text); bigger ones only show their details.", minimum=0),
    Key("ui.theme", "word", "auto",
        "The dashboard's colours: auto (follow the system), light or dark.",
        choices=("auto", "light", "dark")),
)}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


# ---------------------------------------------------------------- values

def parse_text(key, items):
    """Turn command-line / environment text into a value for `key`.

    `items` is a list of strings; only list settings take more than one.
    Raises ValueError with a message meant for the user.
    """
    if key.kind == "list":
        if len(items) == 1 and items[0].lstrip().startswith("["):
            try:
                value = json.loads(items[0])
            except ValueError:
                raise ValueError("not a valid JSON list")
            return check_value(key, value)
        return check_value(key, [i for i in items if i != ""])
    if len(items) != 1:
        raise ValueError("takes exactly one value")
    text = items[0].strip()
    if key.kind == "int":
        try:
            n = int(text)
        except ValueError:
            n = None
        if n is None or n < key.minimum or (key.maximum is not None and n > key.maximum):
            raise ValueError(f"{_int_rule(key)}, not {text!r}")
        return n
    if key.kind == "size":
        return size_from_text(text)
    if key.kind == "bool":
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ValueError(f"must be true or false (or yes/no, on/off, 1/0), not {text!r}")
    if key.kind == "word":
        return check_value(key, text)
    return check_value(key, items[0])


def parse_env(key, text):
    """An environment variable's text; lists are comma-separated or JSON."""
    if key.kind == "list" and not text.lstrip().startswith("["):
        return parse_text(key, [i.strip() for i in text.split(",")])
    return parse_text(key, [text])


def check_value(key, value):
    """Validate a value read from the settings file (JSON types). Returns it,
    normalised, or raises ValueError."""
    if key.kind == "int":
        if (not isinstance(value, int) or isinstance(value, bool) or value < key.minimum
                or (key.maximum is not None and value > key.maximum)):
            raise ValueError(f"{_int_rule(key)}, not {value!r}")
    elif key.kind == "size":
        if isinstance(value, str):
            value = size_from_text(value)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"must be a size such as 0, 4096 or \"10M\", not {value!r}")
    elif key.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"must be true or false, not {value!r}")
    elif key.kind == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError("must be a list of non-empty strings")
        if key.choices:
            value = [v.strip().lower() for v in value]
            bad = [v for v in value if v not in key.choices]
            if bad:
                raise ValueError(f"has {', '.join(map(repr, bad))}; the choices are "
                                 f"{', '.join(key.choices)}")
        value = list(dict.fromkeys(value))  # drop repeats, keep order
    elif key.kind == "path":
        if not isinstance(value, str):
            raise ValueError(f"must be a folder path, not {value!r}")
        if value and not os.path.isabs(os.path.expanduser(value)):
            raise ValueError(f"must be an absolute path (or empty for the default), "
                             f"not {value!r}")
    elif key.kind == "word":
        if not isinstance(value, str) or value.strip().lower() not in key.choices:
            raise ValueError(f"must be one of {', '.join(key.choices)}, not {value!r}")
        value = value.strip().lower()
    if key.check:
        value = key.check(value)
    return value


def _int_rule(key):
    if key.maximum is not None:
        return f"must be a whole number from {key.minimum} to {key.maximum}"
    return f"must be a whole number of at least {key.minimum}"


def format_value(key, value):
    """A value as shown by `clean config`."""
    if key.kind == "bool":
        return "true" if value else "false"
    if key.kind == "size":
        return f"{value}" if value < 1024 else f"{human(value)} ({value})"
    if key.kind == "list":
        return ", ".join(value) if value else "(none)"
    if key.kind == "path":
        return value or "(default)"
    return str(value)


def text_value(key, value):
    """A single value as printed by `clean config get`: text that `set`
    accepts back."""
    if key.kind == "bool":
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------- the store

class SettingsError(Exception):
    """The settings file can't be changed safely (unreadable or not valid)."""


class Settings:
    """The effective settings: environment over settings file over defaults."""

    def __init__(self, path=None, env=None):
        self.path = path or paths.settings_file()
        self.env = os.environ if env is None else env
        self.warnings = []
        self.file_values = {}   # valid values of known settings from the file
        self.kept = {}          # other entries (unknown or invalid): saved back as they are
        self.broken = None      # why the file can't be used, if it can't
        self.exists = False
        self._load()

    def _warn(self, text):
        self.warnings.append(text)

    @property
    def shown_path(self):
        return printable(paths.tilde(self.path))

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError) as e:
            reason = why(e) if isinstance(e, OSError) else "not valid UTF-8"
            self.broken = f"cannot read it: {reason}"
            self._warn(f"cannot read the settings file {self.shown_path} ({reason}); "
                       f"using the defaults")
            return
        self.exists = True
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as e:
            self.broken = f"it is not valid JSON ({e})"
            self._warn(f"the settings file {self.shown_path} is not valid JSON ({e}); "
                       f"using the defaults. Fix it with `clean config edit`.")
            return
        if not isinstance(data, dict):
            self.broken = "it is not a JSON object"
            self._warn(f"the settings file {self.shown_path} is not a JSON object "
                       f"({{\"name\": value, ...}}); using the defaults")
            return
        for name, value in data.items():
            key = KEYS.get(name)
            if key is None:
                self.kept[name] = value
                self._warn(f"unknown setting {name!r} in {self.shown_path}; ignored "
                           f"(see `clean config` for the valid names)")
                continue
            try:
                self.file_values[name] = check_value(key, value)
            except ValueError as e:
                self.kept[name] = value
                self._warn(f"setting {name} in {self.shown_path} {e}; using the default")

    def _from_env(self, key):
        """(found, value) from the environment variable of `key`."""
        text = self.env.get(key.env)
        if text is None:
            return False, None
        try:
            return True, parse_env(key, text)
        except ValueError as e:
            msg = f"{key.env}={text!r}: {e}; ignored"
            if msg not in self.warnings:
                self._warn(msg)
            return False, None

    def get(self, name):
        key = KEYS[name]
        found, value = self._from_env(key)
        if found:
            return value
        if name in self.file_values:
            return self.file_values[name]
        return key.default

    def check_all(self):
        """Read every setting once, so bad environment values are warned
        about too; returns all warnings."""
        for name in KEYS:
            self.get(name)
        return self.warnings

    def source(self, name):
        """Where the value of `name` comes from, for `clean config`."""
        key = KEYS[name]
        if self._from_env(key)[0]:
            return f"environment ({key.env})"
        if name in self.file_values:
            return "settings file"
        return "default"

    # ------------------------------------------------------------ changes

    def set(self, name, value):
        self._save({**self.file_values, name: value}, [name])

    def reset(self, names):
        values = {n: v for n, v in self.file_values.items() if n not in names}
        self._save(values, names)

    def _save(self, values, changed):
        """Write `values` to the file. Entries the file has that clean can't
        use are kept as they are, unless they are the ones being changed."""
        if self.broken:
            raise SettingsError(f"the settings file {self.shown_path} can't be changed "
                                f"because {self.broken}. Fix it with `clean config "
                                f"edit`, or delete it to start over.")
        kept = {n: v for n, v in self.kept.items() if n not in changed}
        data = {**kept, **values}
        text = json.dumps(dict(sorted(data.items())), indent=2, ensure_ascii=False) + "\n"
        atomic_write(self.path, text.encode("utf-8"))
        self.file_values = dict(values)
        self.kept = kept
        self.exists = True


_current = None


def current():
    """The settings for this run, loaded once."""
    global _current
    if _current is None:
        _current = Settings()
    return _current


def reload():
    global _current
    _current = None
    return current()
