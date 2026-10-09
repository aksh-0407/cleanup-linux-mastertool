"""
`clean empty`: list or delete truly empty folders (the engine is in
cleanup/empty.py). Every run is saved in the data folder (see cleanup/runs.py):
summary.txt, report.html, data.json.gz, and folders.tsv, which a --delete run
fills in as it goes, so even an interrupted run has an exact record.
"""

import argparse
import os
import sys

from cleanup import empty, options, paths, runs
from cleanup.errors import Cancelled, DriveGone
from cleanup.fsutil import Excluder, Progress, parallel_map, printable, resolve_roots, why
from cleanup.report import data, html, text
from cleanup.safety import DriveWatch, check_delete_allowed

TSV_HEADER = "status\tdepth\tpath\tnote\n"

# Why the default is 1 worker (setting empty.jobs): counter-intuitively,
# parallel traversal is SLOWER on a spinning USB drive. Benchmarked on a 5 TB
# exFAT USB hard drive (~9k dirs): a 406-dir subtree took 2.3s at -j1 but 12.6s at -j8,
# and a whole-drive scan blew past 9 minutes at -j8 vs find's 2m47s. The work
# is seek-bound, not CPU-bound: several walkers request inodes scattered across
# the platter, so the head thrashes between them. One walker lets the head move
# monotonically. More jobs help only on SSD/NVMe, where there is no seek.

EXAMPLES = """\
examples:
  clean empty ~/Downloads                      list what would go (nothing is deleted)
  clean empty ~/Downloads --delete             delete those folders
  clean empty "/media/$USER/Backup Drive" -x   stay on that drive
  clean empty /media/$USER/USB/{old-phone,downloads} --delete
  clean empty ~/Projects -e node_modules -e build --delete

A folder counts as empty only if it holds no files at all (a lone .DS_Store or
Thumbs.db keeps it), counting folders that are themselves empty. Folders you
name on the command line are never removed, nor are mount points, folders
matching --exclude, anything inside .git, .hg, .svn, .bzr, lost+found and
.stfolder (see --no-default-excludes), and Trash folders (.Trash-*, .Trash
and ~/.local/share/Trash, whose contents `clean undo` may still put back).

Every run is saved: `clean show` prints the last one again, `clean show --open`
opens its report page, `clean history` lists them all.

Exit status: 0 done; 1 some folders could not be read or removed, or the drive
went away mid-run; 2 bad arguments or refused; 130 interrupted.
"""


def add_parser(subparsers):
    p = subparsers.add_parser(
        "empty",
        help="find or delete empty folders (and folders holding only empty ones)",
        description="Find truly-empty folders (bottom-up, so a folder holding only\n"
                    "empty folders counts as empty) and list or delete them. Only ever\n"
                    "uses rmdir, which the OS refuses on any non-empty folder, so no\n"
                    "file can be lost.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="+", metavar="PATH",
                   help="folder(s) to clean up inside (they themselves are kept)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("-n", "--dry-run", action="store_true",
                      help="list the empty folders that WOULD be deleted; delete "
                           "nothing (this is the default)")
    mode.add_argument("--delete", action="store_true",
                      help="actually remove the empty folders")
    options.add_removed_options(p, "--log")
    p.add_argument("--force", action="store_true",
                   help="allow --delete in your whole home folder or in system "
                        "folders such as /usr, /etc, /var (dangerous)")
    options.add_walk_options(
        p, "empty.jobs",
        jobs_help="parallel folder walkers; >1 helps ONLY on SSD/NVMe, it is "
                  "slower on spinning USB drives due to head-seek thrash")
    p.set_defaults(func=run)
    return p


def run(args):
    delete = args.delete
    roots, given = resolve_roots(args.paths, refuse_symlinks=True)
    data_dir = paths.data_dir()
    if delete:
        for root in roots:
            check_delete_allowed(root, args.force)
        held = runs.lock("clean empty --delete " + " ".join(printable(r) for r in roots))
    try:
        run = runs.start("empty", "delete" if delete else "dry-run", roots)
        with runs.recording(run):
            return _run(args, run, roots, given, data_dir)
    finally:
        if delete:
            os.close(held)


