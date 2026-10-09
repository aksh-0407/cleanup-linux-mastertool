"""
Known caches: where programs keep what they can download or rebuild, how
much space that takes, and the command each program offers for clearing its
own. clean never deletes these itself: only the program knows what in its
cache is still in use (a conda environment shares files with conda's package
cache; a running browser holds its cache open).

Caches in your home folder are measured by the `clean space` scan they are
in. System caches (the journal, apt's downloads, old snap versions) are
measured on their own, read-only, when a scanned folder is on the system
disk; the commands for those need sudo, which clean never runs.
"""

import os
import re
import shlex
import subprocess

SNAPS = "/var/lib/snapd/snaps"
SNAP_TIMEOUT = 10


class Known:
    """A cache clean knows about. `paths` are candidates (any that exist
    count); `command` is the program's own way to clear it, or None when
    there is none (then `note` says what to do)."""

    def __init__(self, key, label, paths, command=None, note=""):
        self.key, self.label, self.paths = key, label, paths
        self.command, self.note = command, note


def cache_homes():
    """The folders where programs keep caches: $XDG_CACHE_HOME and ~/.cache
    (both, when they differ)."""
    out = []
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    if os.path.isabs(xdg):
        out.append(xdg)
    out.append(os.path.join(os.path.expanduser("~"), ".cache"))
    return list(dict.fromkeys(os.path.realpath(p) for p in out))


EDITORS = (("Code", "VS Code"), ("Code - OSS", "Code - OSS"), ("VSCodium", "VSCodium"),
           ("Cursor", "Cursor"), ("Antigravity", "Antigravity"), ("Windsurf", "Windsurf"))
EDITOR_CACHES = ("Cache", "CachedData", "CachedExtensionVSIXs", "Code Cache", "GPUCache")


def catalogue():
    """Every known user cache, with its candidate folders (real paths)."""
    home = os.path.expanduser("~")
    caches = cache_homes()

    def h(*rel):
        return [os.path.join(home, *rel)]

    def c(*rel):
        return [os.path.join(top, *rel) for top in caches]

    out = [
        Known("conda", "conda packages",
              [p for d in ("miniconda3", "anaconda3", "miniforge3", "mambaforge", "miniforge",
                           ".conda") for p in h(d, "pkgs")],
              "conda clean --all",
              "Removes downloaded packages and those no environment uses; your "
              "environments keep working."),
        Known("pip", "pip downloads", c("pip"), "pip cache purge"),
        Known("uv", "uv cache", c("uv"), "uv cache clean",
              "Or `uv cache prune`, which keeps what is still in use."),
        Known("npm", "npm cache", h(".npm", "_cacache"), "npm cache clean --force"),
        Known("yarn", "Yarn cache", c("yarn") + h(".yarn", "berry", "cache"), "yarn cache clean"),
        Known("pnpm", "pnpm store", h(".local", "share", "pnpm", "store"), "pnpm store prune",
              "Removes only packages no project uses."),
        Known("go-build", "Go build cache", c("go-build"), "go clean -cache"),
        Known("go-mod", "Go modules", h("go", "pkg", "mod"), "go clean -modcache",
              "Downloaded again when a project needs them."),
        Known("huggingface", "Hugging Face models", c("huggingface", "hub"),
              "huggingface-cli delete-cache", "Lets you pick which models to delete."),
        Known("chrome", "Chrome cache", c("google-chrome"), None,
              "In Chrome: Settings › Privacy and security › Delete browsing data › "
              "Cached images and files."),
        Known("chromium", "Chromium cache", c("chromium"), None,
              "In Chromium: Settings › Privacy and security › Delete browsing data › "
              "Cached images and files."),
        Known("firefox", "Firefox cache",
              c("mozilla", "firefox") + h("snap", "firefox", "common", ".cache"), None,
              "In Firefox: Settings › Privacy & Security › Cookies and Site Data › "
              "Clear Data › Temporary cached files and pages."),
        Known("thumbnails", "Thumbnails", c("thumbnails"), None,
              "No command: the Files app makes thumbnails again as it needs them, so "
              "deleting what is inside this folder is safe."),
    ]
    for folder, label in EDITORS:
        out.append(Known("editor:" + folder, f"{label} caches",
                         [p for name in EDITOR_CACHES for p in h(".config", folder, name)],
                         None, f"Close {label} first, then delete these folders: it makes "
                               "them again."))
    for k in out:
        k.paths = list(dict.fromkeys(os.path.realpath(p) for p in k.paths))
    return out


def watch_paths():
    """Every folder whose size `clean space` should note during its scan."""
    out = set(cache_homes())
    for k in catalogue():
        out.update(k.paths)
    return out


def user_caches(scan, skip=()):
    """[{...}] for each known cache that the scan measured, biggest first,
    plus "other caches" for the rest of ~/.cache. `skip`: folders not to
    count as other caches (clean's own hash cache)."""
    out = []
    counted = []
    for k in catalogue():
        found = [(p, scan.watched[p]) for p in k.paths if p in scan.watched]
        # A folder inside another listed one is already counted in it.
        found = [(p, i) for p, i in found
                 if not any(q != p and _within(p, q) for q, _ in found)]
        if not found:
            continue
        counted += [p for p, _ in found]
        out.append(_entry(k.key, k.label, scan, found, k.command, k.note))
    for top in cache_homes():
        i = scan.watched.get(top)
        if i is None:
            continue
        inside = [scan.watched[p] for p in counted if _within(p, top) and p in scan.watched]
        inside += [scan.watched[p] for p in skip if p in scan.watched and _within(p, top)]
        e = _entry("other:" + top, "Other app caches", scan, [(top, i)], None,
                   "Apps make these again as needed; there is no one command for them all.")
        for j in inside:
            for f in ("disk", "frees", "shared", "files"):
                e[f] -= _measure(scan, j)[f]
        if e["disk"] > 0:
            out.append(e)
    out.sort(key=lambda e: -e["disk"])
    return out


