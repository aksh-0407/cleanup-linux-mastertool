#!/usr/bin/env python3
"""Runs: how a run's status is recorded however it ends, the history and its
numbers, which old runs are pruned, the lock that lets only one clean delete
at a time, and index.html."""

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

from helpers import CLEAN, Base

from cleanup import runs
from cleanup.errors import UsageError

DAY = 86400


def wrapper(patch, args):
    """Source for `python -c`: apply `patch`, then run clean with `args`."""
    return ("import os, runpy, signal, sys, time\n"
            "signal.signal(signal.SIGINT, signal.default_int_handler)\n" + patch +
            f"sys.argv = {[CLEAN] + list(args)!r}\n"
            f"runpy.run_path({CLEAN!r}, run_name='__main__')\n")


SLOW_RMDIR = ("real = os.rmdir\n"
              "def slowed(*a, **k):\n"
              "    time.sleep(0.02)\n"
              "    return real(*a, **k)\n"
              "os.rmdir = slowed\n")


class TestLifecycle(Base):
    def wait_for(self, check, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def start_slow_delete(self):
        for i in range(300):
            self.mk(f"d{i}")
        p = subprocess.Popen([sys.executable, "-c", wrapper(SLOW_RMDIR, ["empty", self.root, "--delete"])],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def stop():
            if p.poll() is None:
                p.kill()
            p.communicate()
        self.addCleanup(stop)

        def deleting():
            found = self.runs()     # (folders.tsv appears once the scan is done)
            if not (found and found[0].has("folders.tsv")):
                return False
            with open(found[0].file("folders.tsv")) as f:
                return sum(line.startswith("deleted\t") for line in f) >= 5
        self.wait_for(deleting)
        return p

    def test_done(self):
        self.mk("a")
        r = self.empty(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = self.last_run()
        meta = self.run_meta()
        self.assertEqual(meta["name"], run.name)
        self.assertRegex(run.name, r"^\d{4}-\d\d-\d\d_\d{6}-empty$")
        self.assertEqual((meta["status"], meta["exit_code"], meta["command"], meta["mode"]),
                         ("done", 0, "empty", "dry-run"))
        self.assertEqual(meta["argv"], ["empty", self.root])
        self.assertEqual(meta["headline"], "1 empty folder would be deleted")
        self.assertEqual(meta["totals"]["removed"], 1)
        self.assertIsInstance(meta["pid"], int)
        for key in ("started", "finished", "duration_s", "version", "boot_id", "host", "cwd"):
            self.assertIn(key, meta)
        self.assertGreaterEqual(meta["finished_ts"], meta["started_ts"])
        self.assertEqual(meta["roots"][0]["path"], self.root)
        self.assertEqual(sorted(os.listdir(run.folder)),
                         ["data.json.gz", "folders.tsv", "report.html", "run.json", "summary.txt"])
        with open(run.file("report.html"), encoding="utf-8") as f:
            page = f.read()
        for name in ("data.json.gz", "folders.tsv", "run.json", "summary.txt"):
            self.assertIn(f'<a href="{name}">', page)
        self.assertEqual(run.status, "done")

    def test_failed_run_is_recorded(self):
        self.mk("a")
        patch = ("import cleanup.empty\n"
                 "def boom(*a, **k): raise RuntimeError('boom')\n"
                 "cleanup.empty.Walker.list_dir = boom\n")
        r = subprocess.run([sys.executable, "-c", wrapper(patch, ["empty", self.root])],
                           capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"RuntimeError: boom", r.stderr)   # a bug shows itself
        meta = self.run_meta()
        self.assertEqual((meta["status"], meta["error"]), ("failed", "RuntimeError: boom"))
        out = self.run_clean("show").stdout.decode()
        self.assertIn(": failed", out)
        self.assertIn("Error:   RuntimeError: boom", out)

    def test_kill_9_shows_as_unfinished(self):
        p = self.start_slow_delete()
        p.kill()
        p.wait()
        run = self.last_run()
        self.assertEqual(self.run_meta(run)["status"], "running")   # it never got to say
        self.assertEqual(run.status, "unfinished")
        # What it deleted before it died is on record; at most the one folder
        # it removed in the instant before the kill can be missing.
        gone = {os.path.join(self.root, f"d{i}") for i in range(300)} - {
            e.path for e in os.scandir(self.root)}
        recorded = {r[2] for r in self.folder_rows("deleted", run)}
        self.assertTrue(recorded)
        self.assertLessEqual(recorded, gone)
        self.assertLessEqual(len(gone - recorded), 1)
        r = self.run_clean("history")
        self.assertIn("empty --delete  unfinished  stopped without finishing · ", r.stdout.decode())
        out = self.run_clean("show").stdout.decode()
        self.assertIn(f"Run {run.name} (#1 in clean history): unfinished", out)
        self.assertIn("killed (kill -9)", out)
        self.assertIn(f"What it recorded before then: {run.file('folders.tsv')}", out)
        with open(os.path.join(self.data_dir, "index.html")) as f:
            self.assertIn('class="badge s-unfinished"', f.read())
        # The lock went with the process: the next delete may start.
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.root), [])

    def test_running_then_sigterm(self):
        p = self.start_slow_delete()
        run = self.last_run()
        self.assertEqual(run.status, "running")
        r = self.run_clean("history")
        self.assertIn("empty --delete  running", r.stdout.decode())
        out = self.run_clean("show").stdout.decode()
        self.assertIn("It is still running.", out)
        self.assertIn(f"Process: {p.pid}", out)
        # A second delete has to wait for this one.
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"another clean is deleting right now (process {p.pid}: "
                      f"clean empty --delete {self.root})".encode(), r.stderr)
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 130, err)
        self.assertEqual(self.run_meta(run)["status"], "interrupted")
        self.assertEqual(runs.load(run.folder).status, "interrupted")


