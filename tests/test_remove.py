#!/usr/bin/env python3
"""`clean remove` and `clean dupes --remove`, for real. Every run that may
move or delete anything happens inside a sealed namespace (Base.sealed), on
trees in the test's temp folder, with the Trash in there too: your files,
drives and Trash are never involved.

Covered: what goes and what stays, the Trash entries and actions.tsv,
--permanent, dry runs, asking first, re-runs; every check made just before
acting (a copy or the copy that stays changed, something new in a folder, a
symlink or a second view of the same file swapped in); failures and crashes
part-way, then a re-run and undo; Ctrl-C, kill and a pulled drive; the lock;
drives moved or missing since the scan."""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest

from helpers import Base, blob, needs_unshare

from cleanup.actions import trash as _trash
from cleanup.actions.remove import read_log
from cleanup.report import data as report_data

# Builds N folders g00, g01...: in each, x-copy is a newer copy of x, so the
# default rules remove every x-copy (one item each), g00's first (the
# biggest). `FILL TOP N [TAG]`: TAG makes the contents differ from another fill.
FILL = r"""
import os, sys, time
top, n = sys.argv[1], int(sys.argv[2])
tag = sys.argv[3] if len(sys.argv) > 3 else "content"
old = time.time() - 7200
for i in range(n):
    d = os.path.join(top, f"g{i:02}")
    os.makedirs(d, exist_ok=True)
    data = (f"{tag} {i} ".encode() * 400)[:4000 - i]
    for name in ("x", "x-copy"):
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
    os.utime(os.path.join(d, "x"), (old, old))
"""

# A python script for pystep: applies PATCH, then runs clean in-process.
PATCHED = r"""
import errno, os, signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)  # a shell's & ignores it
from cleanup.actions import remove, trash
from cleanup.cli import main
calls = [0]
real_put = trash.Trash.put
real_rename = trash.rename_noreplace
real_row = remove.Log.row
PATCH
sys.exit(main(sys.argv[1:]))
"""

SLOW_PUT = """
def put(self, path, before_move=None):
    time.sleep(0.3)
    return real_put(self, path, before_move)
trash.Trash.put = put
"""


