"""
Carrying out a removal plan (cleanup/selection.py), one item at a time:
check it (actions/verify.py), then move it to the Trash, delete it, merge it
into the folder that stays, or replace its files with hard links.

Every step that changes something is written to actions.tsv, and synced to
disk, *before* it happens: whatever stops the run (Ctrl-C, a pulled drive, a
crash, a power cut), the log names everything that may have moved, and that
is what `clean undo` reads. A step the log names that never happened is
harmless; undo checks before acting.

    step  when  action  path  to  note
    trash   path -> where it is in the Trash
    delete  path (gone for good; note: how many files)
    move    path -> new path (merge, same drive; note: size:mtime_ns)
    copy    path -> new path (merge across drives; note: size:mtime_ns:hash:temp name)
    mkdir   a folder made for a merge
    link    path -> the kept file it is now a hard link to (note: dev:ino)
    replace path, replaced by a hard link for good (--permanent --link)
"""

import errno
import os
import stat
import time

from cleanup.actions import trash as _trash
from cleanup.actions.verify import (Bad, Gone, Stopped, Survivors, check_folder, read_file,
                                    same_file)
from cleanup.errors import DriveGone
from cleanup.fsutil import cell, rename_noreplace, uncell, why
from cleanup.report.text import plural
from cleanup.safety import FATAL_ERRNOS

LOG_HEADER = "step\twhen\taction\tpath\tto\tnote\n"
# Filesystems that can't hold hard links (--link is refused there).
NO_LINKS = frozenset(("vfat", "msdos", "exfat", "fat", "umsdos", "fuse.exfat"))
CONFLICTS = ("both", "skip", "newer")


class LogError(Exception):
    """actions.tsv can't be written: the run stops, since undo relies on it."""


class Log:
    """actions.tsv, one row per step, flushed and synced before the step."""

    def __init__(self, path):
        self.path = path
        try:
            self.f = open(path, "x", encoding="utf-8", newline="")
            self.f.write(LOG_HEADER)
            self._sync()
        except OSError as e:
            raise LogError(f"could not create {path}: {why(e)}")
        self.count = 0

    def row(self, step, action, path, to="", note=""):
        when = time.strftime("%Y-%m-%dT%H:%M:%S")
        line = "\t".join((str(step), when, action, cell(path), cell(to), cell(note)))
        try:
            self.f.write(line + "\n")
            self._sync()
        except OSError as e:
            raise LogError(f"could not write to {self.path}: {why(e)}")
        self.count += 1

    def _sync(self):
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


def read_log(path):
    """The rows of an actions.tsv: [(step, when, action, path, to, note)].
    A last line cut short by a crash is ignored."""
    rows = []
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        lines = f.read().split("\n")
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 6 or not parts[0].isdigit():
            continue
        step, when, action, path, to, note = parts
        rows.append((int(step), when, action, uncell(path), uncell(to), uncell(note)))
    return rows


class Done:
    """What happened to one item: status is removed, linked, merged, gone
    (already gone), skipped (it or its copy changed: nothing touched),
    failed, partial (some of it done), or not-done (the run stopped first)."""

    __slots__ = ("step", "item", "status", "note", "files", "size")

    def __init__(self, step, item, status, note="", files=0, size=0):
        self.step, self.item, self.status, self.note = step, item, status, note
        self.files, self.size = files, size


