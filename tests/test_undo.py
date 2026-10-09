#!/usr/bin/env python3
"""`clean undo` and `clean trash`, for real. Everything runs inside a sealed
namespace (Base.sealed): the trees, the home Trash and the drives (tmpfs
mounts) are all in the test's temp folder, so your files, drives and Trash
are never involved.

Covered, for undo: everything comes back byte for byte (contents, dates,
modes, odd names, empty folders); nothing is ever overwritten (what is in
the way stays, and a later undo finishes the job); items emptied from the
Trash, or whose record there no longer matches, stay where they are; a
Trash name the removal had to try twice; which run is undone, and what is
refused; dry runs and asking first; Ctrl-C, SIGTERM and a pulled drive.
For `clean trash`: list (by drive, item by item, with the run that put
each there), restore (folders, things trashed from inside them, the newest
of two, never overwriting) and empty (by age, by run, by drive; items with
no record). And `gio trash` sees what a removal did, and undo copes with
what gio put back."""

import json
import os
import re
import shutil
import time
import unittest

from helpers import HAVE_UNSHARE, blob, needs_unshare
from test_remove import FILL, RemoveBase

from cleanup import runs as _runs

# Asks on a terminal: `tty.py ANSWER ARG...` runs `clean ARG...` with a
# pseudo-terminal as stdin, types ANSWER, saves exit status and output.
TTY = r"""
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
"""

# `PUT PATH...`: each to its Trash, as the Files app would (not a removal).
PUT = r"""
import sys
from cleanup.actions import trash
for p in sys.argv[1:]:
    trash.for_path(p).put(p)
"""

# Undo restoring slowly, so that it can be stopped part-way.
SLOW_RESTORE = """
real_restore = trash.Entry.restore
def restore(self, to=None):
    time.sleep(0.3)
    return real_restore(self, to)
trash.Entry.restore = restore
"""

HERE_NOTE = "something new is at {}, so the item stays in the Trash ({}); nothing was overwritten"