class RemoveBase(Base):
    def setUp(self):
        super().setUp()
        self.trash = os.path.join(self.tmp, "xdg", "data", "Trash")

    def p(self, rel):
        return os.path.join(self.root, rel)

    def standard(self):
        """A/ and its identical copy B/; in C/, u-copy.bin is a newer copy
        of u.bin. The default rules remove B and C/u-copy.bin."""
        for rel in ("A/a1", "A/a2", "A/sub/a3"):
            self.write(rel, blob(os.path.basename(rel)))
        shutil.copytree(self.p("A"), self.p("B"))
        self.write("C/u.bin", blob("u"))
        self.write("C/u-copy.bin", blob("u"))
        self.write("C/unique.txt", blob("unique"))
        old = time.time() - 7200
        os.utime(self.p("C/u.bin"), (old, old))

    def fill(self, n, top=None, tag="content"):
        script = self.script("fill.py", FILL)
        subprocess.run([sys.executable, script, top or self.root, str(n), tag], check=True)

    def script(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def patched(self, name, patch):
        """Write a pystep script that applies `patch` first; returns its path."""
        return self.script(name, PATCHED.replace("PATCH", patch))

    def scan(self, *args, roots=None):
        r = self.dupes(*(roots or [self.root]), "-q", *args)
        self.assertEqual(r.returncode, 0, r.stderr.decode() + r.stdout.decode())
        run = self.last_run()
        self.assertEqual(run.command, "dupes")
        return run.name

    def actions(self, run=None):
        """actions.tsv of a run (default: the newest) as (action, path, to, note)."""
        run = run or self.last_run()
        return [(a, path, to, note) for _, _, a, path, to, note
                in read_log(run.file("actions.tsv"))]

    def items(self, run=None):
        """The items of a remove run: {path: (status, note)}."""
        doc = report_data.read((run or self.last_run()).file(report_data.NAME))
        cols = doc["columns"]
        i, s, n = cols.index("path"), cols.index("status"), cols.index("note")
        return {row[i]: (row[s], row[n]) for row in doc["items"]}

    def statuses(self, run=None):
        out = {}
        for status, _ in self.items(run).values():
            out[status] = out.get(status, 0) + 1
        return out

    def in_trash(self, folder=None):
        """{name in the Trash: where it came from} (default: the home Trash
        in the test folder)."""
        return {e.name: e.original
                for e in _trash.Trash(folder or self.trash, None).entries() if not e.missing}

    def remove_runs(self):
        return [r for r in self.runs() if r.command == "remove"]


@needs_unshare
class TestRemove(RemoveBase):
    def test_to_the_trash(self):
        self.standard()
        before = self.snapshot()
        dupes = self.scan()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        after = self.snapshot()
        self.assertEqual(set(before) - set(after), {"B/a1", "B/a2", "B/sub/a3", "C/u-copy.bin"})
        self.assertTrue(all(after[k] == before[k] for k in after))
        self.assertFalse(os.path.lexists(self.p("B")))
        # Whole and unchanged in the Trash, which knows where each came from.
        self.assertEqual(self.in_trash(), {"B": self.p("B"), "u-copy.bin": self.p("C/u-copy.bin")})
        self.assertEqual(self.snapshot(os.path.join(self.trash, "files", "B")),
                         {k[2:]: v for k, v in before.items() if k.startswith("B/")})
        run = self.last_run()
        meta = self.run_meta(run)
        self.assertEqual((meta["command"], meta["mode"], meta["source"], meta["status"],
                          meta["exit_code"]), ("remove", "trash", dupes, "done", 0))
        self.assertEqual(meta["headline"], "moved 4 files (11.7 KB) to the Trash")
        files = os.path.join(self.trash, "files")
        self.assertEqual(self.actions(run), [
            ("trash", self.p("B"), os.path.join(files, "B"), ""),
            ("trash", self.p("C/u-copy.bin"), os.path.join(files, "u-copy.bin"), "")])
        self.assertEqual(self.statuses(run), {"removed": 2})
        for text in ("TRASH: `clean undo` puts everything back",
                     "Space freed:     11.7 KB once the Trash is emptied",
                     f"Undo:            clean undo {run.name}",
                     f"Free the space:  clean trash empty --run {run.name}"):
            self.assertIn(text, r.out)
        self.assertTrue(all(run.has(f) for f in ("report.html", "summary.txt",
                                                 report_data.NAME, "run.json")))
        with open(run.file("report.html"), encoding="utf-8") as f:
            page = f.read()
        for name in ("summary.txt", "actions.tsv", report_data.NAME, "run.json"):
            self.assertIn(f'<a href="{name}">', page)   # written after the page, linked too
        self.assertEqual(self.leftovers(self.root) + self.leftovers(run.folder), [])

    def test_permanent(self):
        self.standard()
        before = self.snapshot()
        self.scan()
        r = self.clean_sealed("remove", "-y", "--permanent")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(set(before) - set(self.snapshot()),
                         {"B/a1", "B/a2", "B/sub/a3", "C/u-copy.bin"})
        self.assertFalse(os.path.lexists(self.p("B")))
        self.assertFalse(os.path.exists(self.trash))
        run = self.last_run()
        self.assertEqual(self.run_meta(run)["mode"], "permanent")
        self.assertEqual(self.actions(run), [("delete", self.p("B"), "", "3 files"),
                                             ("delete", self.p("C/u-copy.bin"), "", "")])
        self.assertIn("PERMANENT: deleted for good", r.out)
        self.assertIn("Space freed:     11.7 KB\n", r.out)
        self.assertNotIn("clean undo", r.out)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no removal left to undo", r.err)
        r = self.clean_sealed("undo", "-y", run.name)
        self.assertEqual(r.rc, 2, r)
        self.assertIn("deleted for good (--permanent): there is nothing to bring back", r.err)

    def test_dry_run_touches_nothing(self):
        self.standard()
        before = self.snapshot()
        dupes = self.scan()
        r = self.clean_sealed("remove", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.trash))
        run = self.last_run()
        self.assertEqual((self.run_meta(run)["mode"], run.has("actions.tsv")), ("dry-run", False))
        self.assertIn("Would move 1 folder and 1 file (4 files in all, 11.7 KB) to the Trash:\n",
                      r.out)
        self.assertIn(f"To go ahead: clean remove {dupes}\n", r.out)
        # The line to go ahead keeps the rules: here B stays, so A goes.
        r = self.clean_sealed("remove", "-n", "--keep-in", self.p("B"), "--rank", "newest")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"  folder  {self.p('A')}\n", r.out)
        self.assertIn(f"To go ahead: clean remove {dupes} --keep-in {self.p('B')} "
                      f"--rank newest\n", r.out)
        self.assertEqual(self.snapshot(), before)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no removal left to undo", r.err)

    def test_asks_first(self):
        self.standard()
        before = self.snapshot()
        self.scan()
        r = self.clean_sealed("remove")             # no terminal to ask on
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no terminal to ask on before removing: add -y", r.err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.remove_runs(), [])
        # On a terminal: "n" (or nothing) keeps everything, "y" goes ahead.
        tty = self.script("tty.py", r"""
import os, subprocess, sys
m, s = os.openpty()
p = subprocess.Popen([os.environ["PY"], os.environ["CLEAN"]] + sys.argv[2:], stdin=s,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
os.close(s)
os.write(m, sys.argv[1].encode() + b"\n")
out, err = p.communicate(timeout=60)
os.close(m)
with open(os.path.join(os.environ["T"], "tty-" + sys.argv[1]), "w") as f:
    f.write(f"{p.returncode}\n{out.decode()}---\n{err.decode()}")
""")
        self.sealed(f'"$PY" "{tty}" n remove; "$PY" "{tty}" "" remove')
        for answer in ("n", ""):
            with open(os.path.join(self.tmp, "tty-" + answer)) as f:
                rc, rest = f.read().split("\n", 1)
            self.assertEqual(rc, "2", rest)
            self.assertIn("Move 1 folder and 1 file (4 files in all, 11.7 KB) to the Trash? "
                          "[y/N] Nothing was removed.", rest)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.remove_runs(), [])
        self.sealed(f'"$PY" "{tty}" y remove')
        with open(os.path.join(self.tmp, "tty-y")) as f:
            rc, rest = f.read().split("\n", 1)
        self.assertEqual(rc, "0", rest)
        self.assertFalse(os.path.lexists(self.p("B")))

    def test_rerun_says_already_gone(self):
        self.standard()
        self.scan()
        self.assertEqual(self.clean_sealed("remove", "-y").rc, 0)
        first = self.last_run()
        after = self.snapshot()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(), after)
        again = self.last_run()
        self.assertEqual(self.statuses(again), {"gone": 2})
        self.assertEqual(self.run_meta(again)["headline"],
                         "moved 0 files (0 B) to the Trash · 2 already gone")
        self.assertEqual(self.actions(again), [])
        # Undo goes for the run that did something, not the re-run.
        r = self.clean_sealed("undo", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Undoing:         run {first.name} ", r.out)
        self.assertIn("Items:           2: 2 would come back", r.out)

    def test_refusals(self):
        self.standard()
        before = self.snapshot()
        self.scan()
        for args, text in (
                (["--keep-in", "/usr/share"], "--keep-in /usr/share: that is not inside any "
                                              "folder the scan looked at"),
                (["--link", "--merge"], "--link and --merge can't be used together"),
                (["--conflicts", "skip"], "--conflicts only matters with --merge"),
                (["--merge", "--conflicts", "newer", "--permanent"],
                 "can't be used with --permanent"),
                (["--rank", "biggest"], "unknown rank rule 'biggest'"),
                (["no-such-run"], "no run")):
            r = self.clean_sealed("remove", "-y", *args)
            self.assertEqual(r.rc, 2, (args, r))
            self.assertIn(text, r.err, args)
        # --only files: B is not removed as a folder, its files go one by one
        r = self.clean_sealed("remove", "-y", self.last_run().name, "--only", "files")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(set(before) - set(self.snapshot()),
                         {"B/a1", "B/a2", "B/sub/a3", "C/u-copy.bin"})
        self.assertEqual(sorted(self.items()), sorted(self.p(x) for x in (
            "B/a1", "B/a2", "B/sub/a3", "C/u-copy.bin")))
        self.assertTrue(os.path.isdir(self.p("B/sub")))
        # A run that isn't a scan
        r = self.clean_sealed("remove", "-y", self.last_run().name)
        self.assertEqual(r.rc, 2, r)
        self.assertIn("is a `clean remove` run; `clean remove` acts on the results of a "
                      "`clean dupes` run", r.err)

    def test_lock(self):
        self.standard()
        before = self.snapshot()
        self.scan()
        hold = self.script("hold.py", "import os, sys, time\nfrom cleanup import runs\n"
                           "fd = runs.lock('the test holding it')\n"
                           "open(sys.argv[1], 'w').close()\ntime.sleep(60)\n")
        held = os.path.join(self.tmp, "held")
        self.sealed(f'"$PY" "{hold}" "{held}" & h=$!\n'
                    f'while [ ! -e "{held}" ]; do sleep 0.05; done\n'
                    'step locked remove -y\nstep undo undo -y\nkill $h')
        r = self.step("locked")
        self.assertEqual(r.rc, 2, r)
        self.assertRegex(r.err, r"another clean is deleting right now \(process \d+: the test "
                                r"holding it\)\. Only one may delete at a time")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.remove_runs(), [])


