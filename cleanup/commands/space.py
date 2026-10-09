"""
`clean space`: where the space goes. One read-only scan of the folders given
(your home folder by default) finds the biggest folders and files, the caches
programs keep, clutter (zero-byte files, broken symlinks, temp leftovers,
empty folders), and adds the drives, the Trash of every drive, the newest
duplicates scan and clean's own data. The engine is cleanup/space.py.

It deletes nothing. What can go is reported with the command for it: the
program's own one for a cache, `clean trash empty` for the Trash, `clean
empty` for empty folders, `clean cache` and `clean history --prune` for
clean's own data.
"""

import argparse
import os
import sys

from cleanup import caches, options, paths, runs
from cleanup import settings as _settings
from cleanup import space as _space
from cleanup import volumes as _volumes
from cleanup.commands import trash as _trash_cmd
from cleanup.errors import Cancelled, DriveGone
from cleanup.fsutil import DEFAULT_EXCLUDES, Excluder, Progress, is_within, resolve_roots
from cleanup.report import data
from cleanup.report import space as _report
from cleanup.safety import DriveWatch
from cleanup.scan import cache as _cache

EXAMPLES = """\
examples:
  clean space                       your home folder
  clean space ~ /media/$USER/USB    your home folder and that drive
  clean space ~/Projects --top 40   the 40 biggest folders and files there
  clean show --open                 the report page: every folder, to drill down

What it looks at, read-only: the biggest folders and files (a file with
several names counts once, and sizes are what files take on the disk); the
caches programs keep, with the command each program offers to clear its own;
the Trash of every drive; clutter (zero-byte files, broken symlinks, temp
leftovers such as *.tmp, ~$* and .~lock.*, and empty folders); the newest
duplicates scan of these folders; and clean's own data. Each PATH is walked
without leaving its drive: name another drive as a PATH of its own.

Nothing is deleted. The commands it suggests are yours to run: clean never
runs sudo, and never clears a program's cache itself (only the program knows
what in it is still in use).

Exit status: 0 done; 1 some folders could not be read, or a drive went away
mid-scan; 2 bad arguments; 130 interrupted.
"""

TERMINAL_TOP = 15


