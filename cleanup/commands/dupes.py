"""
`clean dupes`: find duplicates, as whole folders and as single files. Reports
only; never deletes. The engine: scan/tree.py lists the files, dupes/files.py
finds identical contents (with the hash cache, scan/cache.py), dupes/folders.py
works out which folders are copies of which, dupes/diff.py how they differ,
and with --media dupes/lookalikes.py finds look-alike photos and songs.
Every run is saved in the data folder (see cleanup/runs.py): summary.txt,
report.html, data.json.gz, files.tsv and folders.tsv.
"""

import argparse
import os
import sys

from cleanup import options, paths, runs
from cleanup import settings as _settings
from cleanup.commands import remove as _remove_cmd
from cleanup.dupes import lookalikes as _lookalikes
from cleanup.dupes import manifest as _manifest
from cleanup.dupes import results as _results
from cleanup.dupes.files import Matcher
from cleanup.dupes.folders import Relations
from cleanup.errors import Cancelled, DriveGone, UsageError
from cleanup.fsutil import Excluder, Progress, resolve_roots
from cleanup.report import data, html, text
from cleanup.safety import DriveWatch
from cleanup.scan import filters as _filters
from cleanup.scan.cache import Cache
from cleanup.scan.tree import Scanner
from cleanup.volumes import Volumes

# The default of 1 worker (setting dupes.jobs) is deliberate, as for `empty`:
# on a spinning USB drive the walk is seek-bound, and several walkers make the
# head thrash (3-5x SLOWER at -j8 than -j1 on a 5 TB exFAT USB hard drive). Hashing is
# disk-bound there too. More jobs help only on SSD/NVMe.

EXAMPLES = """\
examples:
  clean dupes "/media/$USER/Backup Drive/Photos"
  clean dupes ~/Pictures "/media/$USER/Backup Drive"       compare two places
  clean dupes ~/Pictures --type images --min-size 100K     photos, no thumbnails
  clean dupes /media/$USER/USB --name '*.mp4' --newer-than 1y
  clean dupes ~/Music /media/$USER/USB/Music --media       also songs in other formats
  clean show --open                                        the last run's report page
  clean dupes ~/Pictures /media/$USER/USB --remove --keep-in ~/Pictures -n
                       scan, then show what `clean remove` would do with the results

Files are duplicates when their bytes are identical: names and dates don't
matter unless --same-name or --same-date ask for them too. Results come in
these kinds:
  folder families  folders that are copies of each other: identical, the same
                   files renamed or reorganised, partial copies, and diverged
                   copies (similar, but each has files of its own)
  covered folders  folders whose every file also exists somewhere else
  file groups      the duplicate files, one group per content; those the
                   folder results already account for are folded away
  look-alikes      with --media: the same photo or song in files whose bytes
                   differ (an edited photo, a song at another bitrate). They
                   are only reported: nothing picks them for removal, and the
                   last version of one can't be removed

Not compared: empty files, symlinks, extra names of hard-linked files, system
junk such as Thumbs.db and .DS_Store (setting junk.names), Trash folders, and
anything inside .git, .hg, .svn, .bzr, lost+found and .stfolder. Hidden files
are compared, as part of their folder, but listed only with --hidden. A folder
holding anything left out (except junk) is never called a copy as a whole.

Nothing is deleted by this command unless --remove is given: then, once the
scan is done, it goes on exactly as `clean remove` would (the same rules,
checks, question and undo; see `clean remove --help`). Every run is saved
(`clean history` lists them); files.tsv in the run's folder lists every
duplicate file and folders.tsv every folder result. File hashes are kept in a
cache, so a second scan of the same files reads only what changed (--no-cache:
not this time).

Exit status: 0 done; 1 some files could not be read, or the drive went away
mid-run (then there are no results, rather than incomplete ones); 2 bad
arguments; 130 interrupted.
"""


