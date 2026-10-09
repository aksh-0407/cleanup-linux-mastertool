"""
Which files a duplicate scan looks at: kinds of files (images, audio, ...),
extensions, sizes, dates and name patterns; the OS junk that is ignored
everywhere; hidden names; Trash folders.

A file left out by a filter is not compared, so the folder holding it can't be
called a copy of anything as a whole (see scan/tree.py). Junk is different:
it is not worth keeping, so it is simply ignored, and two folders that differ
only in a Thumbs.db still count as copies.
"""

import fnmatch
import os
import re
import time
from datetime import datetime

# Kinds of files, by extension, as offered by --type (and shown as the "by
# type" totals). Each extension is in one kind only.
TYPES = {
    "images": "jpg jpeg jpe jfif png gif bmp dib tif tiff webp heic heif avif jxl ico "
              "svg psd psb xcf tga exr hdr raw cr2 cr3 crw nef nrw arw srf sr2 orf rw2 "
              "raf dng pef srw x3f erf kdc mrw 3fr",
    "audio": "mp3 m4a m4b m4p aac flac wav wave ogg oga opus wma aif aiff aifc alac ape "
             "mka mid midi amr ac3 dts ra wv",
    "video": "mp4 m4v mkv mov avi wmv flv webm mpg mpeg mpe m2v m2ts mts ts vob 3gp 3g2 "
             "ogv divx xvid rm rmvb asf f4v insv lrv",
    "documents": "pdf doc docx docm dot dotx odt ott rtf txt md tex xls xlsx xlsm xlsb ods "
                 "csv ppt pptx pptm pps ppsx odp key pages numbers epub mobi azw azw3 "
                 "djvu xps oxps chm one vsd vsdx pub",
    "archives": "zip rar 7z tar gz tgz bz2 tbz2 xz txz zst lz lzma lz4 z cab iso img dmg "
                "wim arj lzh ace cpio",
    "applications": "exe msi msix msixbundle appx appxbundle com bat cmd ps1 dll sys ocx "
                    "scr cpl app pkg deb rpm apk xapk aab appimage flatpak snap run jar",
}
TYPES = {kind: frozenset(exts.split()) for kind, exts in TYPES.items()}
_KIND_OF = {ext: kind for kind, exts in TYPES.items() for ext in exts}
CATEGORIES = [k.capitalize() for k in TYPES] + ["Other"]

# Clutter the OS writes by itself: thumbnail caches, folder view settings.
JUNK_NAMES = ("Thumbs.db", "ehthumbs.db", "desktop.ini", ".DS_Store", ".directory")

# The freedesktop Trash on a drive (.Trash-1000, .Trash/1000) and in the home
# folder. Copies in the Trash don't count as copies: emptying it loses them.
_TRASH_NAME = re.compile(r"\.Trash(-\d+)?")


def extension(name):
    """'Photo.JPG' -> 'jpg'; '' when there is none ('.bashrc' has none)."""
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot > 0 else ""


def category_of(name):
    """'Images', 'Audio', ... or 'Other', by the file's extension."""
    kind = _KIND_OF.get(extension(name))
    return kind.capitalize() if kind else "Other"


def is_hidden(name):
    return name.startswith(".")


class Junk:
    """Junk file names, matched ignoring case."""

    def __init__(self, names):
        self.names = frozenset(n.lower() for n in names)

    def __call__(self, name):
        return name.lower() in self.names


def is_trash(name, path):
    """A Trash folder: .Trash-1000 or .Trash on a drive, or ~/.local/share/Trash."""
    if _TRASH_NAME.fullmatch(name):
        return True
    return name == "Trash" and path == _home_trash()


def _home_trash():
    data = os.environ.get("XDG_DATA_HOME", "")
    if not os.path.isabs(data):
        data = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(os.path.realpath(data), "Trash")