class UndoBase(RemoveBase):
    def removed(self, *args):
        """standard(), scanned and removed (B and C/u-copy.bin, to the home
        Trash). Returns (the tree before, the removal)."""
        self.standard()
        before = self.snapshot()
        self.scan()
        r = self.clean_sealed("remove", "-y", *args)
        self.assertEqual(r.rc, 0, r)
        return before, self.remove_runs()[0]

    def tfile(self, name, folder=None):
        return os.path.join(folder or self.trash, "files", name)

    def tinfo(self, name, folder=None):
        return os.path.join(folder or self.trash, "info", name + ".trashinfo")

    def set_info(self, name, key, value):
        """Change one line of an item's .trashinfo."""
        path = self.tinfo(name)
        with open(path) as f:
            lines = [f"{key}={value}" if ln.startswith(key + "=") else ln
                     for ln in f.read().splitlines()]
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def trash_names(self, folder=None):
        """(sorted names in files/, sorted names in info/) of a Trash."""
        return tuple(sorted(os.listdir(os.path.join(folder or self.trash, x)))
                     for x in ("files", "info"))

    def stats(self):
        """{relative path: (mode, mtime_ns)} for files; {path/: mode} for
        folders (their dates change as things move in and out)."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for n in dirnames:
                p = os.path.join(dirpath, n)
                out[os.path.relpath(p, self.root) + "/"] = os.lstat(p).st_mode
            for n in filenames:
                st = os.lstat(os.path.join(dirpath, n))
                out[os.path.relpath(os.path.join(dirpath, n), self.root)] = (
                    st.st_mode, st.st_mtime_ns)
        return out

    def undo_runs(self):
        return [r for r in self.runs() if r.command == "undo"]

    def ask(self, answer, *args):
        """`clean ARG...` on a terminal, answering ANSWER: (rc, output)."""
        tty = self.script("tty.py", TTY)
        self.sealed(f'"$PY" "{tty}" "{answer}" ' + " ".join(f'"{a}"' for a in args))
        with open(os.path.join(self.tmp, "tty-" + answer)) as f:
            rc, rest = f.read().split("\n", 1)
        return int(rc), rest


@needs_unshare
class TestUndo(UndoBase):
    def test_byte_for_byte(self):
        # Odd names, an empty folder, modes and dates: all come back as they
        # were; the Trash is left empty; the removal is marked undone.
        self.write("A/new\nline", blob("nl"))
        self.write(os.fsdecode(b"A/\xff\xfe latin.bin"), blob("latin"))
        self.write("A/caf\u00e9 \u2615.txt", blob("cafe"))
        os.makedirs(self.p("A/empty/inner"))
        self.standard()
        os.chmod(self.p("B/a2"), 0o600)
        os.chmod(self.p("B/sub"), 0o700)
        os.utime(self.p("B/a1"), ns=(1, 1234567890123456789))
        tree, stats, before = self.tree(), self.stats(), self.snapshot()
        self.scan()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        removal = self.last_run()
        self.assertFalse(os.path.lexists(self.p("B")))
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Items:           2: 2 put back\n", r.out)
        self.assertEqual((self.tree(), self.stats(), self.snapshot()), (tree, stats, before))
        self.assertEqual(self.trash_names(), ([], []))
        undo = self.last_run()
        meta = self.run_meta(undo)
        self.assertEqual((meta["command"], meta["mode"], meta["source"], meta["status"],
                          meta["exit_code"], meta["headline"]),
                         ("undo", "undo", removal.name, "done", 0,
                          f"put back 2 items from run {removal.name}"))
        self.assertEqual([(a, path) for a, path, _, _ in self.actions(undo)],
                         [("restore", self.p("C/u-copy.bin")), ("restore", self.p("B"))])
        self.assertEqual(self.run_meta(removal)["undone_by"], {"run": undo.name, "full": True})
        self.assertTrue(all(undo.has(f) for f in ("report.html", "summary.txt", "run.json")))
        # Nothing is left to undo: by default, or by name.
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no removal left to undo", r.err)
        r = self.clean_sealed("undo", "-y", removal.name)
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(r.out, "Already back: 2 items.\n\nNothing to put back.\n")
        self.assertEqual(self.snapshot(), before)

    def test_never_overwrites(self):
        before, removal = self.removed()
        self.write("C/u-copy.bin", "mine now")
        self.write("B/mine.txt", "mine too")
        mine = self.snapshot()
        in_the_way = (f"Not put back: something new is in the way (nothing was overwritten) "
                      f"(2):\n"
                      f"  file    {self.p('C/u-copy.bin')}\n          "
                      + HERE_NOTE.format(self.p("C/u-copy.bin"), self.tfile("u-copy.bin"))
                      + f"\n  folder  {self.p('B')}\n          "
                      + HERE_NOTE.format(self.p("B"), self.tfile("B")) + "\n")
        r = self.clean_sealed("undo", "-n")
        self.assertEqual(r.rc, 1, r)
        self.assertIn(in_the_way, r.out)
        self.assertNotIn("To go ahead", r.out)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 1, r)
        self.assertIn(in_the_way, r.out)
        self.assertTrue(r.out.endswith("Nothing to put back.\n"), r.out)
        self.assertEqual(self.snapshot(), mine)
        self.assertEqual(self.trash_names()[0], ["B", "u-copy.bin"])
        # B's way is cleared: B comes back; the file is still in the way.
        os.rename(self.p("B"), self.p("B.mine"))
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("Items:           2: 1 put back, 1 in the way\n", r.out)
        self.assertIn(f"What is in the way was left alone. Move it aside and run `clean undo "
                      f"{removal.name}` again, or look in the Trash: clean trash list", r.out)
        self.assertEqual(self.run_meta(removal)["undone_by"]["full"], False)
        self.assertEqual(self.snapshot(self.p("B")), self.snapshot(self.p("A")))
        self.assertEqual(self.trash_names(), (["u-copy.bin"], ["u-copy.bin.trashinfo"]))
        # Then the file's: the newest removal not fully undone is still this one.
        os.rename(self.p("C/u-copy.bin"), self.p("C/u-copy.mine"))
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Undoing:         run {removal.name} ", r.out)
        self.assertIn("Items:           2: 1 put back, 1 already back\n", r.out)
        after = self.snapshot()
        self.assertEqual(after.pop("B.mine/mine.txt"), b"mine too")
        self.assertEqual(after.pop("C/u-copy.mine"), b"mine now")
        self.assertEqual(after, before)
        self.assertEqual(self.trash_names(), ([], []))
        self.assertEqual(self.run_meta(removal)["undone_by"]["full"], True)

    def test_trash_changed_since(self):
        # Five items: one emptied from the Trash, three whose record there no
        # longer matches (none, another place, another time), one untouched.
        self.fill(5)
        before = self.snapshot()
        self.scan()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        removal = self.last_run()
        names = {path: os.path.basename(to) for _, path, to, _ in self.actions(removal)}
        self.assertEqual(sorted(names.values()),
                         ["x-copy", "x-copy.2", "x-copy.3", "x-copy.4", "x-copy.5"])
        by = {names[self.p(f"g0{i}/x-copy")]: self.p(f"g0{i}/x-copy") for i in range(5)}
        gone, no_info, moved, dated, fine = sorted(by)
        os.unlink(self.tfile(gone))
        os.unlink(self.tinfo(gone))
        os.unlink(self.tinfo(no_info))
        self.set_info(moved, "Path", "/somewhere/else")
        self.set_info(dated, "DeletionDate", "2020-01-02T03:04:05")
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 1, r)
        out = r.out
        self.assertIn(f"Can't come back: not in the Trash any more (1):\n"
                      f"  file    {by[gone]}\n"
                      f"          not in the Trash any more (it was emptied?)\n", out)
        self.assertIn("Left as they are: changed since (3):\n", out)
        self.assertIn(f"  file    {by[no_info]}\n          its record in the Trash "
                      f"({no_info}.trashinfo) is missing or unreadable: it stays in the "
                      f"Trash\n", out)
        self.assertIn(f"  file    {by[moved]}\n          the item called {moved} in the Trash "
                      f"came from /somewhere/else, not from here: it stays in the Trash\n", out)
        self.assertIn(f"  file    {by[dated]}\n          the item called {dated} in the Trash "
                      f"was put there at 2020-01-02T03:04:05, not by this removal: it stays "
                      f"there\n", out)
        self.assertIn("Items:           5: 1 put back, 1 not in the Trash, 3 changed since\n", out)
        self.assertEqual(self.run_meta()["headline"],
                         f"put back 1 item from run {removal.name} · 1 not in the Trash · "
                         f"3 changed since")
        after = self.snapshot()
        self.assertEqual(sorted(set(before) - set(after)),
                         sorted(os.path.relpath(by[n], self.root)
                                for n in (gone, no_info, moved, dated)))
        self.assertEqual(self.trash_names()[0], sorted([dated, moved, no_info]))
        # Again: what came back is said so; the rest stays as it is.
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("Already back: 1 item.\n", r.out)
        self.assertTrue(r.out.endswith("Nothing to put back.\n"), r.out)
        self.assertEqual(self.snapshot(), after)

    def test_name_tried_twice(self):
        # An item with no record holds the name B in the Trash: the removal
        # logged B, found it taken, then logged and used B.2. Undo brings B
        # back from B.2, and leaves the other item alone.
        self.standard()
        before = self.snapshot()
        self.scan()
        os.makedirs(self.tfile("B"))
        os.makedirs(os.path.dirname(self.tinfo("B")))
        with open(os.path.join(self.tfile("B"), "theirs"), "w") as f:
            f.write("theirs")
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual([(a, p, t) for a, p, t, _ in self.actions()], [
            ("trash", self.p("B"), self.tfile("B")),
            ("trash", self.p("B"), self.tfile("B.2")),
            ("trash", self.p("C/u-copy.bin"), self.tfile("u-copy.bin"))])
        r = self.clean_sealed("undo", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Would put back (2):\n  file    {self.p('C/u-copy.bin')}\n"
                      f"  folder  {self.p('B')}\n", r.out)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.trash_names(), (["B"], []))
        self.assertEqual(self.snapshot(self.tfile("B")), {"theirs": b"theirs"})

    def test_dry_run(self):
        before, removal = self.removed()
        after = self.snapshot()
        r = self.clean_sealed("undo", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertTrue(r.out.startswith(
            f"Would put back (2):\n  file    {self.p('C/u-copy.bin')}\n"
            f"  folder  {self.p('B')}\n\n"), r.out)
        for text in ("Mode:            DRY RUN (nothing moved)\n",
                     f"Undoing:         run {removal.name} (",
                     "Items:           2: 2 would come back\n",
                     f"To go ahead:     clean undo {removal.name}\n"):
            self.assertIn(text, r.out)
        self.assertEqual(self.snapshot(), after)
        dry = self.last_run()
        meta = self.run_meta(dry)
        self.assertEqual((meta["command"], meta["mode"], meta["source"], meta["headline"]),
                         ("undo", "dry-run", removal.name,
                          f"would put back 2 items from run {removal.name}"))
        self.assertFalse(dry.has("actions.tsv"))
        self.assertNotIn("undone_by", self.run_meta(removal))
        # A dry run of an undo is not something to undo, nor a removal.
        r = self.clean_sealed("undo", "-y", dry.name)
        self.assertEqual(r.rc, 2, r)
        self.assertIn(f"run {dry.name} is a `clean undo` run; `clean undo` undoes `clean "
                      f"remove` runs", r.err)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(), before)

    def test_which_run(self):
        # Two removals: undo takes the newest not undone yet, then the other.
        self.standard()
        before = self.snapshot()
        self.scan(roots=[self.p("C")])
        self.assertEqual(self.clean_sealed("remove", "-y").rc, 0)
        first = self.last_run()
        dupes = self.scan(roots=[self.p("A"), self.p("B")])
        self.assertEqual(self.clean_sealed("remove", "-y").rc, 0)
        second = self.last_run()
        self.assertFalse(os.path.lexists(self.p("B")) or os.path.lexists(self.p("C/u-copy.bin")))
        for run, path in ((second, "B"), (first, "C/u-copy.bin")):
            r = self.clean_sealed("undo", "-y")
            self.assertEqual(r.rc, 0, r)
            self.assertIn(f"Undoing:         run {run.name} ", r.out)
            self.assertTrue(os.path.lexists(self.p(path)))
        self.assertEqual(self.snapshot(), before)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no removal left to undo", r.err)
        # Number 1 of `clean history` is the newest run: the last undo.
        r = self.clean_sealed("undo", "-y", "1")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("is a `clean undo` run; `clean undo` undoes `clean remove` runs", r.err)
        r = self.clean_sealed("undo", "-y", dupes)
        self.assertEqual(r.rc, 2, r)
        self.assertIn(f"run {dupes} is a `clean dupes` run", r.err)

    def test_refusals(self):
        before, removal = self.removed()
        after = self.snapshot()
        r = self.clean_sealed("undo")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no terminal to ask on before putting things back: add -y to "
                      "go ahead without asking (or -n to see what would come back)", r.err)
        self.assertEqual(self.undo_runs(), [])
        r = self.clean_sealed("remove", "-n")
        dry = self.last_run()
        self.assertEqual((r.rc, dry.command), (0, "remove"))
        r = self.clean_sealed("undo", "-y", dry.name)
        self.assertEqual(r.rc, 2, r)
        self.assertIn(f"run {dry.name} was a dry run (-n): nothing was removed, so there is "
                      f"nothing to undo", r.err)
        self.fake_run("2020-01-02_030405-remove", mode="trash")
        r = self.clean_sealed("undo", "-y", "2020-01-02_030405")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("run 2020-01-02_030405-remove has no log of what it did (actions.tsv), "
                      "so it can't be undone", r.err)
        self.fake_run("2020-01-03_030405-remove", status="running", mode="trash",
                      files=("actions.tsv",), pid=os.getpid(),
                      proc_start=_runs.proc_start(os.getpid()))
        r = self.clean_sealed("undo", "-y", "2020-01-03_030405")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("run 2020-01-03_030405-remove is still running; undo it once it has "
                      "finished", r.err)
        r = self.clean_sealed("undo", "-y", "no-such-run")
        self.assertEqual(r.rc, 2, r)
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(self.undo_runs(), [])
        # Asked on a terminal: no (or nothing) moves nothing; yes goes ahead.
        for answer in ("n", ""):
            rc, out = self.ask(answer, "undo", removal.name)
            self.assertEqual(rc, 2, out)
            self.assertIn(f"Put back 2 items removed by run {removal.name}? [y/N] Nothing "
                          f"was moved.", out)
        self.assertEqual(self.snapshot(), after)
        rc, out = self.ask("y", "undo", removal.name)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.snapshot(), before)

    def stop_part_way(self, sig):
        self.fill(6)
        before = self.snapshot()
        self.scan()
        self.assertEqual(self.clean_sealed("remove", "-y").rc, 0)
        removal = self.last_run()
        script = self.patched("slow.py", SLOW_RESTORE)
        log = os.path.join(self.data_dir, "runs", "*-undo", "actions.tsv")
        self.sealed(f'"$PY" "{script}" undo -y >"$T/steps/u.out" 2>"$T/steps/u.err" '
                    f'</dev/null &\np=$!\n'
                    f'until [ "$(cat {log} 2>/dev/null | wc -l)" -ge 3 ] || ! kill -0 $p 2>/dev/null; do sleep 0.05; done\n'
                    f'kill -{sig} $p\nrc=0; wait $p || rc=$?; echo $rc >"$T/steps/u.rc"')
        r = self.step("u")
        self.assertEqual(r.rc, 130, r)
        self.assertIn("INTERRUPTED: run the same command again to finish; what is back "
                      "already is skipped.", r.out)
        meta = self.run_meta()
        self.assertEqual((meta["status"], meta["exit_code"]), ("interrupted", 130))
        back = meta["totals"]["restored"]
        self.assertTrue(2 <= back < 6, meta["totals"])
        self.assertEqual(meta["totals"]["not_done"], 6 - back)
        self.assertEqual(self.run_meta(removal)["undone_by"]["full"], False)
        self.assertEqual(sum(k.endswith("x-copy") for k in self.snapshot()), back)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Items:           6: {6 - back} put back, {back} already back\n", r.out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.trash_names(), ([], []))

    def test_ctrl_c(self):
        self.stop_part_way("INT")

    def test_sigterm(self):
        self.stop_part_way("TERM")

    def test_drive_pulled(self):
        # The drive goes while undo is putting its items back: it stops,
        # saying so; once the drive is back, undo finishes.
        disk = os.path.join(self.tmp, "disk")
        os.mkdir(disk)
        fill = self.script("fill.py", FILL)
        slow = self.patched("slow.py", SLOW_RESTORE)
        log = os.path.join(self.data_dir, "runs", "*-undo", "actions.tsv")
        self.sealed(f'mount -t tmpfs drive "{disk}"\n"$PY" "{fill}" "{disk}" 6\n'
                    f'cp -a "{disk}" "$T/before"\n'
                    f'step scan dupes "{disk}" -q\nstep rm remove -y\n'
                    f'pystep undo "{slow}" undo -y &\np=$!\n'
                    f'until [ "$(cat {log} 2>/dev/null | wc -l)" -ge 3 ] || ! kill -0 $p 2>/dev/null; do sleep 0.05; done\n'
                    f'mkdir "$T/moved"; mount --move "{disk}" "$T/moved"\nwait $p\n'
                    f'ls -A "{disk}" > "$T/bare"\n'
                    f'mount --move "$T/moved" "{disk}"\nstep again undo -y\n'
                    f'cp -a "{disk}" "$T/after"\n')
        self.assertEqual(self.step("rm").rc, 0)
        r = self.step("undo")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("STOPPED: ", r.out)
        self.assertIn("Once the drive is back, run the same command again to finish.", r.out)
        self.assertNotIn("Traceback", r.err)
        undo = self.undo_runs()[-1]
        meta = self.run_meta(undo)
        self.assertEqual(meta["status"], "drive-gone")
        self.assertTrue(1 <= meta["totals"]["restored"] <= 5, meta["totals"])
        # Nothing was made on the empty folder the drive was mounted on.
        with open(os.path.join(self.tmp, "bare")) as f:
            self.assertEqual(f.read(), "")
        r = self.step("again")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(os.path.join(self.tmp, "after")),
                         self.snapshot(os.path.join(self.tmp, "before")))


@needs_unshare
class TestTrashCommand(UndoBase):
    def test_list(self):
        # The home Trash (a removal's two items, and one of yours) and a
        # drive's (two of yours); --drive picks one.
        put = self.script("put.py", PUT)
        disk = os.path.join(self.tmp, "disk")
        os.mkdir(disk)
        _, removal = self.removed()
        self.write("mine.txt", blob("mine", 1000))
        self.sealed(f'mount -t tmpfs disk "{disk}"\n'
                    f'echo d > "{disk}/d.txt"; mkdir -p "{disk}/dir/in"; '
                    f'echo e > "{disk}/dir/e"; echo f > "{disk}/dir/in/f"\n'
                    f'"$PY" "{put}" "{disk}/d.txt" "{disk}/dir" "{self.p("mine.txt")}"\n'
                    f'step all trash\nstep v trash list -v\nstep home trash list --drive home\n'
                    f'step disk trash list --drive "{disk}/"\n'
                    f'step bad trash list --drive nowhere\n')
        home = (f"Trash of the drive of your home folder: {self.trash}\n"
                f"  3 items, 12.7 KB; 2 put there by `clean remove`\n")
        drive = f"Trash of the drive at {disk}: {disk}/.Trash-0\n  2 items, 6 B\n"
        r = self.step("all")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(home + drive, r.out)
        self.assertTrue(r.out.endswith(
            "In all: 5 items, 12.7 KB.\nEmpty it for good: clean trash empty (--older-than "
            "DAYS, --drive DRIVE or --run RUN for only part)\n"), r.out)
        r = self.step("v")
        self.assertEqual(r.rc, 0, r)
        date = r"\d{4}-\d\d-\d\d \d\d:\d\d"
        # oldest first, then by name within one second: compare as a set
        lines = r.out.split("put there by `clean remove`\n", 1)[1].split("\nTrash of")[0]
        want = [rf"    {date}     8.8 KB  {self.p('B')}/  \(3 files\)  \[run {removal.name}\]",
                rf"    {date}     2.9 KB  {self.p('C/u-copy.bin')}  \[run {removal.name}\]",
                rf"    {date}     1000 B  {self.p('mine.txt')}"]
        got = lines.split("\n")
        self.assertEqual(len(got), 3, r.out)
        for pattern in want:
            self.assertEqual(sum(1 for g in got if re.fullmatch(pattern, g)), 1,
                             (pattern, r.out))
        self.assertRegex(r.out, rf"  2 items, 6 B\n    {date}        2 B  {disk}/d.txt\n"
                                rf"    {date}        4 B  {disk}/dir/  \(2 files\)\n")
        r = self.step("home")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(r.out, home + "In all: 3 items, 12.7 KB.\nEmpty it for good: clean "
                         "trash empty (--older-than DAYS, --drive DRIVE or --run RUN for only "
                         "part)\n")
        r = self.step("disk")
        self.assertEqual(r.rc, 0, r)
        self.assertTrue(r.out.startswith(drive + "In all: 2 items, 6 B.\n"), r.out)
        r = self.step("bad")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("--drive nowhere: no connected drive with a Trash is called that "
                      "(give a folder on it, its name, or home)", r.err)

    def test_restore(self):
        put = self.script("put.py", PUT)
        self.write("P/f1", "f1")
        self.write("P/sub/f2", "f2")
        self.write("x.txt", "x old")
        self.write("y.txt", "y")
        self.sealed(f'"$PY" "{put}" "{self.p("P/sub/f2")}" "{self.p("P")}" "{self.p("x.txt")}" '
                    f'"{self.p("y.txt")}"')
        self.set_info("x.txt", "DeletionDate", "2020-01-02T03:04:05")
        self.write("x.txt", "x new")
        self.write("y.txt", "y is back")
        self.sealed(f'"$PY" "{put}" "{self.p("x.txt")}"')
        self.assertEqual(self.trash_names()[0], ["P", "f2", "x.txt", "x.txt.2", "y.txt"])
        self.assertEqual(self.tree(), {"y.txt"})
        r = self.clean_sealed("trash", "restore", self.p("P"))
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no terminal to ask on: add -y", r.err)
        args = [self.p(x) for x in ("P", "x.txt", "y.txt", "P/f1", "nothing")]
        r = self.clean_sealed("trash", "restore", "-n", *args)
        self.assertEqual(r.rc, 1, r)
        since = r"\(in the Trash since \d{4}-\d\d-\d\d \d\d:\d\d\)"
        self.assertRegex(r.out, (
            rf"^Not in the Trash by itself: {self.p('P/f1')} is inside {self.p('P')}, which "
            rf"is; restore that\nNot in the Trash: {self.p('nothing')}\n"
            rf"  would come back: {self.p('P')}  {since}\n"
            rf"  would come back: {self.p('P/sub/f2')}  {since}\n"
            rf"  would come back: {self.p('x.txt')}  {since}\n"
            rf"  in the way: {self.p('y.txt')} exists again, so its item stays in the Trash "
            rf"\({self.tfile('y.txt')}\)\n$"))
        self.assertEqual(self.tree(), {"y.txt"})
        r = self.clean_sealed("trash", "restore", "-y", *args[:3])
        self.assertEqual(r.rc, 1, r)
        self.assertTrue(r.out.endswith("Put back 3 items.\n"), r.out)
        self.assertEqual(self.snapshot(), {"P/f1": b"f1", "P/sub/f2": b"f2", "x.txt": b"x new",
                                           "y.txt": b"y is back"})
        self.assertEqual(self.trash_names(),
                         (["x.txt", "y.txt"], ["x.txt.trashinfo", "y.txt.trashinfo"]))
        r = self.clean_sealed("trash", "restore", "-y", self.p("P"))
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(r.out, f"Not in the Trash: {self.p('P')}\n")

    def test_empty(self):
        put = self.script("put.py", PUT)
        disk = os.path.join(self.tmp, "disk")
        os.mkdir(disk)
        before, removal = self.removed()
        self.write("old.txt", blob("old", 500))
        self.write("new.txt", blob("new", 700))
        self.sealed(f'"$PY" "{put}" "{self.p("old.txt")}" "{self.p("new.txt")}"')
        self.set_info("old.txt", "DeletionDate", time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 40 * 86400)))
        with open(self.tfile("orphan"), "w") as f:
            f.write("no record")
        self.sealed(
            f'mount -t tmpfs disk "{disk}"\necho d > "{disk}/d.txt"\n"$PY" "{put}" "{disk}/d.txt"\n'
            'step tty trash empty\n'
            'step n trash empty -n --older-than 30\n'
            'step old trash empty -y --older-than 30d\n'
            f'step back trash restore -y "{self.p("C/u-copy.bin")}"\n'
            f'step run trash empty -y --run {removal.name}\n'
            'step undo undo -y\n'
            f'step undo2 undo -y {removal.name}\n'
            f'step disk trash empty -y --drive "{disk}"\n'
            f'ls -A "{disk}/.Trash-0/files" > "$T/disk-left"\n'
            f'ls -A "{self.trash}/files" "{self.trash}/info" > "$T/home-left"\n'
            'step rest trash empty -y\n'
            'step none trash empty -y\n'
            'step list trash\n')
        r = self.step("tty")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no terminal to ask on: add -y", r.err)
        r = self.step("n")
        self.assertEqual(r.rc, 0, r)
        self.assertRegex(r.out, rf"^Would delete for good: 1 item \(1 file, 500 B\)\n"
                                rf"  \d{{4}}-\d\d-\d\d \d\d:\d\d      500 B  "
                                rf"{self.p('old.txt')}\n$")
        r = self.step("old")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Emptied:         deleted 1 item from the Trash for good (500 B)\n", r.out)
        trash_run = [x for x in self.runs() if x.command == "trash"][-1]
        meta = self.run_meta(trash_run)
        self.assertEqual((meta["mode"], meta["status"], meta["older_than"]),
                         ("empty", "done", 30))
        self.assertEqual([(a, p) for a, _, p, _ in self.actions(trash_run)],
                         [("empty", self.p("old.txt"))])
        self.assertEqual(self.step("back").rc, 0)
        # What the removal put there: only B is left (u-copy.bin is back).
        r = self.step("run")
        self.assertEqual(r.rc, 0, r)
        for text in ("Emptied:         deleted 1 item from the Trash for good (8.8 KB)\n",
                     f"From:            what run {removal.name} put in the Trash\n",
                     f"No undo left:    run {removal.name} (nothing of it is in the Trash "
                     f"any more)\n"):
            self.assertIn(text, r.out)
        run_meta = self.run_meta(removal)
        self.assertRegex(run_meta["emptied_by"], r"-trash(-\d+)?$")
        r = self.step("undo")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("there is no removal left to undo", r.err)
        r = self.step("undo2")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(r.out, f"Already back: 1 item.\nCan't come back: not in the Trash any "
                                f"more (1):\n  folder  {self.p('B')}\n          not in the "
                                f"Trash any more (it was emptied?)\n\nNothing to put back.\n")
        r = self.step("disk")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Emptied:         deleted 1 item from the Trash for good (2 B)\n", r.out)
        with open(os.path.join(self.tmp, "disk-left")) as f:
            self.assertEqual(f.read(), "")
        with open(os.path.join(self.tmp, "home-left")) as f:
            self.assertEqual(f.read(), f"{self.trash}/files:\nnew.txt\norphan\n\n"
                                       f"{self.trash}/info:\nnew.txt.trashinfo\n")
        r = self.step("rest")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Emptied:         deleted 2 items from the Trash for good (709 B)\n", r.out)
        self.assertEqual(self.trash_names(), ([], []))
        r = self.step("none")
        self.assertEqual((r.rc, r.out), (0, "Nothing to empty.\n"))
        r = self.step("list")
        self.assertIn("In all: 0 items, 0 B.\n", r.out)
        expected = {k: v for k, v in before.items() if not k.startswith("B/")}
        self.assertEqual(self.snapshot(), expected)

    @unittest.skipUnless(HAVE_UNSHARE and shutil.which("gio") and shutil.which("dbus-run-session"),
                         "needs gio and dbus-run-session")
    def test_gio_sees_what_was_removed(self):
        # gio (on a private session bus in the namespace) lists what two
        # removals put in the home Trash and a drive's, and puts one back;
        # undo then says so and brings back the rest.
        fill = self.script("fill.py", FILL)
        disk = os.path.join(self.tmp, "disk")
        os.mkdir(disk)
        self.standard()
        before = self.snapshot()
        snap = self.script("snap.py", "import json, os, sys\nout = {}\n"
                           "for dp, _, ns in os.walk(sys.argv[2]):\n"
                           "    for n in ns:\n"
                           "        p = os.path.join(dp, n)\n"
                           "        out[os.path.relpath(p, sys.argv[2])] = open(p).read()\n"
                           "json.dump(out, open(sys.argv[1], 'w'))\n")
        self.sealed(f'mount -t ramfs disk "{disk}"\n"$PY" "{fill}" "{disk}" 2\n'
                    f'"$PY" "{snap}" "$T/disk-before.json" "{disk}"\n'
                    f'step s1 dupes "{self.root}" -q\nstep r1 remove -y\n'
                    f'step s2 dupes "{disk}" -q\nstep r2 remove -y\n'
                    'dbus-run-session -- gio trash --list > "$T/gio.list"\n'
                    f'uri=$(awk -F "\t" -v p="{self.p("B")}" \'$2 == p {{print $1}}\' '
                    '"$T/gio.list")\n'
                    'dbus-run-session -- gio trash --restore "$uri"\n'
                    'step u2 undo -y\nstep u1 undo -y\n'
                    f'"$PY" "{snap}" "$T/disk-after.json" "{disk}"\n')
        for name in ("r1", "r2"):
            self.assertEqual(self.step(name).rc, 0, self.step(name))
        with open(os.path.join(self.tmp, "gio.list")) as f:
            listed = sorted(line.split("\t")[1] for line in f.read().splitlines())
        self.assertEqual(listed, sorted([self.p("B"), self.p("C/u-copy.bin"),
                                         f"{disk}/g00/x-copy", f"{disk}/g01/x-copy"]))
        r = self.step("u2")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Items:           2: 2 put back\n", r.out)
        r = self.step("u1")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Already back: 1 item.\n", r.out)
        self.assertIn("Items:           2: 1 put back, 1 already back\n", r.out)
        self.assertEqual(self.snapshot(), before)
        with open(os.path.join(self.tmp, "disk-before.json")) as a, \
                open(os.path.join(self.tmp, "disk-after.json")) as b:
            self.assertEqual(json.load(a), json.load(b))


if __name__ == "__main__":
    unittest.main()