def _types(text):
    try:
        return _filters.parse_types([text])
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _when(text):
    try:
        return _filters.parse_when(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def add_parser(subparsers):
    p = subparsers.add_parser(
        "dupes",
        help="find duplicate folders and files (report only)",
        description="Find duplicates: folders that are copies of each other (whole,\n"
                    "renamed, partial or diverged) and byte-identical files, in one\n"
                    "folder or across several; with --media, also look-alike photos\n"
                    "and songs. Reports only, unless --remove.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s = _settings.current()
    p.add_argument("paths", nargs="+", metavar="PATH",
                   help="folder(s) to search; duplicates are found across all of them. "
                        "Of folders that are copies of each other, the one in the first "
                        "PATH given is preferred for keeping")

    which = p.add_argument_group(
        "which files", "Files left out are not compared, and a folder holding one is "
                       "not compared as a whole.")
    which.add_argument("--type", action="append", type=_types, metavar="KINDS",
                       help="only these kinds of files, comma-separated: "
                            + ", ".join(_filters.TYPES)
                            + f" ({_shown_list(s, 'dupes.types', 'every kind')})")
    which.add_argument("--ext", action="append", default=[], metavar="EXTS",
                       help="only files with these extensions, e.g. jpg,png (with --type: "
                            "these as well)")
    which.add_argument("--not-ext", action="append", default=[], metavar="EXTS",
                       help="leave out files with these extensions")
    which.add_argument("--min-size", type=options.parse_size,
                       default=s.get("dupes.min_size"), metavar="SIZE",
                       help=f"ignore files smaller than this, e.g. 4096, 500K, 10M "
                            f"({options.shown('dupes.min_size', s)})")
    which.add_argument("--max-size", type=options.parse_size,
                       default=s.get("dupes.max_size"), metavar="SIZE",
                       help=f"ignore files bigger than this; 0 = no limit "
                            f"({options.shown('dupes.max_size', s)})")
    which.add_argument("--newer-than", type=_when, metavar="WHEN",
                       help="only files changed since WHEN: a date (2024-03-01, "
                            "'2024-03-01 14:30') or a time ago (30d, 6w, 2y)")
    which.add_argument("--older-than", type=_when, metavar="WHEN",
                       help="only files last changed before WHEN")
    which.add_argument("--name", action="append", default=[], metavar="GLOB",
                       help="only files whose name matches, ignoring case, e.g. "
                            "'IMG_*' or '*.mp4'. Repeatable: any of them")
    which.add_argument("--max-depth", type=options.whole_number, metavar="N",
                       help="look only N folders deep: 0 = just the files in PATH, "
                            "1 = and its subfolders, ... (default: no limit)")
    hidden = s.get("dupes.hidden")
    which.add_argument("--hidden", dest="hidden", action="store_true", default=hidden,
                       help=argparse.SUPPRESS if hidden else
                       "also list hidden files and folders (names starting with \".\") "
                       "in the results; they are always compared as part of their folder")
    which.add_argument("--no-hidden", dest="hidden", action="store_false",
                       help="don't list hidden files and folders (on by default; "
                            "setting dupes.hidden)" if hidden else argparse.SUPPRESS)

    match = p.add_argument_group("how files and folders match")
    for flag, key, what, short in (
            ("same-name", "dupes.same_name", "their names match too (ignoring case)",
             "matching names"),
            ("same-date", "dupes.same_date",
             "their modification times match too (to the second)", "matching dates")):
        on = s.get(key)
        dest = flag.replace("-", "_")
        match.add_argument(f"--{flag}", dest=dest, action="store_true", default=on,
                           help=argparse.SUPPRESS if on else
                           f"files are duplicates only if {what}; the content always "
                           f"has to")
        match.add_argument(f"--no-{flag}", dest=dest, action="store_false",
                           help=f"don't require {short} (on by default; setting {key})"
                           if on else argparse.SUPPRESS)
    media = s.get("dupes.media")
    match.add_argument("--media", dest="media", action="store_true", default=media,
                       help=argparse.SUPPRESS if media else
                       "also find look-alikes: the same photo (capture time, camera, "
                       "dimensions) or song (artist, title, length) in files whose bytes "
                       "differ. Reads the tags of every photo and song")
    match.add_argument("--no-media", dest="media", action="store_false",
                       help="don't look for look-alike photos and songs (on by default; "
                            "setting dupes.media)" if media else argparse.SUPPRESS)
    match.add_argument("--similar", type=options.percent, metavar="PCT",
                       default=s.get("dupes.similar_pct"),
                       help=f"call two folders diverged copies when they share at least "
                            f"PCT %% of their files ({options.shown('dupes.similar_pct', s)})")
    cache = s.get("cache.enabled")
    match.add_argument("--no-cache", dest="cache", action="store_false", default=cache,
                       help="read every file compared, ignoring hashes saved by earlier "
                            "scans (and don't save new ones)" if cache else argparse.SUPPRESS)
    match.add_argument("--cache", dest="cache", action="store_true",
                       help=argparse.SUPPRESS if cache else
                       "use and update the hash cache (off by default; setting "
                       "cache.enabled)")

    options.add_removed_options(p, "--report", "--json")
    options.add_walk_options(
        p, "dupes.jobs",
        jobs_help="parallel workers for the walk and hashing; >1 helps ONLY on "
                  "SSD/NVMe, it is slower on spinning USB drives")
    rm = p.add_argument_group("removing straight away (`clean remove` does it later)")
    rm.add_argument("--remove", action="store_true",
                    help="after the scan, remove the extra copies as `clean remove` "
                         "would (to the Trash; asks first)")
    rm.add_argument("-n", "--dry-run", action="store_true",
                    help="--remove: only show what would go, and save it as a run")
    _remove_cmd.add_options(p, prefix="--remove: ")
    p.set_defaults(func=run)
    return p


# Options only --remove uses, and what they are when not given.
_REMOVE_ONLY = (("keep_in", "--keep-in", []), ("remove_from", "--remove-from", []),
                ("rank", "--rank", None), ("rank_folders", "--rank-folders", False),
                ("only", "--only", None), ("merge", "--merge", False),
                ("conflicts", "--conflicts", "both"), ("link", "--link", False),
                ("yes", "-y", False), ("force", "--force", False),
                ("dry_run", "-n", False))


def _check_remove(args):
    """Refuse removal options without --remove (they would silently do
    nothing), and bad ones with it before scanning for an hour."""
    if not args.remove:
        given = [flag for key, flag, unset in _REMOVE_ONLY if getattr(args, key) != unset]
        if args.method != _settings.current().get("remove.method"):
            given.append("--" + args.method)
        if given:
            raise UsageError(f"{', '.join(given)} only {'works' if len(given) == 1 else 'work'}"
                             f" with --remove (or run `clean remove` after the scan)")
        return
    _remove_cmd.precheck(args)


def _shown_list(s, key, empty):
    value = s.get(key)
    return f"default: {', '.join(value) if value else empty}; setting {key}"


def run(args):
    _check_remove(args)
    roots, _ = resolve_roots(args.paths, refuse_symlinks=False)
    data_dir = paths.data_dir()
    run = runs.start("dupes", "report", roots)
    with runs.recording(run):
        code = _run(args, run, roots, data_dir)
    if not args.remove or run.meta.get("status") != "done":
        return code
    print(file=sys.stdout)
    return _remove_cmd.act(args, run) or code


def _filters_of(args):
    s = _settings.current()
    types = ([t for group in args.type for t in group] if args.type is not None
             else s.get("dupes.types"))
    return _filters.Filters(
        types=list(dict.fromkeys(types)), exts=_filters.parse_extensions(args.ext),
        not_exts=_filters.parse_extensions(args.not_ext), min_size=args.min_size,
        max_size=args.max_size, newer_ns=args.newer_than, older_ns=args.older_than,
        names=args.name)


def _run(args, run, roots, data_dir):
    s = _settings.current()
    progress = Progress(args.quiet)
    watch = DriveWatch(roots)
    flt = _filters_of(args)
    excluded = Excluder(options.excludes(args), args.default_excludes,
                        skip=[os.path.realpath(data_dir), os.path.realpath(paths.cache_dir())])
    scanner = Scanner(excluded, args.one_file_system, flt, _filters.Junk(s.get("junk.names")),
                      args.max_depth, progress, watch)
    info = {"roots": roots, "jobs": args.jobs, "quiet": args.quiet, "stopped": None,
            "filters": flt.describe(), "same_name": args.same_name,
            "same_date": args.same_date, "similar": args.similar, "hidden": args.hidden,
            "max_depth": args.max_depth, "total_files": 0, "hard_links": 0,
            "skipped": {}, "errors": [], "hashing": None,
            "media": args.media, "lookalikes": [], "media_stats": None,
            "families": [], "covered": [], "groups": [], "totals": None}
    cache = None
    stage = "scanning"
    try:
        tree = scanner.scan(roots, args.jobs)
        info["errors"] = tree.errors
        stage = "comparing files"
        if args.cache:
            cache = Cache()
            if cache.active:
                try:
                    vols = Volumes()
                except (OSError, ValueError):
                    vols = None
                cache.attach(tree, vols)
        matcher = Matcher(tree, args.same_name, args.same_date, cache, args.jobs,
                          progress, watch)
        matcher.run()
        stage = "comparing folders"
        if progress.enabled:
            progress.show(f"comparing folders: {tree.nfolders:,}")
        rel = Relations(tree, matcher, args.similar, args.hidden).run()
        info.update(_results.collect(tree, matcher, rel))
        finder = None
        if args.media:
            stage = "comparing photos and songs"
            if progress.enabled:
                progress.show("comparing photos and songs")
            finder = _lookalikes.Finder(tree, matcher, cache, args.jobs, progress, watch,
                                        args.hidden)
            finder.run()
            info["lookalikes"] = _lookalikes.collect(tree, finder)
            info["media_stats"] = finder.stats()
            info["totals"]["lookalikes"] = len(info["lookalikes"])
        junk = s.get("junk.names")

        def manifest():
            return data.encode(_manifest.build(tree, matcher, rel, junk, finder))

        if cache is not None and cache.active:
            cache.prune()
            cache.trim(s.get("cache.max_mb") * 1024 * 1024)
    except (KeyboardInterrupt, Cancelled):
        progress.clear()
        print("\nInterrupted — the scan did not finish, so there are no results.",
              file=sys.stderr)
        info["stopped"] = f"interrupted while {stage}; no results"
        info["errors"] = []
        _finish(run, info, "interrupted", 130)
        return 130
    except DriveGone as e:
        progress.clear()
        print(f"clean: error: {e}\nStopped — no results were saved (a scan with gaps "
              f"would miss duplicates).", file=sys.stderr)
        info["stopped"] = f"stopped: {e}"
        info["errors"] = []
        _finish(run, info, "drive-gone", 1, error=str(e))
        return 1
    finally:
        if cache is not None:
            cache.close()
            if cache.warning:
                progress.clear()
                print(f"clean: warning: {cache.warning}", file=sys.stderr)
    progress.clear()
    info.update(total_files=tree.nfiles_total, hard_links=matcher.hard_links,
                skipped=dict(tree.skipped), hashing={
                    "read": matcher.files_read, "bytes": matcher.bytes_read,
                    "cached": matcher.cache_hits,
                    "cache": ("off" if cache is None else
                              "failed" if cache.warning else "on"),
                    "stored": cache.stored if cache else 0,
                    "pruned": cache.pruned if cache else 0,
                    "trimmed": cache.trimmed if cache else 0})
    code = 1 if info["errors"] else 0
    warnings = _finish(run, info, "done", code, manifest=manifest)
    print(text.dupes_summary(info, run, terminal=True), end="")
    return 1 if code or warnings else 0


def _finish(run, info, status, code, error=None, manifest=None):
    """Save the run's results and final status; returns warnings (already
    printed) about anything that couldn't be saved. `manifest` makes
    manifest.json.gz, which `clean remove` acts on."""
    t = info["totals"] or {}
    totals = {"files": info["total_files"], "hard_links": info["hard_links"],
              "groups": t.get("groups", 0), "redundant": t.get("redundant", 0),
              "reclaimable": t.get("reclaimable", 0), "families": t.get("families", 0),
              "covered": t.get("covered", 0), "folders_freed": t.get("folders_freed", 0),
              "lookalikes": len(info["lookalikes"]), "errors": len(info["errors"])}
    run.conclude(status, code, text.dupes_headline(info), totals, error=error)
    files = []
    if status == "done":    # an unfinished scan has no results to list
        files += [("files.tsv", lambda: text.dupes_files_tsv(info["groups"])),
                  ("folders.tsv", lambda: text.dupes_folders_tsv(info)),
                  (data.NAME, lambda: data.encode(_data(info))),
                  (_manifest.NAME, manifest)]
    files += [("report.html", lambda: html.dupes_page(run, info)),
              ("summary.txt", lambda: text.dupes_summary(info, run))]
    return run.complete(files)


def _data(info):
    table = data.PathTable(info["roots"])
    folder = table.add
    families = [{
        "id": f["id"], "kind": f["kind"], "anchor": folder(f["anchor"]), "files": f["files"],
        "bytes": f["bytes"], "freed": f["freed"], "types": f["types"],
        "members": [{"folder": folder(m["path"]), "role": m["role"], "files": m["files"],
                     "bytes": m["bytes"], "freed": m["freed"], "shared": m["shared"],
                     "diff": m["diff"]} for m in f["members"]]}
        for f in info["families"]]
    covered = [{"id": c["id"], "folder": folder(c["path"]), "files": c["files"],
                "bytes": c["bytes"], "freed": c["freed"], "types": c["types"],
                "where": [[folder(p), n] for p, n in c["where"]],
                "where_total": c["where_total"]} for c in info["covered"]]
    groups = [{"id": g["id"], "size": g["size"], "hash": g["hash"], "category": g["category"],
               "folded": g["folded"], "files": [table.split(p) for p in g["files"]],
               "explained": g["explained"]} for g in info["groups"]]
    lookalikes = [{"id": g["id"], "kind": g["kind"], "label": g["label"],
                   "versions": g["versions"], "bytes": g["bytes"], "spare": g["spare"],
                   "files": [table.split(f["path"]) + [f["size"], f["mtime"], f["letter"],
                                                      f["best"], f["detail"]]
                             for f in g["files"]]}
                  for g in info["lookalikes"]]
    errors = [table.split(path) + [msg] for path, msg in info["errors"]]
    t = info["totals"] or {}
    return {"format": data.FORMAT, "kind": "dupes", "roots": info["roots"],
            "options": {"filters": info["filters"], "same_name": info["same_name"],
                        "same_date": info["same_date"], "similar_pct": info["similar"],
                        "hidden": info["hidden"], "max_depth": info["max_depth"],
                        "media": info["media"]},
            "total_files": info["total_files"], "hard_links": info["hard_links"],
            "skipped": info["skipped"], "hashing": info["hashing"],
            "totals": {k: v for k, v in t.items() if k != "categories"},
            "categories": t.get("categories", {}),
            "dirs": table.rows, "families": families, "covered": covered,
            "groups": groups, "lookalikes": lookalikes, "media": info["media_stats"],
            "errors": errors}
