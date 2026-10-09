#!/usr/bin/env python3
"""A drive that disappears or fails mid-run stops the run with one clear
message, instead of an error per remaining file and a report with holes."""

import os
import shutil
import subprocess
import sys
import unittest

from helpers import CLEAN, Base, needs_unshare


class TestDriveLoss(Base):
    """A drive that disappears or fails mid-run stops the run with one clear
    message, instead of an error per remaining file and a report with holes."""

    def wrapper(self, patch):
        """Source for `python -c` that applies `patch`, then runs clean with
        the remaining arguments."""
        return ("import builtins, errno, os, runpy, sys, time\n" + patch +
                f"sys.argv = [{CLEAN!r}] + sys.argv[1:]\n"
                f"runpy.run_path({CLEAN!r}, run_name='__main__')\n")

    def failing_open(self, bad):
        """A patch making builtins.open fail with EIO for paths where
        `bad(path)` is true."""
        return ("real_open = builtins.open\n"
                f"bad = {bad}\n"
                "def failing(path, *a, **k):\n"
                "    if isinstance(path, str) and bad(path):\n"
                "        raise OSError(errno.EIO, 'Input/output error', path)\n"
                "    return real_open(path, *a, **k)\n"
                "builtins.open = failing\n")

    @needs_unshare
    def test_unplugged_mid_run(self):
        # The root is a tmpfs "drive", unmounted while clean runs, the way the
        # desktop unmounts a USB drive that has been pulled out.
        slow = os.path.join(self.tmp, "slow.py")
        with open(slow, "w") as f:
            f.write(self.wrapper(
                "real_open, real_scandir = builtins.open, os.scandir\n"
                "def o(*a, **k): time.sleep(0.01); return real_open(*a, **k)\n"
                "def s(*a, **k): time.sleep(0.005); return real_scandir(*a, **k)\n"
                "builtins.open, os.scandir = o, s\n"))
        fill = os.path.join(self.tmp, "fill.py")
        with open(fill, "w") as f:
            f.write("import os, sys\nd = os.urandom(10000)\n"
                    "for i in range(300):\n"
                    "    os.makedirs(f'{sys.argv[1]}/d{i}/x')\n"
                    "    open(f'{sys.argv[1]}/d{i}/f', 'wb').write(d)\n")
        out, err = os.path.join(self.tmp, "out"), os.path.join(self.tmp, "err")
        # (command, seconds before the unplug): mid-scan, mid-hashing, mid-scan
        for cmd, delay in (("dupes", 1.0), ("dupes", 5.0), ("empty", 1.0)):
            extra = "" if cmd == "dupes" else "--delete"
            script = (f'mount -t tmpfs none "{self.root}" && '
                      f'"{sys.executable}" "{fill}" "{self.root}" || exit 9\n'
                      f'"{sys.executable}" "{slow}" {cmd} "{self.root}" {extra} '
                      f'>"{out}" 2>"{err}" &\n'
                      f'pid=$!; sleep {delay}; umount -l "{self.root}"; '
                      f'wait $pid; echo "rc=$?"')
            r = self.run_shell(script)
            with open(err, "rb") as f:
                stderr = f.read()
            self.assertIn(b"rc=1", r.stdout, (cmd, delay, r.stdout, r.stderr, stderr))
            self.assertIn(b"has disappeared", stderr, (cmd, delay))
            self.assertNotIn(b"Traceback", stderr)
            run = self.last_run()
            meta = self.run_meta(run)
            self.assertEqual((meta["status"], meta["exit_code"]), ("drive-gone", 1))
            self.assertIn("has disappeared", meta["error"])
            self.assertTrue(run.has("summary.txt") and run.has("report.html"))
            self.assertEqual(self.leftovers(run.folder), [])
            if cmd == "dupes":
                self.assertIn(b"no results were saved", stderr)
                self.assertFalse(run.has("files.tsv"))   # no list with holes in it
            else:
                self.assertIn(b"nothing was deleted", stderr)
                self.assertIn("nothing was deleted", meta["headline"])
                self.assertFalse(run.has("folders.tsv"))
            shutil.rmtree(run.folder)

    def test_failing_drive_stops_after_a_run_of_read_errors(self):
        data = os.urandom(5000)
        for i in range(150):
            self.write(f"d{i}/f", data)
        patch = self.failing_open(f"lambda p: p.startswith({self.root + '/'!r})")
        r = subprocess.run([sys.executable, "-c", self.wrapper(patch), "dupes", self.root],
                           capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(b"the last 100 reads all failed (Input/output error)", r.stderr)
        self.assertNotIn(b"Traceback", r.stderr)
        run = self.last_run()
        self.assertEqual(run.status, "drive-gone")
        self.assertIn("the last 100 reads all failed", self.run_meta(run)["error"])
        self.assertFalse(run.has("files.tsv"))
        with open(run.file("summary.txt")) as f:
            self.assertIn("STOPPED:", f.read())

    def test_a_few_unreadable_files_do_not_stop_the_run(self):
        data = os.urandom(5000)
        for i in range(20):
            self.write(f"d{i}/f", data)
        bad = {os.path.join(self.root, f"d{i}", "f") for i in (3, 4, 5)}
        patch = self.failing_open(f"{bad!r}.__contains__")
        r = subprocess.run([sys.executable, "-c", self.wrapper(patch), "dupes", self.root],
                           capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.run_meta()["status"], "done")
        self.assertIn(b"Errors:          3", r.stdout)
        self.assertIn(b"Input/output error", r.stdout)
        self.assertEqual(self.report_groups(),
                         [sorted(f"d{i}/f" for i in range(20) if i not in (3, 4, 5))])


if __name__ == "__main__":
    unittest.main(verbosity=2)
