#!/usr/bin/env python3
"""Ctrl-C, `kill` and hang-up at any moment: nothing half-done, an exact
record of what was deleted, and a run that says it was interrupted."""

import os
import shutil
import signal
import subprocess
import sys
import time
import unittest

from helpers import CLEAN, Base


class TestInterrupts(Base):
    def run_slowed(self, slow, args, delay, sig=signal.SIGINT, after=False, patch=""):
        """Run clean with os.<slow> made slow (the pause comes after the real
        call with `after`), Ctrl-C it after `delay` seconds."""
        wrapper = (
            "import os, runpy, signal, sys, time\n"
            # a shell starts background jobs with Ctrl-C ignored; undo that
            "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
            f"real = os.{slow}\n"
            "def slowed(*a, **k):\n"
            + ("    r = real(*a, **k); time.sleep(0.01); return r\n" if after else
               "    time.sleep(0.01)\n"
               "    return real(*a, **k)\n") +
            f"os.{slow} = slowed\n" + patch +
            f"sys.argv = {[CLEAN] + list(args)!r}\n"
            f"runpy.run_path({CLEAN!r}, run_name='__main__')\n")
        p = subprocess.Popen([sys.executable, "-c", wrapper], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(delay)
        p.send_signal(sig)
        out, err = p.communicate(timeout=20)
        return p.returncode, out, err

    def check_interrupted(self, run=None):
        run = run or self.last_run()
        meta = self.run_meta(run)
        self.assertEqual((meta["status"], meta["exit_code"]), ("interrupted", 130))
        self.assertTrue(run.has("summary.txt") and run.has("report.html"))
        self.assertEqual(self.leftovers(run.folder), [])
        return run

    def check_record(self, run=None):
        """folders.tsv lists exactly the folders that went, and the rest as
        not done."""
        deleted = {r[2] for r in self.folder_rows("deleted", run)}
        gone = {os.path.join(self.root, f"d{i}") for i in range(300)} - {
            e.path for e in os.scandir(self.root)}
        self.assertTrue(0 < len(gone) < 300)
        self.assertEqual(deleted, gone)
        not_done = {r[2] for r in self.folder_rows("not-done", run)}
        self.assertEqual(not_done | deleted, {os.path.join(self.root, f"d{i}") for i in range(300)})

    def test_interrupt_during_scan_deletes_nothing(self):
        for i in range(300):
            self.mk(f"d{i}")
        for jobs in ("1", "4"):
            rc, out, err = self.run_slowed(
                "scandir", ["empty", self.root, "--delete", "-j", jobs], 0.5)
            self.assertEqual(rc, 130, err)
            self.assertIn(b"nothing was deleted", err)
            self.assertNotIn(b"Traceback", err)
            self.assertEqual(len(os.listdir(self.root)), 300)
            run = self.check_interrupted()
            self.assertEqual(run.headline, "interrupted while scanning; nothing was deleted")
            self.assertFalse(run.has("folders.tsv"))
            shutil.rmtree(run.folder)

    def test_interrupt_during_delete_logs_exactly_what_went(self):
        for i in range(300):
            self.mk(f"d{i}")
        rc, out, err = self.run_slowed(
            "rmdir", ["empty", self.root, "--delete"], 0.8)
        self.assertEqual(rc, 130, err)
        self.assertIn(b"INTERRUPTED", out)
        self.assertNotIn(b"Traceback", err)
        self.check_record()
        run = self.check_interrupted()
        self.assertRegex(run.headline, r"^interrupted: \d+ of 300 empty folders deleted$")

    def test_interrupt_right_after_rmdir_is_still_logged(self):
        # Every signal lands just after a folder is removed and before it is
        # logged: it must still be logged, and the run must then stop.
        for sig in (signal.SIGINT, signal.SIGTERM):
            for i in range(300):
                self.mk(f"d{i}")
            rc, out, err = self.run_slowed(
                "rmdir", ["empty", self.root, "--delete"], 0.8, sig, after=True)
            self.assertEqual(rc, 130, err)
            self.assertIn(b"INTERRUPTED", out)
            self.check_record()
            shutil.rmtree(self.check_interrupted().folder)

    def test_interrupt_dupes_saves_no_results(self):
        data = os.urandom(10_000)
        for i in range(300):
            self.write(f"d{i}/f", data)
        for jobs in ("1", "4"):
            rc, out, err = self.run_slowed(
                "scandir", ["dupes", self.root, "-j", jobs], 0.5)
            self.assertEqual(rc, 130, err)
            self.assertNotIn(b"Traceback", err)
            run = self.check_interrupted()
            self.assertFalse(run.has("files.tsv"))
            with open(run.file("summary.txt")) as f:
                self.assertIn("STOPPED:         interrupted while scanning; no results", f.read())
            shutil.rmtree(run.folder)

    def test_sigterm_and_sighup_stop_cleanly(self):
        data = os.urandom(10_000)
        for i in range(300):
            self.write(f"f{i}/f", data)
            self.mk(f"d{i}")
        for sig in (signal.SIGTERM, signal.SIGHUP):
            rc, out, err = self.run_slowed(
                "scandir", ["dupes", self.root], 0.5, sig)
            self.assertEqual(rc, 130, err)
            self.assertNotIn(b"Traceback", err)
            self.assertFalse(self.check_interrupted().has("files.tsv"))
        rc, out, err = self.run_slowed(
            "rmdir", ["empty", self.root, "--delete"], 0.8, signal.SIGTERM)
        self.assertEqual(rc, 130, err)
        self.assertIn(b"INTERRUPTED", out)
        self.check_record()
        self.check_interrupted()
        self.assertEqual(len(self.runs()), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