def parse_extensions(values):
    """['jpg,PNG', '.gif'] -> {'jpg', 'png', 'gif'}."""
    out = set()
    for value in values:
        for part in value.split(","):
            part = part.strip().lstrip(".").lower()
            if part:
                out.add(part)
    return out


def parse_types(values):
    """['images,audio'] -> ['images', 'audio']; raises ValueError for an unknown kind."""
    out = []
    for value in values:
        for part in value.split(","):
            part = part.strip().lower()
            if not part:
                continue
            if part not in TYPES:
                raise ValueError(f"unknown type {part!r}; the types are {', '.join(TYPES)}")
            out.append(part)
    return list(dict.fromkeys(out))


_AGO = re.compile(r"(\d+)\s*([dwy])", re.IGNORECASE)


def parse_when(text, now=None):
    """'2024-03-01', '2024-03-01 14:30' (local time) or '30d' / '6w' / '2y' ago
    -> a timestamp in nanoseconds. Raises ValueError."""
    text = text.strip()
    m = _AGO.fullmatch(text)
    if m:
        days = int(m.group(1)) * {"d": 1, "w": 7, "y": 365}[m.group(2).lower()]
        now = time.time() if now is None else now
        return int((now - days * 86400) * 1e9)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1e9)
        except ValueError:
            continue
    raise ValueError(f"invalid date {text!r} (examples: 2024-03-01, '2024-03-01 14:30', "
                     f"30d, 6w, 2y)")


class Filters:
    """Which files are compared. Empty files are never duplicates, so the size
    and other filters don't apply to them (they are kept for the folder
    comparisons)."""

    def __init__(self, types=(), exts=(), not_exts=(), min_size=0, max_size=0,
                 newer_ns=None, older_ns=None, names=()):
        self.types = list(types)
        allowed = set(exts)
        for kind in self.types:
            allowed |= TYPES[kind]
        self.exts = frozenset(allowed) if (self.types or exts) else None
        self.not_exts = frozenset(not_exts)
        self.min_size = min_size
        self.max_size = max_size
        self.newer_ns = newer_ns
        self.older_ns = older_ns
        self.name_globs = list(names)
        self._names = [re.compile(fnmatch.translate(p), re.IGNORECASE | re.DOTALL)
                       for p in names]
        self._ext_only = self.exts is not None or bool(self.not_exts)

    @property
    def active(self):
        return bool(self.exts is not None or self.not_exts or self.min_size > 1
                    or self.max_size or self.newer_ns is not None
                    or self.older_ns is not None or self._names)

    def file_ok(self, name, size, mtime_ns):
        if size < self.min_size or (self.max_size and size > self.max_size):
            return False
        if self._ext_only:
            ext = extension(name)
            if self.exts is not None and ext not in self.exts:
                return False
            if ext in self.not_exts:
                return False
        if self.newer_ns is not None and mtime_ns < self.newer_ns:
            return False
        if self.older_ns is not None and mtime_ns >= self.older_ns:
            return False
        if self._names and not any(p.match(name) for p in self._names):
            return False
        return True

    def describe(self):
        """The active filters as short phrases, for the summary."""
        from cleanup.fsutil import human
        out = []
        if self.types:
            out.append("types: " + ", ".join(self.types))
        extra = sorted(self.exts - set().union(*(TYPES[k] for k in self.types))) \
            if self.exts is not None else []
        if extra:
            out.append("extensions: " + ", ".join(extra))
        if self.not_exts:
            out.append("not: " + ", ".join(sorted(self.not_exts)))
        if self.min_size > 1:
            out.append(f"at least {human(self.min_size)}")
        if self.max_size:
            out.append(f"at most {human(self.max_size)}")
        if self.newer_ns is not None:
            out.append(f"changed since {_day(self.newer_ns)}")
        if self.older_ns is not None:
            out.append(f"changed before {_day(self.older_ns)}")
        if self.name_globs:
            out.append("names: " + ", ".join(self.name_globs))
        return out


def _day(ns):
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M")
