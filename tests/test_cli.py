#!/usr/bin/env python3
"""The command line itself: help, version, running through a link, and the
small parsing helpers."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from helpers import CLEAN, TOOL, Base, pretend

import cleanup
from cleanup import cli, fsutil, options
from cleanup import settings as S


class TestCli(Base):
    def test_help_and_version(self):
        for args in (["-h"], ["--help"], ["empty", "-h"], ["dupes", "--help"]):
            r = self.run_clean(*args)
            self.assertEqual(r.returncode, 0, args)
            self.assertIn(b"usage: clean", r.stdout)
        r = self.run_clean("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn(cleanup.__version__.encode(), r.stdout)
        r = self.run_clean()
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"usage: clean", r.stdout)
        r = self.run_clean("bogus")
        self.assertEqual(r.returncode, 2)
        r = self.run_clean("empty", self.root, "--dry-run", "--delete")
        self.assertEqual(r.returncode, 2)

    def test_runs_through_a_symlink_from_any_cwd(self):
        link = os.path.join(self.tmp, "bin", "clean")
        os.makedirs(os.path.dirname(link))
        os.symlink(CLEAN, link)
        r = subprocess.run([link, "--version"], capture_output=True, cwd="/", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_python_m_cleanup(self):
        r = subprocess.run([sys.executable, "-B", "-m", "cleanup", "--version"],
                           capture_output=True, cwd=TOOL, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode(), f"clean {cleanup.__version__}\n")
        r = subprocess.run([sys.executable, "-B", "-m", "cleanup", "empty", self.root],
                           capture_output=True, cwd="/", env=dict(self.env, PYTHONPATH=TOOL))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_meta()["cwd"], "/")

    def test_no_bytecode_left_in_the_tool_folder(self):
        copy = os.path.join(self.tmp, "tool")
        os.mkdir(copy)
        shutil.copy2(CLEAN, copy)
        shutil.copytree(os.path.join(TOOL, "cleanup"), os.path.join(copy, "cleanup"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        env = {k: v for k, v in self.env.items() if k != "PYTHONDONTWRITEBYTECODE"}
        self.mk("e")
        self.write("a", "same")
        self.write("b", "same")
        for args in (["--help"], ["empty", self.root], ["dupes", self.root], ["config"],
                     ["config", "set", "empty.jobs", "2"], ["history"], ["show"],
                     ["history", "--prune"]):
            r = subprocess.run([sys.executable, os.path.join(copy, "clean"), *args],
                               capture_output=True, env=env)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
        found = [d for d, dirs, _ in os.walk(copy) if "__pycache__" in dirs]
        self.assertEqual(found, [])

    def test_help_names_the_settings(self):
        out = self.run_clean("empty", "-h").stdout.decode()
        self.assertIn("setting empty.jobs", out)
        self.assertIn("clean show", out)
        self.assertIn("clean history", out)
        out = " ".join(self.run_clean("-h").stdout.decode().split())
        self.assertIn(f"the full lists, in {self.data_dir}/runs `clean history`", out)
        self.assertIn(f"{self.data_dir}/index.html", out)
        out = " ".join(self.run_clean("dupes", "-h").stdout.decode().split())
        self.assertIn("setting dupes.min_size", out)
        self.assertIn("setting dupes.jobs", out)
        out = self.run_clean("-h").stdout.decode()
        self.assertIn("clean config", out)
        out = self.run_clean("config", "-h").stdout.decode()
        self.assertIn("CLEAN_DUPES_MIN_SIZE", out)

    def test_help_shows_values_from_the_settings(self):
        env = {"CLEAN_DUPES_MIN_SIZE": "1M", "CLEAN_SCAN_EXCLUDE": "node_modules,*.tmp",
               "CLEAN_SCAN_ONE_FILE_SYSTEM": "yes", "CLEAN_SCAN_DEFAULT_EXCLUDES": "no",
               "CLEAN_DUPES_JOBS": "4"}
        out = self.run_clean("dupes", "-h", env=env).stdout.decode()
        flat = " ".join(out.split())
        self.assertIn("default: 1.0 MB; setting dupes.min_size", flat)
        self.assertIn("default: 4; setting dupes.jobs", flat)
        self.assertIn("(setting scan.exclude): node_modules, *.tmp", flat)
        self.assertIn("--no-one-file-system", flat)
        self.assertIn("--default-excludes", flat)
        out = " ".join(self.run_clean("dupes", "-h").stdout.decode().split())
        self.assertNotIn("--no-one-file-system", out)
        self.assertNotIn("--default-excludes ", out)

    def test_bad_setting_warns_and_the_command_still_runs(self):
        self.mk("gone")
        r = self.empty(self.root, env={"CLEAN_EMPTY_JOBS": "many"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.decode().count("clean: warning: CLEAN_EMPTY_JOBS='many'"),
                         1, r.stderr)
        self.assertIn(b"gone", r.stdout)
        os.makedirs(os.path.dirname(self.settings_file))
        with open(self.settings_file, "w") as f:
            json.dump({"dupes.min_size": "huge", "typo.key": 1}, f)
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        err = r.stderr.decode()
        self.assertIn("setting dupes.min_size in", err)
        self.assertIn("unknown setting 'typo.key'", err)
        # An invalid --jobs on the command line is still an error.
        self.assertEqual(self.empty(self.root, "-j", "0").returncode, 2)

    def test_min_size_from_settings_and_the_command_line_wins(self):
        self.write("small1", "12345")
        self.write("small2", "12345")
        self.write("big1", "x" * 20)
        self.write("big2", "x" * 20)
        env = {"CLEAN_DUPES_MIN_SIZE": "10"}
        self.assertEqual(self.dupes(self.root, env=env).returncode, 0)
        self.assertEqual(self.report_groups(), [["big1", "big2"]])
        self.dupes(self.root, "--min-size", "1", env=env)
        self.assertEqual(self.report_groups(), [["big1", "big2"], ["small1", "small2"]])
        S.atomic_write(self.settings_file, b'{"dupes.min_size": "16"}')
        self.dupes(self.root)
        self.assertEqual(self.report_groups(), [["big1", "big2"]])
        self.dupes(self.root, env={"CLEAN_DUPES_MIN_SIZE": "0"})
        self.assertEqual(self.report_groups(), [["big1", "big2"], ["small1", "small2"]])

    def test_exclude_setting_adds_to_e(self):
        self.mk("a", "node_modules", "x")
        self.mk("b", "build")
        self.mk("c", "gone")
        self.run_clean("config", "add", "scan.exclude", "node_modules")
        r = self.empty(self.root, "--delete", "-e", "build")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(), {"a/", "a/node_modules/", "a/node_modules/x/",
                                       "b/", "b/build/"})

    def test_default_excludes_setting(self):
        self.mk("proj", ".git", "refs")
        self.write("proj/.git/HEAD", "x")
        env = {"CLEAN_SCAN_DEFAULT_EXCLUDES": "false"}
        self.empty(self.root, "--delete", "--default-excludes", env=env)
        self.assertIn("proj/.git/refs/", self.tree())
        self.empty(self.root, "--delete", env=env)
        self.assertEqual(self.tree(), {"proj/", "proj/.git/", "proj/.git/HEAD"})

    def test_settings_become_option_defaults(self):
        env = {"CLEAN_SCAN_ONE_FILE_SYSTEM": "on", "CLEAN_EMPTY_JOBS": "3",
               "CLEAN_SCAN_DEFAULT_EXCLUDES": "off"}
        with mock.patch.dict(os.environ, env):
            S.reload()
            try:
                parser = cli.build_parser()
                a = parser.parse_args(["empty", "x"])
                self.assertEqual((a.one_file_system, a.jobs, a.default_excludes),
                                 (True, 3, False))
                a = parser.parse_args(["empty", "x", "--no-one-file-system", "-j", "1",
                                       "--default-excludes"])
                self.assertEqual((a.one_file_system, a.jobs, a.default_excludes),
                                 (False, 1, True))
                a = parser.parse_args(["dupes", "x"])
                self.assertEqual((a.one_file_system, a.jobs, a.min_size), (True, 1, 0))
            finally:
                S.reload()
        a = cli.build_parser().parse_args(["empty", "x"])
        self.assertEqual((a.one_file_system, a.jobs, a.default_excludes), (False, 1, True))

    def test_runs_go_to_the_data_folder(self):
        self.mk("gone")
        self.write("a", "same")
        self.write("b", "same")
        r = self.run_clean("empty", self.root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deleted(), ["gone"])
        r = self.run_clean("dupes", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.report_groups(), [["a", "b"]])
        self.assertEqual(sorted(os.listdir(self.data_dir)), [".index.lock", "index.html", "lock", "runs"])
        self.assertEqual([run.label for run in self.runs()], ["dupes", "empty --delete"])
        # paths.data moves them; ~ is the home folder.
        home = os.path.join(self.tmp, "home")
        self.run_clean("config", "set", "paths.data", "~/clean-data", env={"HOME": home})
        self.mk("gone2")
        r = self.run_clean("empty", self.root, "--delete", env={"HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        moved = os.path.join(home, "clean-data")
        self.assertEqual(self.deleted(self.last_run(moved)), ["gone2"])
        self.assertTrue(os.path.isfile(os.path.join(moved, "index.html")))
        self.assertEqual(len(self.runs()), 2)

    def test_the_data_folder_is_never_scanned(self):
        # Data kept inside the scanned folder: its runs aren't duplicates or
        # empty folders of the user's.
        self.run_clean("config", "set", "paths.data", os.path.join(self.root, "data"))
        self.write("a", "same")
        self.write("b", "same")
        for _ in range(2):
            r = self.run_clean("dupes", self.root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(b"Duplicate groups: 1", r.stdout)
        os.makedirs(os.path.join(self.root, "data", "runs", "x", "empty"))
        r = self.run_clean("empty", self.root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "data", "runs", "x", "empty")))

    def test_parse_size(self):
        self.assertEqual(options.parse_size("0"), 0)
        self.assertEqual(options.parse_size("4096"), 4096)
        self.assertEqual(options.parse_size("1k"), 1024)
        self.assertEqual(options.parse_size("10M"), 10 << 20)
        self.assertEqual(options.parse_size("2GiB"), 2 << 30)
        self.assertEqual(options.parse_size("1.5G"), 3 << 29)

    def test_printable(self):
        self.assertEqual(fsutil.printable("plain/ünï"), "plain/ünï")
        self.assertEqual(fsutil.printable("a\tb\nc"), "a\\tb\\nc")
        self.assertEqual(fsutil.printable(b"x\xff".decode("utf-8", "surrogateescape")), "x\\xff")
        self.assertEqual(fsutil.printable("e\x1b["), "e\\x1b[")
        self.assertEqual(fsutil.printable("n\x85c\x9b"), "n\\u0085c\\u009b")
        self.assertEqual(fsutil.printable("nb\u00a0sp"), "nb\u00a0sp")

    def test_progress_on_a_terminal_that_does_not_know_its_size(self):
        import io
        out = io.StringIO()
        out.isatty = lambda: True
        for cols, shown in ((0, "removing: 0 of 6 done"), (12, "removing: 0")):
            with mock.patch.object(sys, "stderr", out), \
                    mock.patch.object(fsutil.shutil, "get_terminal_size",
                                      return_value=os.terminal_size((cols, 0))):
                out.seek(0)
                out.truncate()
                fsutil.Progress(False).show("removing: 0 of 6 done")
            self.assertEqual(out.getvalue(), "\r\033[K" + shown)


class TestSupported(Base):
    """Where clean can't run (another system, an older Python) it says so
    plainly, before anything else loads."""

    PRETEND = {"sys.platform = 'darwin'": "clean runs on Linux only; this system is darwin.",
               "sys.version_info = (3, 8, 10)":
                   "clean needs Python 3.10 or newer; this is Python 3.8."}

    def test_reasons(self):
        self.assertIsNone(cleanup.unsupported())
        self.assertIsNone(cleanup.unsupported((3, 10, 0), "linux"))
        self.assertIsNone(cleanup.unsupported((4, 0), "linux"))
        for platform in ("darwin", "win32", "cygwin", "freebsd14"):
            self.assertTrue(cleanup.unsupported(platform=platform).startswith(
                f"clean runs on Linux only; this system is {platform}."))
        self.assertEqual(cleanup.unsupported((3, 9, 18), "linux"),
                         "clean needs Python 3.10 or newer; this is Python 3.9.")

    def test_the_command_refuses(self):
        for code, reason in self.PRETEND.items():
            env = dict(self.env, **pretend(self.tmp, code))
            for cmd in ([CLEAN, "dupes", self.tmp], [sys.executable, "-m", "cleanup", "space"]):
                with self.subTest(code=code, cmd=cmd[1]):
                    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd="/")
                    self.assertEqual((r.returncode, r.stdout), (2, ""), r.stderr)
                    self.assertTrue(r.stderr.startswith(reason), r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.env["XDG_DATA_HOME"], "clean")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
