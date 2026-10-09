#!/usr/bin/env python3
"""`clean remove --merge` and `--link`, for real, inside a sealed namespace
(Base.sealed) on trees in the test's temp folder; drives are tmpfs mounts
made in there.

Covered: a diverged folder merged into the one that stays, with each way of
handling a name taken by a different file (--conflicts both, skip, newer);
a partial merge finished by a re-run; a merge across two drives (copied,
synced, read back, then undone) and a copy that doesn't read back the same;
undo of all of it, byte for byte. Hard links on the test folder's drive and
on tmpfs, made twice, undone; drives that can't hold hard links (exFAT, or
a link refused with EPERM); copies on another drive than the one that
stays."""

import json
import os
import shutil
import time
import unittest

from helpers import blob, needs_unshare
from test_remove import RemoveBase

# `SNAP OUT TOP...`: every file under each TOP as {path: [inode, links,
# content]} (content as latin-1 text), for checks after the namespace (and
# its mounts) is gone.
SNAP = r"""
import json, os, sys
out = {}
for top in sys.argv[2:]:
    for dp, dns, ns in os.walk(top):
        for n in ns:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            with open(p, "rb") as f:
                out[p] = [st.st_ino, st.st_nlink, f.read().decode("latin-1")]
with open(sys.argv[1], "w") as f:
    json.dump(out, f)
"""


class MergeBase(RemoveBase):
    def pair(self):
        """A/ and B/ went separate ways: 8 files in common (half in sub/);
        A has 3 of its own, B 2 (one in new/), and each has a doc.txt of
        its own. The scan pairs them (--similar 50); A, the bigger, stays."""
        a, b = "A", "B"
        for i in range(8):
            self.write(os.path.join(a, "sub" if i % 2 else "", f"s{i}"), blob(f"s{i}"))
        shutil.copytree(self.p(a), self.p(b))
        for n in ("a1", "a2", "a3"):
            self.write(os.path.join(a, n), blob("A " + n))
        self.write(os.path.join(b, "b1"), blob("B b1"))
        self.write(os.path.join(b, "new", "b2"), blob("B b2"))
        self.write(os.path.join(a, "doc.txt"), blob("doc A"))
        self.write(os.path.join(b, "doc.txt"), blob("doc B"))

    def age(self, rel, hours):
        t = time.time() - hours * 3600
        os.utime(self.p(rel), (t, t))

    def merged(self, before, extra):
        """The tree once B is merged into A: A as it was, plus `extra`
        {path in A: path in B it came from}."""
        out = {k: v for k, v in before.items() if not k.startswith("B/")}
        out.update({a: before[b] for a, b in extra.items()})
        return out

    def snaps(self, name):
        with open(os.path.join(self.tmp, name + ".json")) as f:
            return {k: (ino, n, data.encode("latin-1"))
                    for k, (ino, n, data) in json.load(f).items()}