@needs_unshare
class TestChecks(RemoveBase):
    """Each item is checked again just before it goes; anything not exactly
    as the scan saw it is skipped, and nothing of it is touched."""

    def remove(self, code=1):
        before = self.snapshot()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, code, r)
        return before, r

    def test_copy_changed_but_not_its_size_or_date(self):
        self.standard()
        self.scan()
        path = self.p("B/sub/a3")
        st = os.stat(path)
        with open(path, "r+b") as f:
            f.seek(100)
            f.write(b"#")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        before, r = self.remove()
        self.assertEqual(set(before) - set(self.snapshot()), {"C/u-copy.bin"})
        self.assertEqual(self.items()[self.p("B")],
                         ("skipped", "sub/a3: its content changed since the scan, though its "
                                     "size and date did not"))
        self.assertIn("Skipped: left exactly as they were (1):", r.out)
        self.assertEqual(self.in_trash(), {"u-copy.bin": self.p("C/u-copy.bin")})

    def test_copy_changed_size(self):
        self.standard()
        self.scan()
        with open(self.p("C/u-copy.bin"), "ab") as f:
            f.write(b"more")
        before, r = self.remove()
        self.assertEqual(set(before) - set(self.snapshot()), {"B/a1", "B/a2", "B/sub/a3"})
        self.assertEqual(self.items()[self.p("C/u-copy.bin")],
                         ("skipped", "its size changed since the scan (3000 -> 3004 bytes)"))

    def test_copy_that_stays_changed(self):
        # B's a1 relies on A's a1, the copy that stays; u-copy.bin on u.bin.
        self.standard()
        self.scan()
        with open(self.p("A/a1"), "ab") as f:
            f.write(b"edited")
        os.unlink(self.p("C/u.bin"))
        before, r = self.remove()
        self.assertEqual(self.snapshot(), before)
        items = self.items()
        self.assertEqual(items[self.p("B")][0], "skipped")
        self.assertRegex(items[self.p("B")][1], r"^no verified copy of a1 remains \(.*/A/a1: "
                                                r"its size changed since the scan")
        self.assertEqual(items[self.p("C/u-copy.bin")],
                         ("skipped", f"no verified copy of u-copy.bin remains "
                                     f"({self.p('C/u.bin')} is gone)"))

    def test_only_kept_copies_count(self):
        # Three copies of A: B and D both go, so A is the only copy they can
        # rely on (not each other); A changed, so both stay.
        self.standard()
        shutil.copytree(self.p("A"), self.p("D"))
        self.scan()
        with open(self.p("A/a2"), "ab") as f:
            f.write(b"edited")
        before, r = self.remove()
        self.assertEqual(set(before) - set(self.snapshot()), {"C/u-copy.bin"})
        items = self.items()
        for x in "BD":
            self.assertRegex(items[self.p(x)][1], r"^no verified copy of a2 remains \(.*/A/a2: "
                                                  r"its size changed since the scan")

    def test_next_copy_that_stays_is_tried(self):
        # u.bin is kept twice (C and K are reference folders); R/u-copy.bin
        # relies on C's or, when that changed, on K's.
        for d in "CKR":
            self.write(f"{d}/only-{d}.txt", blob("only " + d))
        for rel in ("C/u.bin", "K/u.bin", "R/u-copy.bin"):
            self.write(rel, blob("u"))
        self.scan()
        keep = ["--keep-in", self.p("C"), "--keep-in", self.p("K")]
        r = self.clean_sealed("remove", "-n", *keep)
        self.assertEqual(sorted(self.items()), [self.p("R/u-copy.bin")])
        with open(self.p("C/u.bin"), "ab") as f:
            f.write(b"edited")
        before = self.snapshot()
        r = self.clean_sealed("remove", "-y", *keep)
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(set(before) - set(self.snapshot()), {"R/u-copy.bin"})
        self.assertEqual(self.statuses(), {"removed": 1})

    def test_something_new_in_a_folder(self):
        self.standard()
        self.scan()
        self.write("B/sub/new.txt", "new")
        before, r = self.remove()
        self.assertEqual(set(before) - set(self.snapshot()), {"C/u-copy.bin"})
        self.assertEqual(self.items()[self.p("B")], ("skipped", "sub/new.txt is new since the scan"))
        os.unlink(self.p("B/sub/new.txt"))
        os.mkdir(self.p("B/newdir"))
        before, r = self.remove()
        self.assertEqual(self.items()[self.p("B")], ("skipped", "newdir/ is new since the scan"))
        self.assertEqual(self.snapshot(), before)

    def test_junk_goes_with_its_folder(self):
        self.standard()
        self.scan()
        self.write("B/sub/Thumbs.db", "thumbnail cache")
        before, r = self.remove(code=0)
        self.assertFalse(os.path.lexists(self.p("B")))
        self.assertTrue(os.path.isfile(os.path.join(self.trash, "files", "B", "sub", "Thumbs.db")))

    def test_files_already_gone_from_a_folder(self):
        self.standard()
        self.scan()
        os.unlink(self.p("B/a2"))
        before, r = self.remove(code=0)
        self.assertEqual(self.items()[self.p("B")],
                         ("removed", f"identical copy of {self.p('A')} (1 of its files were "
                                     f"already gone)"))

    def test_copy_deleted_by_hand(self):
        # A copy that went since the scan is "already gone", not a failure.
        self.standard()
        self.scan()
        os.unlink(self.p("C/u-copy.bin"))
        self.remove(code=0)
        self.assertEqual(self.statuses(), {"removed": 1, "gone": 1})
        self.assertEqual(self.items()[self.p("C/u-copy.bin")][0], "gone")

    def test_scanned_folder_not_there_any_more(self):
        # One of the folders scanned is gone: what was in it is left alone,
        # and said; the rest goes ahead. If nothing else is left, refused.
        self.standard()
        self.scan(roots=[self.p("A"), self.p("B"), self.p("C")])
        shutil.rmtree(self.p("C"))
        r = self.clean_sealed("remove", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertTrue(r.out.startswith(f"Note: {self.p('C')} is not there any more: what "
                                         f"was in it is left alone\n"), r.out)
        self.assertIn("Not there:     1 item in a folder that is not there any more; left "
                      "alone.\n", r.out)
        self.assertIn("Not there:       1 item left alone\n", r.out)
        before, r = self.remove(code=0)
        self.assertEqual(self.statuses(), {"removed": 1})
        self.assertEqual(self.tree(), {"A/", "A/a1", "A/a2", "A/sub/", "A/sub/a3"})
        os.makedirs(self.p("D/x"))
        self.write("D/x/1.bin", blob("one"))
        self.write("D/x/2.bin", blob("one"))
        self.scan(roots=[self.p("D")])
        shutil.rmtree(self.p("D"))
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 2, r)
        self.assertIn(f"nothing can be removed: {self.p('D')} is not there any more", r.err)

    def test_symlink_swapped_in(self):
        self.standard()
        self.scan()
        os.unlink(self.p("B/a2"))
        os.symlink(self.p("A/a2"), self.p("B/a2"))
        os.unlink(self.p("C/u-copy.bin"))
        os.symlink("u.bin", self.p("C/u-copy.bin"))
        before, r = self.remove()
        self.assertEqual(self.snapshot(), before)
        items = self.items()
        self.assertEqual(items[self.p("B")], ("skipped", "a2 is a symlink the scan didn't see"))
        self.assertEqual(items[self.p("C/u-copy.bin")], ("skipped", "it is now a symlink"))
        self.assertTrue(os.path.isfile(self.p("C/u.bin")))

    def test_same_folder_seen_twice(self):
        # B made a bind mount of A (inside the namespace) since the scan: one
        # folder seen through two paths, so removing B would take A with it.
        # The plan names B as blocked; should a plan name it anyway, it is
        # refused just before acting. A bind mount within one drive looks
        # like any folder (same device): only the mount table tells.
        self.standard()
        self.scan()
        before = self.snapshot()
        here = ("it is a mount point (a drive, or another folder, is mounted here): it is "
                "never removed as a whole")
        script = self.patched("nocheck.py", "from cleanup import selection\n"
                                            "selection._Selector.mount_in = lambda self, p: None\n")
        self.sealed(f'mount --bind "{self.p("A")}" "{self.p("B")}"\nstep n remove -n\n'
                    f'pystep rm "{script}" remove -y -q')
        r = self.step("n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Blocked (1): picked to go, but they stay:\n"
                      f"  - {self.p('B')}/\n      {here}\n", r.out)
        r = self.step("rm")
        self.assertEqual(r.rc, 1, r)
        del before["C/u-copy.bin"]          # removed as usual
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.items()[self.p("B")], ("skipped", here))
        self.assertEqual(self.statuses(), {"skipped": 1, "removed": 1})

    def test_hard_link_to_the_copy_that_stays(self):
        # u-copy.bin made a hard link to u.bin since the scan (u.bin stays):
        # its content is u.bin's, verified, and u.bin keeps it, so the name
        # can go; no space is freed. Its date now differs from the scan's.
        self.standard()
        self.scan()
        os.unlink(self.p("C/u-copy.bin"))
        os.link(self.p("C/u.bin"), self.p("C/u-copy.bin"))
        before, r = self.remove(code=0)
        before = {k: v for k, v in before.items()
                  if k != "C/u-copy.bin" and not k.startswith("B/")}
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.items()[self.p("C/u-copy.bin")],
                         ("removed", f"copy of {self.p('C/u.bin')}; it was a hard "
                                     f"link to {self.p('C/u.bin')}, which stays, so no space "
                                     f"is freed"))
        self.assertIn("u-copy.bin", self.in_trash())

    def test_hard_link_seen_through_a_bind_mount(self):
        # A/a1 got a second name since the scan, and B became a bind mount of
        # A: B/a1 is A/a1 itself, the same name in the same folder, so it
        # doesn't count as a copy that stays.
        self.standard()
        self.scan()
        os.link(self.p("A/a1"), os.path.join(self.tmp, "a1-elsewhere"))
        before = self.snapshot()
        self.sealed(f'mount --bind "{self.p("A")}" "{self.p("B")}"\n'
                    f'step rm remove -y -q --only files')
        r = self.step("rm")
        self.assertEqual(r.rc, 1, r)
        del before["C/u-copy.bin"]          # removed as usual
        self.assertEqual(self.snapshot(), before)
        self.assertRegex(self.items()[self.p("B/a1")][1],
                         r"^no verified copy of a1 remains \(.*/A/a1 is the same file on "
                         r"disk, seen through another path\)$")