class TestLiveness(unittest.TestCase):
    def meta(self, **kw):
        m = {"pid": os.getpid(), "proc_start": runs.proc_start(os.getpid()),
             "boot_id": runs.boot_id(), "host": socket.gethostname()}
        m.update(kw)
        return m

    def test_is_alive(self):
        self.assertTrue(runs.is_alive(self.meta()))
        self.assertTrue(runs.is_alive(self.meta(proc_start=None)))
        self.assertFalse(runs.is_alive(self.meta(proc_start=self.meta()["proc_start"] + 1)))
        self.assertFalse(runs.is_alive(self.meta(boot_id="another-boot")))
        self.assertTrue(runs.is_alive(self.meta(host="another-computer")))   # can't tell
        for pid in (None, 0, -1, True, "1", 1.5):
            self.assertFalse(runs.is_alive(self.meta(pid=pid)), pid)
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertFalse(runs.is_alive(self.meta(pid=p.pid, proc_start=None)))

    def test_proc_start_with_odd_process_names(self):
        # The name in /proc/PID/stat may hold spaces and ")" (a process may
        # only rename itself, so this one does, for a moment).
        start = runs.proc_start(os.getpid())
        self.assertIsInstance(start, int)
        with open("/proc/self/comm") as f:
            name = f.read().rstrip("\n")
        try:
            with open("/proc/self/comm", "w") as f:
                f.write("a) b (c")
            with open("/proc/self/stat") as f:
                self.assertIn("(a) b (c)", f.read())
            self.assertEqual(runs.proc_start(os.getpid()), start)
        finally:
            with open("/proc/self/comm", "w") as f:
                f.write(name)
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertIsNone(runs.proc_start(p.pid))


