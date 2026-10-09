"""
Undoing a removal: reading its actions.tsv backwards and putting each thing
back, checking first that it is still exactly what the removal left.

    trash    the item comes back from the Trash, if its .trashinfo still says
             it came from that path and was put there during that run
    move     a file a merge moved comes back, if it is still the same size
             and date
    copy     a copy a merge made across drives is deleted, but only once the
             original is back and both still hash the same
    mkdir    a folder a merge made is removed, if it is empty
    link     a hard link makes way for the original it replaced, which comes
             back from the Trash; if that fails, the link is put back

Nothing is ever overwritten: when something new is at a path, the item stays
in the Trash and that is reported. Undo can run again: what is already back
is said so.

When a name was taken and the removal tried another, the log has a row for
each try; only the newest row for a path can have happened, so the older
ones are passed over (they may name someone else's file).
"""

import errno
import os
import stat
import time

from cleanup.actions import trash as _trash
from cleanup.actions.remove import read_log
from cleanup.actions.verify import Bad, Gone, Stopped, read_file
from cleanup.errors import DriveGone
from cleanup.fsutil import is_within, rename_noreplace, why
from cleanup.safety import FATAL_ERRNOS

WINDOW = 120        # seconds of slack around the run for a .trashinfo's date
STATUSES = ("restored", "already", "conflict", "missing", "skipped", "failed", "offline",
            "not-done")
TROUBLE = ("failed", "conflict", "missing", "skipped", "offline", "not-done")   # worst first
QUIET = "quiet"     # a step with nothing to say (a temporary name, a folder made)


class Outcome:
    """What became of one step of the removal. status: restored, already
    (already back), conflict (something new is in the way: nothing was
    overwritten), missing (not in the Trash any more), skipped (changed
    since: left as it is), failed, offline (its drive isn't connected), or
    not-done (the undo stopped first). partly: a step with trouble that
    still put some of it back (a merge whose other files came back)."""

    __slots__ = ("step", "path", "status", "note", "partly")

    def __init__(self, step, path, status, note="", partly=False):
        self.step, self.path, self.status, self.note = step, path, status, note
        self.partly = partly