class Remover:
    """Runs a plan. `method` is "trash" or "permanent"; `link` replaces the
    copies with hard links instead (the copies themselves still go to the
    Trash, or are replaced for good with "permanent"); `conflicts` says what
    a merge does with a file whose name is taken in the kept folder by
    different content: keep "both" (the one from B as "name (from B).ext"),
    "skip" it (B then stays), or the "newer" wins (the other goes to the
    Trash)."""

    def __init__(self, m, plan, log, method="trash", link=False, conflicts="both",
                 stop=None, watch=None, vols=None, unavailable=(), progress=None):
        self.m, self.plan, self.log = m, plan, log
        self.method, self.link, self.conflicts = method, link, conflicts
        self.stop = stop or (lambda: False)
        self.watch = watch
        self.vols = vols
        self.unavailable = tuple(r.rstrip("/") + "/" for r in unavailable)
        self.progress = progress
        self.survivors = Survivors(m, plan, self.stop, unavailable)
        self.trashes = {}           # (dev, mount point) -> Trash or TrashError
        self.no_links = {}          # dev -> why --link can't work there
        self.freed = {}             # (dev, ino) -> [size, nlink, names gone]
        self.results = []
        self.stopped = False
        self.fatal = None           # what stopped the run: the drive went, or so

    # ------------------------------------------------------------ the run

    def run(self):
        items = self.plan.items
        for step, item in enumerate(items, 1):
            if self.stop():
                self.stopped = True
                break
            if self.progress is not None and self.progress.due():
                self.progress.show(f"removing: {step - 1} of {len(items)} done")
            try:
                done = self.one(step, item)
                self.results.append(done)
                if (self.watch is not None and item.kind != "file"
                        and done.status in ("removed", "merged", "partial")):
                    self.watch.forget(item.path)
            except DriveGone as e:
                self.fatal = str(e)
                self.results.append(Done(step, item, "failed", str(e)))
            if self.stopped or self.fatal:
                break
        for step in range(len(self.results) + 1, len(items) + 1):
            self.results.append(Done(step, items[step - 1], "not-done"))
        if self.progress is not None:
            self.progress.clear()
        return self.results

    def one(self, step, item):
        if self.unavailable and (item.path + "/").startswith(self.unavailable):
            return Done(step, item, "skipped", "its drive is not connected")
        try:
            if item.kind == "file":
                return self.do_file(step, item)
            if item.kind == "folder":
                return self.do_folder(step, item)
            return self.do_merge(step, item)
        except Gone:
            self.gone_check(item.path)
            return Done(step, item, "gone", "already gone")
        except Bad as e:
            return Done(step, item, "skipped", str(e))
        except Stopped:
            self.stopped = True
            return Done(step, item, "not-done", "stopped before it was touched")
        except _trash.TrashError as e:
            return Done(step, item, "failed", f"cannot move it to the Trash: {e}")
        except OSError as e:
            self.drive_check(e, item.path)
            return Done(step, item, "failed", why(e))

    def drive_check(self, err, path):
        """After an OSError: raises DriveGone if the drive itself went; an
        error that will hit every other item too stops the run."""
        if self.watch is not None:
            self.watch.failed(err)
        if err.errno in FATAL_ERRNOS:
            self.fatal = f"{path}: {why(err)}"

    def gone_check(self, path):
        """`path` isn't there: raises DriveGone if that is because its drive
        went (unmounted: the path now leads into the empty mount point), not
        because it was removed already."""
        if self.watch is not None:
            self.watch.failed(FileNotFoundError(errno.ENOENT, "No such file or directory",
                                                path))

    def gone_file(self, st):
        e = self.freed.setdefault((st.st_dev, st.st_ino), [st.st_size, st.st_nlink, 0])
        e[2] += 1

    @property
    def freed_bytes(self):
        """Space freed (once the Trash is emptied): each file on disk once,
        and only when every name of it went."""
        return sum(size for size, nlink, names in self.freed.values() if names >= nlink)

    # ------------------------------------------------------------ items

    def do_file(self, step, item):
        f = item.target
        _, _, size, mtime, c, _ = self.m.files[f]
        try:
            now = os.lstat(item.path)
        except FileNotFoundError:
            raise Gone
        keep = self.survivors.linked_to(c, item.path, now)
        if keep is not None:
            # A hard link to a verified copy that stays (an earlier --link
            # run made it, say): that copy vouches for its content.
            if self.link:
                return Done(step, item, "gone", f"already a hard link to {keep[1]}")
            self.discard(step, item.path, now)
            self.gone_file(now)
            return Done(step, item, "removed", f"{item.why}; it was a hard link to "
                        f"{keep[1]}, which stays, so no space is freed", 1, now.st_size)
        st, _ = read_file(item.path, size, mtime, self.m.hashes[c], self.stop)
        try:
            keep = self.survivors.find(c, {(st.st_dev, st.st_ino)},
                                       st.st_dev if self.link else None)
        except Bad:
            if f not in self.plan.versions:
                raise
            # The last copy, chosen to go because another version of the
            # photo or song stays: that version must check out instead.
            if self.link:
                raise Bad("its look-alike is not the same file, so it can't be replaced "
                          "with a hard link to it")
            keep = self.version_left(f, st)
            self.discard(step, item.path, st)
            self.gone_file(st)
            note = item.why if keep[1] in item.why else f"{item.why}; {keep[1]} stays"
            return Done(step, item, "removed", note, 1, size)
        if self.link:
            self.link_one(step, item.path, st, keep)
            self.gone_file(st)
            return Done(step, item, "linked", f"now a hard link to {keep[1]}", 1, size)
        self.discard(step, item.path, st)
        self.gone_file(st)
        return Done(step, item, "removed", item.why, 1, size)

    def version_left(self, f, st):
        """(file, path, stat) of a verified file of another version of file
        f's photo or song. Raises Bad when there is none."""
        last = None
        for g in self.m.versions(f):
            try:
                return self.survivors.find(self.m.files[g][4], {(st.st_dev, st.st_ino)})
            except Bad as e:
                last = e
        raise Bad("no verified version of it remains" + (f" ({last})" if last else ""))

    def do_folder(self, step, item):
        linked = None
        if self.link:
            def linked(f, p, st):
                c = self.m.files[f][4]
                return self.survivors.linked_to(c, p, st) if c else None
        fold = check_folder(self.m, item.target, item.path, self.stop, linked)
        avoid = {(st.st_dev, st.st_ino) for _, _, st, _ in fold.present}
        same_dev = fold.top.st_dev if self.link else None
        keeps = {}
        for f, _, _, _ in fold.present:
            c = self.m.files[f][4]
            if c and c not in keeps:
                keeps[c] = self.survivors.find(c, avoid, same_dev)
        note = item.why
        if fold.gone:
            note += f" ({len(fold.gone)} of its files were already gone)"
        if self.link:
            return self.link_folder(step, item, fold, keeps)
        if self.method == "trash":
            self.trash(step, item.path, fold.top.st_dev)
        else:
            left, errors = self.delete_folder(fold)
            self.log.row(step, "delete", item.path,
                         note=f"{len(fold.present) + len(fold.junk)} files")
            if left or errors:
                return self.partial_delete(step, item, fold, left, errors)
        for _, _, st, _ in fold.present:
            self.gone_file(st)
        return Done(step, item, "removed", note, len(fold.present),
                    sum(st.st_size for _, _, st, _ in fold.present))

    # ------------------------------------------------------------ trash / delete

    def trash_for(self, dev, path):
        point = None
        if self.vols is not None:
            mount = self.vols.find_mount(os.path.realpath(os.path.dirname(path) or "/"))
            point = mount.point if mount is not None else None
        key = (dev, point)
        t = self.trashes.get(key)
        if t is None:
            try:
                t = _trash.for_path(path, self.vols)
            except _trash.TrashError as e:
                t = e
            self.trashes[key] = t
        if isinstance(t, Exception):
            raise t
        return t

    def trash(self, step, path, dev):
        t = self.trash_for(dev, path)
        return t.put(path, before_move=lambda e: self.log.row(step, "trash", path, e.file))

    def discard(self, step, path, st):
        """The verified file `st` at `path`: to the Trash, or deleted."""
        if self.method == "trash":
            self.trash(step, path, st.st_dev)
            return
        now = os.lstat(path)
        if not (same_file(now, st) and now.st_size == st.st_size
                and now.st_mtime_ns == st.st_mtime_ns):
            raise Bad("it changed while being checked")
        os.unlink(path)
        self.log.row(step, "delete", path)

    def delete_folder(self, fold):
        """Delete the verified files of a checked folder, its junk, then its
        folders bottom-up. A folder where something new appeared meanwhile
        stays (rmdir refuses). Returns (folders left, errors)."""
        errors = []
        for _, p, st, _ in fold.present:
            try:
                now = os.lstat(p)
                if not (same_file(now, st) and now.st_mtime_ns == st.st_mtime_ns):
                    errors.append((p, "it changed while being checked"))
                    continue
                os.unlink(p)
            except FileNotFoundError:
                pass
            except OSError as e:
                self.drive_check(e, p)
                errors.append((p, why(e)))
                if self.fatal:
                    return [], errors
        for p in fold.junk:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
            except OSError as e:
                errors.append((p, why(e)))
        left = []
        for d in fold.dirs:
            try:
                os.rmdir(d)
            except FileNotFoundError:
                pass
            except OSError as e:
                if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    self.drive_check(e, d)
                    errors.append((d, why(e)))
                left.append(d)
        return left, errors

    def partial_delete(self, step, item, fold, left, errors):
        gone = 0
        for _, p, st, _ in fold.present:
            if not os.path.lexists(p):
                self.gone_file(st)
                gone += 1
        if errors:
            p, msg = errors[0]
            note = f"deleted {gone} of {len(fold.present)} files; {p}: {msg}"
        else:
            note = f"deleted its files, but {left[0]} stays: something new appeared in it"
        return Done(step, item, "partial", note, gone)

    # ------------------------------------------------------------ hard links

    def fstype(self, path):
        if self.vols is None:
            return None
        mount = self.vols.find_mount(os.path.realpath(os.path.dirname(path)))
        return mount.fstype if mount is not None else None

    def link_one(self, step, path, st, keep):
        """Replace the verified file at `path` with a hard link to `keep`."""
        _, kpath, kst = keep
        reason = self.no_links.get(st.st_dev)
        fstype = self.fstype(path)
        if reason is None and fstype in NO_LINKS:
            reason = self.no_links[st.st_dev] = f"this drive ({fstype}) can't hold hard links"
        if reason:
            raise Bad(reason)
        ident = f"{kst.st_dev}:{kst.st_ino}"
        folder = os.path.dirname(path)
        for n in range(1, 1000):
            tmp = os.path.join(folder, f".clean-link-{os.getpid()}-{step}-{n}")
            self.log.row(step, "link", tmp, kpath, ident)
            try:
                os.link(kpath, tmp, follow_symlinks=False)
                break
            except FileExistsError:
                continue
            except OSError as e:
                if e.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
                    reason = self.no_links[st.st_dev] = (
                        f"this drive ({fstype or 'its filesystem'}) can't hold hard links")
                    raise Bad(reason)
                if e.errno == errno.EMLINK:
                    raise Bad(f"{kpath} already has as many hard links as its drive allows")
                raise
        else:
            raise Bad("no free temporary name for the link")
        try:
            if not same_file(os.lstat(tmp), kst):
                raise Bad(f"{kpath} changed while being linked")
            if self.method == "trash":
                entry = self.trash(step, path, st.st_dev)
                self.log.row(step, "link", path, kpath, ident)
                try:
                    rename_noreplace(tmp, path)
                except BaseException:
                    entry.restore()         # put the original back where it was
                    raise
            else:
                now = os.lstat(path)
                if not (same_file(now, st) and now.st_mtime_ns == st.st_mtime_ns):
                    raise Bad("it changed while being checked")
                os.rename(tmp, path)        # replaces the copy in one step
                self.log.row(step, "replace", path, kpath, ident)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def link_folder(self, step, item, fold, keeps):
        todo = [(f, p, st) for f, p, st, _ in fold.present if self.m.files[f][4]]
        if not todo:
            return Done(step, item, "gone", "its files are already hard links to the "
                        "copies that stay" if fold.linked else "its files are already gone")
        done = size = 0
        for f, p, st in todo:
            try:
                if self.stop():
                    raise Stopped
                self.link_one(step, p, st, keeps[self.m.files[f][4]])
            except (Bad, OSError, Stopped, _trash.TrashError) as e:
                if isinstance(e, OSError):
                    self.drive_check(e, p)
                if isinstance(e, Stopped):
                    self.stopped = True
                if not done and not isinstance(e, Stopped):
                    raise
                what = "stopped" if isinstance(e, Stopped) else f"{p}: {why(e) if isinstance(e, OSError) else e}"
                return Done(step, item, "partial" if done else "not-done",
                            f"linked {done} of {len(todo)} files, then {what}", done, size)
            self.gone_file(st)
            done += 1
            size += st.st_size
        before = f" ({len(fold.linked)} already were)" if fold.linked else ""
        what = (f"its {done} files are now hard links to the copies that stay" if done > 1
                else "its file is now a hard link to the copy that stays")
        return Done(step, item, "linked", what + before, done, size)

    # ------------------------------------------------------------ merge

    def do_merge(self, step, item):
        """Merge folder B (item.path) into A (item.into): B's files whose
        content A lacks move to the same place in A; then B, fully covered,
        goes. Conflicts per self.conflicts."""
        m, B = self.m, item.path
        A = m.paths[item.into]
        fold = check_folder(m, item.target, B, self.stop)
        try:
            ast = os.lstat(A)
        except FileNotFoundError:
            raise Bad(f"{A}, which it would be merged into, is gone")
        if not stat.S_ISDIR(ast.st_mode):
            raise Bad(f"{A}, which it would be merged into, is no longer a folder")
        in_a = {m.files[f][4] for f in m.files_in(item.into)}
        avoid = {(st.st_dev, st.st_ino) for _, _, st, _ in fold.present}
        move, extra = [], []
        for entry in fold.present:
            c = m.files[entry[0]][4]
            if c and c in in_a:
                self.survivors.find(c, avoid)
                extra.append(entry)
            else:
                move.append(entry)
        same = ast.st_dev == fold.top.st_dev
        bname = os.path.basename(B.rstrip("/"))
        a_files = None
        moved, left, older, renamed, replaced, only = [], [], [], 0, 0, 0
        for f, p, st, digest in move:
            if self.stop():
                self.stopped = True
                break
            rel = p[len(B):].lstrip("/")
            try:
                if not self.make_parents(step, A, os.path.dirname(rel)):
                    left.append((rel, "a file in the kept folder is in the way"))
                    continue
                dest = os.path.join(A, rel)
                old = None
                if os.path.lexists(dest):
                    dst = os.lstat(dest)
                    if not stat.S_ISREG(dst.st_mode):
                        left.append((rel, f"{dest} is a folder or something else, not a file"))
                        continue
                    if not m.files[f][4]:
                        older.append(p)         # an empty file: nothing to keep
                        continue
                    if self.conflicts == "skip":
                        left.append((rel, f"{dest} is a different file"))
                        continue
                    if self.conflicts == "newer":
                        # The older one goes to the Trash, but only when a
                        # copy of it stays: else both are kept, as with "both".
                        b_older = dst.st_mtime_ns >= st.st_mtime_ns
                        if b_older and self.has_copy(m.files[f][4], avoid):
                            older.append(p)     # B's goes with B, to the Trash
                            continue
                        if not b_older:
                            if a_files is None:
                                a_files = {m.file_path(k): k for k in m.files_in(item.into)}
                            if self.make_way(dest, dst, a_files.get(dest)):
                                old = self.trash(step, dest, dst.st_dev)
                        if old is None:
                            dest = self.free_name(dest, bname)
                            renamed += 1
                            only += 1
                    else:
                        dest = self.free_name(dest, bname)
                        renamed += 1
                try:
                    self.move_in(step, p, st, digest, dest, same, bname)
                except BaseException:
                    if old is not None:
                        _put_back(old)          # A keeps its file after all
                    raise
                replaced += old is not None
                moved.append((p, st))
            except Stopped:
                self.stopped = True
                break
            except (Bad, _trash.TrashError) as e:
                left.append((rel, str(e)))
            except OSError as e:
                self.drive_check(e, p)
                left.append((rel, why(e)))
                if self.fatal:
                    break
        what = f"moved {plural(len(moved), 'file')} into {A}"
        if renamed:
            what += f" ({renamed} renamed \"(from {bname})\""
            if only:
                what += (f"; {only} of them since the older version has no other copy"
                         if renamed > 1 else ", since the older version has no other copy")
            what += ")"
        if replaced:
            what += (f" ({replaced} replacing older files, which went to the Trash)" if replaced > 1
                     else " (1 replacing an older file, which went to the Trash)")
        if self.stopped or self.fatal or left:
            if self.stopped or self.fatal:
                why_left = "the run stopped"
            else:
                why_left = (f"it still holds {plural(len(left), 'file')} only there "
                            f"({left[0][0]}: {left[0][1]})")
            return Done(step, item, "partial" if moved else "skipped",
                        f"{what}; {B} stays: {why_left}", len(moved))
        if older:
            what += (f"; {len(older)} older or empty files went with it" if len(older) > 1
                     else "; 1 older or empty file went with it")
        # B is now covered: what's left in it is in A (or elsewhere) already.
        if self.method == "trash":
            self.trash(step, B, fold.top.st_dev)
        else:
            gone_now = {p for p, _ in moved} if same else set()
            fold.present = [e for e in fold.present if e[1] not in gone_now]
            left_dirs, errors = self.delete_folder(fold)
            self.log.row(step, "delete", B, note=f"{len(fold.present)} files")
            if left_dirs or errors:
                return self.partial_delete(step, item, fold, left_dirs, errors)
        for _, _, st, _ in extra:
            self.gone_file(st)
        return Done(step, item, "merged", what, len(extra),
                    sum(st.st_size for _, _, st, _ in extra))

    def has_copy(self, c, avoid):
        """Whether a verified copy of content `c` stays, none of `avoid`."""
        if not c:
            return True                 # the empty file: nothing to keep
        if not self.m.hashes[c]:
            return False                # the scan found no other copy
        try:
            self.survivors.find(c, avoid)
        except Bad:
            return False
        return True

    def make_way(self, dest, dst, f):
        """Before the older file `dest` (stat `dst`, manifest file `f`) in the
        kept folder makes way for a newer one from B: it must be what the
        scan saw (else Bad), and a copy of it must stay elsewhere: False if
        none does. Once replaced it is never relied on again in this run."""
        m = self.m
        if f is None:
            raise Bad(f"{dest} is new since the scan, so it is not replaced")
        size, mtime, c = m.files[f][2], m.files[f][3], m.files[f][4]
        try:
            now, _ = read_file(dest, size, mtime, m.hashes[c] if c else None, self.stop)
        except Gone:
            raise Bad(f"{dest} changed while being checked")
        except Bad as e:
            raise Bad(f"{dest}: {e}; it is not replaced")
        if not same_file(now, dst):
            raise Bad(f"{dest} changed while being checked")
        if not self.has_copy(c, {(dst.st_dev, dst.st_ino)}):
            return False
        self.survivors.failed[f] = f"{dest} was replaced by a newer file in a merge"
        if c in self.survivors.good:
            self.survivors.good[c] = [g for g in self.survivors.good[c]
                                      if (g[2].st_dev, g[2].st_ino) != (dst.st_dev, dst.st_ino)]
        return True

    def make_parents(self, step, top, rel):
        """Make the folders of `rel` under `top` that don't exist yet, logging
        each. False if something that isn't a folder is in the way."""
        path = top
        for part in [p for p in rel.split("/") if p]:
            path = os.path.join(path, part)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                self.log.row(step, "mkdir", path)
                os.mkdir(path)
                continue
            if not stat.S_ISDIR(st.st_mode):
                return False
        return True

    @staticmethod
    def free_name(dest, bname):
        """"name (from B).ext", or "name (from B 2).ext"..., that isn't taken."""
        folder, name = os.path.split(dest)
        stem, ext = os.path.splitext(name)
        if not stem:
            stem, ext = ext, ""
        try:
            limit = os.pathconf(folder, "PC_NAME_MAX")
        except OSError:
            limit = 255
        for n in range(1, 10000):
            tag = f" (from {bname}{'' if n == 1 else f' {n}'})"
            room = limit - len(os.fsencode(tag + ext))
            s = os.fsencode(stem)
            if len(s) > room:
                s = s[:max(room, 1)]
                while s and (s[-1] & 0xC0) == 0x80:     # don't cut a character
                    s = s[:-1]
            candidate = os.path.join(folder, os.fsdecode(s) + tag + ext)
            if not os.path.lexists(candidate):
                return candidate
        raise Bad(f"no free name for {name} in {folder}")

    def move_in(self, step, src, st, digest, dest, same, bname):
        if same:
            for _ in range(5):
                self.log.row(step, "move", src, dest, f"{st.st_size}:{st.st_mtime_ns}")
                try:
                    rename_noreplace(src, dest)
                    return
                except FileExistsError:     # taken meanwhile: keep both
                    dest = self.free_name(dest, bname)
            raise Bad(f"{dest} keeps being taken")
        folder = os.path.dirname(dest)
        tmp = os.path.join(folder, f".clean-copy-{os.getpid()}-{step}")
        self.log.row(step, "copy", src, dest,
                     f"{st.st_size}:{st.st_mtime_ns}:{digest}:{os.path.basename(tmp)}")
        try:
            copy_verified(src, tmp, st, digest, self.stop)
            for _ in range(5):
                try:
                    rename_noreplace(tmp, dest)
                    break
                except FileExistsError:
                    dest = self.free_name(dest, bname)
                    self.log.row(step, "copy", src, dest,
                                 f"{st.st_size}:{st.st_mtime_ns}:{digest}:")
            else:
                raise Bad(f"{dest} keeps being taken")
            _fsync_dir(folder)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def copy_verified(src, dst, st, digest, stop=None):
    """Copy src (checked: still `st`) to the new file dst, synced to disk
    with src's permissions and modification time, then read dst back: its
    content must hash to `digest`. Raises Bad otherwise (dst removed)."""
    fin = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        now = os.fstat(fin)
        if not (same_file(now, st) and now.st_size == st.st_size
                and now.st_mtime_ns == st.st_mtime_ns):
            raise Bad("it changed while being checked")
        fout = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            while True:
                if stop is not None and stop():
                    raise Stopped
                chunk = os.read(fin, 1 << 20)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fout, view):]
            os.fsync(fout)
        finally:
            os.close(fout)
        after = os.fstat(fin)
        if after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
            raise Bad("it changed while being copied")
    except BaseException:
        try:
            os.unlink(dst)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fin)
    try:
        os.chmod(dst, stat.S_IMODE(st.st_mode))
    except OSError:
        pass                # some drives (exFAT) have no permissions to set
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
    try:
        back, got = read_file(dst, st.st_size, os.lstat(dst).st_mtime_ns, None, stop)
    except (Gone, Bad) as e:
        _unlink(dst)
        raise Bad(f"the copy could not be read back: {e}")
    if got != digest:
        _unlink(dst)
        raise Bad("the copy did not read back the same; nothing was moved")


def _put_back(entry):
    """Bring a Trash entry back where it came from; if that fails, it stays
    in the Trash (the log names it, so `clean undo` can still)."""
    try:
        entry.restore()
    except OSError:
        pass


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