@needs_unshare
class TestMerge(MergeBase):
    def test_nothing_to_do_without_merge(self):
        self.pair()
        self.scan("--similar", "50")
        r = self.clean_sealed("remove", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Nothing to remove", r.out + r.err)
        r = self.clean_sealed("remove", "-n", "--merge")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Would merge 1 folder into the folder that stays (what is left of it "
                      "then goes to the Trash):\n"
                      f"  merge   {self.p('B')}\n          into {self.p('A')}  (3 files only "
                      "there move in)\n", r.out)
        self.assertIn("Would merge:     1 folder into the folder that stays\n", r.out)
        self.assertNotIn("Would remove:", r.out)
        with open(self.last_run().file("report.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn('<td class="what">merge · 3 files only here move in</td>'
                      f'<td class="why">diverged copy of {self.p("A")}</td>', page)
        self.assertIn('<div class="n">1</div><div class="l">item</div>', page)
        # With other copies to remove too.
        self.write("C/x", blob("x"))
        self.write("C/x-copy", blob("x"))
        self.scan("--similar", "50")
        r = self.clean_sealed("remove", "-n", "--merge", "--permanent")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Would delete 1 file (2.9 KB) for good, and merge 1 folder into the "
                      "folder that stays:\n", r.out)

    def test_both_kept(self):
        # doc.txt is in both, different: B's comes in as "doc (from B).txt"
        self.pair()
        before = self.snapshot()
        self.scan("--similar", "50")
        r = self.clean_sealed("remove", "-y", "--merge")
        self.assertEqual(r.rc, 0, r)
        A, B = self.p("A"), self.p("B")
        self.assertEqual(self.items(), {B: ("merged", f'moved 3 files into {A} '
                                                      f'(1 renamed "(from B)")')})
        self.assertEqual(self.snapshot(), self.merged(before, {
            "A/b1": "B/b1", "A/new/b2": "B/new/b2", "A/doc (from B).txt": "B/doc.txt"}))
        # B, with what A had already, is in the Trash.
        self.assertEqual(self.in_trash(), {"B": B})
        self.assertEqual(sorted(self.snapshot(os.path.join(self.trash, "files", "B"))),
                         sorted(k[2:] for k in before if k.startswith("B/s")
                                or k.startswith("B/sub/")))
        self.assertEqual(sorted((a, p, t) for a, p, t, _ in self.actions()), sorted([
            ("mkdir", self.p("A/new"), ""),
            ("move", self.p("B/b1"), self.p("A/b1")),
            ("move", self.p("B/new/b2"), self.p("A/new/b2")),
            ("move", self.p("B/doc.txt"), self.p("A/doc (from B).txt")),
            ("trash", B, os.path.join(self.trash, "files", "B"))]))
        self.assertIn("merged 1 folder", self.run_meta()["headline"])
        # Undo: B comes back from the Trash, then its files out of A.
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Items:           1: 1 put back", r.out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.p("A/new")))
        self.assertEqual(self.in_trash(), {})
        self.assertEqual(self.leftovers(self.root), [])

    def test_skip_then_finish(self):
        # --conflicts skip: B's doc.txt stays, so B does; a re-run that keeps
        # both finishes the merge; undo both, newest first.
        self.pair()
        before = self.snapshot()
        self.scan("--similar", "50")
        r = self.clean_sealed("remove", "-y", "--merge", "--conflicts", "skip")
        self.assertEqual(r.rc, 1, r)
        A, B = self.p("A"), self.p("B")
        self.assertEqual(self.items(), {B: ("partial", (
            f"moved 2 files into {A}; {B} stays: it still holds 1 file only there "
            f"(doc.txt: {A}/doc.txt is a different file)"))})
        first = self.last_run()
        after = self.snapshot()
        self.assertEqual(after, dict(self.merged(before, {"A/b1": "B/b1", "A/new/b2": "B/new/b2"}),
                                     **{k: v for k, v in before.items() if k.startswith("B/")
                                        and k not in ("B/b1", "B/new/b2")}))
        self.assertEqual(self.in_trash(), {})
        # Again with skip: nothing more can move.
        r = self.clean_sealed("remove", "-y", "--merge", "--conflicts", "skip")
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(self.statuses(), {"skipped": 1})
        self.assertEqual(self.snapshot(), after)
        r = self.clean_sealed("remove", "-y", "--merge")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.items(), {B: ("merged", f'moved 1 file into {A} '
                                                      f'(1 renamed "(from B)")')})
        self.assertEqual(self.snapshot(), self.merged(before, {
            "A/b1": "B/b1", "A/new/b2": "B/new/b2", "A/doc (from B).txt": "B/doc.txt"}))
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(), after)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Undoing:         run {first.name} ", r.out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.in_trash(), {})

    def test_newer_wins_only_when_the_older_has_a_copy(self):
        # B's doc.txt is newer. A's has no other copy: both are kept.
        self.pair()
        self.age("A/doc.txt", 5)
        before = self.snapshot()
        self.scan("--similar", "50")
        r = self.clean_sealed("remove", "-y", "--merge", "--conflicts", "newer")
        self.assertEqual(r.rc, 0, r)
        A = self.p("A")
        self.assertEqual(self.items(), {self.p("B"): ("merged", (
            f'moved 3 files into {A} (1 renamed "(from B)", since the older version has '
            f'no other copy)'))})
        self.assertEqual(self.snapshot(), self.merged(before, {
            "A/b1": "B/b1", "A/new/b2": "B/new/b2", "A/doc (from B).txt": "B/doc.txt"}))
        self.assertEqual(self.clean_sealed("undo", "-y").rc, 0)
        self.assertEqual(self.snapshot(), before)

    def test_newer_replaces_an_older_that_has_a_copy(self):
        # A copy of A's older doc.txt stays in C (--keep-in): B's newer one
        # takes its place, and A's goes to the Trash.
        self.pair()
        self.write("C/doc-a.txt", blob("doc A"))
        self.age("A/doc.txt", 5)
        before = self.snapshot()
        self.scan("--similar", "50")
        r = self.clean_sealed("remove", "-y", "--merge", "--conflicts", "newer",
                              "--keep-in", self.p("C"))
        self.assertEqual(r.rc, 0, r)
        A = self.p("A")
        self.assertEqual(self.items(), {self.p("B"): ("merged", (
            f"moved 3 files into {A} (1 replacing an older file, which went to the Trash)"))})
        expected = self.merged(before, {"A/b1": "B/b1", "A/new/b2": "B/new/b2",
                                        "A/doc.txt": "B/doc.txt"})
        self.assertEqual(self.snapshot(), expected)
        self.assertEqual(self.in_trash(), {"B": self.p("B"), "doc.txt": self.p("A/doc.txt")})
        r = self.clean_sealed("undo", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Would put back (1):", r.out)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.in_trash(), {})

    def test_older_from_b_goes_with_it(self):
        # B's doc.txt is the older one, and a copy of it stays in C: it goes
        # with B, and A keeps its own.
        self.pair()
        self.write("C/doc-b.txt", blob("doc B"))
        self.age("B/doc.txt", 5)
        before = self.snapshot()
        self.scan("--similar", "50")
        r = self.clean_sealed("remove", "-y", "--merge", "--conflicts", "newer",
                              "--keep-in", self.p("C"))
        self.assertEqual(r.rc, 0, r)
        A = self.p("A")
        self.assertEqual(self.items(), {self.p("B"): ("merged", (
            f"moved 2 files into {A}; 1 older or empty file went with it"))})
        self.assertEqual(self.snapshot(), self.merged(before, {"A/b1": "B/b1",
                                                               "A/new/b2": "B/new/b2"}))
        self.assertIn("doc.txt", self.snapshot(os.path.join(self.trash, "files", "B")))
        self.assertEqual(self.clean_sealed("undo", "-y").rc, 0)
        self.assertEqual(self.snapshot(), before)

    def test_something_new_in_the_way_of_undo(self):
        # After the merge, A/b1 is edited: undo leaves it in A (and says so),
        # puts back the rest, and nothing is overwritten.
        self.pair()
        before = self.snapshot()
        self.scan("--similar", "50")
        self.assertEqual(self.clean_sealed("remove", "-y", "--merge").rc, 0)
        self.write("A/b1", b"edited since")
        r = self.clean_sealed("undo", "-n")
        self.assertEqual(r.rc, 1, r)
        self.assertIn(f"partly: {self.p('A/b1')} changed since it was moved there: it stays "
                      f"there", r.out)
        self.assertIn(f"To go ahead:     clean undo {self.remove_runs()[0].name}", r.out)
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("Left as they are: changed since (1):\n"
                      f"  merge   {self.p('B')}\n"
                      f"          partly: {self.p('A/b1')} changed since it was moved there: it "
                      "stays there\n", r.out)
        now = self.snapshot()
        self.assertEqual(now["A/b1"], b"edited since")
        self.assertNotIn("B/b1", now)
        self.assertEqual({k: v for k, v in now.items() if k != "A/b1"},
                         {k: v for k, v in before.items() if k != "B/b1"})
        # Nothing more can come back.
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("Nothing to put back.", r.out)
        self.assertEqual(self.snapshot(), now)


@needs_unshare
class TestMergeAcrossDrives(MergeBase):
    """A is in the test folder; B is on a tmpfs drive mounted in the
    namespace. Each scenario runs in one sealed script (the drive is gone
    afterwards), saving snapshots on the way."""

    def setUp(self):
        super().setUp()
        self.pair()
        shutil.move(self.p("B"), os.path.join(self.tmp, "B"))
        os.makedirs(self.p("drive"))
        self.snap = self.script("snap.py", SNAP)
        self.mount = (f'mount -t tmpfs drive "{self.p("drive")}"\n'
                      f'cp -a "$T/B" "{self.p("drive/B")}"\n')
        self.roots = f'"{self.p("A")}" "{self.p("drive")}"'

    def take(self, name):
        return f'"$PY" "{self.snap}" "$T/{name}.json" "{self.p("A")}" "{self.p("drive")}"\n'

    def files(self, name):
        """{path relative to root: content} of a snapshot, Trash left out."""
        return {os.path.relpath(k, self.root): v[2] for k, v in self.snaps(name).items()
                if "/.Trash-" not in k}

    def test_copied_checked_and_undone(self):
        self.sealed(self.mount + self.take("before")
                    + f"step scan dupes {self.roots} -q --similar 50\n"
                    + "step merge remove -y --merge\n" + self.take("merged")
                    + "step undo undo -y\n" + self.take("undone"))
        self.assertEqual(self.step("scan").rc, 0)
        r = self.step("merge")
        self.assertEqual(r.rc, 0, r)
        A, B = self.p("A"), self.p("drive/B")
        before, merged = self.files("before"), self.files("merged")
        merge_run = self.remove_runs()[0]
        self.assertEqual(self.items(merge_run), {B: ("merged", f'moved 3 files into {A} '
                                                      f'(1 renamed "(from B)")')})
        expect = {k: v for k, v in before.items() if not k.startswith("drive/")}
        expect.update({"A/b1": before["drive/B/b1"], "A/new/b2": before["drive/B/new/b2"],
                       "A/doc (from B).txt": before["drive/B/doc.txt"]})
        self.assertEqual(merged, expect)
        # Copied (B's drive is another one), then B went to its own drive's Trash.
        trash = self.p("drive/.Trash-0/files/B")
        self.assertIn(trash + "/s0", self.snaps("merged"))
        acts = self.actions(merge_run)
        self.assertEqual(sorted((a, p, t) for a, p, t, _ in acts), sorted([
            ("mkdir", self.p("A/new"), ""),
            ("copy", self.p("drive/B/b1"), self.p("A/b1")),
            ("copy", self.p("drive/B/new/b2"), self.p("A/new/b2")),
            ("copy", self.p("drive/B/doc.txt"), self.p("A/doc (from B).txt")),
            ("trash", B, trash)]))
        for a, p, t, note in acts:
            if a == "copy":
                size, mtime, digest, tmp = note.split(":")
                self.assertEqual(int(size), len(before[os.path.relpath(p, self.root)]))
                self.assertTrue(tmp.startswith(".clean-copy-"), note)
        r = self.step("undo")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.files("undone"), before)
        self.assertFalse([k for k in self.snaps("undone") if "/.Trash-0/files/" in k])
        self.assertEqual(self.leftovers(self.root), [])

    def test_copy_that_does_not_read_back_the_same(self):
        # The first copy reads back wrong: it is deleted, its file stays in
        # B (so B does); a re-run finishes; undo both.
        bad = self.patched("bad.py", """
real_read = remove.read_file
def read_file(path, *a, **k):
    st, digest = real_read(path, *a, **k)
    if ".clean-copy-" in path and calls[0] == 0:
        calls[0] += 1
        return st, "0" * len(digest)
    return st, digest
remove.read_file = read_file
""")
        self.sealed(self.mount + self.take("before")
                    + f"step scan dupes {self.roots} -q --similar 50\n"
                    + f'pystep bad "{bad}" remove -y --merge\n' + self.take("bad")
                    + "step again remove -y --merge\n" + self.take("again")
                    + "step undo1 undo -y\nstep undo2 undo -y\n" + self.take("undone"))
        r = self.step("bad")
        self.assertEqual(r.rc, 1, r)
        B = self.p("drive/B")
        status, note = self.items(self.remove_runs()[-1])[B]
        self.assertEqual(status, "partial", note)
        self.assertRegex(note, r"^moved 2 files into .*; .* stays: it still holds 1 file only "
                               r"there \((b1|new/b2|doc\.txt): the copy did not read back the "
                               r"same; nothing was moved\)$")
        bad_files = self.files("bad")
        self.assertFalse([k for k in bad_files if ".clean-copy-" in k])
        self.assertEqual(sum(1 for k in ("A/b1", "A/new/b2", "A/doc (from B).txt")
                             if k in bad_files), 2)
        self.assertEqual(self.step("again").rc, 0, self.step("again"))
        self.assertEqual(self.statuses(self.remove_runs()[0]), {"merged": 1})
        for name in ("undo1", "undo2"):
            self.assertEqual(self.step(name).rc, 0, self.step(name))
        self.assertEqual(self.files("undone"), self.files("before"))