class TestHistory(Base):
    def make_three(self):
        self.fake_run("2026-01-01_100000-empty", headline="old")
        self.fake_run("2026-02-01_100000-dupes", headline="middle")
        self.fake_run("2026-03-01_100000-empty", headline="new", mode="delete")
        return [r.name for r in self.runs()]

    def test_numbering_newest_first(self):
        names = self.make_three()
        self.assertEqual(names, ["2026-03-01_100000-empty", "2026-02-01_100000-dupes",
                                 "2026-01-01_100000-empty"])
        self.assertEqual([r.number for r in self.runs()], [1, 2, 3])
        # Same second: the -2 suffix sorts after.
        self.fake_run("2026-03-01_100000-empty-2")
        self.assertEqual(self.runs()[0].name, "2026-03-01_100000-empty-2")
        # started_ts in run.json wins over the name.
        self.fake_run("2025-01-01_000000-dupes", started_ts=time.time())
        self.assertEqual(self.runs()[0].name, "2025-01-01_000000-dupes")

    def test_resolve(self):
        self.make_three()
        new, mid, old = "2026-03-01_100000-empty", "2026-02-01_100000-dupes", "2026-01-01_100000-empty"
        cases = {None: new, "": new, "last": new, " last ": new, "1": new, "#1": new, "2": mid,
                 "#3": old, "2026-02": mid, "2026-01-01_100000-empty": old, "2026-03-01_10": new}
        for spec, name in cases.items():
            self.assertEqual(runs.resolve(spec, self.data_dir).name, name, spec)
        folder = os.path.join(self.data_dir, "runs", mid)
        self.assertEqual(runs.resolve(folder, self.data_dir).name, mid)
        self.assertEqual(runs.resolve(folder + "/", self.data_dir).name, mid)
        self.assertEqual(runs.resolve(os.path.join(folder, "run.json"), self.data_dir).name, mid)
        self.assertEqual(runs.resolve(folder, self.data_dir).number, 2)
        # 2026 is too big to be a run number, so it is the start of a name...
        with self.assertRaises(UsageError) as e:
            runs.resolve("2026", self.data_dir)
        self.assertIn("could be any of 3 runs", str(e.exception))
        with self.assertRaises(UsageError) as e:
            runs.resolve("2026-0", self.data_dir)
        self.assertIn("give more of the name, or its number", str(e.exception))
        for spec, msg in (("4", "there is no run 4: the history has 3"),
                          ("0", "there is no run 0"),
                          ("#9", "there is no run 9"),
                          ("1999", "there is no run 1999: the history has 3"),
                          ("nope", "no run is called nope..."),
                          (self.tmp, "is not a run folder")):
            with self.assertRaises(UsageError, msg=spec) as e:
                runs.resolve(spec, self.data_dir)
            self.assertIn(msg, str(e.exception), spec)

    def test_resolve_a_run_folder_elsewhere(self):
        other = os.path.join(self.tmp, "copied")
        name = "2026-05-05_050505-dupes"
        folder = self.fake_run(name, data_dir=other, headline="copied run", files=["summary.txt"])
        found = runs.resolve(folder, self.data_dir)
        self.assertEqual((found.headline, found.number), ("copied run", None))
        r = self.run_clean("show", folder)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode(), f"Run {name}: done\nsummary.txt of {name}\n")
        os.unlink(os.path.join(folder, "summary.txt"))
        out = self.run_clean("show", folder).stdout.decode()
        self.assertIn("It finished, but its summary.txt is missing", out)

    def test_no_runs(self):
        with self.assertRaises(UsageError) as e:
            runs.resolve("last", self.data_dir)
        self.assertIn("there are no runs yet", str(e.exception))
        r = self.run_clean("show")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"there are no runs yet", r.stderr)
        r = self.run_clean("history")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"No runs yet", r.stdout)

    def test_odd_things_in_the_runs_folder(self):
        self.make_three()
        base = os.path.join(self.data_dir, "runs")
        outside = self.fake_run("2020-01-01_000000-empty", data_dir=os.path.join(self.tmp, "x"))
        os.symlink(outside, os.path.join(base, "2020-01-01_000000-empty"))
        os.mkdir(os.path.join(base, "notes"))
        with open(os.path.join(base, "2020-02-02_000000-empty"), "w") as f:
            f.write("a file named like a run")
        self.fake_run("2026-04-01_100000-empty", status=None)      # no run.json
        bad = self.fake_run("2026-04-02_100000-dupes", status=None)
        with open(os.path.join(bad, "run.json"), "w") as f:
            f.write("{ not json")
        self.fake_run("2026-04-03_100000-dupes", status="weird", started_ts="soon")
        found = self.runs()
        self.assertEqual([r.name for r in found][:3], ["2026-04-03_100000-dupes",
                                                       "2026-04-02_100000-dupes",
                                                       "2026-04-01_100000-empty"])
        self.assertEqual(len(found), 6)
        self.assertEqual([r.status for r in found][:3], ["unfinished"] * 3)
        r = self.run_clean("history")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(b"Traceback", r.stderr)
        r = self.run_clean("show", "2")
        self.assertIn(b"run.json is missing or unreadable", r.stdout)

    def test_history_listing(self):
        for i in range(25):
            self.fake_run(f"2026-01-{i + 1:02d}_100000-empty", headline=f"run {i + 1}",
                          started_ts=1767261600 + i * DAY)
        out = self.run_clean("history").stdout.decode()
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["#", "When", "What", "Status", "Result"])
        self.assertRegex(lines[1], r"^ 1  \w{3} \d+ \w{3} 2026, \d\d:\d\d  empty  done    run 25 · ")
        self.assertIn(self.root, lines[1])
        self.assertIn("The newest 20 of 25 runs (--all lists every one).", out)
        self.assertEqual(len(self.run_clean("history", "--all").stdout.decode().splitlines()),
                         len(lines) + 4)
        out = self.run_clean("history", "-n", "2").stdout.decode()
        self.assertIn("The newest 2 of 25 runs", out)
        self.assertIn(f"All runs as a page: {self.data_dir}/index.html", out)
        for bad in ("0", "-1", "x"):
            r = self.run_clean("history", "-n", bad)
            self.assertEqual(r.returncode, 2, bad)

    def test_show(self):
        self.mk("a")
        self.empty(self.root)
        self.write("x", "same")
        self.write("y", "same")
        self.dupes(self.root)
        r = self.run_clean("show")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode()
        dupes_run, empty_run = self.runs()
        self.assertTrue(out.startswith(f"Run {dupes_run.name} (#1 in clean history): done\n"))
        with open(dupes_run.file("summary.txt")) as f:
            self.assertTrue(out.endswith(f.read()))
        out = self.run_clean("show", "2").stdout.decode()
        self.assertIn("Empty folders that WOULD be deleted (1):", out)
        r = self.run_clean("show", "2", "--path")
        self.assertEqual(r.stdout.decode(), empty_run.folder + "\n")
        r = self.run_clean("show", "9")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"there is no run 9: the history has 2", r.stderr)
        r = self.run_clean("show", "--open", "--path")
        self.assertEqual(r.returncode, 2)

    def test_show_never_prints_control_characters(self):
        folder = self.fake_run("2026-01-01_100000-empty", files=["summary.txt"])
        with open(os.path.join(folder, "summary.txt"), "w") as f:
            f.write("line one\n\x1b]0;evil title\x07\x1b[2J and more\n")
        out = self.run_clean("show").stdout
        self.assertNotIn(b"\x1b", out)
        self.assertNotIn(b"\x07", out)
        self.assertIn(b"line one\n\\x1b]0;evil title", out)

    def fake_opener(self):
        """An xdg-open on PATH that records what it was asked to open."""
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        record = os.path.join(self.tmp, "opened")
        with open(os.path.join(bin_dir, "xdg-open"), "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{record}"\n')
        os.chmod(os.path.join(bin_dir, "xdg-open"), 0o755)
        return {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}, record

    def opened(self, record):
        self.wait_until = time.monotonic() + 5
        while not os.path.exists(record) and time.monotonic() < self.wait_until:
            time.sleep(0.02)
        with open(record) as f:
            return f.read().splitlines()

    def test_open(self):
        env, record = self.fake_opener()
        self.mk("a")
        self.empty(self.root)
        run = self.last_run()
        r = self.run_clean("show", "--open", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode(), f"Opening {run.file('report.html')}\n")
        self.assertEqual(self.opened(record), [run.file("report.html")])
        os.unlink(record)
        r = self.run_clean("history", "--open", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.opened(record), [os.path.join(self.data_dir, "index.html")])
        os.unlink(record)
        os.unlink(run.file("report.html"))      # no page: its folder opens instead
        self.run_clean("show", "1", "--open", env=env)
        self.assertEqual(self.opened(record), [run.folder])
        r = self.run_clean("show", "--open", env={"PATH": os.path.join(self.tmp, "empty-bin")})
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"xdg-open is not installed", r.stderr)