@needs_unshare
class TestFaults(RemoveBase):
    """Errors and crashes part-way: what was done is exactly what the log
    says, the rest is untouched, a re-run finishes and undo brings it all
    back."""

    def setUp(self):
        super().setUp()
        self.fill(6)
        self.before = self.snapshot()
        self.scan()

    def copies(self):
        """Which x-copy files are still there (sorted folder names)."""
        return sorted(k.split("/")[0] for k in self.snapshot() if k.endswith("x-copy"))

    def undo_all(self):
        """Undo every removal, newest first; everything is back as it was."""
        for _ in range(5):
            r = self.clean_sealed("undo", "-y")
            if r.rc == 2:
                self.assertIn("there is no removal left to undo", r.err)
                break
            self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(), self.before)

    def test_one_item_fails(self):
        script = self.patched("fail.py", """
def put(self, path, before_move=None):
    calls[0] += 1
    if calls[0] == 2:
        raise PermissionError(errno.EACCES, "Permission denied", path)
    return real_put(self, path, before_move)
trash.Trash.put = put
""")
        self.sealed(f'pystep a "{script}" remove -y')
        r = self.step("a")
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(self.statuses(), {"removed": 5, "failed": 1})
        self.assertEqual(self.copies(), ["g01"])
        self.assertEqual(self.items()[self.p("g01/x-copy")], ("failed", "Permission denied"))
        self.assertEqual(len(self.actions()), 5)
        self.assertEqual(self.clean_sealed("remove", "-y").rc, 0)      # the re-run finishes
        self.assertEqual(self.statuses(), {"gone": 5, "removed": 1})
        self.assertEqual(self.copies(), [])
        self.undo_all()

    def test_move_fails_after_it_was_logged(self):
        script = self.patched("fail.py", """
def rename(a, b):
    calls[0] += 1
    if calls[0] == 3:
        raise PermissionError(errno.EACCES, "Permission denied", a)
    return real_rename(a, b)
trash.rename_noreplace = rename
""")
        self.sealed(f'pystep a "{script}" remove -y')
        self.assertEqual(self.step("a").rc, 1)
        self.assertEqual(self.copies(), ["g02"])
        self.assertEqual(len(self.actions()), 6)        # the failed move is logged too
        self.assertEqual(len(os.listdir(os.path.join(self.trash, "info"))), 5)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Items:           6: 5 put back, 1 already back", r.out)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.in_trash(), {})

    def test_drive_error_stops_the_run(self):
        script = self.patched("eio.py", """
def put(self, path, before_move=None):
    calls[0] += 1
    if calls[0] == 3:
        raise OSError(errno.EIO, "Input/output error", path)
    return real_put(self, path, before_move)
trash.Trash.put = put
""")
        self.sealed(f'pystep a "{script}" remove -y')
        r = self.step("a")
        self.assertEqual(r.rc, 1, r)
        meta = self.run_meta()
        self.assertEqual(meta["status"], "drive-gone")
        self.assertIn("Input/output error", meta["error"])
        self.assertEqual(self.statuses(), {"removed": 2, "failed": 1, "not-done": 3})
        self.assertIn("STOPPED: ", r.out)
        self.assertEqual(self.copies(), ["g02", "g03", "g04", "g05"])
        self.assertEqual(self.clean_sealed("remove", "-y").rc, 0)
        self.assertEqual(self.copies(), [])
        self.undo_all()

    def test_log_cannot_be_written(self):
        script = self.patched("log.py", """
def row(self, *a, **k):
    calls[0] += 1
    if calls[0] == 3:
        raise remove.LogError(f"could not write to {self.path}: No space left on device")
    return real_row(self, *a, **k)
remove.Log.row = row
""")
        self.sealed(f'pystep a "{script}" remove -y')
        r = self.step("a")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("No space left on device\nStopped: the log `clean undo` relies on "
                      "can't be written.", r.err)
        meta = self.run_meta()
        self.assertEqual((meta["status"], meta["headline"]),
                         ("failed", "failed: the log of what was done could not be written"))
        # Nothing moved without its line in the log: the third stayed.
        self.assertEqual(self.copies(), ["g02", "g03", "g04", "g05"])
        self.assertEqual(len(self.actions()), 2)
        self.assertEqual(len(os.listdir(os.path.join(self.trash, "info"))), 2)
        self.undo_all()

    def test_log_cannot_be_made(self):
        script = self.patched("log.py", """
def init(self, path):
    raise remove.LogError(f"could not create {path}: Read-only file system")
remove.Log.__init__ = init
""")
        self.sealed(f'pystep a "{script}" remove -y')
        r = self.step("a")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("Nothing was removed: without its log, it couldn't be undone.", r.err)
        self.assertEqual(self.run_meta()["status"], "failed")
        self.assertEqual(self.snapshot(), self.before)

    def test_killed_part_way(self):
        # kill -9 just after the third item's line is logged, before it moves:
        # the run stays "unfinished"; the re-run finishes; undo both.
        script = self.patched("kill.py", """
def put(self, path, before_move=None):
    calls[0] += 1
    def logged(entry):
        before_move(entry)
        if calls[0] == 3:
            os.kill(os.getpid(), signal.SIGKILL)
    return real_put(self, path, logged)
trash.Trash.put = put
""")
        self.sealed(f'pystep a "{script}" remove -y')
        self.assertEqual(self.step("a").rc, 137)
        killed = self.last_run()
        self.assertEqual(killed.status, "unfinished")
        self.assertEqual(self.copies(), ["g02", "g03", "g04", "g05"])
        self.assertEqual(len(self.actions(killed)), 3)
        r = self.clean_sealed("history")
        self.assertIn("unfinished", r.out)
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.statuses(), {"gone": 2, "removed": 4})
        self.assertEqual(self.copies(), [])
        r = self.clean_sealed("undo", "-y")             # the re-run
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Items:           4: 4 put back\n", r.out)
        r = self.clean_sealed("undo", "-y", killed.name)
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Items:           3: 2 put back, 1 already back", r.out)
        self.assertEqual(self.snapshot(), self.before)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 2, r)

    def interrupt(self, sig):
        script = self.patched("slow.py", SLOW_PUT)
        log = os.path.join(self.data_dir, "runs", "*-remove", "actions.tsv")
        self.sealed(f'"$PY" "{script}" remove -y >"$T/steps/i.out" 2>"$T/steps/i.err" '
                    f'</dev/null &\np=$!\n'
                    f'until [ "$(cat {log} 2>/dev/null | wc -l)" -ge 3 ] || ! kill -0 $p 2>/dev/null; do sleep 0.05; done\n'
                    f'kill -{sig} $p\nrc=0; wait $p || rc=$?; echo $rc >"$T/steps/i.rc"')
        r = self.step("i")
        self.assertEqual(r.rc, 130, r)
        run = self.last_run()
        meta = self.run_meta(run)
        self.assertEqual((meta["status"], meta["exit_code"]), ("interrupted", 130))
        self.assertIn("INTERRUPTED: the items not reached are untouched.", r.out)
        st = self.statuses(run)
        self.assertEqual(st.get("removed", 0) + st.get("not-done", 0), 6, st)
        self.assertTrue(st["removed"] >= 2 and st["not-done"] >= 1, st)
        self.assertEqual(len(self.copies()), st["not-done"])
        self.assertEqual(self.clean_sealed("remove", "-y").rc, 0)
        self.assertEqual(self.copies(), [])
        self.undo_all()

    def test_ctrl_c(self):
        self.interrupt("INT")

    def test_kill(self):
        self.interrupt("TERM")