@needs_unshare
class TestLink(MergeBase):
    def inode(self, rel):
        return os.lstat(self.p(rel)).st_ino

    def test_twice_then_undo(self):
        self.standard()
        before = self.snapshot()
        self.scan()
        r = self.clean_sealed("remove", "-y", "--link")
        self.assertEqual(r.rc, 0, r)
        first = self.last_run()
        self.assertEqual(self.items(), {
            self.p("B"): ("linked", "its 3 files are now hard links to the copies that stay"),
            self.p("C/u-copy.bin"): ("linked", f"now a hard link to {self.p('C/u.bin')}")})
        self.assertIn("HARD LINKS: copies replaced by links to the copy that stays; the "
                      "copies themselves are in the Trash", r.out)
        self.assertEqual(self.snapshot(), before)
        for a, b in (("A/a1", "B/a1"), ("A/a2", "B/a2"), ("A/sub/a3", "B/sub/a3"),
                     ("C/u.bin", "C/u-copy.bin")):
            self.assertEqual(self.inode(a), self.inode(b), b)
        self.assertEqual(sorted(self.in_trash().values()),
                         sorted(self.p(x) for x in ("B/a1", "B/a2", "B/sub/a3",
                                                    "C/u-copy.bin")))
        # Again: already done, nothing changes.
        r = self.clean_sealed("remove", "-y", "--link")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.items(), {
            self.p("B"): ("gone", "its files are already hard links to the copies that stay"),
            self.p("C/u-copy.bin"): ("gone", f"already a hard link to {self.p('C/u.bin')}")})
        self.assertIn("2 already done", self.run_meta()["headline"])
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.snapshot(), before)
        # Undo goes for the run that made the links: separate files again.
        r = self.clean_sealed("undo", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Undoing:         run {first.name} ", r.out)
        self.assertEqual(self.snapshot(), before)
        for rel in before:
            self.assertEqual(os.lstat(self.p(rel)).st_nlink, 1, rel)
        self.assertEqual(self.in_trash(), {})
        self.assertEqual(self.leftovers(self.root), [])

    def test_on_tmpfs(self):
        self.standard()
        snap = self.script("snap.py", SNAP)
        take = f'"$PY" "{snap}" "$T/%s.json" "{self.p("")}"\n'
        self.sealed(f'cp -a "{self.root}" "$T/stage"\nmount -t tmpfs drive "{self.root}"\n'
                    f'cp -a "$T/stage/." "{self.root}/"\n' + take % "before"
                    + f'step scan dupes "{self.root}" -q\nstep link remove -y --link\n'
                    + take % "linked" + "step undo undo -y\n" + take % "undone")
        r = self.step("link")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.statuses(self.remove_runs()[0]), {"linked": 2})
        before, linked, undone = (self.snaps(n) for n in ("before", "linked", "undone"))

        def files(s):
            return {k: v[2] for k, v in s.items() if "/.Trash-" not in k}
        self.assertEqual(files(linked), files(before))
        for a, b in (("A/a1", "B/a1"), ("C/u.bin", "C/u-copy.bin")):
            self.assertEqual(linked[self.p(a)][0], linked[self.p(b)][0])
            self.assertEqual(linked[self.p(a)][1], 2)
        self.assertEqual(len([k for k in linked if "/.Trash-0/files/" in k]), 4)
        self.assertEqual(self.step("undo").rc, 0, self.step("undo"))
        self.assertEqual(files(undone), files(before))
        self.assertEqual({k: v[1] for k, v in undone.items() if "/.Trash-" not in k},
                         {k: 1 for k in files(before)})
        self.assertFalse([k for k in undone if "/.Trash-0/files/" in k])

    def test_drive_that_cannot_hold_hard_links(self):
        self.standard()
        before = self.snapshot()
        self.scan()
        script = self.patched("exfat.py", "remove.Remover.fstype = lambda self, path: 'exfat'\n")
        self.sealed(f'pystep a "{script}" remove -y --link')
        r = self.step("a")
        self.assertEqual(r.rc, 1, r)
        reason = "this drive (exfat) can't hold hard links"
        self.assertEqual(self.items(), {self.p("B"): ("skipped", reason),
                                        self.p("C/u-copy.bin"): ("skipped", reason)})
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.trash))

    def test_link_refused_by_the_drive(self):
        # EPERM from link(): the drive can't; the next item isn't even tried.
        self.standard()
        before = self.snapshot()
        self.scan()
        script = self.patched("eperm.py", """
def link(*a, **k):
    calls[0] += 1
    raise PermissionError(errno.EPERM, "Operation not permitted")
os.link = link
""")
        self.sealed(f'pystep a "{script}" remove -y --link')
        r = self.step("a")
        self.assertEqual(r.rc, 1, r)
        for status, note in self.items().values():
            self.assertEqual(status, "skipped")
            self.assertRegex(note, r"^this drive \(.+\) can't hold hard links$")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.trash))
        self.assertEqual(self.leftovers(self.root), [])
        self.assertEqual([a for a, _, _, _ in self.actions()], ["link"])     # its temp name

    def test_copy_on_another_drive(self):
        # B is on another drive than A: a link can't reach, so B stays.
        self.standard()
        shutil.move(self.p("B"), os.path.join(self.tmp, "B"))
        self.sealed(f'mkdir "{self.p("drive")}"\nmount -t tmpfs drive "{self.p("drive")}"\n'
                    f'cp -a "$T/B" "{self.p("drive/B")}"\necho own > "{self.p("drive/own")}"\n'
                    f'step scan dupes "{self.root}" -q\nstep link remove -y --link\n'
                    f'find "{self.p("drive")}" -type f -links 1 | sort > "$T/left"')
        r = self.step("link")
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(self.items(), {
            self.p("drive/B"): ("skipped", "the copies of a1 that stay are on another drive, "
                                           "and a hard link can't reach another drive"),
            self.p("C/u-copy.bin"): ("linked", f"now a hard link to {self.p('C/u.bin')}")})
        with open(os.path.join(self.tmp, "left")) as f:
            self.assertEqual(f.read().split(), [self.p(x) for x in (
                "drive/B/a1", "drive/B/a2", "drive/B/sub/a3", "drive/own")])