class TestPrune(Base):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def old(self, days, kind="empty", **kw):
        ts = self.now - days * DAY
        name = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(ts)) + f"-{kind}"
        return self.fake_run(name, started_ts=ts, **kw)

    def names(self, found):
        return sorted(os.path.basename(r.folder) for r in found)

    def test_plan(self):
        a = self.old(400)
        b = self.old(200)
        c = self.old(100, "remove", files=["actions.tsv"], mode="trash")   # an undo record
        d = self.old(95, status="running", pid=os.getpid(),
                     proc_start=runs.proc_start(os.getpid()), host=socket.gethostname())
        e = self.old(50)
        f = self.old(1)
        found = runs.history(self.data_dir)
        base = os.path.basename
        remove, kept = runs.prune_plan(found, 90, 0, now=self.now)
        self.assertEqual(self.names(remove), sorted([base(a), base(b)]))
        self.assertEqual([(base(r.folder), why) for r, why in kept],
                         [(base(d), "still running"),
                          (base(c), "its undo record (clean undo) is still needed")])
        # The newest keep_runs always stay, however old.
        remove, _ = runs.prune_plan(found, 90, 5, now=self.now)
        self.assertEqual(self.names(remove), [base(a)])
        remove, _ = runs.prune_plan(found, 90, 30, now=self.now)
        self.assertEqual(remove, [])
        remove, _ = runs.prune_plan(found, 30, 0, now=self.now)
        self.assertEqual(self.names(remove), sorted(base(x) for x in (a, b, e)))
        # 0 days: kept for ever.
        self.assertEqual(runs.prune_plan(found, 0, 0, now=self.now), ([], []))
        self.assertTrue(os.path.isdir(f))

    def test_keeps_undo(self):
        days = iter(range(1, 100))

        def keeps(kind="remove", files=("actions.tsv",), **meta):
            return runs.keeps_undo(runs.load(self.old(next(days), kind, files=files, **meta)))
        self.assertTrue(keeps(mode="trash"))
        self.assertTrue(keeps(mode="trash", undone_by={"run": "x", "full": False}))
        self.assertTrue(keeps(mode="trash", merge=True))
        self.assertFalse(keeps(mode="trash", undone_by={"run": "x", "full": True}))
        self.assertFalse(keeps(mode="trash", emptied_by="2026-01-01_000000-trash"))
        self.assertFalse(keeps(mode="permanent"))
        self.assertFalse(keeps(mode="dry-run"))
        self.assertFalse(keeps(mode="trash", files=()))
        header_only = self.old(next(days), "remove", mode="trash")
        with open(os.path.join(header_only, "actions.tsv"), "w") as f:
            f.write("step\twhen\taction\tpath\tto\tnote\n")     # everything was gone already
        self.assertFalse(runs.keeps_undo(runs.load(header_only)))
        self.assertFalse(keeps("empty", mode="delete"))
        self.assertFalse(keeps("undo", mode="undo"))

    def test_unfinished_old_runs_are_pruned(self):
        dead = self.old(100, status="running")      # pid 999999999: long gone
        self.assertEqual(runs.load(dead).status, "unfinished")
        remove, kept = runs.prune_plan(runs.history(self.data_dir), 90, 0, now=self.now)
        self.assertEqual([r.folder for r in remove], [dead])

    def test_cli(self):
        a = self.old(400)
        b = self.old(300, "remove", files=["actions.tsv"], mode="trash")
        self.old(200)
        env = {"CLEAN_HISTORY_KEEP_DAYS": "90", "CLEAN_HISTORY_KEEP_RUNS": "1"}
        before = sorted(os.listdir(os.path.join(self.data_dir, "runs")))
        for args in (["--prune", "--dry-run"], ["--dry-run"]):
            r = self.run_clean("history", *args, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = r.stdout.decode()
            self.assertIn("Would remove 1 run (older than 90 days, and not among the newest 1):", out)
            self.assertIn(f"  {os.path.basename(a)}", out)
            self.assertIn(f"Kept although old: {os.path.basename(b)} (its undo record", out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.data_dir, "runs"))), before)
        r = self.run_clean("history", "--prune", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Removed 1 run", r.stdout.decode())
        self.assertFalse(os.path.exists(a))
        self.assertTrue(os.path.exists(b))
        r = self.run_clean("history", "--prune", env=env)
        self.assertEqual(r.stdout.decode(), "Nothing to prune (a run goes once it is older than "
                         "90 days, and not among the newest 1).\n"
                         f"Kept although old: {os.path.basename(b)} (its undo record (clean undo) "
                         "is still needed)\n")
        r = self.run_clean("history", "--prune", env={"CLEAN_HISTORY_KEEP_DAYS": "0"})
        self.assertIn("history.keep_days is 0", r.stdout.decode())
        self.assertEqual(len(self.runs()), 2)
        with open(os.path.join(self.data_dir, "index.html")) as f:
            self.assertNotIn(os.path.basename(a), f.read())

    def test_after_every_run(self):
        olds = [self.old(d) for d in (500, 400, 300, 200)]
        undo = self.old(250, "remove", files=["actions.tsv"], mode="trash")
        self.mk("e")
        env = {"CLEAN_HISTORY_KEEP_DAYS": "30", "CLEAN_HISTORY_KEEP_RUNS": "2"}
        r = self.empty(self.root, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Kept: the new run and the next newest (keep_runs 2), and the undo record.
        self.assertEqual([r.folder for r in self.runs()][1:], [olds[3], undo])
        with open(os.path.join(self.data_dir, "index.html")) as f:
            page = f.read()
        self.assertIn("3 runs", page)
        self.assertNotIn(os.path.basename(olds[0]), page)

    def test_only_run_folders_are_ever_deleted(self):
        outside = self.fake_run("2000-01-01_000000-empty", data_dir=os.path.join(self.tmp, "x"),
                                started_ts=0)
        link = os.path.join(self.data_dir, "runs", "2000-01-01_000001-empty")
        os.makedirs(os.path.dirname(link))
        os.symlink(outside, link)
        stray = os.path.join(self.data_dir, "runs", "keep-me")
        os.mkdir(stray)
        r = self.run_clean("history", "--prune", env={"CLEAN_HISTORY_KEEP_RUNS": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.islink(link) and os.path.isdir(outside) and os.path.isdir(stray))
        for folder in (link, outside, stray):
            with self.assertRaises(OSError) as e:
                runs.delete_run(runs.load(folder), self.data_dir)
            self.assertEqual(e.exception.errno, errno.EPERM)
        self.assertTrue(os.path.islink(link) and os.path.isdir(outside) and os.path.isdir(stray))

    def test_settings(self):
        for key in ("history.keep_days", "history.keep_runs"):
            r = self.run_clean("config", "set", key, "-1")
            self.assertEqual(r.returncode, 2, key)
            r = self.run_clean("config", "set", key, "0")
            self.assertEqual(r.returncode, 0, (key, r.stderr))
        with open(self.settings_file) as f:
            self.assertEqual(json.load(f), {"history.keep_days": 0, "history.keep_runs": 0})


class TestLock(Base):
    def test_one_delete_at_a_time(self):
        self.mk("a")
        fd = runs.lock("the test", data_dir=self.data_dir)
        try:
            r = self.empty(self.root, "--delete")
            self.assertEqual(r.returncode, 2)
            self.assertIn(f"another clean is deleting right now (process {os.getpid()}: "
                          f"the test). Only one may delete at a time".encode(), r.stderr)
            self.assertIn("a/", self.tree())
            self.assertEqual(self.runs(), [])     # refused before the run started
            # Looking is always fine.
            self.assertEqual(self.empty(self.root).returncode, 0)
            self.assertEqual(self.dupes(self.root).returncode, 0)
            with self.assertRaises(UsageError):
                runs.lock("a second one", data_dir=self.data_dir)
        finally:
            os.close(fd)
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(), set())


class TestIndex(Base):
    def test_rebuilt_from_the_run_folders(self):
        self.mk("a")
        self.empty(self.root)
        self.write("x", "same")
        self.write("y", "same")
        self.dupes(self.root)
        index = os.path.join(self.data_dir, "index.html")
        with open(index) as f:
            page = f.read()
        dupes_run, empty_run = self.runs()
        self.assertIn("2 runs", page)
        for run in (dupes_run, empty_run):
            self.assertIn(f'href="runs/{run.name}/report.html"', page)
            self.assertIn(f'href="runs/{run.name}/summary.txt"', page)
        self.assertLess(page.index(dupes_run.name), page.index(empty_run.name))
        # A run folder deleted by hand is simply gone from the list.
        subprocess.run(["rm", "-rf", empty_run.folder], check=True)
        self.run_clean("history")
        with open(index) as f:
            page = f.read()
        self.assertIn("1 run ", page)
        self.assertNotIn(empty_run.name, page)
        # And the report pages link back to it.
        with open(dupes_run.file("report.html")) as f:
            self.assertIn('href="../../index.html"', f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
