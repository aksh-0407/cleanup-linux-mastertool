"""
Guards for destructive runs: which folders may be cleaned, noticing a drive
that disappears mid-run, and stopping between items on Ctrl-C or `kill`.
"""

import errno
import os
import signal

from cleanup.errors import DriveGone, UsageError
from cleanup.fsutil import is_within, printable, why

# `--delete` refuses these (and anything inside them) without --force: the OS
# and installed programs rely on empty folders here.
SYSTEM_DIRS = ("/bin", "/boot", "/etc", "/lib", "/lib32", "/lib64", "/libx32",
               "/opt", "/root", "/run", "/sbin", "/snap", "/srv", "/usr", "/var")
# ...except removable drives, which some distros mount under /run/media.
SYSTEM_DIR_EXCEPTIONS = ("/run/media",)

# OS errors meaning the drive itself is gone or has turned read-only. When one
# of these happens while deleting, stop: every remaining item would fail too.
FATAL_ERRNOS = frozenset(e for e in (
    errno.EIO, errno.EROFS, errno.ENODEV, errno.ENXIO,
    getattr(errno, "ENOTCONN", None), getattr(errno, "ESTALE", None),
) if e is not None)


def check_delete_allowed(root, force):
    """Refuse destructive runs on places where empty folders are load-bearing,
    and on drives that can't be written anyway."""
    home = os.path.realpath(os.path.expanduser("~"))
    if not force:
        if is_within(home, root):  # ~ itself, or a folder that contains it
            what = ("your whole home folder" if root == home
                    else f"{printable(root)}, which contains your home folder")
            raise UsageError(
                f"refusing to delete inside {what}: programs keep empty folders "
                f"under ~/.config, ~/.local and the like. Pick a subfolder such as "
                f"~/Downloads, or add --force.")
        if (any(is_within(root, d) for d in SYSTEM_DIRS)
                and not any(is_within(root, d) for d in SYSTEM_DIR_EXCEPTIONS)):
            raise UsageError(
                f"refusing to delete inside system folder {printable(root)}: the OS "
                f"and installed programs rely on empty folders there. Add --force "
                f"if you are sure.")
    try:
        read_only = os.statvfs(root).f_flag & os.ST_RDONLY
    except (OSError, AttributeError):
        read_only = False
    if read_only:
        raise UsageError(f"the drive holding {printable(root)} is mounted read-only, "
                         f"so nothing can be deleted")


class DriveWatch:
    """Notices mid-run that the drive under the scanned folders has gone away
    (unplugged, unmounted, or failing), so the run stops with one clear message
    instead of an error for every remaining file and a report with holes.

    After an I/O-type error the roots are checked: one that can no longer be
    stat'ed, or that now sits on a different device (its drive was unmounted
    from under it), means the drive is gone. A drive that is still mounted but
    fails STREAK_LIMIT reads in a row is treated as gone too.
    """

    STREAK_LIMIT = 100
    _CHECK_ERRNOS = FATAL_ERRNOS | {errno.ENOENT}

    def __init__(self, roots):
        self.roots = [(r, os.stat(r).st_dev) for r in roots]
        self.streak = 0     # shared by the worker threads; approximate is fine

    def ok(self):
        self.streak = 0

    def forget(self, path):
        """`path` was removed on purpose: the roots inside it are no longer
        a sign of the drive."""
        self.roots = [(r, dev) for r, dev in self.roots if not is_within(r, path)]

    def check(self):
        """Raises DriveGone if a root can no longer be stat'ed, or sits on
        another device now (one stat per root)."""
        for root, dev in self.roots:
            try:
                moved = os.stat(root).st_dev != dev
            except OSError:
                moved = True
            if moved:
                raise DriveGone(f"the drive holding {printable(root)} has "
                                f"disappeared (unplugged or unmounted?)")

    def failed(self, err):
        """Call with every OSError from a read; raises DriveGone if the drive
        itself is the problem."""
        if err.errno not in self._CHECK_ERRNOS:
            return
        self.check()
        if err.errno != errno.ENOENT:  # files deleted mid-run are no sign of trouble
            self.streak += 1
            if self.streak >= self.STREAK_LIMIT:
                raise DriveGone(f"the last {self.streak} reads all failed "
                                f"({why(err)}): the drive seems to be failing "
                                f"or disconnected")


class StopFlag:
    """While active, Ctrl-C, `kill` and hang-up only set `hit`; a delete loop
    checks it between items. An interrupt can then never land between removing
    something and logging it. Signals that are ignored (nohup) stay ignored."""

    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __enter__(self):
        self.hit = False
        self._old = {}
        for sig in self.SIGNALS:
            old = signal.getsignal(sig)
            if old in (signal.SIG_IGN, None):
                continue
            try:
                signal.signal(sig, self._set)
            except ValueError:  # not the main thread: interrupts stay as they are
                continue
            self._old[sig] = old
        return self

    def _set(self, signum, frame):
        self.hit = True

    def __exit__(self, *exc):
        for sig, old in self._old.items():
            signal.signal(sig, old)


def confirm(question, yes):
    """Ask before a destructive step. True to go ahead. `yes` (-y) answers
    for the user; with no terminal to ask on, refuse rather than guess."""
    if yes:
        return True
    try:
        interactive = os.isatty(0)
    except OSError:
        interactive = False
    if not interactive:
        raise UsageError("not going ahead without asking, and there is no terminal to "
                         "ask on: add -y to confirm")
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")