def _run(args, run, roots, given, data_dir):
    delete = args.delete
    progress = Progress(args.quiet)
    excluded = Excluder(options.excludes(args), args.default_excludes,
                        skip=[os.path.realpath(data_dir)])
    walker = empty.Walker(excluded, args.one_file_system, protected=set(given),
                          progress=progress, watch=DriveWatch(roots))
    info = {"delete": delete, "roots": roots, "scanned": 0, "jobs": args.jobs,
            "quiet": args.quiet, "rows": [], "errors": [], "root_left_empty": [],
            "stage": "scan", "drive_gone": None,
            "result": {"removed": 0, "skipped": 0, "failed": 0, "gone": 0, "not_done": 0,
                       "fatal": None, "interrupted": False}}
    found, errors = [], info["errors"]
    try:
        for root in roots:
            dev = os.lstat(root).st_dev
            has_content, subdirs = walker.list_dir(root, dev, errors)
            info["scanned"] += 1
            shards = [(path, sdev, is_mount, 1) for path, sdev, is_mount in subdirs]
            for f, n, e in parallel_map(walker.walk, shards, args.jobs):
                found.extend(f)
                info["scanned"] += n
                errors.extend(e)
            done = {p for p, _ in found}
            if not has_content and all(p in done for p, _, _ in subdirs):
                info["root_left_empty"].append(root)
    except (KeyboardInterrupt, Cancelled):
        progress.clear()
        info["result"]["interrupted"] = True
        print("\nInterrupted while scanning — nothing was deleted.", file=sys.stderr)
        _finish(run, info, "interrupted", 130)
        return 130
    except DriveGone as e:
        progress.clear()
        info["drive_gone"] = str(e)
        print(f"clean: error: {e}\nStopped while scanning — nothing was deleted.",
              file=sys.stderr)
        _finish(run, info, "drive-gone", 1)
        return 1
    progress.clear()

    info["stage"] = "delete" if delete else "done"
    tsv = _Tsv(run.file("folders.tsv"), info["rows"])
    try:
        for path, msg in errors:
            tsv.record("error", "", path, msg)
        if delete:
            info["result"] = empty.delete(found, roots, tsv.record, progress, errors)
            info["root_left_empty"] = [r for r in roots if empty.is_empty(r)]
        else:
            found.sort()
            for path, depth in found:
                tsv.record("would-delete", depth, path)
            info["result"]["removed"] = len(found)
    finally:
        tsv.close()
    res = info["result"]
    if res["interrupted"]:
        status, code = "interrupted", 130
    elif res["fatal"]:
        status, code = "drive-gone", 1
    else:
        status, code = "done", 1 if errors else 0
    if status == "done":
        info["stage"] = "done"
    if tsv.error:
        run.unsaved.append("folders.tsv")
    warnings = _finish(run, info, status, code, [tsv.error])
    print(text.empty_summary(info, run, terminal=True), end="")
    return code if code or not warnings else 1


class _Tsv:
    """folders.tsv, written row by row as things happen. If it can't be
    written (the data folder's disk is full), the run goes on: the rows are
    also kept in memory for the summary and the report."""

    def __init__(self, path, rows):
        self.rows = rows
        self.error = None
        try:
            self.f = open(path, "w", encoding="utf-8", newline="")
            self.f.write(TSV_HEADER)
            self.f.flush()
        except OSError as e:
            self.f = None
            self.error = f"could not save folders.tsv: {why(e)}"

    def record(self, status, depth, path, note=""):
        self.rows.append((status, depth, path, note))
        if self.f is None:
            return
        try:
            self.f.write(f"{status}\t{depth}\t{printable(path)}\t{printable(note)}\n")
            self.f.flush()
        except OSError as e:
            self.error = f"could not save folders.tsv: {why(e)}"
            self.close()

    def close(self):
        f, self.f = self.f, None
        if f is not None:
            try:
                f.close()
            except OSError as e:
                self.error = self.error or f"could not save folders.tsv: {why(e)}"


def _finish(run, info, status, code, problems=()):
    """Save the run's results and final status; returns warnings (already
    printed) about anything that couldn't be saved, `problems` included."""
    res = info["result"]
    totals = {"scanned": info["scanned"], "removed": res["removed"], "kept": res["skipped"],
              "failed": res["failed"], "gone": res["gone"], "not_done": res["not_done"],
              "errors": len(info["errors"])}
    run.conclude(status, code, text.empty_headline(info), totals,
                 error=info["drive_gone"] or (why(res["fatal"][1]) if res["fatal"] else None))
    return run.complete([("report.html", lambda: html.empty_page(run, info)),
                         (data.NAME, lambda: data.encode(_data(info))),
                         ("summary.txt", lambda: text.empty_summary(info, run))], problems)


def _data(info):
    table = data.PathTable(info["roots"])
    folders = [[table.add(path), depth, status, note]
               for status, depth, path, note in info["rows"] if status != "error"]
    errors = [table.split(path) + [msg] for path, msg in info["errors"]]
    res = info["result"]
    return {"format": data.FORMAT, "kind": "empty", "delete": info["delete"],
            "roots": info["roots"], "scanned": info["scanned"], "stage": info["stage"],
            "result": {k: v for k, v in res.items() if k != "fatal"},
            "dirs": table.rows, "folders": folders, "errors": errors}
