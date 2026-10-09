"""
data.json.gz: a run's full results, for programs (and later for `clean ui`).

Paths are stored as a folder table, each folder once as [parent, name] (parent
-1 for a top entry, which holds a full path), and results point into it by
number, so a million files under the same long prefix don't repeat it. Names
that aren't valid UTF-8 survive the round trip exactly: their bytes are kept as
lone surrogates, written as \\udcNN escapes.

    {"format": 1, "kind": "empty", "roots": [...], "dirs": [[-1, "/media/x"], [0, "a"]],
     "folders": [[dir, depth, status, note]], "errors": [[dir, name, message]], ...}
"""

import gzip
import io
import json
import os

from cleanup.fsutil import json_text

FORMAT = 1
NAME = "data.json.gz"


class PathTable:
    """Folder paths, each stored once as [parent index, name]."""

    def __init__(self, tops=()):
        self.rows = []
        self.index = {}
        for top in tops:
            self.add_top(top)

    def add_top(self, path):
        if path not in self.index:
            self.index[path] = len(self.rows)
            self.rows.append([-1, path])
        return self.index[path]

    def add(self, path):
        """The index of folder `path`, adding it and any missing parents.
        Parents are found by walking up (no recursion, so any depth works)
        until a known folder or a top entry; a path under none of them gets
        its own chain from "/"."""
        missing = []
        p = path
        while p not in self.index:
            parent = os.path.dirname(p)
            if parent == p:             # reached "/"
                self.add_top(p)
                break
            missing.append(p)
            p = parent
        for p in reversed(missing):
            self.index[p] = len(self.rows)
            self.rows.append([self.index[os.path.dirname(p)], os.path.basename(p)])
        return self.index[path]

    def split(self, path):
        """[folder index, name] for a file or folder path."""
        return [self.add(os.path.dirname(path)), os.path.basename(path)]


def paths_of(rows):
    """Every folder's full path, from a stored folder table."""
    out = []
    for parent, name in rows:
        out.append(name if parent < 0 else os.path.join(out[parent], name))
    return out


def encode(obj):
    """gzip of the compact JSON; the same input always gives the same bytes.
    Level 6 (gzip's own default): about half the time of 9 on a big scan's
    results, for files under 1% bigger."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=6) as gz:
        gz.write(json_text(obj).encode("utf-8"))
    return buf.getvalue()


def read(path):
    with gzip.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))