@needs_unshare
class TestMountPoints(MergeBase):
    """A drive mounted inside what was scanned is never removed (or merged
    away) as a folder, even when it holds just a copy: emptying the Trash
    would then delete a whole drive's files."""

    def setUp(self):
        super().setUp()
        self.standard()
        shutil.move(self.p("B"), os.path.join(self.tmp, "B"))
        os.makedirs(self.p("drive"))
        self.mount = (f'mount -t tmpfs drive "{self.p("drive")}"\n'
                      f'cp -a "$T/B/." "{self.p("drive")}/"\n')
        self.left = f'find "{self.p("drive")}" -type f | sort > "$T/left"\n'

    def left_on_drive(self):
        with open(os.path.join(self.tmp, "left")) as f:
            return [os.path.relpath(x, self.p("drive")) for x in f.read().split()]

    def test_blocked(self):
        # drive/ holds a copy of A: blocked (named in the plan); its files
        # stay too. Same when a drive is mounted inside the copy.
        self.sealed(self.mount + f'step scan dupes "{self.root}" -q\n'
                    'step n remove -n\nstep y remove -y --permanent\n' + self.left)
        r = self.step("n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"Blocked (1): picked to go, but they stay:\n"
                      f"  - {self.p('drive')}/\n"
                      f"      it is a mount point (a drive, or another folder, is mounted "
                      f"here): it is never removed as a whole\n", r.out)
        r = self.step("y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(self.items(), {self.p("C/u-copy.bin"): (
            "removed", f"copy of {self.p('C/u.bin')}")})
        self.assertEqual(self.left_on_drive(), ["a1", "a2", "sub/a3"])

    def test_drive_inside_the_copy(self):
        os.rename(self.p("drive"), self.p("B"))
        os.makedirs(self.p("B/drive"))
        shutil.move(os.path.join(self.tmp, "B", "sub"), os.path.join(self.tmp, "sub"))
        self.write("A/drive/x", blob("x"))
        self.write("A/drive/sub/a3", self.snapshot(os.path.join(self.tmp, "sub"))["a3"])
        self.sealed(f'cp -a "$T/B/." "{self.p("B")}/"\n'
                    f'mount -t tmpfs drive "{self.p("B/drive")}"\n'
                    f'cp -a "$T/sub" "{self.p("B/drive/")}"\n'
                    f'cp -a "{self.p("A/drive/x")}" "{self.p("B/drive/x")}"\n'
                    f'step scan dupes "{self.root}" -q\nstep n remove -n\n')
        r = self.step("n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"  - {self.p('B')}/\n      it holds a mount point, "
                      f"{self.p('B/drive')}: it is never removed as a whole\n", r.out)

    def test_refused_when_acting(self):
        # Should a plan name a drive's mount point anyway, it is refused just
        # before acting.
        script = self.patched("nocheck.py", "from cleanup import selection\n"
                                            "selection._Selector.mount_in = lambda self, p: None\n")
        self.sealed(self.mount + f'step scan dupes "{self.root}" -q\n'
                    f'pystep y "{script}" remove -y --permanent\n' + self.left)
        r = self.step("y")
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(self.items()[self.p("drive")], (
            "skipped", "it is a mount point (a drive, or another folder, is mounted here): "
                       "it is never removed as a whole"))
        self.assertEqual(self.left_on_drive(), ["a1", "a2", "sub/a3"])


if __name__ == "__main__":
    unittest.main()
