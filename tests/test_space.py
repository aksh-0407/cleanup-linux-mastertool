"""
Tests for `clean space` (cleanup/space.py, caches.py, commands/space.py,
report/space.py) and `clean cache` (scan/cache.py, commands/cache.py).

The engine is tested in this process on fixture trees. Every `clean space` run
goes through a sealed namespace: it lists the Trash of every drive, so it runs
where the only Trash is the test's own. Documents built in this process get
an empty Trash list instead.
"""

import ast
import gzip
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import CLEAN, IS_ROOT, TOOL, Base, blob, needs_unshare
from test_report import PageTest

from cleanup import caches, runs
from cleanup import settings as _settings
from cleanup import space as _space
from cleanup.actions import trash as _trash
from cleanup.commands import space as _space_cmd
from cleanup.errors import Cancelled
from cleanup.fsutil import CANCEL, Excluder, human
from cleanup.report import data
from cleanup.report import space as _report
from cleanup.report.html import esc
from cleanup.scan import cache as _cache

MB = 1 << 20
OLD = time.time() - 30 * 86400
NASTY = "<img src=x onerror=alert(1)>"


def du(top):
    """What `du` says `top` takes on the disk (each inode once, one drive)."""
    out = subprocess.run(["du", "-sxB1", top], capture_output=True, check=True).stdout
    return int(out.split()[0])


def blocks(path):
    return os.lstat(path).st_blocks * 512