def add_parser(subparsers):
    p = subparsers.add_parser(
        "space",
        help="where the space goes: biggest folders, caches, Trash, clutter",
        description="Show where the space goes under PATH (your home folder by\n"
                    "default): the drives, the biggest folders and files, caches,\n"
                    "the Trash, clutter and clean's own data. Read-only.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", metavar="PATH",
                   help="folder(s) to look at (default: your home folder)")
    s = _settings.current()
    defaults = s.get("scan.default_excludes")
    extra = s.get("scan.exclude")
    p.add_argument("-e", "--exclude", action="append", default=[], metavar="PATTERN",
                   help="leave out files and folders matching PATTERN (a shell glob "
                        "matched against the name; with a '/', against the full "
                        "path); they are not counted. Repeatable."
                        + (f" Always left out too (setting scan.exclude): "
                           f"{', '.join(extra)}." if extra else ""))
    p.add_argument("--no-default-excludes", dest="default_excludes", action="store_false",
                   default=defaults,
                   help="also look for clutter inside " + ", ".join(DEFAULT_EXCLUDES)
                        + " (they are always counted; by default nothing inside is "
                          "listed, because programs need them intact)")
    p.add_argument("--default-excludes", dest="default_excludes", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--top", type=options.positive_int, default=TERMINAL_TOP, metavar="N",
                   help=f"how many folders and files to list in the terminal "
                        f"(default: {TERMINAL_TOP}; the report has more)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="no progress line")
    p.set_defaults(func=run)
    return p


def run(args):
    roots = space_roots(args.paths or [os.path.expanduser("~")])
    run = runs.start("space", "read-only", roots)
    with runs.recording(run):
        return _run(args, run, roots)


def space_roots(raw):
    """The folders to walk. A PATH inside another one is walked with it,
    unless it is on another drive (a drive mounted inside): the walk doesn't
    enter that, so it is a root of its own."""
    roots, given = resolve_roots(raw, refuse_symlinks=True)
    for p in sorted(dict.fromkeys(given), key=len):
        if p in roots:
            continue
        outer = max((r for r in roots if is_within(p, r)), key=len)
        try:
            if os.stat(p).st_dev != os.stat(outer).st_dev:
                roots.append(p)
        except OSError:
            pass
    return roots


def _run(args, run, roots):
    progress = Progress(args.quiet)
    data_dir = os.path.realpath(paths.data_dir())
    cache_dir = os.path.realpath(paths.cache_dir())
    excluded = Excluder(options.excludes(args), False)
    scan = _space.Scan(excluded, quiet_paths=(data_dir, cache_dir),
                       quiet_names=DEFAULT_EXCLUDES if args.default_excludes else (),
                       progress=progress, watch=DriveWatch(roots),
                       watch_paths=caches.watch_paths() | {data_dir, cache_dir},
                       keep_paths=user_dirs())
    status, code, stopped = "done", 0, None
    try:
        scan.run(roots)
    except (KeyboardInterrupt, Cancelled):
        progress.clear()
        print("\nInterrupted — the scan did not finish, so there are no results.",
              file=sys.stderr)
        status, code, stopped = "interrupted", 130, "interrupted while scanning; no results"
    except DriveGone as e:
        progress.clear()
        print(f"clean: error: {e}\nStopped — no results were saved.", file=sys.stderr)
        status, code, stopped = "drive-gone", 1, f"stopped: {e}"
    progress.clear()
    if stopped:
        doc = {"format": data.FORMAT, "kind": "space", "roots": roots, "stopped": stopped,
               "seconds": round(scan.seconds, 1)}
        _finish(run, doc, status, code, error=stopped if status == "drive-gone" else None)
        return code
    if not args.quiet:
        progress.show("measuring the system's caches and the Trash")
    doc = build(scan, roots, data_dir, cache_dir, run.name)
    progress.clear()
    if doc["errors_total"]:
        code = 1
    warnings = _finish(run, doc, "done", code)
    print(_report.summary(doc, run, terminal=True, top=args.top), end="")
    return code if code or not warnings else 1


def _finish(run, doc, status, code, error=None):
    run.conclude(status, code, _report.headline(doc), _report.totals(doc), error=error)
    return run.complete([("report.html", lambda: _report.page(run, doc)),
                         (data.NAME, lambda: data.encode(doc)),
                         ("summary.txt", lambda: _report.summary(doc, run))])


# ---------------------------------------------------------------- gathering

def build(scan, roots, data_dir, cache_dir, this_run=None):
    """Everything the reports show, as plain JSON data."""
    table = data.PathTable(roots)
    paths_memo = {}

    def folder_path(i):
        p = paths_memo.get(i)
        if p is None:
            p = paths_memo[i] = scan.path(i)
        return p

    def where(i):
        return table.add(folder_path(i))

    kept = scan.kept()
    pos = {i: n for n, i in enumerate(kept)}
    folders = []
    for i in kept:
        p = scan.parent[i]
        folders.append([pos[p] if p >= 0 else -1, scan.name[i], scan.tdisk[i], scan.tsize[i],
                        scan.tfiles[i], scan.tdirs[i], scan.tflags[i], scan.disk[i],
                        scan.files[i], scan.look[i]])
    big = [[pos[i], name, disk, size, int(mtime)]
           for i, files in scan.big.items() if i in pos
           for disk, size, name, mtime in files]
    top = [[where(i), name, disk, size, int(mtime)]
           for disk, size, i, name, mtime in scan.top_files()]

    def clutter(c, item):
        return {"count": c.count, "bytes": c.bytes, "unlisted": c.unlisted, "more": c.more,
                "items": [item(*x) for x in c.items]}

    count, unlisted, outer, more = scan.empty_folders()
    empty = {"count": count, "bytes": 0, "unlisted": unlisted, "more": more,
             "items": [table.split(folder_path(i)) + [inner] for i, inner in outer]}
    doc = {
        "format": data.FORMAT, "kind": "space", "roots": roots,
        "seconds": round(scan.seconds, 1),
        "total": {"disk": scan.total(),
                  "size": sum(scan.tsize[i] for _, i, _ in scan.roots),
                  "files": sum(scan.tfiles[i] for _, i, _ in scan.roots),
                  "dirs": sum(scan.tdirs[i] for _, i, _ in scan.roots)},
        "folders": folders, "keep_min": scan.keep_bar, "big": big, "top": top,
        "clutter": {
            "zero": clutter(scan.zero, lambda i, name: [where(i), name]),
            "broken": clutter(scan.broken, lambda i, name, target, reason:
                              [where(i), name, target, reason]),
            "temp": clutter(scan.temp, lambda i, name, disk, mtime:
                            [where(i), name, disk, int(mtime)]),
            "empty": empty,
        },
        "links": {"names": scan.link_names, "bytes": scan.link_bytes},
        "excluded": scan.excluded_n,
        "mounts": [p for p, _ in scan.mounts][:_space.LIST_MAX],
        "errors": [table.split(p) + [msg] for p, msg in scan.errors[:_space.LIST_MAX]],
        "errors_total": len(scan.errors),
        "now": int(scan.now),
    }
    skip = [p for p in (data_dir, cache_dir)]
    doc["caches"] = caches.user_caches(scan, skip=skip)
    doc["system_caches"] = (caches.system_caches()
                            if _settings.current().get("space.system_caches")
                            and _on_system_disk(roots) else None)
    vols = _trash_cmd._volumes()
    doc["trash"] = _trash_cmd.overview(vols)
    doc["trash_warn_gb"] = _settings.current().get("trash.warn_gb")
    doc["drives"] = drives()
    doc["dupes"] = newest_dupes(roots)
    doc["own"] = own_data(this_run)
    doc["dirs"] = table.rows
    return doc


def drives():
    return [{"name": name, "point": point, "fstype": fstype, "total": total, "used": used,
             "free": free, "ro": ro}
            for name, point, fstype, total, used, free, ro in _volumes.drives()]


def _on_system_disk(roots):
    """True when a root is on the drive of / or of the home folder (the
    system's caches live there)."""
    devs = set()
    for p in ("/", os.path.expanduser("~")):
        try:
            devs.add(os.stat(p).st_dev)
        except OSError:
            pass
    for r in roots:
        try:
            if os.stat(r).st_dev in devs:
                return True
        except OSError:
            pass
    return False


def user_dirs():
    """The folders the desktop keeps even when empty (Desktop, Templates,
    Public...), from user-dirs.dirs and the usual names."""
    home = os.path.expanduser("~")
    out = {os.path.join(home, n) for n in ("Desktop", "Documents", "Downloads", "Music",
                                           "Pictures", "Public", "Templates", "Videos")}
    conf = os.environ.get("XDG_CONFIG_HOME", "")
    for folder in ([conf] if os.path.isabs(conf) else []) + [os.path.join(home, ".config")]:
        try:
            with open(os.path.join(folder, "user-dirs.dirs"), encoding="utf-8",
                      errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            name, _, value = line.partition("=")
            if not name.strip().startswith("XDG_") or not value:
                continue
            value = value.strip().strip('"').replace("$HOME", home)
            if os.path.isabs(value) and value.rstrip("/") != home:
                out.add(value.rstrip("/"))
    return {os.path.realpath(p) for p in out}


def newest_dupes(roots, data_dir=None):
    """The newest finished duplicates scan of any of these folders (or of
    folders inside or around them): {name, number, started, headline, roots,
    reclaimable} or None."""
    for r in runs.history(data_dir):
        if r.command != "dupes" or r.status != "done":
            continue
        theirs = [p for p in r.roots if isinstance(p, str)]
        if any(is_within(a, b) or is_within(b, a) for a in theirs for b in roots):
            return {"name": r.name, "number": r.number, "started": r.started_ts,
                    "headline": r.headline, "roots": theirs,
                    "reclaimable": (r.meta.get("totals") or {}).get("reclaimable")}
    return None


def own_data(this_run=None, data_dir=None):
    """clean's own data: the runs (with what retention would prune) and the
    hash cache. For `this_run`, those runs go as it ends (runs.housekeeping),
    which "at_end" says."""
    s = _settings.current()
    data_dir = data_dir or paths.data_dir()
    found = runs.history(data_dir)
    prunable, _ = runs.prune_plan(found, s.get("history.keep_days"), s.get("history.keep_runs"))
    size = caches.measure(data_dir) if os.path.isdir(data_dir) else {"disk": 0, "files": 0}
    info = _cache.info()
    return {"data_dir": data_dir, "runs": len(found), "data_bytes": size["disk"],
            "data_files": size["files"], "keep_days": s.get("history.keep_days"),
            "keep_runs": s.get("history.keep_runs"), "prunable": len(prunable),
            "at_end": this_run is not None,
            "cache": {"path": info["path"], "exists": info["exists"], "bytes": info["bytes"],
                      "entries": info["entries"], "drives": len(info["drives"]),
                      "oldest": info["oldest"], "error": info["error"],
                      "enabled": s.get("cache.enabled")},
            "cache_max_mb": s.get("cache.max_mb")}
