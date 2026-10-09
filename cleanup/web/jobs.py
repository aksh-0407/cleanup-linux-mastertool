"""
Jobs: the scans, removals and undos the dashboard starts, each one a `clean
...` child process, so it goes through exactly the same checks as when typed
in a terminal. Only one runs at a time.

The child writes its progress as marked lines (CLEAN_PROGRESS=lines, see
fsutil.Progress); everything else it prints is its output, kept for the
job's page. Cancel sends it SIGTERM, which `clean` treats as Ctrl-C: it stops
between two items and saves what it did.
"""

import itertools
import os
import signal
import subprocess
import sys
import threading
import time

import cleanup
from cleanup import runs
from cleanup.fsutil import Progress

TOOL = os.path.dirname(os.path.dirname(os.path.abspath(cleanup.__file__)))
LAUNCHER = os.path.join(TOOL, "clean")
MAX_LINES = 5000        # output lines kept per job (the first and the newest)
HEAD_LINES = 500


def command(argv):
    """The child's command line: this Python running this copy of clean."""
    if os.path.isfile(LAUNCHER):
        return [sys.executable, LAUNCHER] + list(argv)
    return [sys.executable, "-m", "cleanup"] + list(argv)


class Busy(Exception):
    """Another job is still running."""


class Job:
    def __init__(self, jid, argv, kind, data_dir, after=None):
        self.id = jid
        self.argv = list(argv)
        self.kind = kind            # what it is, for the page: "scan", "remove", ...
        self.data_dir = data_dir
        self.after = after          # where to go when it ends (a page path), if not its run
        self.started = time.time()
        self.ended = None
        self.code = None
        self.head = []              # the first HEAD_LINES lines
        self.tail = []              # the newest lines, up to MAX_LINES - HEAD_LINES
        self.total = 0              # lines seen in all
        self.progress = ""
        self.cancelled = False
        self.proc = None
        self.run_name = None
        self._looked = 0.0
        self._lock = threading.Lock()
        self._done = threading.Event()

    # ------------------------------------------------------------ output

    def _add(self, text):
        with self._lock:
            if len(self.head) < HEAD_LINES:
                self.head.append(text)
            else:
                self.tail.append(text)
                if len(self.tail) > MAX_LINES - HEAD_LINES:
                    del self.tail[:len(self.tail) - (MAX_LINES - HEAD_LINES)]
            self.total += 1

    def lines_since(self, since):
        """The output lines numbered `since` and after (a note stands in for
        lines dropped in the middle of a very long output)."""
        with self._lock:
            start_tail = self.total - len(self.tail)
            if since < len(self.head):
                out = self.head[since:]
                if start_tail > len(self.head):
                    out.append(f"... {start_tail - len(self.head):,} lines not kept ...")
                return out + self.tail
            return self.tail[max(since - start_tail, 0):]

    def _read(self):
        mark = Progress.LINE_MARK.encode()
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.rstrip(b"\n").rstrip(b"\r")
            if line.startswith(mark):
                self.progress = line[len(mark):].decode("utf-8", "backslashreplace")
                continue
            self._add(line.decode("utf-8", "backslashreplace"))
        self.proc.stdout.close()
        self.code = self.proc.wait()
        self.progress = ""
        self.ended = time.time()
        self.find_run()
        self._done.set()

    # ------------------------------------------------------------ state

    @property
    def running(self):
        return self.ended is None

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def cancel(self):
        """Stop it the way Ctrl-C does (SIGTERM to its process group)."""
        if self.proc is None or not self.running:
            return False
        self.cancelled = True
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except OSError:
            return False
        return True

    def find_run(self):
        """The run this job's process saved (it records its pid), once it
        has started one."""
        if self.run_name or self.proc is None:
            return self.run_name
        now = time.monotonic()
        if self.running and now < self._looked + 1.5:
            return None
        self._looked = now
        for r in runs.history(self.data_dir)[:30]:
            if r.meta.get("pid") == self.proc.pid and r.started_ts >= self.started - 5:
                self.run_name = r.name
                break
        return self.run_name

    def state(self, since=0):
        return {"id": self.id, "argv": self.argv, "kind": self.kind,
                "running": self.running, "code": self.code, "cancelled": self.cancelled,
                "progress": self.progress, "lines": self.lines_since(since),
                "next": self.total, "run": self.find_run(), "after": self.after,
                "elapsed": round((self.ended or time.time()) - self.started, 1)}


class Jobs:
    """The jobs of one dashboard. `spawn(argv, env)` starts a process (a
    hook for tests); one job at a time."""

    def __init__(self, data_dir, spawn=None):
        self.data_dir = data_dir
        self.spawn = spawn or self._spawn
        self.all = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _spawn(self, argv, env):
        return subprocess.Popen(command(argv), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True, close_fds=True)

    def active(self):
        return next((j for j in self.all.values() if j.running), None)

    def start(self, argv, kind, after=None):
        with self._lock:
            busy = self.active()
            if busy is not None:
                raise Busy(busy)
            env = dict(os.environ)
            env.update(CLEAN_PROGRESS="lines", PYTHONUNBUFFERED="1",
                       PYTHONDONTWRITEBYTECODE="1", CLEAN_PATHS_DATA=self.data_dir)
            job = Job(str(next(self._ids)), argv, kind, self.data_dir, after)
            job.proc = self.spawn(argv, env)
            self.all[job.id] = job
            threading.Thread(target=job._read, name=f"job-{job.id}", daemon=True).start()
            return job

    def get(self, jid):
        return self.all.get(jid)