def state(top):
    """{relative path: (mode, inode, size, mtime, links)} of everything under
    `top`, folders and symlinks included: what a read-only run must leave as
    it was."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(top):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            out[os.path.relpath(path, top)] = (st.st_mode, st.st_ino, st.st_size,
                                               st.st_mtime_ns, st.st_nlink)
    return out


def roundtrip(doc):
    """The document as a later reader gets it back from data.json.gz."""
    return json.loads(gzip.decompress(data.encode(doc)).decode("utf-8"))


class EngineBase(Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(self.root)     # the scan reports real paths

    def p(self, rel=""):
        return os.path.join(self.root, rel) if rel else self.root

    def scan(self, *roots, exclude=(), **kw):
        s = _space.Scan(Excluder(exclude, False), **kw)
        s.run([os.path.realpath(r) for r in roots] or [self.root])
        return s

    def at(self, s, rel=""):
        """The folder number of `rel` (under root) in scan `s`."""
        hits = [i for i in range(len(s.parent)) if s.path(i) == self.p(rel)]
        self.assertEqual(len(hits), 1, rel)
        return hits[0]

    def names(self, s, clutter):
        """The listed items of one kind of clutter, as paths relative to root."""
        return sorted(os.path.relpath(os.path.join(s.path(item[0]), item[1]), self.root)
                      for item in clutter.items)


# ---------------------------------------------------------------- sizes

class TestSizes(EngineBase):
    def test_sizes_are_what_du_says(self):
        self.write("a/one.bin", os.urandom(300_000))
        self.write("a/b/two.bin", blob("two", 70_000))
        self.write("a/b/c/tiny", "x")
        self.write("d/e/f/g/deep", blob("deep", 9000))
        self.mk("empty", "inner")
        os.symlink("a/one.bin", self.p("link"))
        os.link(self.p("a/one.bin"), self.p("d/hard"))
        with open(self.p("sparse"), "wb") as f:
            f.truncate(20 * MB)
        s = self.scan()
        self.assertEqual(s.total(), du(self.root))
        root = self.at(s)
        self.assertEqual(s.tdisk[root], s.total())
        self.assertEqual(s.tfiles[root], 7)         # every name, the links too
        self.assertEqual(s.tdirs[root], 9)
        a = self.at(s, "a")
        self.assertEqual(s.tdisk[a], du(self.p("a")))
        self.assertEqual((s.files[a], s.tfiles[a]), (1, 3))
        self.assertEqual((s.errors, s.mounts, s.excluded_n), ([], [], 0))

    def test_a_sparse_file_counts_what_it_uses(self):
        with open(self.p("sparse.img"), "wb") as f:
            f.truncate(50 * MB)
            f.write(b"data")
        s = self.scan()
        root = self.at(s)
        self.assertEqual(s.tsize[root], 50 * MB)
        self.assertEqual(s.tdisk[root], blocks(self.root) + blocks(self.p("sparse.img")))
        self.assertLess(s.tdisk[root], MB)

    def test_hard_links_count_once(self):
        a = self.write("A/a.bin", os.urandom(2 * MB))
        self.mk("B")
        os.link(a, self.p("B/b.bin"))
        os.link(a, self.p("A/again.bin"))
        size = blocks(a)
        s = self.scan()
        dirs = sum(blocks(self.p(x)) for x in ("", "A", "B"))
        self.assertEqual(s.total(), dirs + size)
        self.assertEqual(s.total(), du(self.root))
        self.assertEqual((s.link_names, s.link_bytes), (2, 2 * size))
        # It counts in one folder, the first one met
        own = [s.disk[self.at(s, x)] - blocks(self.p(x)) for x in ("A", "B")]
        self.assertEqual(sorted(own), [0, size])
        self.assertEqual(s.tfiles[self.at(s)], 3)
        # Two roots sharing it: still once
        s = self.scan(self.p("A"), self.p("B"))
        self.assertEqual(s.total(), blocks(self.p("A")) + blocks(self.p("B")) + size)
        self.assertEqual(s.link_names, 2)

    def test_a_link_from_outside_counts_here(self):
        a = self.write("in/a.bin", os.urandom(MB))
        os.link(a, os.path.join(self.tmp, "outside.bin"))
        s = self.scan()
        self.assertEqual(s.total(), blocks(self.root) + blocks(self.p("in")) + blocks(a))
        self.assertEqual(s.link_names, 0)
        i = self.at(s, "in")
        self.assertEqual((s.tsolo[i], s.tshared[i]), (0, blocks(a)))

    def test_biggest_files_and_the_folders_kept(self):
        for n, size in enumerate((5, 4, 3, 2, 1)):
            self.write(f"Big/f{n}.bin", os.urandom(size * MB // 2))
        self.write("Big/Inner/x.bin", os.urandom(MB + 1000))
        self.write("Small/x", "x")
        with mock.patch.object(_space, "TOP_FILES", 3):
            s = self.scan()
        self.assertEqual([t[3] for t in s.top_files()], ["f0.bin", "f1.bin", "f2.bin"])
        # 1 MB or more: kept per folder for the drill-down
        self.assertEqual([t[2] for t in s.big[self.at(s, "Big")]],
                         ["f0.bin", "f1.bin", "f2.bin", "f3.bin"])
        self.assertEqual([t[2] for t in s.big[self.at(s, "Big/Inner")]], ["x.bin"])
        kept = s.kept()
        self.assertEqual(s.keep_bar, _space.KEEP_MIN)
        self.assertEqual({s.path(i) for i in kept},
                         {self.root, self.p("Big"), self.p("Big/Inner")})
        with mock.patch.object(_space, "MAX_FOLDERS", 2):
            kept = s.kept()
        self.assertEqual({s.path(i) for i in kept}, {self.root, self.p("Big")})
        self.assertGreater(s.keep_bar, _space.KEEP_MIN)
        with mock.patch.object(_space, "MAX_FOLDERS", 1):
            self.assertEqual([s.path(i) for i in s.kept()], [self.root])   # roots always
        for i in kept:              # a kept folder's parent is kept too
            self.assertTrue(s.parent[i] < 0 or s.parent[i] in kept)

    def test_excluded_entries_are_not_counted(self):
        self.write("disc.iso", os.urandom(MB))
        self.write("Skip/inside.bin", os.urandom(MB))
        self.write("Keep/k.txt", "k")
        self.write("Only/b.iso", "x")
        s = self.scan(exclude=["*.iso", self.p("Skip")])
        self.assertEqual(s.excluded_n, 3)
        self.assertTrue(s.tflags[self.at(s)] & _space.EXCLUDED)
        self.assertTrue(s.flags[self.at(s, "Only")] & _space.EXCLUDED)
        self.assertFalse(s.flags[self.at(s, "Keep")] & _space.EXCLUDED)
        self.assertEqual(s.total(), du(self.root) - blocks(self.p("disc.iso"))
                         - du(self.p("Skip")) - blocks(self.p("Only/b.iso")))
        self.assertFalse(s.empty[self.at(s, "Only")])   # something is there

    @unittest.skipIf(IS_ROOT, "root reads everything")
    def test_unreadable_folders(self):
        self.write("Locked/inner/x.bin", os.urandom(1000))
        self.write("Open/y", "y")
        os.chmod(self.p("Locked"), 0)
        s = self.scan()
        locked = self.at(s, "Locked")
        self.assertTrue(s.flags[locked] & _space.UNREADABLE)
        self.assertTrue(s.tflags[self.at(s)] & _space.UNREADABLE)
        self.assertFalse(s.tflags[self.at(s, "Open")] & _space.UNREADABLE)
        self.assertEqual([(p, m.split(":")[0]) for p, m in s.errors],
                         [(self.p("Locked"), "cannot read folder")])
        self.assertFalse(s.empty[locked])           # can't tell: never called empty

    def test_cancel(self):
        self.mk("a", "b")
        CANCEL.set()
        try:
            with self.assertRaises(Cancelled):
                self.scan()
        finally:
            CANCEL.clear()


# ---------------------------------------------------------------- clutter

class TestClutter(EngineBase):
    def test_zero_byte_files(self):
        for rel in ("Docs/zero.txt", "Docs/again", "pkg/__init__.py", "Docs/.keep",
                    "Docs/CACHEDIR.TAG", "Code/node_modules/x/empty.js", ".config/app/z",
                    "Proj/.git/zero", "venv/lib/z.py", "Docs/x.tmp"):
            self.write(rel, b"")
        self.write("venv/pyvenv.cfg", "home = /usr")
        self.write("Docs/full.txt", "not empty")
        s = self.scan(quiet_names=(".git",))
        self.assertEqual(self.names(s, s.zero), ["Docs/again", "Docs/zero.txt"])
        # Markers, dot-files, and files in program, package and environment
        # folders: counted, not listed. Inside .git: not looked at. x.tmp is a
        # temp leftover.
        self.assertEqual((s.zero.count, s.zero.unlisted, s.zero.bytes), (8, 6, 0))
        self.assertEqual(self.names(s, s.temp), ["Docs/x.tmp"])
        self.assertEqual(s.look[self.at(s, "venv")], _space.APP)
        self.assertEqual(s.look[self.at(s, "Proj/.git")], _space.QUIET)

    def test_broken_symlinks(self):
        self.write("real.txt", "r")
        self.mk("folder")
        for name, target in (("gone", "nowhere"), ("loop", "loop"), ("ok", "real.txt"),
                             ("dir-link", "folder"), ("through-file", "real.txt/x"),
                             ("absolute", "/nonexistent/for/sure")):
            os.symlink(target, self.p(name))
        self.mk("node_modules")
        os.symlink("nowhere", self.p("node_modules/gone"))
        s = self.scan()
        found = sorted((name, target, reason) for _, name, target, reason in s.broken.items)
        self.assertEqual(found, [("absolute", "/nonexistent/for/sure", "missing"),
                                 ("gone", "nowhere", "missing"), ("loop", "loop", "a loop"),
                                 ("through-file", "real.txt/x", "missing")])
        self.assertEqual((s.broken.count, s.broken.unlisted), (5, 1))
        self.assertEqual(s.tdirs[self.at(s)], 2)    # a link to a folder isn't followed
        self.assertEqual(s.tfiles[self.at(s)], 8)   # symlinks count as files

    def test_temp_leftovers(self):
        names = ("x.tmp", "Y.TEMP", "~$Report.docx", ".~lock.notes.odt#", "a.Tmp")
        for name in names:
            self.write(f"Docs/{name}", blob(name, 5000))
        for name in ("keep.tmpl", "tmp", "temp.txt", "my~$file", "lock.~x"):
            self.write(f"Docs/{name}", "not temp")
        self.write("node_modules/t.tmp", "t")
        os.utime(self.p("Docs/x.tmp"), (OLD, OLD))
        s = self.scan()
        self.assertEqual(self.names(s, s.temp), sorted(f"Docs/{n}" for n in names))
        self.assertEqual((s.temp.count, s.temp.unlisted), (6, 1))
        self.assertEqual(s.temp.bytes, sum(blocks(self.p(f"Docs/{n}")) for n in names)
                         + blocks(self.p("node_modules/t.tmp")))
        mtimes = {name: mtime for _, name, _, mtime in s.temp.items}
        self.assertEqual(int(mtimes["x.tmp"]), int(OLD))
        self.assertTrue(_space.is_temp("~$x"))
        self.assertFalse(_space.is_temp("x~$"))

    def test_empty_folders(self):
        self.mk("Empty", "a", "b")
        self.mk("Full", files=["f"])
        self.mk("Proj", ".git")                     # .git is left alone: Proj isn't empty
        self.mk("Home", "Desktop")                  # the desktop keeps it
        self.write("Code/main.py", "print()")
        self.mk("Code", "node_modules", "pkg", "empty")
        self.mk(".Trash-1000", "files")
        self.write("Ex/skip.iso", "x")
        s = self.scan(exclude=["*.iso"], quiet_names=(".git",),
                      keep_paths={self.p("Home/Desktop")})
        count, unlisted, out, more = s.empty_folders()
        self.assertEqual([(s.path(i), inner) for i, inner in out], [(self.p("Empty"), 2)])
        # Empty, a, b; and node_modules, pkg, empty inside a package folder
        self.assertEqual((count, unlisted, more), (6, 3, 0))
        for rel in ("", "Full", "Proj", "Proj/.git", "Home", "Home/Desktop", "Code",
                    ".Trash-1000", ".Trash-1000/files", "Ex"):
            self.assertFalse(s.empty[self.at(s, rel)], rel)
        # Without those: Proj (with an empty .git) and Home are empty too
        with mock.patch.object(_space, "LIST_MAX", 0):
            s = self.scan()
            self.assertEqual(s.empty_folders()[3], 3)

    def test_nothing_is_looked_for_in_trash_and_own_data(self):
        for top in (".Trash-1000/files", "own", "xdg/data/Trash/files"):
            self.write(f"{top}/zero", b"")
            self.write(f"{top}/old.tmp", "t")
            os.symlink("nowhere", self.p(f"{top}/gone"))
            self.mk(top, "empty")
        s = self.scan(quiet_paths={self.p("own")})
        # The home Trash is only one where XDG_DATA_HOME says
        self.assertEqual((s.zero.count, s.temp.count, s.broken.count), (1, 1, 1))
        self.assertEqual(self.names(s, s.zero), ["xdg/data/Trash/files/zero"])
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.p("xdg/data")}):
            s = self.scan(quiet_paths={self.p("own")})
        self.assertEqual((s.zero.count, s.temp.count, s.broken.count), (0, 0, 0))
        self.assertEqual(s.empty_folders()[0], 0)
        self.assertEqual(s.total(), du(self.root))   # all counted all the same

    def test_the_lists_stop_at_list_max(self):
        for n in range(7):
            self.write(f"z{n}", b"")
        with mock.patch.object(_space, "LIST_MAX", 4):
            s = self.scan()
        self.assertEqual((s.zero.count, len(s.zero.items), s.zero.more), (7, 4, 3))


# ---------------------------------------------------------------- caches

class TestCaches(EngineBase):
    def test_known_caches_are_measured(self):
        cache = self.p("xdg-cache")
        self.write("xdg-cache/pip/http/a", os.urandom(300_000))
        self.write("xdg-cache/thumbnails/large/t.png", os.urandom(50_000))
        self.write("xdg-cache/someapp/blob", os.urandom(80_000))
        self.write("xdg-cache/clean/hashes.sqlite", os.urandom(60_000))
        conda = self.write("miniconda3/pkgs/numpy/lib.so", os.urandom(200_000))
        self.mk("miniconda3/envs/a")
        os.link(conda, self.p("miniconda3/envs/a/lib.so"))
        self.write(".cache/pip/x", os.urandom(10_000))      # ~/.cache too
        with mock.patch.dict(os.environ, {"HOME": self.root, "XDG_CACHE_HOME": cache}):
            own = self.p("xdg-cache/clean")
            s = self.scan(watch_paths=caches.watch_paths() | {own})
            found = {e["key"]: e for e in caches.user_caches(s, skip=[own])}
        pip = found["pip"]
        self.assertEqual(pip["paths"], [self.p("xdg-cache/pip"), self.p(".cache/pip")])
        self.assertEqual(pip["disk"], du(self.p("xdg-cache/pip")) - blocks(self.p("xdg-cache/pip"))
                         - blocks(self.p("xdg-cache/pip/http")) + blocks(self.p(".cache/pip/x")))
        self.assertEqual(pip["frees"], pip["disk"])
        self.assertEqual((pip["command"], pip["commands"]), ("pip cache purge",
                                                             ["pip cache purge"]))
        self.assertIsNone(found["thumbnails"]["command"])
        self.assertIn("safe", found["thumbnails"]["note"])
        # conda's files that an environment also uses free nothing when cleared
        c = found["conda"]
        self.assertEqual((c["disk"], c["shared"], c["frees"]),
                         (blocks(conda), blocks(conda), 0))
        self.assertEqual(c["command"], "conda clean --all")
        # The rest of the cache folder, without clean's own and the known ones
        other = found["other:" + cache]
        self.assertEqual((other["disk"], other["files"]),
                         (blocks(self.p("xdg-cache/someapp/blob")), 1))
        self.assertEqual(list(found)[0], "pip")     # biggest first
        self.assertNotIn("other:" + self.p(".cache"), found)   # nothing else there

    def test_measure_counts_links_once(self):
        a = self.write("sys/a.bin", os.urandom(100_000))
        os.link(a, self.p("sys/b.bin"))
        c = self.write("sys/sub/c.bin", os.urandom(30_000))
        self.assertEqual(caches.measure(self.p("sys")),
                         {"disk": blocks(a) + blocks(c), "frees": blocks(c),
                          "shared": blocks(a), "files": 3, "partial": False})
        self.assertTrue(caches.measure(self.p("nope"))["partial"])

    def test_system_caches_of_any_distribution(self):
        """Only the package managers whose cache folder exists are listed."""
        dnf = self.write("var/cache/dnf/fedora/packages/a.rpm", os.urandom(40_000))
        self.mk("var/cache/pacman/pkg")             # there, but empty: not listed
        self.mk("var/cache/apt/archives/partial")   # nothing readable in it: not listed
        table = [(key, label, self.p(path.lstrip("/")), command, note)
                 for key, label, path, command, note in caches.SYSTEM]
        real = caches.measure

        def measure(path):
            if path.endswith("apt/archives"):       # as apt's root-only partial/ makes it
                return dict(real(path), partial=True)
            return real(path)
        with mock.patch.object(caches, "SYSTEM", table), \
                mock.patch.object(caches, "measure", measure), \
                mock.patch.object(caches, "old_snaps", return_value=[]):
            found = caches.system_caches()
        self.assertEqual([e["key"] for e in found], ["dnf"])
        self.assertEqual((found[0]["disk"], found[0]["command"], found[0]["system"]),
                         (blocks(dnf), "sudo dnf clean packages", True))
        self.assertEqual({e[0] for e in caches.SYSTEM},
                         {"journal", "apt", "dnf", "dnf5", "pacman", "zypper"})

    def test_old_snaps(self):
        listing = (b"Name    Version   Rev    Tracking       Publisher   Notes\n"
                   b"core22  20240111  1122   latest/stable  canonical** base,disabled\n"
                   b"core22  20240408  1380   latest/stable  canonical** base\n"
                   b"firefox 124.0     4033   latest/stable  mozilla**   disabled\n"
                   b"bad;rm  1         1      latest/stable  x           disabled\n"
                   b"short disabled\n")
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, listing, b"")
        self.assertEqual(caches.old_snaps(run), [("core22", "1122", 0), ("firefox", "4033", 0)])
        self.assertEqual(calls, [["snap", "list", "--all"]])

        def missing(argv, **kw):
            raise FileNotFoundError("snap")
        self.assertEqual(caches.old_snaps(missing), [])
        self.assertEqual(caches.old_snaps(
            lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"")), [])


# ---------------------------------------------------------------- the report

class ReportBase(EngineBase):
    """Space documents built in this process, with this test's settings and
    data folders, no Trash and no system caches."""

    def setUp(self):
        super().setUp()
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        _settings.reload()

        def restore():
            os.environ.clear()
            os.environ.update(saved)
            _settings.reload()
        self.addCleanup(restore)
        for patch in (mock.patch.object(_trash, "all_trashes", lambda vols=None: []),
                      mock.patch.object(_space_cmd, "_on_system_disk", lambda roots: False)):
            patch.start()
            self.addCleanup(patch.stop)

    def doc(self, *roots, **kw):
        roots = [os.path.realpath(r) for r in roots] or [self.root]
        s = self.scan(*roots, **kw)
        return _space_cmd.build(s, roots, self.data_dir, os.path.join(self.tmp, "c"))


class TestReport(ReportBase):
    def test_the_document(self):
        self.write("A/big.bin", os.urandom(2 * MB))
        self.write("A/small", "s")
        self.write("A/B/b.bin", os.urandom(MB + 4096))
        self.write("A/tiny/x", "x")
        self.write("C/c", "c")
        doc = roundtrip(self.doc())
        d = _report.Doc(doc)
        self.assertEqual([d.path(n) for n in range(len(d.folders))],
                         [self.root, self.p("A"), self.p("A/B")])
        root, a, b = range(3)
        self.assertEqual(d.kids[root], [a])
        f = d.folders[a]
        self.assertEqual((f[_report.FILES], f[_report.OWN_FILES], f[_report.DIRS]), (4, 2, 2))
        # What the shown folders don't account for: those too small for the
        # table (tiny), and the hidden ones
        self.assertEqual(d.more(a), (du(self.p("A/tiny")), 1))
        self.assertEqual(d.more(a, {b}), (du(self.p("A/tiny")) + du(self.p("A/B")), 2))
        self.assertEqual(d.more(root), (du(self.p("C")), 1))
        self.assertEqual(d.more(b), (0, 0))
        self.assertEqual(doc["total"], {"disk": du(self.root), "size": 2 * MB + MB + 4096 + 3,
                                        "files": 5, "dirs": 4})
        self.assertEqual(sorted(x[1] for x in doc["big"]), ["b.bin", "big.bin"])
        self.assertEqual(d.item_path(doc["top"][0]), self.p("A/big.bin"))
        self.assertEqual(doc["keep_min"], _space.KEEP_MIN)
        self.assertIsNone(doc["system_caches"])
        self.assertEqual((doc["trash"], doc["dupes"], doc["mounts"]), ([], None, []))
        self.assertEqual(_report.totals(doc), {"disk": du(self.root), "files": 5, "folders": 4,
                                               "caches": 0, "trash": 0, "clutter": 0,
                                               "errors": 0})
        self.assertEqual(_report.headline(doc), f"{human(du(self.root))} in {self.root}")

    def tree_doc(self):
        for n in range(6):
            self.write(f"T/k{n}/f.bin", os.urandom((n + 3) * MB // 2))
        doc = self.doc()
        d = _report.Doc(doc)
        return doc, d, d.kids[d.roots()[0]][0]

    def test_the_tree(self):
        doc, d, t = self.tree_doc()
        self.assertEqual([d.folders[k][_report.NAME] for k in d.kids[t]],
                         [f"k{n}" for n in range(5, -1, -1)])       # biggest first
        full = _report.tree_html(d)
        for n in range(6):
            self.assertIn(f">k{n}/<", full)
        self.assertNotIn("more folder", full)
        self.assertIn('<details class="node" open>', full)          # the root is open
        # A budget of 4 folders: the root, T and its two biggest
        cut = _report.tree_html(d, budget=4, focus=lambda n: f"/x?dir={n}")
        self.assertIn(f'<a href="/x?dir={t}">+ 4 more folders</a>', cut)
        self.assertIn(">k5/<", cut)
        self.assertIn(">k4/<", cut)
        self.assertNotIn(">k3/<", cut)
        plain = _report.tree_html(d, budget=4)
        self.assertIn("+ 4 more folders", plain)
        self.assertNotIn("<a ", plain)
        # One folder and what is inside it
        alone = _report.tree_html(d, start=t)
        self.assertIn(f'<span class="nm">{esc(self.p("T"))}', alone)
        self.assertNotIn(f'<span class="nm">{esc(self.root)}</span>', alone)
        self.assertIn(">k0/<", alone)

    def test_crumbs(self):
        doc, d, t = self.tree_doc()
        k = d.kids[t][0]

        def focus(n):
            return "/r" if n is None else f"/r?dir={n}"
        page = _report.sections(doc, d, focus=focus, start=k)
        self.assertIn(f'<p class="crumbs"><a href="/r">All</a> / <a href="/r?dir=0">'
                      f'{esc(self.root)}</a> / <a href="/r?dir={t}">T</a></p>', page)
        page = _report.sections(doc, d, focus=focus, start=0)
        self.assertIn('<p class="crumbs"><a href="/r">All</a></p>', page)
        self.assertNotIn("crumbs", _report.sections(doc, d))

    def test_deep_folders(self):
        deep = os.path.join(*["d"] * 400)
        self.write(os.path.join(deep, "f.bin"), os.urandom(MB + 4096))
        doc = roundtrip(self.doc())
        d = _report.Doc(doc)
        self.assertEqual(len(d.folders), 401)
        self.assertEqual(d.path(400), self.p(deep))
        page = _report.tree_html(d)
        self.assertEqual(page.count("<details"), 401)
        self.assertEqual(page.count("</details>"), 401)
        self.assertIn(f'class="nm d{_report.MAX_INDENT}"', page)
        self.assertNotIn(f'class="nm d{_report.MAX_INDENT + 1}"', page)
        self.assertIn("d/", _report.summary(doc))

    def test_trash_and_its_warning(self):
        trashes = [{"where": "Home folder", "path": "/t", "drive": "home", "home": True,
                    "items": 3, "bytes": 3 * 1024 ** 3, "files": 9},
                   {"where": "Drive X", "path": "/m/.Trash-1000", "drive": "X",
                    "home": False, "items": 0, "bytes": 0, "files": 0}]
        page = _report.trash_html(trashes, 2, button=lambda t: f"[{t['drive']}]")
        self.assertEqual(page.count("trash.warn_gb (2 GB)"), 1)
        self.assertIn("[home]", page)
        self.assertIn("[X]", page)
        self.assertNotIn("trash.warn_gb", _report.trash_html(trashes, 0))
        self.assertNotIn("trash.warn_gb", _report.trash_html(trashes, 5))
        self.assertIn("no Trash on any connected drive", _report.trash_html([], 2))
        doc = self.doc()
        doc["trash"], doc["trash_warn_gb"] = trashes, 2
        text = _report.summary(doc)
        self.assertIn("more than trash.warn_gb (2 GB)", text)
        self.assertIn("empty it for good: clean trash empty", text)
        self.assertIn(f"about {human(3 * 1024 ** 3)} could be freed", _report.headline(doc))
        # Caches under the folders and the system's (sudo) ones are told apart
        mb = 1024 ** 2
        doc["caches"] = [{"label": "pip", "paths": ["/c/pip"], "disk": mb, "frees": mb}]
        doc["system_caches"] = [{"label": "apt", "paths": ["/var/cache/apt"],
                                 "disk": 2 * mb, "frees": 2 * mb}]
        self.assertIn(f"Could be freed:  about {human(3 * 1024 ** 3 + 3 * mb)}: caches 1.0 MB, "
                      f"system caches 2.0 MB, Trash {human(3 * 1024 ** 3)}\n",
                      _report.summary(doc))

    def test_a_stopped_run(self):
        doc = {"format": data.FORMAT, "kind": "space", "roots": [self.root],
               "stopped": "interrupted while scanning; no results", "seconds": 1.0}
        self.assertEqual(_report.headline(doc), "Interrupted while scanning; no results")
        self.assertEqual(_report.totals(doc), {})
        self.assertIn("Stopped:         interrupted", _report.summary(doc))


class TestReportPage(PageTest, ReportBase):
    def test_names_are_text_everywhere(self):
        names = [NASTY, "a&amp;b", "new\nline", "evil‮gpj"]
        for n, name in enumerate(names):
            self.write(f"{name}/big.bin", os.urandom(MB + 4096))
            self.write(f"Docs/{name}.tmp", "t")
            self.write(f"Docs/zero {name}", b"")
            os.symlink(name, self.p(f"Docs/link {n}"))
            self.mk(f"Empty {name}")
        bad = os.path.join(os.fsencode(self.root), b"bad\xffbyte")
        os.mkdir(bad)
        with open(os.path.join(bad, b"z.tmp"), "w") as f:
            f.write("z")
        doc = self.doc()
        run = runs.start("space", "read-only", [self.root])
        with runs.recording(run):
            _space_cmd._finish(run, doc, "done", 0)
        visible = self.check_page(run.file("report.html"))
        for shown in (NASTY, "a&amp;b", "new\\nline", "bad\\xffbyte/z.tmp"):
            self.assertIn(shown, visible)
        with open(run.file("summary.txt"), encoding="utf-8") as f:
            text = f.read()
        for shown in (NASTY, "a&amp;b", "new\\nline", "bad\\xffbyte/z.tmp"):
            self.assertIn(shown, text)
        self.assertNotIn("‮", text)
        self.assertNotIn("new\nline", text)
        # The exact names survive in the data
        back = data.read(run.file(data.NAME))
        stored = {f[1] for f in back["folders"]} | {r[1] for r in back["dirs"]}
        self.assertTrue({NASTY, "new\nline", "evil‮gpj", "bad\udcffbyte"} <= stored)
        self.assertEqual({k: v["count"] for k, v in back["clutter"].items()},
                         {"zero": 4, "broken": 4, "temp": 5, "empty": 4})


# ---------------------------------------------------------------- read-only

# What the space code may never call: `clean space` only reports, with the
# command for what can go. (clean's own runs are written by runs.py.)
OS_CHANGES = {"unlink", "remove", "rmdir", "removedirs", "rename", "renames", "replace",
              "truncate", "ftruncate", "chmod", "lchmod", "chown", "lchown", "utime", "mkdir",
              "makedirs", "symlink", "link", "system", "popen", "kill", "killpg", "execv",
              "execve", "execvp", "mkfifo", "mknod", "setxattr", "removexattr", "open",
              "write"}
ANY_CHANGES = {"rmtree", "unlink", "rmdir", "write_text", "write_bytes", "touch",
               "symlink_to", "hardlink_to", "copyfile", "copytree", "move", "put",
               "empty_trash", "remove_items", "restore"}
# The calls into other parts of clean that commands/space.py makes: all read.
ALLOWED = {"_trash_cmd": {"_volumes", "overview"}, "_cache": {"info"},
           "runs": {"start", "recording", "history", "prune_plan"},
           "caches": {"watch_paths", "user_caches", "system_caches", "measure"},
           "_volumes": {"drives"}}
SPACE_MODULES = ("cleanup/space.py", "cleanup/caches.py", "cleanup/commands/space.py",
                 "cleanup/report/space.py")


class TestReadOnly(unittest.TestCase):
    def test_the_space_code_changes_nothing(self):
        for rel in SPACE_MODULES:
            with open(os.path.join(TOOL, rel), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                    for m in mods:
                        self.assertNotIn(m.split(".")[0], ("shutil", "ctypes", "tempfile"), rel)
                    self.assertNotIn("cleanup.actions.remove", mods, rel)
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                owner = fn.value.id if (isinstance(fn, ast.Attribute)
                                        and isinstance(fn.value, ast.Name)) else None
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                where = f"{rel}:{node.lineno} {owner}.{name}"
                self.assertNotIn(name, ANY_CHANGES, where)
                if owner == "os":
                    self.assertNotIn(name, OS_CHANGES, where)
                if owner == "subprocess":
                    self.fail(where)
                if owner in ALLOWED and rel == "cleanup/commands/space.py":
                    self.assertIn(name, ALLOWED[owner], where)
                if name == "open" and owner is None:
                    mode = (node.args[1:2] or [k.value for k in node.keywords
                                               if k.arg == "mode"] or [ast.Constant("r")])[0]
                    self.assertIsInstance(mode, ast.Constant, where)
                    self.assertNotRegex(mode.value, "[wax+]", where)
                if name == "run" and owner is None:
                    # only `snap list --all` (caches.old_snaps)
                    self.assertEqual(rel, "cleanup/caches.py", where)


# ---------------------------------------------------------------- clean space

STOP = """
import os, sys
from cleanup import space
from cleanup.cli import main
real = space.Scan._list
calls = [0]
def _list(self, *a):
    calls[0] += 1
    if calls[0] == 3:
        raise KeyboardInterrupt
    return real(self, *a)
