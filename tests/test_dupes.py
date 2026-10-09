#!/usr/bin/env python3
"""`clean dupes`: which files are duplicates, what a run saves (files.tsv,
data.json.gz), and odd names, permissions and full disks."""

import json
import os
import sys
import unittest

from helpers import CLEAN, IS_ROOT, Base, needs_unshare


class TestDupes(Base):
    def test_basic_groups(self):
        big = os.urandom(50_000)
        self.write("a/photo.jpg", big)
        self.write("b/copy of photo.jpg", big)
        self.write("c/deep/x.bin", big)
        self.write("small1.txt", "same text")
        self.write("z/small2.txt", "same text")
        self.write("unique.txt", "only me")
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.report_groups(), [
            ["a/photo.jpg", "b/copy of photo.jpg", "c/deep/x.bin"],
            ["small1.txt", "z/small2.txt"],
        ])
        out = r.stdout.decode()
        self.assertIn("Duplicate groups: 2", out)
        self.assertIn("Redundant copies: 3", out)

    def test_same_size_different_content(self):
        a = bytearray(os.urandom(100_000))
        b = bytearray(a)
        b[-1] ^= 1           # same head, different tail
        c = bytearray(a)
        c[10] ^= 1           # different head
        self.write("a", bytes(a))
        self.write("b", bytes(b))
        self.write("c", bytes(c))
        self.write("s1", "abcd")
        self.write("s2", "abce")
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.report_groups(), [])

    def test_exactly_head_size_boundaries(self):
        for size in (4095, 4096, 4097, 8192):
            data = os.urandom(size)
            self.write(f"{size}/one", data)
            self.write(f"{size}/two", data)
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.report_groups()), 4)

    def test_empty_files_and_min_size(self):
        self.write("e1", b"")
        self.write("e2", b"")
        self.write("k1", b"k" * 2000)
        self.write("k2", b"k" * 2000)
        self.dupes(self.root)
        self.assertEqual(self.report_groups(), [["k1", "k2"]])
        self.dupes(self.root, "--min-size", "0")
        self.assertEqual(self.report_groups(), [["k1", "k2"]])     # empty files: never
        self.dupes(self.root, "--min-size", "2K")
        self.assertEqual(self.report_groups(), [])
        self.dupes(self.root, "--min-size", "1.5k")
        self.assertEqual(self.report_groups(), [["k1", "k2"]])
        r = self.dupes(self.root, "--min-size", "lots")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"invalid size", r.stderr)

    def test_hard_links_are_one_file(self):
        data = os.urandom(10_000)
        a = self.write("a", data)
        os.link(a, os.path.join(self.root, "a_link"))
        os.link(a, os.path.join(self.root, "a_link2"))
        r = self.dupes(self.root)
        self.assertEqual(self.report_groups(), [])
        self.assertIn(b"Hard links:      2", r.stdout)
        self.write("real_copy", data)
        self.dupes(self.root)
        self.assertEqual(self.report_groups(), [["a", "real_copy"]])

    def test_symlinks_ignored(self):
        data = os.urandom(10_000)
        a = self.write("a", data)
        os.symlink(a, os.path.join(self.root, "link"))
        os.symlink(os.path.join(self.root, "sub"), os.path.join(self.root, "dirlink"))
        self.write("sub/b", data)
        self.dupes(self.root)
        self.assertEqual(self.report_groups(), [["a", "sub/b"]])

    def test_unreadable_file(self):
        if IS_ROOT:
            self.skipTest("root can read anything")
        data = os.urandom(10_000)
        self.write("a", data)
        self.write("b", data)
        self.write("c", data)
        os.chmod(os.path.join(self.root, "c"), 0o000)
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"cannot read", r.stdout)
        self.assertEqual(self.report_groups(), [["a", "b"]])

    def test_across_roots_and_overlap(self):
        data = os.urandom(20_000)
        self.write("drive1/x", data)
        self.write("drive2/y", data)
        d1 = os.path.join(self.root, "drive1")
        d2 = os.path.join(self.root, "drive2")
        r = self.dupes(d1, d2, self.root)  # overlapping roots: no double counting
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.report_groups(), [["drive1/x", "drive2/y"]])
        self.assertIn(b"Files scanned:   2", r.stdout)

    def test_excludes(self):
        data = os.urandom(5_000)
        self.write("a", data)
        self.write("proj/.git/objects/ab/cdef", data)
        self.write("tmp/b.tmp", data)
        self.dupes(self.root)
        self.assertEqual(self.report_groups(), [["a", "tmp/b.tmp"]])
        self.dupes(self.root, "-e", "*.tmp")
        self.assertEqual(self.report_groups(), [])
        self.dupes(self.root, "--no-default-excludes", "-e", "*.tmp")
        self.assertEqual(self.report_groups(), [])                  # .git is hidden
        self.dupes(self.root, "--no-default-excludes", "--hidden", "-e", "*.tmp")
        self.assertEqual(self.report_groups(), [["a", "proj/.git/objects/ab/cdef"]])

    def test_data_json(self):
        from cleanup.report import data as report_data
        content = os.urandom(5_000)
        self.write("a", content)
        bad = b"b\xff".decode("utf-8", "surrogateescape")
        self.write(bad, content)
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = report_data.read(self.last_run().file(report_data.NAME))
        self.assertEqual((doc["kind"], doc["roots"]), ("dupes", [self.root]))
        self.assertEqual(len(doc["groups"]), 1)
        self.assertEqual(doc["groups"][0]["size"], 5000)
        names = report_data.paths_of(doc["dirs"])
        files = sorted(os.path.join(names[d], n) for d, n in doc["groups"][0]["files"])
        # The name that isn't valid UTF-8 comes back exactly.
        self.assertEqual(files, [os.path.join(self.root, "a"), os.path.join(self.root, bad)])
        # files.tsv shows it escaped instead.
        self.assertEqual(self.report_groups(), [["a", "b\\xff"]])

    def test_removed_options_explain_themselves(self):
        self.write("a", "same")
        self.write("b", "same")
        for args in (["--json"], ["--report", "r.tsv"], ["--report=r.tsv"]):
            r = self.run_clean("dupes", self.root, *args)
            self.assertEqual(r.returncode, 2, args)
            err = r.stderr.decode()
            self.assertIn("was removed in clean 3", err)
            self.assertIn("clean show", err)
            self.assertNotIn("Traceback", err)
        r = self.run_clean("empty", self.root, "--log", "x.tsv")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"--log was removed in clean 3", r.stderr)
        self.assertEqual(self.runs(), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "r.tsv")))

    def test_strange_names(self):
        names = ["with space", "tab\there", "new\nline", b"bad\xffbyte", "esc\x1b[31mred",
                 "ünïcødé 漢字", "-dash", "back\\slash", "'quote\"s", "<script>alert(1)",
                 "<img src=x onerror=alert(1)>",
                 "rtl\u202egpj.exe"]
        content = os.urandom(3000)
        for n in names:
            raw = os.fsencode(self.root) + b"/" + (n if isinstance(n, bytes) else n.encode())
            with open(raw, "wb") as f:
                f.write(content)
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(b"Traceback", r.stderr)
        self.assertNotIn(b"\x1b", r.stdout)
        run = self.last_run()
        for name in ("files.tsv", "summary.txt", "report.html"):
            with open(run.file(name), "rb") as f:
                saved = f.read()
            saved.decode("utf-8")
            self.assertNotIn(b"\x1b", saved, name)
            self.assertNotIn("\u202e".encode(), saved, name)
        with open(run.file("report.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertEqual(page.count("<script"), 1)      # the page's own, at the end
        self.assertTrue(page.split("<script>")[0].count("<") > 100)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        groups = self.report_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), len(names))
        self.assertIn("new\\nline", groups[0])
        self.assertIn("rtl\\u202egpj.exe", groups[0])

    def test_parallel_matches_serial(self):
        for i in range(80):
            self.write(f"t{i % 6}/m{i % 5}/f{i}", f"content {i % 23}" * (i % 23 % 4 + 1))
        self.dupes(self.root, "-j1")
        serial = self.report_groups()
        self.dupes(self.root, "-j8")
        self.assertEqual(self.report_groups(), serial)
        self.assertTrue(serial)

    def test_each_run_is_saved_with_its_files(self):
        self.write("a", "same")
        self.write("b", "same")
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = self.last_run()
        self.assertEqual(sorted(os.listdir(run.folder)),
                         ["data.json.gz", "files.tsv", "folders.tsv", "manifest.json.gz",
                          "report.html", "run.json", "summary.txt"])
        meta = self.run_meta()
        self.assertEqual((meta["status"], meta["exit_code"], meta["command"], meta["mode"]),
                         ("done", 0, "dupes", "report"))
        self.assertEqual(meta["headline"], "1 duplicate group · 1 redundant copy · 4 B reclaimable")
        self.assertEqual([x["path"] for x in meta["roots"]], [self.root])
        self.assertEqual(meta["totals"]["groups"], 1)
        with open(run.file("summary.txt")) as f:
            summary = f.read()
        self.assertIn("Duplicate groups: 1", summary)
        self.assertIn(f"Report:          {run.file('report.html')}", summary)
        self.assertIn(f"Report:          {run.file('report.html')}", r.stdout.decode())
        self.assertEqual(self.leftovers(run.folder), [])

    def test_unwritable_data_folder_fails_before_scanning(self):
        self.write("a", "same")
        self.write("b", "same")
        r = self.run_clean("dupes", self.root, env={"CLEAN_PATHS_DATA": "/proc/nope"})
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"cannot save the run in the data folder /proc/nope", r.stderr)
        self.assertNotIn(b"Traceback", r.stderr)
        self.assertNotIn(b"DUPLICATE", r.stdout)

    @needs_unshare
    def test_data_folder_disk_full(self):
        for i in range(200):
            self.write(f"d{i}/a", f"content {i}")
            self.write(f"d{i}/b", f"content {i}")
        full = os.path.join(self.tmp, "full")
        os.mkdir(full)
        script = (f'mount -t tmpfs -o size=8k none "{full}" && '
                  f'CLEAN_PATHS_DATA="{full}" "{sys.executable}" "{CLEAN}" dupes "{self.root}" -q; '
                  f'echo "rc=$?"; find "{full}" -name ".clean-*"; '
                  f'cat "{full}"/runs/*/run.json')
        r = self.run_shell(script)
        err = r.stderr.decode()
        self.assertIn("could not save", err)
        self.assertIn("No space left on device", err)
        self.assertNotIn("Traceback", err)
        out = r.stdout.decode()
        self.assertIn("rc=1", out)
        self.assertNotIn(".clean-", out)             # no temp file left behind
        meta = json.loads(out[out.index("{"):])
        self.assertEqual(meta["status"], "done")      # the run still ends properly
        self.assertEqual(meta["exit_code"], 1)
        self.assertTrue(meta["warnings"])

    def test_saved_files_follow_umask(self):
        self.write("a", "same")
        self.write("b", "same")
        self.dupes(self.root)
        umask = os.umask(0)
        os.umask(umask)
        for name in ("files.tsv", "report.html", "run.json", "summary.txt"):
            mode = os.stat(self.last_run().file(name)).st_mode & 0o777
            self.assertEqual(mode, 0o666 & ~umask, name)

    def test_file_changing_during_scan_is_not_reported(self):
        # Simulate by hashing with a size that doesn't match the file.
        from cleanup.scan import hashing
        p = self.write("f", os.urandom(10_000))
        with self.assertRaises(hashing.FileChanged):
            hashing.digest(p, 12_000, 12_000)   # shrank
        with self.assertRaises(hashing.FileChanged):
            hashing.digest(p, 8_000, 8_000)     # grew
        self.assertEqual(len(hashing.digest(p, 10_000, 10_000)), 128)


if __name__ == "__main__":
    unittest.main(verbosity=2)