class Undoer:
    """Undoes the removal `run` (a runs.Run with actions.tsv). `moves`
    {old: new} maps paths of drives now mounted somewhere else; `log` (an
    actions.Log) records what the undo itself does. Nothing is done on the
    drives under `unavailable` (paths as logged): they aren't connected."""

    def __init__(self, run, moves=None, stop=None, watch=None, dry_run=False, log=None,
                 unavailable=()):
        self.run = run
        self.unavailable = list(unavailable)
        self.moves = sorted((moves or {}).items(), key=lambda kv: -len(kv[0]))
        self.stop = stop or (lambda: False)
        self.watch = watch
        self.dry_run = dry_run
        self.log = log
        start, end = run.meta.get("started_ts"), run.meta.get("finished_ts")
        self.window = ((start - WINDOW) if _number(start) else None,
                       (end + WINDOW) if _number(end) else time.time() + WINDOW)
        self.would = []         # dry run: (path, what is there by now, or None: gone)
        self.step = 0
        self.results = []
        self.stopped = False
        self.fatal = None

    def here(self, path):
        """`path` as logged, where it is now (its drive may be mounted
        somewhere else)."""
        if not path:
            return path
        for old, new in self.moves:
            if is_within(path, old):
                return new.rstrip("/") + path[len(old.rstrip("/")):] if old != "/" else path
        return path

    # ------------------------------------------------------------ the run

    def steps(self):
        """The removal's steps, newest first: [(step, rows newest first)]."""
        steps = {}
        for row in read_log(self.run.file("actions.tsv")):
            steps.setdefault(row[0], []).append(row)
        return [(s, list(reversed(rows))) for s, rows in sorted(steps.items(), reverse=True)]

    def go(self):
        todo = self.steps()
        for i, (step, rows) in enumerate(todo):
            if self.stop():
                self.stopped = True
            if self.stopped or self.fatal:
                self.results += [Outcome(s, self.main(r), "not-done") for s, r in todo[i:]]
                break
            try:
                if self.watch is not None:
                    self.watch.check()      # nothing is done on a drive that went
                self.results.append(self.one(step, rows))
            except DriveGone as e:
                self.fatal = str(e)
                self.results.append(Outcome(step, self.main(rows), "failed", str(e)))
        return self.results

    def main(self, rows):
        """The path a step is about: what it removed (for a merge, the folder
        merged away, which went last)."""
        for row in rows:                            # newest first
            if row[2] in ("trash", "delete", "replace") and not _is_tmp(row[3]):
                return self.here(row[3])
        return self.here(rows[-1][3])

    def one(self, step, rows):
        self.step = step
        off = [u for u in self.unavailable
               if any(is_within(r[3], u) or (r[4] and is_within(r[4], u)) for r in rows)]
        if off:
            return Outcome(step, self.main(rows), "offline",
                           f"its drive is not connected (it was at {off[0]})")
        out = []
        latest = set()              # (kind, path) already handled by a newer row
        for n, (_, _, action, path, to, note) in enumerate(rows):
            path, to = self.here(path), self.here(to)
            key = ("put" if action in ("trash", "move", "copy") else action, path)
            superseded = key in latest
            latest.add(key)
            try:
                if action == "copy":
                    out.append(self.uncopy(path, to, note, superseded))
                elif superseded:
                    continue
                elif action == "trash":
                    if any(r[2] == "link" and self.here(r[3]) == path for r in rows[:n]):
                        continue        # undone with its link row, newer than it
                    out.append(self.untrash(path, to))
                elif action == "link":
                    out.append(self.unlink(path, to, note, rows[n + 1:]))
                elif action == "move":
                    out.append(self.unmove(path, to, note))
                elif action == "mkdir":
                    out.append(self.unmkdir(path))
                elif action in ("delete", "replace"):
                    out.append(("missing", "deleted for good: it can't come back"))
            except Stopped:
                self.stopped = True
                out.append(("not-done", "stopped"))
                break
            except Bad as e:
                out.append(("skipped", str(e)))
            except OSError as e:
                out.append(("failed", f"{path}: {why(e)}"))
                if self.watch is not None:
                    self.watch.failed(e)    # raises DriveGone if the drive went
                if e.errno in FATAL_ERRNOS:
                    self.fatal = f"{path}: {why(e)}"
                    break
        if self.watch is not None and any(o[0] == "missing" for o in out):
            # not in the Trash, nor back: maybe its drive went (unmounted)
            self.watch.failed(FileNotFoundError(errno.ENOENT, "No such file or directory",
                                                self.main(rows)))
        return _sum_up(step, self.main(rows), out)

    def record(self, action, path, to=""):
        if self.log is not None:
            self.log.row(self.step, action, path, to)

    def there(self, path):
        """Is something at `path`? In a dry run: would it be by now, after
        what the undo pretended to do so far (the newest pretend move that
        covers the path decides)."""
        for p, now in reversed(self.would):
            if now is None:
                if p == path:
                    return False
            elif is_within(path, p):
                return os.path.lexists(now + path[len(p):])
        return os.path.lexists(path)

    # ------------------------------------------------------------ the Trash

    def entry(self, to):
        """(Trash, name, original path, deletion date) of the item `to`, a
        path in some Trash's files/; original and date are None when its
        .trashinfo is missing or unreadable."""
        folder = os.path.dirname(to)
        tdir = os.path.dirname(folder)
        if os.path.basename(folder) != "files":
            raise Bad(f"{to} is not in a Trash")
        if os.path.basename(tdir).startswith(".Trash-"):
            top = os.path.dirname(tdir)
        elif os.path.basename(os.path.dirname(tdir)) == ".Trash":
            top = os.path.dirname(os.path.dirname(tdir))
        else:
            top = None                              # the home Trash: full paths
        t = _trash.Trash(tdir, top)
        name = os.path.basename(to)
        try:
            with open(os.path.join(t.info, name + _trash.INFO_SUFFIX), "rb") as f:
                raw = f.read(65536)
        except FileNotFoundError:
            return t, name, None, None
        original, date = _trash.parse_info(raw)
        if original is not None and not original.startswith("/"):
            original = os.path.normpath(os.path.join(top, original)) if top else None
        return t, name, original, date

    def untrash(self, path, to, check_only=False, in_the_way_ok=False):
        t, name, original, date = self.entry(to)
        item = os.path.join(t.files, name)
        if not os.path.lexists(item):
            if self.there(path):
                return ("already", "already back")
            return ("missing", "not in the Trash any more (it was emptied?)")
        if original is None:
            return ("skipped", f"its record in the Trash ({name}{_trash.INFO_SUFFIX}) is "
                               f"missing or unreadable: it stays in the Trash")
        if os.path.normpath(original) != os.path.normpath(path):
            return ("skipped", f"the item called {name} in the Trash came from {original}, "
                               f"not from here: it stays in the Trash")
        entry = _trash.Entry(t, name, original, date)
        ts, (lo, hi) = entry.deleted_ts, self.window
        if ts is None or (lo is not None and ts < lo) or ts > hi:
            return ("skipped", f"the item called {name} in the Trash was put there "
                               f"{'at ' + date if date else 'at an unknown time'}, not by "
                               f"this removal: it stays there")
        if self.there(path) and not in_the_way_ok:
            return _in_the_way(path, item)
        if check_only:
            return ("ok", "")
        if self.dry_run:
            self.would.append((path, item))
            return ("restored", "")
        self.record("restore", path, item)
        try:
            entry.restore()
        except FileExistsError:
            return _in_the_way(path, item)
        return ("restored", "")

    # ------------------------------------------------------------ hard links

    def unlink(self, path, kept, ident, older):
        """`path` was made a hard link to `kept` (dev:ino `ident`); undo it.
        A temporary name (.clean-link-*) left by a stopped run just goes."""
        if _is_tmp(path):
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                return (QUIET, "")
            if _ident(st) == ident and not self.dry_run:
                self.record("unlink", path)
                os.unlink(path)
            return (QUIET, "")
        trashed = next((self.here(r[4]) for r in older
                        if r[2] == "trash" and self.here(r[3]) == path), None)
        if trashed is None:
            return (QUIET, "")          # stopped before the original moved: nothing to undo
        t, name, _, _ = self.entry(trashed)
        in_trash = os.path.lexists(os.path.join(t.files, name))
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return self.untrash(path, trashed)      # the link never got there
        if _ident(st) != ident or not stat.S_ISREG(st.st_mode):
            if not in_trash:
                return ("already", "already back")
            return _in_the_way(path, os.path.join(t.files, name))
        if not in_trash:
            return ("missing", "the original is not in the Trash any more (it was "
                               "emptied?); the hard link stays, so the file is still there")
        ready = self.untrash(path, trashed, check_only=True, in_the_way_ok=True)
        if ready[0] != "ok":
            return (ready[0], ready[1] + "; the hard link stays, so the file is still there")
        if self.dry_run:
            return ("restored", "the hard link would make way for the original")
        self.record("unlink", path)
        os.unlink(path)
        got = self.untrash(path, trashed)
        if got[0] != "restored" and not os.path.lexists(path):
            try:                        # don't leave nothing: the link goes back
                os.link(kept, path, follow_symlinks=False)
            except OSError:
                pass
        return got

    # ------------------------------------------------------------ merges

    def unmove(self, src, dest, note):
        size, _, mtime = note.partition(":")
        try:
            st = os.lstat(dest)
        except FileNotFoundError:
            if self.there(src):
                return ("already", "already back")
            return ("missing", f"{dest}, where it was moved, is gone")
        if not stat.S_ISREG(st.st_mode) or str(st.st_size) != size or str(st.st_mtime_ns) != mtime:
            return ("skipped", f"{dest} changed since it was moved there: it stays there")
        if self.there(src):
            return ("conflict", f"something new is at {src}: the file stays at {dest}")
        if self.dry_run:
            self.would += [(src, dest), (dest, None)]
            return ("restored", "")
        if not os.path.isdir(os.path.dirname(src)):
            os.makedirs(os.path.dirname(src), exist_ok=True)
        self.record("move", dest, src)
        try:
            rename_noreplace(dest, src)
        except FileExistsError:
            return ("conflict", f"something new is at {src}: the file stays at {dest}")
        return ("restored", "")

    def uncopy(self, src, dest, note, superseded=False):
        """A copy made by a merge across drives goes, once its original is
        back. A temporary copy left by a stopped run goes too."""
        try:
            size, mtime, digest, tmp = note.split(":")
            size, mtime = int(size), int(mtime)
        except ValueError:
            return ("skipped", "its line in actions.tsv is damaged")
        if tmp:
            stray = os.path.join(os.path.dirname(dest), tmp)
            if _is_tmp(stray) and os.path.lexists(stray) and not self.dry_run:
                self.record("delete", stray)
                try:
                    os.unlink(stray)
                except FileNotFoundError:
                    pass
        if superseded or not os.path.lexists(dest):
            return (QUIET, "")
        if self.dry_run and not os.path.lexists(src) and self.there(src):
            self.would.append((dest, None))
            return ("restored", "the copy would be deleted once the original is back")
        try:
            read_file(src, size, mtime, digest, self.stop)
        except Gone:
            return ("skipped", f"the original {src} isn't back, so its copy {dest} stays")
        except Bad as e:
            return ("skipped", f"the original {src}: {e}; its copy {dest} stays")
        try:        # (its date may be rounded: exFAT keeps 10 ms steps)
            read_file(dest, size, os.lstat(dest).st_mtime_ns, digest, self.stop)
        except (Gone, FileNotFoundError):
            return (QUIET, "")
        except Bad as e:
            return ("skipped", f"the copy {dest} changed since it was made ({e}): it stays")
        if self.dry_run:
            self.would.append((dest, None))
            return ("restored", "")
        self.record("delete", dest)
        os.unlink(dest)
        return ("restored", "")

    def unmkdir(self, path):
        if self.dry_run:
            return (QUIET, "")
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
        return (QUIET, "")


def _sum_up(step, main, outcomes):
    """One Outcome for a step from what happened to each of its rows."""
    real = [(s, n) for s, n in outcomes if s != QUIET]
    if not real:
        return Outcome(step, main, "already", "nothing to undo")
    bad = sorted((x for x in real if x[0] in TROUBLE), key=lambda x: TROUBLE.index(x[0]))
    if bad:
        status, note = bad[0]
        partly = any(s == "restored" for s, _ in real)
        return Outcome(step, main, status, ("partly: " if partly else "") + note, partly)
    if all(s == "already" for s, _ in real):
        return Outcome(step, main, "already", "already back")
    notes = [n for s, n in real if s == "restored" and n]
    return Outcome(step, main, "restored", notes[0] if notes else "")


def _in_the_way(path, item):
    return ("conflict", f"something new is at {path}, so the item stays in the Trash "
                        f"({item}); nothing was overwritten")


def _ident(st):
    return f"{st.st_dev}:{st.st_ino}"


def _is_tmp(path):
    name = os.path.basename(path)
    return name.startswith(".clean-link-") or name.startswith(".clean-copy-")


def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)