def _within(path, top):
    return path == top or path.startswith(top.rstrip("/") + "/")


def _measure(scan, i):
    return {"disk": scan.tsolo[i] + scan.tshared[i], "frees": scan.tsolo[i],
            "shared": scan.tshared[i], "files": scan.tfiles[i],
            "partial": bool(scan.tflags[i] & 1)}


def _entry(key, label, scan, found, command, note):
    e = {"key": key, "label": label, "paths": [p for p, _ in found], "command": command,
         "commands": [command] if command else [], "note": note, "system": False,
         "disk": 0, "frees": 0, "shared": 0, "files": 0, "partial": False}
    for _, i in found:
        m = _measure(scan, i)
        for f in ("disk", "frees", "shared", "files"):
            e[f] += m[f]
        e["partial"] = e["partial"] or m["partial"]
    return e


# ---------------------------------------------------------------- system

def measure(path):
    """{disk, frees, shared, files, partial} of a folder outside the scan,
    read-only. partial: some of it could not be read."""
    out = {"disk": 0, "frees": 0, "shared": 0, "files": 0, "partial": False}
    seen = set()
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        out["partial"] = True
                        continue
                    blocks = st.st_blocks * 512
                    out["files"] += 1
                    if st.st_nlink > 1:
                        if (st.st_dev, st.st_ino) in seen:
                            continue
                        seen.add((st.st_dev, st.st_ino))
                        out["shared"] += blocks
                    else:
                        out["frees"] += blocks
                    out["disk"] += blocks
        except OSError:
            out["partial"] = True
    return out


# The system's caches that have a cleaning command: (key, label, folder,
# command, note). Only those whose folder exists are shown, so each
# distribution sees its own package manager.
_KEPT = "The installed programs stay; only the downloaded packages go."
SYSTEM = (
    ("journal", "System logs (journal)", "/var/log/journal",
     "sudo journalctl --vacuum-size=200M", "Keeps the newest 200 MB of logs."),
    ("apt", "Downloaded system updates (apt)", "/var/cache/apt/archives",
     "sudo apt clean", _KEPT),
    ("dnf", "Downloaded system updates (dnf)", "/var/cache/dnf",
     "sudo dnf clean packages", _KEPT),
    ("dnf5", "Downloaded system updates (dnf5)", "/var/cache/libdnf5",
     "sudo dnf clean packages", _KEPT),
    ("pacman", "Downloaded system updates (pacman)", "/var/cache/pacman/pkg",
     "sudo pacman -Sc", "Keeps the packages of the installed versions; older ones go."),
    ("zypper", "Downloaded system updates (zypper)", "/var/cache/zypp/packages",
     "sudo zypper clean --all", _KEPT),
)


def system_caches():
    """The system's own caches, measured read-only: [{...}]."""
    out = []
    for key, label, path, command, note in SYSTEM:
        if not os.path.isdir(path):
            continue
        m = measure(path)
        if m["disk"] <= 0:
            continue    # (apt's partial/ is never readable: 0 B there is nothing to clear)
        out.append({"key": key, "label": label, "paths": [path], "command": command,
                    "commands": [command], "note": note, "system": True, **m})
    snaps = old_snaps()
    if snaps:
        total = sum(s for _, _, s in snaps)
        cmds = [f"sudo snap remove {shlex.quote(name)} --revision={shlex.quote(rev)}"
                for name, rev, _ in snaps]
        out.append({"key": "snap", "label": "Old snap versions",
                    "paths": [os.path.join(SNAPS, f"{n}_{r}.snap") for n, r, _ in snaps],
                    "command": cmds[0] if len(cmds) == 1 else None, "commands": cmds,
                    "note": "snap keeps earlier versions of each app to roll back to; "
                            "`sudo snap set system refresh.retain=2` keeps fewer.",
                    "system": True, "disk": total, "frees": total, "shared": 0,
                    "files": len(snaps), "partial": False})
    out.sort(key=lambda e: -e["disk"])
    return out


_REV = re.compile(r"^[0-9A-Za-z._-]+$")


def old_snaps(run=subprocess.run):
    """[(name, revision, bytes)] of disabled snap revisions, from
    `snap list --all`; [] if snap isn't there or doesn't answer."""
    try:
        r = run(["snap", "list", "--all"], capture_output=True, timeout=SNAP_TIMEOUT,
                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    out = []
    lines = r.stdout.decode("utf-8", "replace").splitlines()
    for line in lines[1:]:
        cols = line.split()
        if len(cols) < 4 or "disabled" not in cols[-1].split(","):
            continue
        name, rev = cols[0], cols[2]
        if not (_REV.match(name) and _REV.match(rev)):
            continue
        try:
            size = os.stat(os.path.join(SNAPS, f"{name}_{rev}.snap")).st_blocks * 512
        except OSError:
            size = 0
        out.append((name, rev, size))
    return out
