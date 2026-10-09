"""
The empty-folder engine behind `clean empty`.

Walks folder trees deepest-first and finds *truly empty* folders: no file of
any kind, and every subfolder empty too (the cascade). Deleting uses only
os.rmdir(), which the OS refuses on a folder that contains anything, so even a
file created after the scan is never at risk.

A folder holding even a single file (OS junk like .DS_Store included) is left
alone, and so is every folder above it. Never deleted: the folders named on
the command line, mount points, and anything inside excluded folders or a
Trash (.Trash-*, .Trash, ~/.local/share/Trash: what is in there belongs
to the Files app, and to `clean undo`).
"""

import errno
import os

from cleanup.errors import Cancelled
from cleanup.fsutil import CANCEL, printable, why
from cleanup.safety import FATAL_ERRNOS, StopFlag
from cleanup.scan.filters import is_trash


class Walker:
    """The settings one scan needs, shared by all walker threads (read-only)."""

    def __init__(self, excluded, one_file_system, protected, progress, watch):
        self.excluded = excluded            # an fsutil.Excluder
        self.one_file_system = one_file_system
        self.protected = protected          # folders named on the command line
        self.progress = progress
        self.watch = watch                  # stops the scan if the drive goes away

    def list_dir(self, path, dev, errors):
        """Read `path` once. Returns (has_content, subdirs).

        has_content is True if `path` holds anything that will survive the
        cleanup no matter what: a file, a symlink (even one to a folder), an
        excluded folder, a mount point we don't cross, or entries we couldn't
        inspect. subdirs is [(path, st_dev, is_mount)] for folders to descend
        into. Only folders cost an extra lstat (for st_dev); on exFAT and ext4
        the dirent type answers is_dir() without one.
        """
        has_content = False
        subdirs = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            has_content = True  # file, symlink, fifo, socket, device
                            continue
                        if self.excluded(entry.name, entry.path) or is_trash(entry.name,
                                                                              entry.path):
                            has_content = True  # left alone; a Trash's own folders too
                            continue
                        child_dev = entry.stat(follow_symlinks=False).st_dev
                        if child_dev != dev and self.one_file_system:
                            has_content = True  # another drive; leave it alone
                            continue
                        subdirs.append((entry.path, child_dev, child_dev != dev))
                    except OSError as e:
                        has_content = True
                        errors.append((entry.path, f"cannot inspect: {why(e)}"))
                        self.watch.failed(e)
            self.watch.ok()
        except OSError as e:
            has_content = True
            errors.append((path, f"cannot read folder: {why(e)}"))
            self.watch.failed(e)
        return has_content, subdirs

    def walk(self, start):
        """Post-order walk of the tree at `start` = (path, dev, is_mount, depth).

        Returns (found, scanned, errors); found lists (path, depth) for every
        folder that is empty once its empty subfolders are gone. Uses an
        explicit stack, so any depth works. All state is local, so subtrees can
        run on several threads with no locking: a folder's emptiness depends
        only on what is inside it.
        """
        empty = set()
        found, errors = [], []
        scanned = 0
        progress = self.progress
        # frame: (path, dev, is_mount, depth, result-of-list_dir or None)
        stack = [(*start, None)]
        while stack:
            path, dev, is_mount, depth, listing = stack.pop()
            if listing is None:
                # First visit: read it, then revisit after all its children.
                if CANCEL.is_set():
                    raise Cancelled
                has_content, subdirs = self.list_dir(path, dev, errors)
                scanned += 1
                progress.count += 1
                if progress.due():
                    progress.show(f"scanning: {progress.count:,} folders  "
                                  f"{printable(path)}")
                stack.append((path, dev, is_mount, depth,
                              (has_content, [s[0] for s in subdirs])))
                for child, child_dev, child_is_mount in subdirs:
                    stack.append((child, child_dev, child_is_mount, depth + 1, None))
                continue
            # Second visit: every child has been decided.
            has_content, children = listing
            if (not has_content and not is_mount and path not in self.protected
                    and all(c in empty for c in children)):
                empty.add(path)
                found.append((path, depth))
        return found, scanned, errors


def delete(found, roots, record, progress, errors):
    """rmdir each found folder, children before parents, reporting each
    outcome to record(status, depth, path, note) as it happens:

        deleted     removed
        gone        already removed by something else
        failed      could not be removed (also added to `errors`)
        kept        not tried: something inside it could not be removed
        not-done    not tried: the run stopped first (Ctrl-C, or `fatal`)

    Reverse path order puts every folder after everything inside it (a parent
    path is a prefix of its children's, so it sorts first). If a folder can't
    be removed, its parent is then necessarily non-empty: it is skipped
    quietly instead of adding one more error per ancestor.
    """
    order = sorted(found, reverse=True)
    removed = skipped = failed = gone = 0
    blocked = set()     # folders known to still contain something
    fatal = None
    i = 0
    with StopFlag() as stop:
        for i, (path, depth) in enumerate(order):
            if stop.hit:
                break
            if progress.due():
                progress.show(f"deleting: {i + 1:,}/{len(order):,}  {printable(path)}")
            if path in blocked:
                skipped += 1
                blocked.add(os.path.dirname(path))
                record("kept", depth, path, "a folder inside could not be removed")
                continue
            try:
                os.rmdir(path)
            except OSError as e:
                if e.errno in FATAL_ERRNOS:
                    fatal = (path, e)
                    break
                if e.errno == errno.ENOENT:
                    if all(os.path.isdir(r) for r in roots):
                        gone += 1   # someone else already removed it
                        record("gone", depth, path, "already removed by something else")
                        continue
                    fatal = (path, e)  # the drive itself has gone away
                    break
                if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    msg = "no longer empty (something was added after the scan); left alone"
                elif e.errno == errno.EBUSY:
                    msg = "in use (a mount point, or held open by a program); left alone"
                else:
                    msg = f"could not remove: {why(e)}"
                errors.append((path, msg))
                failed += 1
                record("failed", depth, path, msg)
                blocked.add(os.path.dirname(path))
                continue
            removed += 1
            record("deleted", depth, path)  # at once: an interrupted run has an exact record
        else:
            i = len(order)
        rest = order[i:]
        for path, depth in rest:
            record("not-done", depth, path, "the run stopped before this folder")
    progress.clear()
    return {"removed": removed, "skipped": skipped, "failed": failed, "gone": gone,
            "not_done": len(rest), "fatal": fatal, "interrupted": stop.hit}


def is_empty(path):
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False