@needs_unshare
class TestDrives(RemoveBase):
    def test_drive_pulled_mid_run(self):
        disk = os.path.join(self.tmp, "disk")
        os.mkdir(disk)
        fill = self.script("fill.py", FILL)
        slow = self.patched("slow.py", SLOW_PUT)
        log = os.path.join(self.data_dir, "runs", "*-remove", "actions.tsv")
        self.sealed(f'mount -t tmpfs drive "{disk}"\n"$PY" "{fill}" "{disk}" 8\n'
                    f'step scan dupes "{disk}" -q\n'
                    f'pystep rm "{slow}" remove -y &\np=$!\n'
                    f'until [ "$(cat {log} 2>/dev/null | wc -l)" -ge 3 ] || ! kill -0 $p 2>/dev/null; do sleep 0.05; done\n'
                    f'umount -l "{disk}"\nwait $p')
        self.assertEqual(self.step("scan").rc, 0)
        r = self.step("rm")
        self.assertEqual(r.rc, 1, r)
        meta = self.run_meta()
        self.assertEqual(meta["status"], "drive-gone")
        self.assertIn("has disappeared (unplugged or unmounted?)", meta["error"])
        self.assertIn("STOPPED: the drive holding", r.out)
        st = self.statuses()
        self.assertNotIn("gone", st)                    # not "already gone": the drive went
        self.assertEqual(st.get("failed"), 1, st)
        self.assertGreaterEqual(st.get("removed", 0), 2)
        self.assertEqual(sum(st.values()), 8)
        self.assertNotIn("Traceback", r.err)

    def drive_script(self):
        """Prelude making fake drives: `drive DEV UUID POINT` mounts a tmpfs
        whose source by-uuid knows as UUID (by-uuid is faked inside)."""
        for d in ("by-uuid", "dev", "media"):
            os.makedirs(os.path.join(self.tmp, d), exist_ok=True)
        return ('mount --bind "$T/by-uuid" /dev/disk/by-uuid\n'
                'drive() { : >"$T/dev/$1"; ln -sf "$T/dev/$1" "$T/by-uuid/$2"; '
                'mkdir -p "$3"; mount -t tmpfs "$T/dev/$1" "$3"; }\n')

    def test_drive_mounted_somewhere_else(self):
        # Scanned at media/D1; removed after it moved to D2, with another
        # drive now at D1 holding the same names; undone after it moved to D3.
        fill = self.script("fill.py", FILL)
        m = os.path.join(self.tmp, "media")
        out = os.path.join(self.tmp, "out")
        self.sealed(self.drive_script() +
                    f'drive sdz1 1234-ABCD "{m}/D1"\n"$PY" "{fill}" "{m}/D1/data" 3\n'
                    f'step scan dupes "{m}/D1/data" -q\n'
                    f'mkdir -p "{m}/D2" "{m}/D3"\nmount --move "{m}/D1" "{m}/D2"\n'
                    f'drive sdy1 9999-0000 "{m}/D1"\n"$PY" "{fill}" "{m}/D1/data" 3\n'
                    f'step rm remove -y\n'
                    f'mkdir -p "{out}"; cp -a "{m}/D2/data" "{out}/removed"\n'
                    f'mount --move "{m}/D2" "{m}/D3"\n'
                    f'step undo undo -y\n'
                    f'cp -a "{m}/D3/data" "{out}/undone"; cp -a "{m}/D1/data" "{out}/other"\n'
                    f'ls -a "{m}/D1" >"{out}/other-top"')
        self.assertEqual(self.step("scan").rc, 0)
        r = self.step("rm")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Note: {m}/D1/data is now at {m}/D2/data (the same drive, mounted "
                      f"somewhere else)", r.out)
        self.assertEqual(sorted(os.listdir(os.path.join(out, "removed", "g00"))), ["x"])
        self.assertEqual(sorted(os.listdir(os.path.join(out, "other", "g00"))), ["x", "x-copy"])
        with open(os.path.join(out, "other-top")) as f:
            self.assertNotIn(".Trash-0", f.read().split())     # the other drive: untouched
        r = self.step("undo")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"is now at {m}/D3", r.out)
        self.assertIn("Items:           3: 3 put back", r.out)
        self.assertEqual(self.snapshot(os.path.join(out, "undone")),
                         self.snapshot(os.path.join(out, "other")))

    def test_drive_not_connected(self):
        # Two scanned folders; the second one's drive "isn't connected" (its
        # run.json names a UUID no drive has): what is on it is left alone,
        # including a copy whose only other copy is there.
        self.fill(2, self.p("R1"), "one")
        self.fill(2, self.p("R2"), "two")
        self.write("R1/z", blob("z"))
        self.write("R2/z", blob("z"))
        before = self.snapshot()
        self.scan(roots=[self.p("R1"), self.p("R2")])
        run = self.last_run()
        meta = self.run_meta(run)
        for entry in meta["roots"]:
            if entry["path"] == self.p("R2"):
                entry.update(uuid="DEAD-0000", drive="TESTDRIVE", inside="/R2")
        with open(run.file("run.json"), "w") as f:
            json.dump(meta, f)
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Note: drive TESTDRIVE, where {self.p('R2')} was scanned, is not "
                      f"connected: what is on it is left alone", r.out)
        self.assertIn("Not connected:   3 items left alone", r.out)
        self.assertEqual(set(before) - set(self.snapshot()), {"R1/g00/x-copy", "R1/g01/x-copy"})
        # With only that drive's items left, nothing can be done.
        r = self.clean_sealed("remove", "-y", "--remove-from", self.p("R2"))
        self.assertEqual(r.rc, 2, r)
        self.assertIn(f"nothing can be removed while the drive of {self.p('R2')} is not "
                      f"connected", r.err)