space.Scan._list = _list
sys.exit(main(sys.argv[1:]))
"""

# A folder that can't be read: inside the namespace you are root there, and
# root reads everything, so the refusal is made up.
LOCKED = """
import errno, os, sys
from cleanup.cli import main
real = os.scandir
def scandir(path="."):
    if os.fsdecode(path).endswith("/Locked"):
        raise PermissionError(errno.EACCES, "Permission denied", path)
    return real(path)
os.scandir = scandir
sys.exit(main(sys.argv[1:]))
"""


@needs_unshare
class TestSpaceCommand(Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(self.root)
        self.env["CLEAN_SPACE_SYSTEM_CACHES"] = "0"

    def p(self, rel=""):
        return os.path.join(self.root, rel) if rel else self.root

    def space(self, *args, before="", patch=None):
        """`clean space ARG...` in a sealed namespace with HOME=root (patched
        by the python `patch`, if given): (exit status, stdout, stderr)."""
        self._n = getattr(self, "_n", 0) + 1
        name = f"space{self._n}"
        argv = " ".join(shlex.quote(a) for a in ("space",) + args)
        if patch:
            script = os.path.join(self.tmp, f"{name}.py")
            with open(script, "w") as f:
                f.write(patch)
            line = f"pystep {name} {shlex.quote(script)} {argv}"
        else:
            line = f"step {name} {argv}"
        self.sealed(before + f"export HOME={shlex.quote(self.root)}\n" + line)
        return self.step(name)

    def doc(self, run=None):
        return data.read((run or self.last_run()).file(data.NAME))

    def fixture(self):
        raw = os.urandom(3 * MB)
        self.write("Photos/2019/raw/a.raw", raw)
        self.write("Photos/copy.raw", raw)
        b = self.write("Photos/2019/b.jpg", os.urandom(2 * MB))
        self.mk("Photos/2020")
        os.link(b, self.p("Photos/2020/b-link.jpg"))
        self.write("Code/proj/node_modules/pkg/big.js", os.urandom(3 * MB // 2))
        self.write("Code/proj/node_modules/pkg/empty.js", b"")
        self.write("Code/proj/.git/HEAD", "ref: refs/heads/main\n")
        self.write("Code/proj/.git/zero", b"")
        self.write(".cache/pip/wheels/w.whl", os.urandom(MB + 4096))
        self.write("Documents/zero.txt", b"")
        self.write("Documents/x.tmp", "temp")
        self.write("Documents/~$report.docx", "lock")
        os.utime(self.p("Documents/x.tmp"), (OLD, OLD))
        os.symlink("nowhere", self.p("Documents/broken"))
        os.symlink("loop", self.p("Documents/loop"))
        self.write(f"Documents/{NASTY}.txt", "nasty")
        self.mk("Empty", "a", "b")
        for rel in ("Photos/copy.raw", "Photos/2019/raw/a.raw"):
            os.utime(self.p(rel), (OLD, OLD))

    def test_a_look_at_a_home_folder(self):
        self.fixture()
        before = state(self.root)
        r = self.space()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(state(self.root), before, "clean space changed something")
        for line in (f"Looked at:       {self.root}\n",
                     "Hard links:      1 extra name of files counted once",
                     "\nBiggest folders  (the bar: share of the folder above)\n",
                     "\nBiggest files\n", "     3.0 MB  ~/Photos/2019/raw/a.raw\n",
                     "  pip downloads  ~/.cache/pip\n", "$ pip cache purge\n",
                     "\nTrash\n  ", "Zero-byte files: 2  (1 inside program and package folders",
                     "      ~/Documents/zero.txt\n",
                     "      ~/Documents/broken -> nowhere  (missing)\n",
                     "      ~/Documents/loop -> loop  (a loop)\n",
                     "  Temp leftovers:  2, ", "~/Documents/x.tmp\n",
                     "~/Documents/~$report.docx  (changed in the last day: may be in use)\n",
                     "  Empty folders:   3\n", "      ~/Empty/  (+2 inside)\n",
                     "No duplicates scan of these folders yet: clean dupes " + self.root,
                     "  Runs:          1 run, ", "  Hash cache:    not created yet",
                     "\nNothing was deleted: clean space only looks.\n"):
            self.assertIn(line, r.out)
        self.assertNotIn(".git/zero", r.out)
        run = self.last_run()
        self.assertEqual((run.command, run.status, run.meta.get("mode")),
                         ("space", "done", "read-only"))
        for f in ("report.html", "summary.txt", data.NAME, "run.json"):
            self.assertTrue(os.path.isfile(run.file(f)), f)
        doc = self.doc(run)
        self.assertEqual(doc["roots"], [self.root])
        self.assertEqual(doc["total"]["disk"], du(self.root))
        self.assertEqual([c["key"] for c in doc["caches"]], ["pip"])
        self.assertIsNone(doc["system_caches"])
        self.assertEqual({k: v["count"] for k, v in doc["clutter"].items()},
                         {"zero": 2, "broken": 2, "temp": 2, "empty": 3})
        self.assertEqual(doc["links"]["names"], 1)
        self.assertEqual(run.meta["totals"]["clutter"], 9)
        with mock.patch.dict(os.environ, {"HOME": self.root}):     # as in the run
            self.assertEqual(run.headline, _report.headline(doc))
        self.assertTrue(run.headline.startswith("10."), run.headline)
        self.assertIn(" MB in ~; about ", run.headline)
        with open(run.file("summary.txt"), encoding="utf-8") as f:
            self.assertIn("~/Photos/", f.read())

    def test_options_and_refusals(self):
        self.fixture()
        r = self.space(self.root, "--top", "1", "-q")
        self.assertEqual(r.rc, 0, r)
        files = r.out.split("\nBiggest files\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual(files, ["     3.0 MB  ~/Photos/2019/raw/a.raw"])
        self.assertEqual(r.err, "")
        r = self.space(self.root, "-e", "*.raw", "-e", self.p("Code"))
        self.assertIn("Left out:        3 entries matched --exclude (not counted)", r.out)
        self.assertEqual(self.doc()["total"]["disk"],
                         du(self.root) - du(self.p("Code")) - 2 * blocks(self.p("Photos/copy.raw")))
        # Inside .git too: counted, still not listed (a program's own folder)
        r = self.space(self.root, "--no-default-excludes")
        self.assertEqual(self.doc()["clutter"]["zero"]["count"], 3)
        self.assertIn("Zero-byte files: 3  (2 inside program and package folders", r.out)
        os.symlink(self.p("Photos"), self.p("photo-link"))
        for args, msg in ((["nope"], "no such folder"),
                          ([self.p("Documents/zero.txt")], "not a folder"),
                          ([self.p("photo-link")], "is a symlink"),
                          (["--top", "0"], "--top")):
            with self.subTest(args=args):
                r = self.space(*args)
                self.assertEqual(r.rc, 2, r)
                self.assertIn(msg, r.err)
        self.assertEqual(len(self.runs()), 3)

    def test_trash_dupes_and_own_data(self):
        self.fixture()
        trash = os.path.join(self.tmp, "xdg", "data", "Trash")
        os.makedirs(os.path.join(trash, "files"))
        os.makedirs(os.path.join(trash, "info"))
        with open(os.path.join(trash, "files", "old.bin"), "wb") as f:
            f.write(os.urandom(200_000))
        with open(os.path.join(trash, "info", "old.bin.trashinfo"), "w") as f:
            f.write(f"[Trash Info]\nPath={self.p('old.bin')}\n"
                    f"DeletionDate=2020-01-01T00:00:00\n")
        r = self.dupes(self.p("Photos"))
        self.assertEqual(r.returncode, 0, r.stderr)
        dupes = self.last_run()
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.assertEqual(self.dupes(other).returncode, 0)       # not of these folders
        self.fake_run("2020-01-01_000000-empty")
        self.assertEqual(self.run_clean("config", "set", "history.keep_runs", "1").returncode, 0)
        trash_before = state(trash)
        before = state(self.root)
        r = self.space()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(state(self.root), before)
        self.assertEqual(state(trash), trash_before)
        doc = self.doc()
        self.assertEqual(doc["dupes"]["name"], dupes.name)
        self.assertIn(f"Newest scan of these folders: {dupes.name}", r.out)
        self.assertIn(f"See it: clean show {dupes.name}", r.out)
        home = [t for t in doc["trash"] if t["home"]]
        self.assertEqual([(t["items"], t["path"]) for t in home], [(1, trash)])
        self.assertGreaterEqual(home[0]["bytes"], 200_000)
        self.assertIn(": 1 item\n", r.out)
        self.assertIn("empty it for good: clean trash empty", r.out)
        own = doc["own"]
        self.assertEqual((own["runs"], own["keep_runs"], own["prunable"]), (4, 1, 1))
        self.assertIn("; 1 older run goes as this run ends\n", r.out)
        self.assertTrue(own["cache"]["exists"])
        self.assertGreater(own["cache"]["entries"], 0)
        self.assertIn("clean cache prune drops files that are gone", r.out)
        left = [x.name for x in self.runs()]        # retention, as for any run
        self.assertEqual(len(left), 3)
        self.assertNotIn("2020-01-01_000000-empty", left)

    def test_a_drive_inside(self):
        self.write("home/file.bin", os.urandom(MB))
        self.mk("home", "usb")
        mount = (f"mount -t tmpfs -o size=8m usb {shlex.quote(self.p('home/usb'))}\n"
                 f"head -c 3000000 /dev/urandom > {shlex.quote(self.p('home/usb/on-usb.bin'))}\n")
        r = self.space(self.p("home"), before=mount)
        self.assertEqual(r.rc, 0, r)
        doc = self.doc()
        self.assertEqual(doc["mounts"], [self.p("home/usb")])
        self.assertLess(doc["total"]["disk"], 2 * MB)
        self.assertTrue(doc["folders"][0][_report.FLAGS] & _space.MOUNTS)
        self.assertIn("Other drives:    1 mounted inside, not counted", r.out)
        # Named as a PATH of its own, it is counted, once
        r = self.space(self.p("home"), self.p("home/usb"), self.p("home/usb/"), before=mount)
        self.assertEqual(r.rc, 0, r)
        doc = self.doc()
        self.assertEqual(doc["roots"], [self.p("home"), self.p("home/usb")])
        self.assertEqual(doc["mounts"], [])
        self.assertGreater(doc["total"]["disk"], 3 * MB)
        self.assertLess(doc["total"]["disk"], 5 * MB)
        self.assertFalse(doc["folders"][0][_report.FLAGS] & _space.MOUNTS)
        self.assertNotIn("Other drives", r.out)
        self.assertIn(f"Looked at:       {self.p('home')}\n                 "
                      f"{self.p('home/usb')}\n", r.out)
        # A folder inside on the same drive is walked with its parent
        self.assertEqual(_space_cmd.space_roots([self.p("home"), self.p("home/usb")]),
                         [self.p("home")])

    def test_unreadable_is_exit_1(self):
        self.write("Locked/x", "x")
        self.write("Open/y", "y")
        r = self.space(self.root, patch=LOCKED)
        self.assertEqual(r.rc, 1, r)
        self.assertIn("Problems:        1\n", r.out)
        self.assertIn("cannot read folder: ", r.out)
        self.assertIn("(some of it unreadable: at least this)", r.out)
        run = self.last_run()
        self.assertEqual((run.status, run.meta["totals"]["errors"]), ("done", 1))

    def test_interrupted(self):
        self.mk("a", "b", "c")
        r = self.space(self.root, patch=STOP)
        self.assertEqual(r.rc, 130, r)
        self.assertIn("Interrupted", r.err)
        run = self.last_run()
        self.assertEqual(run.status, "interrupted")
        self.assertTrue(self.doc(run)["stopped"])
        with open(run.file("summary.txt"), encoding="utf-8") as f:
            self.assertIn("Stopped:         interrupted", f.read())
        with open(run.file("report.html"), encoding="utf-8") as f:
            self.assertIn("did not finish", f.read())


# ---------------------------------------------------------------- the hash cache

def make_cache(path, rows):
    """A cache file holding `rows` [(vol, path bytes, size, mtime, seen)]."""
    c = _cache.Cache(path)
    c.db.execute("BEGIN")
    c.db.executemany("INSERT INTO hashes (vol, path, size, mtime, head, full, seen) "
                     "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                     [(v, p, s, m, b"h" * 64, seen) for v, p, s, m, seen in rows])
    c.db.execute("COMMIT")
    c.close()
    return path


class Offline:
    """Volumes where no drive with a UUID is connected."""

    def by_uuid(self, uuid):
        return None


class TestCacheFile(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp, "c", "hashes.sqlite")

    def file_row(self, rel):
        p = self.write(rel, b"x")
        st = os.stat(p)
        return ("", os.fsencode(p), st.st_size, st.st_mtime_ns, 0)

    def test_a_version_1_file_is_upgraded(self):
        os.makedirs(os.path.dirname(self.path))
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
            CREATE TABLE hashes (vol TEXT NOT NULL, path BLOB NOT NULL, size INTEGER NOT NULL,
                mtime INTEGER NOT NULL, head BLOB, full BLOB, PRIMARY KEY (vol, path))
                WITHOUT ROWID;
            INSERT INTO meta VALUES ('version', '1');
            INSERT INTO hashes VALUES ('', X'2F61', 1, 2, NULL, X'00');""")
        db.commit()
        db.close()
        self.assertEqual(_cache.info(self.path)["version"], "1")
        c = _cache.Cache(self.path)
        self.assertIsNone(c.warning)
        c.close()
        i = _cache.info(self.path)
        self.assertEqual((i["version"], i["entries"], i["oldest"]), ("3", 1, 0))

    def test_a_version_2_file_is_upgraded(self):
        os.makedirs(os.path.dirname(self.path))
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
            CREATE TABLE hashes (vol TEXT NOT NULL, path BLOB NOT NULL, size INTEGER NOT NULL,
                mtime INTEGER NOT NULL, head BLOB, full BLOB,
                seen INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (vol, path)) WITHOUT ROWID;
            INSERT INTO meta VALUES ('version', '2');
            INSERT INTO hashes VALUES ('', X'2F61', 1, 2, NULL, X'00', 7);""")
        db.commit()
        db.close()
        i = _cache.info(self.path)
        self.assertEqual((i["version"], i["oldest"]), ("2", 7))
        c = _cache.Cache(self.path)
        self.assertIsNone(c.warning)
        c.close()
        i = _cache.info(self.path)
        self.assertEqual((i["version"], i["entries"], i["oldest"]), ("3", 1, 7))
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("SELECT full, seen, tags FROM hashes").fetchall(),
                         [(b"\0", 7, None)])
        db.close()

    def test_another_version_is_not_used(self):
        make_cache(self.path, [("", b"/a", 1, 1, 1)])
        db = sqlite3.connect(self.path)
        db.execute("UPDATE meta SET value = '9' WHERE key = 'version'")
        db.commit()
        db.close()
        c = _cache.Cache(self.path)
        self.assertFalse(c.active)
        self.assertIn("another version (9)", c.warning)
        self.assertEqual(c.prune_gone(Offline()), (0, 0))
        self.assertEqual(c.trim(1), 0)
        self.assertEqual(_cache.info(self.path)["entries"], 1)      # left as it was

    def test_info(self):
        i = _cache.info(self.path)
        self.assertEqual((i["exists"], i["bytes"], i["entries"], i["error"]),
                         (False, 0, 0, None))
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))    # only looks
        make_cache(self.path, [("", b"/a/x", 1, 1, 100), ("", b"/a/y", 1, 1, 300),
                               ("ABCD-1234", b"/z", 1, 1, 200)])
        i = _cache.info(self.path)
        self.assertEqual((i["exists"], i["entries"], i["drives"], i["oldest"], i["newest"],
                          i["version"], i["error"]),
                         (True, 3, {"": 2, "ABCD-1234": 1}, 100, 300, "3", None))
        self.assertEqual(i["bytes"], _cache.file_size(self.path))
        with open(self.path, "wb") as f:
            f.write(b"not a database" * 100)
        self.assertTrue(_cache.info(self.path)["error"])

    def test_prune_gone(self):
        keep = self.file_row("keep.txt")
        changed = self.file_row("changed.txt")
        gone = self.file_row("gone.txt")
        folder = self.file_row("folder/x")
        offline = ("ABCD-1234", b"/photos/a.jpg", 1, 1, 0)
        no_folder = ("", os.fsencode(self.p("never/there")), 1, 1, 0)
        make_cache(self.path, [keep, changed, gone, folder, offline, no_folder])
        with open(self.p("changed.txt"), "a") as f:
            f.write("more")
        os.unlink(self.p("gone.txt"))
        shutil.rmtree(self.p("folder"))
        c = _cache.Cache(self.path)
        self.assertEqual(c.prune_gone(Offline()), (4, 1))
        left = sorted(row for row in c.db.execute("SELECT vol, path FROM hashes"))
        c.close()
        self.assertEqual(left, sorted([keep[:2], offline[:2]]))

    def p(self, rel):
        return os.path.join(self.root, rel)

    def test_trim_and_compact(self):
        make_cache(self.path, [("", f"/f/{n:06}".encode(), n, n, n) for n in range(20000)])
        full = _cache.file_size(self.path)
        c = _cache.Cache(self.path)
        self.assertEqual(c.trim(full * 2), 0)       # under the limit: nothing
        self.assertEqual(c.trim(0), 0)              # no limit
        limit = full // 2
        dropped = c.trim(limit)
        self.assertGreater(dropped, 0)
        left = [r[0] for r in c.db.execute("SELECT seen FROM hashes ORDER BY seen")]
        c.close()
        self.assertEqual(len(left), 20000 - dropped)
        self.assertEqual(left[0], dropped)          # the ones met longest ago went
        self.assertLessEqual(_cache.file_size(self.path), limit)
        # VACUUM gives the space back
        c = _cache.Cache(self.path)
        c.db.execute("DELETE FROM hashes")
        c.compact()
        c.close()
        self.assertLess(_cache.file_size(self.path), limit // 4)
        self.assertEqual(_cache.info(self.path)["entries"], 0)

    def test_clear(self):
        self.assertEqual(_cache.clear(self.path), (0, []))
        make_cache(self.path, [("", b"/a", 1, 1, 1)])
        with open(self.path + "-wal", "wb") as f:
            f.write(b"w" * 100)
        self.write("keep.txt", "k")
        size = _cache.file_size(self.path)
        self.assertEqual(_cache.clear(self.path), (size, []))
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [])
        self.assertTrue(os.path.isfile(self.p("keep.txt")))


class TestCacheCommand(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp, "xdg", "cache", "clean", "hashes.sqlite")

    def p(self, rel):
        return os.path.join(self.root, rel)

    def cache(self, *args):
        """`clean cache ARG...` with no terminal: (exit status, stdout, stderr)."""
        r = subprocess.run([sys.executable, CLEAN, "cache", *args], capture_output=True,
                           stdin=subprocess.DEVNULL, env=self.env)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def scanned(self):
        for top in ("A", "B"):
            for n in range(3):
                self.write(f"{top}/f{n}", blob(f"f{n}", 5000 + n))
        for top in ("A", "B"):
            for n in range(3):
                os.utime(self.p(f"{top}/f{n}"), (OLD, OLD))
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_before_there_is_one(self):
        rc, out, _ = self.cache()
        self.assertEqual(rc, 0)
        self.assertIn("Not created yet: clean dupes makes it.", out)
        self.assertEqual(self.cache("prune")[:2],
                         (0, "There is no hash cache yet: nothing to prune.\n"))
        self.assertEqual(self.cache("clear")[:2],
                         (0, "There is no hash cache: nothing to delete.\n"))
        self.assertFalse(os.path.exists(self.path))

    def test_info_prune_clear(self):
        self.scanned()
        rc, out, _ = self.cache("info")
        self.assertEqual(rc, 0)
        self.assertIn("Remembers:       6 files\n", out)
        self.assertIn("limit 500 MB (cache.max_mb)", out)
        self.assertRegex(out, r"\n +6  (drive [\w-]+|folders on no named drive)\n")
        self.assertIn("Upkeep:          clean cache prune", out)
        os.unlink(self.p("B/f2"))
        rc, out, _ = self.cache("prune", "-q")
        self.assertEqual(rc, 0)
        self.assertIn("Dropped 1 entry of files that are gone or changed.\n", out)
        self.assertIn("Compacted:", out)
        self.assertEqual(_cache.info(self.path)["entries"], 5)
        # With no terminal to ask on, clear refuses without -y
        rc, out, err = self.cache("clear")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("add -y to confirm", err)
        self.assertTrue(os.path.exists(self.path))
        rc, out, _ = self.cache("clear", "-y")
        self.assertEqual(rc, 0)
        self.assertIn("Deleted the hash cache", out)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [])
        self.assertTrue(os.path.isfile(self.p("A/f0")))     # only the cache went

    def test_prune_keeps_it_under_the_limit(self):
        n = 30000
        make_cache(self.path, [("ABCD-1234", f"/photos/{i:06}.jpg".encode(), i, i, i)
                               for i in range(n)])
        self.assertGreater(_cache.file_size(self.path), MB)
        self.assertEqual(self.run_clean("config", "set", "cache.max_mb", "1").returncode, 0)
        rc, out, _ = self.cache("prune", "-q")
        self.assertEqual(rc, 0, out)
        i = _cache.info(self.path)
        trimmed = n - i["entries"]
        self.assertGreater(trimmed, 0)
        self.assertIn(f"Dropped 0 entries of files that are gone or changed, and {trimmed:,} "
                      f"used longest ago to keep it under 1 MB.\n", out)
        self.assertIn(f"Kept {n:,} entries on drives that aren't connected", out)
        self.assertLessEqual(_cache.file_size(self.path), MB)
        self.assertEqual(i["oldest"], trimmed)

    def test_a_broken_file(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as f:
            f.write(b"not a database" * 100)
        rc, out, _ = self.cache()
        self.assertEqual(rc, 1)
        self.assertIn("Can't read it:", out)
        self.assertIn("clean cache clear deletes it", out)
        rc, out, err = self.cache("prune")
        self.assertEqual(rc, 1)
        self.assertIn("clean: error: the hash cache", err)
        self.assertEqual(self.cache("clear", "-y")[0], 0)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