@needs_unshare
class TestDupesRemove(RemoveBase):
    def test_scan_then_remove(self):
        self.standard()
        before = self.snapshot()
        r = self.clean_sealed("dupes", self.root, "-q", "--remove", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(set(before) - set(self.snapshot()),
                         {"B/a1", "B/a2", "B/sub/a3", "C/u-copy.bin"})
        removal, scan = self.runs()[:2]
        self.assertEqual((removal.command, scan.command), ("remove", "dupes"))
        self.assertEqual(self.run_meta(removal)["source"], scan.name)

    def test_dry_run_and_rules(self):
        self.standard()
        before = self.snapshot()
        r = self.clean_sealed("dupes", self.root, "-q", "--remove", "-n", "--keep-in", self.p("B"))
        self.assertEqual(r.rc, 0, r)
        scan = [x for x in self.runs() if x.command == "dupes"][0]
        self.assertIn(f"  folder  {self.p('A')}\n", r.out)
        self.assertIn(f"To go ahead: clean remove {scan.name} --keep-in {self.p('B')}", r.out)
        self.assertEqual(self.snapshot(), before)

    def test_refusals(self):
        self.standard()
        for args, text in (
                (["--keep-in", self.p("B")], "--keep-in only works with --remove (or run "
                                             "`clean remove` after the scan)"),
                (["--permanent"], "--permanent only works with --remove"),
                (["--remove"], "there is no terminal to ask on before removing"),
                (["--remove", "-y", "--link", "--merge"], "--link and --merge can't be used "
                                                          "together")):
            r = self.clean_sealed("dupes", self.root, "-q", *args)
            self.assertEqual(r.rc, 2, (args, r))
            self.assertIn(text, r.err, args)
        self.assertEqual(self.runs(), [])               # refused before the scan


if __name__ == "__main__":
    unittest.main(verbosity=2)
