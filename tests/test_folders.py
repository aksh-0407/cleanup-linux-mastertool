#!/usr/bin/env python3
"""The duplicate engine's results, on small trees built for each case:
folder families (identical, renamed, partial, diverged), covered folders,
file groups and how they fold into folder results, the diff of each family
member, taint, junk, hard links, empty files, overlapping roots, every filter,
--same-name / --same-date, and the hash cache.

Most tests run the real `clean dupes` and read what it saved (folders.tsv,
files.tsv, data.json.gz); a few call the engine directly."""

import os
import random
import sqlite3
import time
import unittest

from helpers import IS_ROOT, SLOW, Base

from cleanup.dupes import diff
from cleanup.dupes.files import Matcher
from cleanup.dupes.folders import REDUNDANT_ROLES, Relations
from cleanup.dupes.results import collect
from cleanup.fsutil import Excluder, Progress
from cleanup.report import data as report_data
from cleanup.safety import DriveWatch
from cleanup.scan.filters import JUNK_NAMES, Filters, Junk
from cleanup.scan.tree import Scanner

OLD = time.time() - 3600        # an mtime the hash cache will store


def blob(tag, size=3000):
    """size bytes, different for each tag."""
    seed = tag.encode()
    out = bytearray()
    while len(out) < size:
        out += seed + b"|" + str(len(out)).encode() + b"\n"
    return bytes(out[:size])


class FolderBase(Base):
    def tree_of(self, base, files):
        """files: {relative path: tag or bytes}; each tag becomes distinct
        content (equal tags: equal bytes)."""
        for rel, content in files.items():
            self.write(os.path.join(base, rel) if base else rel,
                       content if isinstance(content, bytes) else blob(content))

    def scan(self, *args, roots=None, code=0):
        r = self.dupes(*(roots or [self.root]), *args)
        self.assertEqual(r.returncode, code, r.stderr.decode() + r.stdout.decode())
        return r

    def rel(self, path):
        return os.path.relpath(path, self.root)

    def doc(self, run=None):
        run = run or self.last_run()
        return report_data.read(run.file(report_data.NAME))

    def families(self, run=None):
        """folders.tsv: {kept folder: sorted [(role, folder)]} and the
        covered folders, relative to root."""
        run = run or self.last_run()
        fams, anchors, covered = {}, {}, []
        with open(run.file("folders.tsv"), encoding="utf-8") as f:
            self.assertEqual(next(f), "result\trole\tfiles\tsize_bytes\tfreed_bytes\tpath\n")
            for line in f:
                rid, role, files, size, freed, path = line.rstrip("\n").split("\t")
                if role == "keep":
                    anchors[rid] = self.rel(path)
                    fams[anchors[rid]] = []
                elif role == "covered":
                    covered.append(self.rel(path))
                else:
                    fams[anchors[rid]].append((role, self.rel(path)))
        return {k: sorted(v) for k, v in fams.items()}, sorted(covered)

    def groups(self, run=None, folded=None):
        """data.json.gz groups as sorted lists of relative paths (folded:
        only those that are / aren't folded into a folder result)."""
        d = self.doc(run)
        paths = report_data.paths_of(d["dirs"])
        out = []
        for g in d["groups"]:
            if folded is None or g["folded"] == folded:
                out.append(sorted(self.rel(os.path.join(paths[i], n)) for i, n in g["files"]))
        return sorted(out)

    def member(self, d, path):
        paths = report_data.paths_of(d["dirs"])
        for f in d["families"]:
            for m in f["members"]:
                if paths[m["folder"]] == os.path.join(self.root, path):
                    return f, m
        self.fail(f"{path} is in no family")


def analyse(roots, hidden=False, similar=80, filters=None, seed=0):
    """The engine in this process: (tree, matcher, relations, results)."""
    tree = Scanner(Excluder([], True), False, filters or Filters(), Junk(JUNK_NAMES), None,
                   Progress(True), DriveWatch(roots)).scan(roots, 1)
    matcher = Matcher(tree)
    matcher.run()
    rel = Relations(tree, matcher, similar, hidden, seed=seed).run()
    return tree, matcher, rel, collect(tree, matcher, rel)


PHOTOS = {"2019/jan/a.jpg": "a", "2019/jan/b.jpg": "b", "2019/feb/c.jpg": "c",
          "2020/d.jpg": "d", "notes.txt": "n"}


class TestFamilies(FolderBase):
    def test_three_identical_copies_are_one_family_and_no_file_groups(self):
        for top in ("Backup1", "Backup2", "Backup3"):
            self.tree_of(top, PHOTOS)
        r = self.scan()
        fams, covered = self.families()
        self.assertEqual(fams, {"Backup1": [("identical", "Backup2"), ("identical", "Backup3")]})
        self.assertEqual(covered, [])
        self.assertEqual(self.groups(folded=False), [])
        self.assertEqual(len(self.groups(folded=True)), 5)
        out = r.stdout.decode()
        self.assertIn("FOLDER FAMILIES (1)", out)
        self.assertIn("F1  identical copies — 3 folders", out)
        self.assertIn("all 5 groups of duplicate files are inside the folder results above", out)
        d = self.doc()
        self.assertEqual(d["families"][0]["freed"], 2 * 5 * 3000)
        self.assertEqual(d["totals"]["folders_freed"], 2 * 5 * 3000)
        # files.tsv: the copies in the redundant folders say which result removes them
        with open(self.last_run().file("files.tsv"), encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("\t") for line in f][1:]
        for g, size, h, explained, path in rows:
            self.assertEqual(explained, "" if "/Backup1/" in path else "F1", path)

    def test_nested_copies_are_reported_only_at_the_top(self):
        self.tree_of("Backup1/Pictures", PHOTOS)
        self.tree_of("Backup2/Pictures", PHOTOS)
        self.tree_of("Backup1/Docs", {"cv.pdf": "cv", "letter.doc": "l"})
        self.tree_of("Backup2/Docs", {"cv.pdf": "cv", "letter.doc": "l"})
        # Only a subfolder copied: the family is that subfolder, not its parents
        self.tree_of("Laptop/Pictures", PHOTOS)
        self.tree_of("Laptop", {"own.txt": "laptop only"})
        self.scan()
        fams, covered = self.families()
        # Backup2's Pictures and Docs are not repeated; Laptop/Pictures is a
        # copy of its own, said as precisely as possible
        self.assertEqual(fams, {"Backup1": [("identical", "Backup2")],
                                "Backup1/Pictures": [("identical", "Laptop/Pictures")]})
        self.assertEqual(covered, [])
        self.assertEqual(self.groups(folded=False), [])

    def test_a_copy_of_a_subfolder(self):
        self.tree_of("Work/Pictures", PHOTOS)
        self.tree_of("Work", {"own.txt": "work only", "more.txt": "more"})
        self.tree_of("Laptop/stuff/Pictures", PHOTOS)
        self.tree_of("Laptop", {"own.txt": "laptop only"})
        self.scan()
        fams, covered = self.families()
        # Laptop/stuff holds nothing but its Pictures: the outer one is named,
        # and it is the one to remove (its files are a level deeper)
        self.assertEqual(fams, {"Work/Pictures": [("renamed", "Laptop/stuff")]})
        self.assertEqual(covered, [])

    def test_a_wrapped_copy_is_not_kept(self):
        # Backup holds only Backup/Photos: Photos is kept whatever the names
        for top in ("Photos", "Backup/Photos", "Zebra/Photos"):
            self.tree_of(top, PHOTOS)
        self.scan()
        fams, covered = self.families()
        self.assertEqual(fams, {"Photos": [("renamed", "Backup"), ("renamed", "Zebra")]})
        self.assertEqual(covered, [])

    def test_a_copy_inside_a_kept_folder_is_kept(self):
        # Pictures is kept over its copy Backup/Pictures. Pictures/2019 is
        # also in Phone dump, renamed: Phone dump is the copy to remove, not
        # Pictures/2019 (though it is a level deeper), so the kept Pictures
        # is not hollowed out
        pics = {"2019/a.jpg": "a", "2019/b.jpg": "b", "2019/c.jpg": "c",
                "2020/d.jpg": "d", "2020/e.jpg": "e"}
        self.tree_of("Pictures", pics)
        self.tree_of("Backup/Pictures", pics)
        self.tree_of("Phone dump", {"IMG_1.jpg": "a", "IMG_2.jpg": "b", "IMG_3.jpg": "c"})
        self.scan()
        self.assertEqual(self.families(), ({"Pictures": [("renamed", "Backup")],
                                            "Pictures/2019": [("renamed", "Phone dump")]}, []))
        self.assertEqual(self.groups(folded=False), [])

    def test_a_copy_inside_a_partial_copy_keeps_the_original(self):
        # Archive/a is the same as Main/a, but the rest of Archive is a part
        # of Main: Main's copy is kept, and Archive is one partial copy
        # (not "keep Archive/a" next to "remove Archive/b")
        self.tree_of("Main", {"a/1": "1", "a/2": "2", "a/3": "3", "b/4": "4", "b/5": "5",
                              "c/6": "6", "own": "main only"})
        self.tree_of("Archive", {"a/1": "1", "a/2": "2", "a/3": "3", "b/4": "4"})
        self.scan()
        self.assertEqual(self.families(), ({"Main": [("partial", "Archive")]}, []))
        self.assertEqual(self.groups(folded=False), [])

    def test_renamed_and_reorganised(self):
        self.tree_of("Phone dump", {"IMG_0001.jpg": "a", "IMG_0002.jpg": "b",
                                    "IMG_0003.jpg": "c", "VID_0004.mp4": "v"})
        self.tree_of("Photos 2019", {"Jan/new year.jpg": "a", "Jan/party.jpg": "b",
                                     "Feb/snow.jpg": "c", "videos/clip.mp4": "v"})
        self.scan()
        fams, _ = self.families()
        self.assertEqual(len(fams), 1)
        (anchor, [(role, member)]), = fams.items()
        self.assertEqual(role, "renamed")
        self.assertEqual({anchor, member}, {"Phone dump", "Photos 2019"})
        d = self.doc()
        _, m = self.member(d, member)
        self.assertEqual(m["diff"]["moved"]["count"], 4)
        self.assertEqual([m["diff"][k]["count"] for k in ("only_here", "only_there", "changed")],
                         [0, 0, 0])
        moved = dict(map(tuple, m["diff"]["moved"]["items"]))
        if anchor == "Phone dump":
            self.assertEqual(moved["IMG_0001.jpg"], "Jan/new year.jpg")
        else:
            self.assertEqual(moved["Jan/new year.jpg"], "IMG_0001.jpg")

    def test_files_moved_with_their_folder_are_listed_once_per_folder(self):
        self.tree_of("Projects", {"P1/a/1": "1", "P1/a/2": "2", "P1/b/3": "3",
                                  "P2/c/4": "4", "P2/c/5": "5", "own": "own", "mine": "m",
                                  "P3/x/6": "6", "P3/x/7": "7", "P3/y/8": "8", "P2/9": "9"})
        self.tree_of("Backups", {"B7/P1/a/1": "1", "B7/P1/a/2": "2", "B7/P1/b/3": "3",
                                 "B9/P2/c/4": "4", "B9/P2/c/5": "5", "B9/renamed": "own"})
        r = self.scan()
        self.assertEqual(self.families(), ({"Projects": [("partial", "Backups")]}, []))
        _, m = self.member(self.doc(), "Backups")
        self.assertEqual(m["diff"]["moved"], {
            "count": 6, "items": [["own", "B9/renamed"]],
            "folders": [["P1", "B7/P1", 3], ["P2", "B9/P2", 2]]})
        # P3 is only in the kept folder, all of it: one line; P2 only in part
        self.assertEqual(m["diff"]["only_there"], {
            "count": 5, "items": ["P2/9", "mine"], "folders": [["P3", 3]]})
        self.assertIn("6 moved or renamed", r.stdout.decode())
        run = self.last_run()
        with open(run.file("summary.txt"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("moved or renamed:\n                   P1/  ->  B7/P1/  (3 files)\n"
                      "                   P2/  ->  B9/P2/  (2 files)\n"
                      "                   own  ->  B9/renamed\n", text)
        self.assertIn("only in the kept folder:\n                   P3/  (3 files)\n"
                      "                   P2/9\n                   mine\n", text)
        self.assertNotIn("more", text)
        with open(run.file("report.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn('<li>P1/ <span class="arrow">&rarr;</span> B7/P1/ '
                      '<span class="muted">(3 files)</span></li>', page)

    def test_moved_folders_take_the_longest_shared_end(self):
        moved = [["x/a/1", "y/a/1"], ["x/b/2", "y/b/2"], ["t/3", "u/v/3"],
                 ["a.jpg", "b.jpg"], ["p/4", "q/4"], ["k/5", "m/k/5"], ["k/6", "m/k/6"]]
        folders, single = diff._moved_folders(moved)
        self.assertEqual(folders, [["k", "m/k", 2], ["x", "y", 2]])
        self.assertEqual(sorted(single), [["a.jpg", "b.jpg"], ["p/4", "q/4"], ["t/3", "u/v/3"]])

    def test_same_names_in_other_folders_is_renamed_not_identical(self):
        self.tree_of("A", {"x/1": "1", "y/2": "2"})
        self.tree_of("B", {"y/1": "1", "x/2": "2"})
        self.scan()
        fams, _ = self.families()
        self.assertEqual(fams, {"A": [("renamed", "B")]})

    def test_partial_copy(self):
        full = {f"f{i}.dat": f"file {i}" for i in range(10)}
        self.tree_of("Full", full)
        self.tree_of("Old backup", {k: v for k, v in list(full.items())[:6]})
        self.scan()
        fams, _ = self.families()
        self.assertEqual(fams, {"Full": [("partial", "Old backup")]})
        _, m = self.member(self.doc(), "Old backup")
        self.assertEqual(m["diff"]["only_there"], {"count": 4, "items": [f"f{i}.dat" for i in range(6, 10)],
                                                   "folders": []})
        self.assertEqual([m["diff"][k]["count"] for k in ("only_here", "changed", "moved")],
                         [0, 0, 0])
        self.assertEqual(m["freed"], 6 * 3000)
        self.assertEqual(self.groups(folded=False), [])

    def test_a_family_inside_a_bigger_folder_joins_it(self):
        # p and q are copies of each other, and r holds all of both and more
        for i in range(4):
            for top in ("p", "q", "r"):
                self.write(f"{top}/d{i}/f", blob(f"c{i}"))
            self.write(f"r/d{i}/extra", blob(f"x{i}"))
        self.scan()
        fams, _ = self.families()
        self.assertEqual(fams, {"r": [("partial", "p"), ("partial", "q")]})

    def test_a_few_files_in_a_big_folder_are_not_a_partial_copy(self):
        self.tree_of("Big", {f"f{i}": f"big {i}" for i in range(40)})
        self.tree_of("Small", {"f1": "big 1", "f2": "big 2"})
        self.scan()
        fams, covered = self.families()
        self.assertEqual(fams, {})
        self.assertEqual(covered, ["Small"])

    def test_diverged_copies_with_the_exact_diff(self):
        shared = {f"s{i:02}.txt": f"shared {i}" for i in range(20)}
        self.tree_of("A", dict(shared, **{"a_only.txt": "a", "f10.txt": "ten A"}))
        b = dict(shared, **{"b_only.txt": "b", "f10.txt": "ten B"})
        b["sub/s00.txt"] = b.pop("s00.txt")                    # moved
        self.tree_of("B", b)
        r = self.scan()
        fams, _ = self.families()
        self.assertEqual(fams, {"A": [("diverged", "B")]})
        f, m = self.member(self.doc(), "B")
        self.assertEqual(f["freed"], 0)
        self.assertEqual(m["freed"], 0)
        self.assertAlmostEqual(m["shared"], 20 / 24)
        self.assertEqual(m["diff"], {
            "only_here": {"count": 1, "items": ["b_only.txt"], "folders": []},
            "only_there": {"count": 1, "items": ["a_only.txt"], "folders": []},
            "changed": {"count": 1, "items": ["f10.txt"]},
            "moved": {"count": 1, "items": [["s00.txt", "sub/s00.txt"]], "folders": []}})
        self.assertIn("83% shared; 1 only here, 1 only in the kept folder, 1 changed, "
                      "1 moved or renamed — keep both", r.stdout.decode())
        # the shared files are explained by the family: no file groups left
        self.assertEqual(self.groups(folded=False), [])
        # Not similar enough: no family; B/sub (only s00.txt) is covered by A
        self.scan("--similar", "90")
        self.assertEqual(self.families(), ({}, ["B/sub"]))
        self.assertEqual(len(self.groups(folded=False)), 19)

    def test_diverged_copies_are_paired_where_they_are(self):
        # X/one and Y/deep/two went separate ways (7 of 10 files shared); X
        # and Y hold other files too. The pair is one and two, not one of
        # them with the other's parent (which would share less, and send a
        # merge to the wrong place), whichever the scan meets first.
        shared = {f"p{i}": f"pair {i}" for i in range(7)}
        for x, y in (("X", "Y"), ("Y", "X")):
            with self.subTest(first=x):
                base = os.path.join(self.root, x + y)
                self.tree_of(f"{x}{y}/{x}", {"own1": "x1", "own2": "x2", "own3": "x3"})
                self.tree_of(f"{x}{y}/{x}/one", dict(shared, only1="o1"))
                self.tree_of(f"{x}{y}/{y}", {"mine": "y1", "deep/mine2": "y2"})
                self.tree_of(f"{x}{y}/{y}/deep/two", dict(shared, only2="o2", only3="o3"))
                self.scan("--similar", "50", roots=[base])
                fams, covered = self.families()
                self.assertEqual(fams, {f"{x}{y}/{y}/deep/two":
                                        [("diverged", f"{x}{y}/{x}/one")]})
                self.assertEqual(covered, [])

    def test_diverged_copies_found_past_copies_kept_inside(self):
        # one and two share 7 files, each also held by a folder of its own
        # elsewhere (3 copies), and each keeps 3 files twice inside itself (2
        # copies, so these come first when the scan looks for a partner): the
        # pair is still found.
        files = {}
        for i in range(7):
            files[f"p{i}"] = f"pair {i}"
            files[f"L{i}/s"] = f"pair {i}"
            files[f"L{i}/u"] = f"loose {i}"
        for side, own in (("X/one", 1), ("Y/two", 2)):
            for i in range(7):
                files[f"{side}/p{i}"] = f"pair {i}"
            for i in range(own):
                files[f"{side}/only{i}"] = f"{side} only {i}"
            for i in range(3):
                files[f"{side}/i{i}"] = files[f"{side}/j{i}"] = f"{side} twice {i}"
        for i in range(7):
            del files[f"p{i}"]
        self.tree_of("", files)
        self.scan("--similar", "40")
        fams, covered = self.families()
        self.assertEqual(fams, {"Y/two": [("diverged", "X/one")]})

    def test_copies_between_diverged_folders_are_part_of_them(self):
        # B/sub is the same as A/sub, and A/x as B/x: the pair says it all,
        # whichever of the two the scan meets first
        shared = {f"s{i:02}.txt": f"shared {i}" for i in range(20)}
        shared.update({"sub/1": "sub 1", "sub/2": "sub 2", "x/1": "x 1"})
        for first, second in (("A", "B"), ("Z", "B")):
            with self.subTest(first=first):
                base =os.path.join(self.root, first + second)
                self.tree_of(f"{first}{second}/{first}", dict(shared, own1="a1", own2="a2"))
                self.tree_of(f"{first}{second}/{second}", dict(shared, own3="b"))
                self.scan(roots=[base])
                fams, covered = self.families()
                self.assertEqual(fams, {f"{first}{second}/{first}":
                                        [("diverged", f"{first}{second}/{second}")]})
                self.assertEqual((covered, self.groups(folded=False)), ([], []))

    def test_a_third_copy_outside_a_diverged_pair_is_still_reported(self):
        # A/sub, B/sub and C/sub are the same: C/sub is a copy, B/sub is part
        # of the pair; when all of C is in A and B, its sub goes with it
        shared = {f"s{i:02}.txt": f"shared {i}" for i in range(20)}
        shared.update({"sub/1": "sub 1", "sub/2": "sub 2"})
        cases = [("A", {"sub/1": "sub 1", "sub/2": "sub 2", "own": "c"},
                  {"A": [("diverged", "B")], "A/sub": [("identical", "C/sub")]}, []),
                 ("Z", {"sub/1": "sub 1", "sub/2": "sub 2", "own": "c"},
                  {"Z": [("diverged", "B")], "B/sub": [("identical", "C/sub")]}, []),
                 ("A", {"sub/1": "sub 1", "sub/2": "sub 2", "s00.txt": "shared 0"},
                  {"A": [("diverged", "B")]}, ["C"])]
        for n, (first, c, want, want_covered) in enumerate(cases):
            with self.subTest(case=n):
                top = f"t{n}"
                self.tree_of(f"{top}/{first}", dict(shared, own1="a1", own2="a2"))
                self.tree_of(f"{top}/B", dict(shared, own3="b"))
                self.tree_of(f"{top}/C", c)
                self.scan(roots=[os.path.join(self.root, top)])
                fams, covered = self.families()
                self.assertEqual(fams, {f"{top}/{k}": [(r, f"{top}/{m}") for r, m in v]
                                        for k, v in want.items()})
                self.assertEqual(covered, [f"{top}/{x}" for x in want_covered])

    def test_covered_elsewhere(self):
        self.tree_of("Photos", {"a.jpg": "a", "own1.jpg": "o1"})
        self.tree_of("Docs", {"b.pdf": "b", "own2.pdf": "o2"})
        self.tree_of("Music", {"c.mp3": "c", "own3.mp3": "o3"})
        self.tree_of("Downloads", {"old/a.jpg": "a", "old/b.pdf": "b", "old/c.mp3": "c",
                                   "new.zip": "z"})
        r = self.scan()
        fams, covered = self.families()
        self.assertEqual((fams, covered), ({}, ["Downloads/old"]))
        c, = self.doc()["covered"]
        paths = report_data.paths_of(self.doc()["dirs"])
        self.assertEqual(sorted(self.rel(paths[w]) for w, n in c["where"]),
                         ["Docs", "Music", "Photos"])
        self.assertEqual((c["where_total"], c["freed"], c["files"]), (3, 9000, 3))
        self.assertIn("COVERED FOLDERS (1)", r.stdout.decode())
        self.assertEqual(self.groups(folded=False), [])

    def test_originals_copied_into_folders_of_copies_are_not_covered(self):
        # Projects/p/a's two files were also put into Downloads/x and Inbox/y,
        # folders that are mostly copies: a is the original, not the copy
        self.tree_of("Projects/p", {"a/1": "1", "a/2": "2", "own": "p",
                                    **{f"c/u{i}": f"u{i}" for i in range(6)}})
        self.tree_of("Music", {f"m{i}": f"m{i}" for i in range(1, 13)})
        self.tree_of("Downloads", {"x/1": "1", "x/m7": "m7", "x/own": "x",
                                   "z/m9": "m9", "z/m10": "m10"})
        self.tree_of("Inbox", {"y/2": "2", "y/m8": "m8", "y/own": "y",
                               "w/m11": "m11", "w/m12": "m12"})
        self.scan()
        # Downloads/z and Inbox/w hold only copies of Music's files
        self.assertEqual(self.families(), (
            {"Music": [("partial", "Downloads/z"), ("partial", "Inbox/w")]}, []))
        self.assertEqual(self.groups(folded=False), [
            ["Downloads/x/1", "Projects/p/a/1"], ["Downloads/x/m7", "Music/m7"],
            ["Inbox/y/2", "Projects/p/a/2"], ["Inbox/y/m8", "Music/m8"]])

    def test_the_last_copy_is_never_inside_a_redundant_folder(self):
        # Three folders, each covered by the other two: not all can go.
        self.tree_of("A", {"x": "x", "y": "y"})
        self.tree_of("B", {"y": "y", "z": "z"})
        self.tree_of("C", {"z": "z", "x": "x"})
        self.scan()
        fams, covered = self.families()
        self.assertEqual(fams, {})
        self.assertEqual(len(covered), 1)

    def test_random_trees_never_lose_the_last_copy(self):
        """Random trees with copies, partial copies, edits, hard links, hidden
        and tainted folders, over one to three roots: whatever is reported,
        removing every redundant folder leaves a copy of each content, and
        each result means what its label says."""
        rng = random.Random(7)
        cases = 300 if SLOW else 60
        for case in range(cases):
            with self.subTest(case=case):
                self.random_case(rng, os.path.join(self.tmp, f"case{case}"))

    def random_case(self, rng, base):
        import shutil
        folders = [""]
        for _ in range(rng.randint(2, 14)):
            name = rng.choice(["d", "d", "d", ".h"]) + str(len(folders))
            folders.append(os.path.join(rng.choice(folders), name))
        made = []
        for k in range(rng.randint(3, 40)):
            path = os.path.join(base, rng.choice(folders), f"f{k}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            size = rng.choice([0, 50, 100, 5000])
            with open(path, "wb") as f:
                f.write(blob(f"c{rng.randint(0, 15)}", size))
            made.append(path)
        os.makedirs(base, exist_ok=True)
        for k in range(rng.randint(0, 4)):          # whole copies, some then edited
            src = os.path.join(base, rng.choice(folders))
            dst = os.path.join(base, rng.choice(folders), f"copy{k}")
            if os.path.isdir(src) and not os.path.exists(dst) and \
                    not os.path.realpath(dst).startswith(os.path.realpath(src) + "/"):
                shutil.copytree(src, dst, symlinks=True)
                inside = [os.path.join(dp, n) for dp, _, ns in os.walk(dst) for n in ns]
                for f in rng.sample(inside, min(len(inside), rng.randint(0, 2))):
                    action = rng.choice(["drop", "edit", "add"])
                    if action == "drop":
                        os.unlink(f)
                    elif action == "edit":
                        with open(f, "ab") as out:
                            out.write(b"edited")
                    else:
                        with open(f + "-new", "wb") as out:
                            out.write(blob(f"new{rng.random()}", 100))
        if made and rng.random() < 0.3:             # hard links
            src = rng.choice(made)
            if os.path.exists(src):
                os.link(src, os.path.join(base, f"link{rng.randint(0, 9)}"))
        if rng.random() < 0.3:                      # taint: a symlink or a .git
            d = os.path.join(base, rng.choice(folders))
            if os.path.isdir(d):
                if rng.random() < 0.5:
                    os.symlink("nowhere", os.path.join(d, "link-x"))
                else:
                    os.makedirs(os.path.join(d, ".git"), exist_ok=True)
                    with open(os.path.join(d, ".git", "HEAD"), "w") as f:
                        f.write("ref")
        tops = sorted(os.listdir(base))
        roots = [base]
        if len(tops) > 2 and rng.random() < 0.4:
            roots = [os.path.join(base, t) for t in rng.sample(tops, rng.randint(2, len(tops)))
                     if os.path.isdir(os.path.join(base, t))] or [base]
        hidden = rng.random() < 0.5
        pct = rng.choice([50, 80, 100])
        tree, matcher, rel, res = analyse(roots, hidden=hidden, similar=pct,
                                          seed=rng.randint(0, 99))
        cid, parent = matcher.cid, tree.parent

        def contents(folder):
            return sorted(cid[j] for j in tree.files_of(folder) if cid[j] > 0)

        gone = {}
        for fam in rel.families:
            self.assertTrue(fam.members, "a family without members")
            a = fam.anchor
            for m in fam.members:
                self.assertFalse(rel.taint[m.folder], "a tainted folder in a family")
                if m.role in REDUNDANT_ROLES:
                    self.assertNotIn(m.folder, gone)
                    gone[m.folder] = fam
                if m.role in ("identical", "renamed"):
                    self.assertEqual(set(contents(m.folder)), set(contents(a)))
                elif m.role == "partial":
                    self.assertLessEqual(set(contents(m.folder)), set(contents(a)))
                else:
                    self.assertGreaterEqual(m.shared, pct / 100 - 1e-9)
        for cov in rel.covered:
            self.assertFalse(rel.taint[cov.folder], "a tainted folder covered")
            self.assertNotIn(cov.folder, gone)
            gone[cov.folder] = cov

        def removed(folder):
            while folder >= 0:
                if folder in gone:
                    return True
                folder = parent[folder]
            return False
        for fam in rel.families:
            self.assertFalse(removed(fam.anchor), "a kept folder inside a removed one")
        # copies between the two folders of a diverged pair are part of it
        end = tree.end
        pairs = [(f.anchor, m.folder) for f in rel.families for m in f.members
                 if m.role == "diverged"]
        for fam in rel.families:
            for m in fam.members:
                for a, b in pairs:
                    if m.role != "diverged":
                        sides = {s for s in (a, b) for x in (fam.anchor, m.folder)
                                 if s <= x < end[s]}
                        self.assertFalse(len(sides) == 2 and (a <= fam.anchor < end[a]) !=
                                         (a <= m.folder < end[a]),
                                         "a result between the two sides of a diverged pair")
        for g in gone:
            self.assertFalse(parent[g] >= 0 and removed(parent[g]),
                             "a result inside another one")
        before = {c for c in cid if c > 0}
        after = {cid[j] for j in range(tree.nfiles_total)
                 if cid[j] > 0 and not removed(tree.fdir[j])}
        self.assertEqual(before, after, "the last copy of something would go")
        # freed: each file once, and a hard-linked one only if all its names go
        names, inside = {}, {}
        for j in range(tree.nfiles_total):
            r = matcher.rep[j]
            names[r] = names.get(r, 0) + 1
            inside[r] = inside.get(r, 0) + removed(tree.fdir[j])
        freed = sum(tree.size[r] for r in names
                    if inside[r] and inside[r] >= max(tree.nlink[r], names[r]))
        self.assertEqual(res["totals"]["folders_freed"], freed)
        self.assertEqual(sum(f["freed"] for f in res["families"]) +
                         sum(c["freed"] for c in res["covered"]), freed)
        # each copy in a file group names the result that removes it, if any
        index = {path: i for i, path in enumerate(tree.paths())}
        diverged = {f.id for f in rel.families if any(m.role == "diverged" for m in f.members)}

        def result_of(folder):
            while folder >= 0:
                if folder in gone:
                    return gone[folder].id
                folder = parent[folder]
            return None
        for g in res["groups"]:
            self.assertGreaterEqual(len(g["files"]), 2)
            for path, e in zip(g["files"], g["explained"]):
                r = result_of(index[os.path.dirname(path)])
                if r is not None:
                    self.assertEqual(e, r, path)
                else:
                    self.assertTrue(e == "" or e in diverged, (path, e))


class TestWhatCounts(FolderBase):
    def test_junk_is_ignored(self):
        self.tree_of("A", PHOTOS)
        self.tree_of("B", PHOTOS)
        self.tree_of("B", {"Thumbs.db": "thumbs", "2019/desktop.ini": "ini",
                           ".DS_Store": "ds", "2020/.directory": "dir"})
        self.tree_of("C", {"thumbs.db": "thumbs"})      # junk is never a duplicate either
        r = self.scan("--hidden")
        fams, _ = self.families()
        self.assertEqual(fams, {"A": [("identical", "B")]})
        self.assertEqual(self.groups(folded=False), [])
        self.assertEqual(self.doc()["skipped"]["junk"], 5)
        self.assertIn("Skipped:         5 junk files", r.stdout.decode())

    def test_taint_from_git_symlinks_and_unreadable(self):
        for top in ("A", "B", "C", "D"):
            self.tree_of(top, {"x": "x", "sub/y": "y", "sub/z": "z"})
        self.write("A/.git/HEAD", "ref")
        self.write("B/.git/HEAD", "ref")
        os.symlink("x", os.path.join(self.root, "C", "link"))
        self.scan()
        fams, covered = self.families()
        # A folder holding something unseen is never redundant as a whole
        # (it may be the one kept): only D, which has nothing odd, and the
        # clean subfolders are. D/sub is part of D, not listed again.
        whole = {p for ms in fams.values() for _, p in ms} | set(covered)
        self.assertFalse(whole & {"A", "B", "C"}, fams)
        self.assertEqual(fams, {"A": [("partial", "D")],
                                "A/sub": [("identical", "B/sub"), ("identical", "C/sub")]})
        self.assertEqual(self.groups(folded=False), [["A/x", "B/x", "C/x", "D/x"]])
        self.assertEqual(self.doc()["skipped"], {"excluded": 2, "symlinks": 1})
        # .git compared too: A and B are whole copies (hidden files count
        # within their folder, though they aren't listed)
        self.scan("--no-default-excludes")
        fams, _ = self.families()
        self.assertIn(("identical", "B"), fams.get("A", []))

    @unittest.skipIf(IS_ROOT, "root can read anything")
    def test_taint_from_an_unreadable_folder(self):
        for top in ("A", "B"):
            self.tree_of(top, {"x": "x", "sub/y": "y"})
        locked = self.mk("A", "locked")
        os.chmod(locked, 0)
        r = self.scan(code=1)
        fams, covered = self.families()
        # A can be kept (what it holds that was read is in B), never removed
        self.assertEqual((fams, covered), ({"A": [("partial", "B")]}, []))
        self.assertIn("cannot read", r.stdout.decode() + r.stderr.decode())

    def test_hard_links_count_once(self):
        big = blob("big", 50_000)
        self.tree_of("A", {"f": big, "g": "g"})
        os.link(os.path.join(self.root, "A", "f"), os.path.join(self.root, "A", "f-link"))
        os.link(os.path.join(self.root, "A", "f"), os.path.join(self.root, "elsewhere"))
        r = self.scan()
        self.assertEqual(self.groups(), [])                   # names of one file: not copies
        self.assertIn("Hard links:      2 extra names", r.stdout.decode())
        # A copy of A: removing it frees each file once
        self.tree_of("B", {"f": big, "g": "g"})
        os.link(os.path.join(self.root, "B", "f"), os.path.join(self.root, "B", "f-link"))
        self.scan()
        fams, _ = self.families()
        self.assertEqual(fams, {"A": [("identical", "B")]})
        f, m = self.member(self.doc(), "B")
        self.assertEqual(m["freed"], 50_000 + 3000)
        self.assertEqual(self.groups(folded=False), [])
        # A file with a name outside the folder frees nothing when the folder goes
        os.link(os.path.join(self.root, "B", "f"), os.path.join(self.root, "B-outside"))
        self.scan()
        f, m = self.member(self.doc(), "B")
        self.assertEqual(m["freed"], 3000)

    def test_empty_files_are_never_duplicates(self):
        self.tree_of("A", {"e1": b"", "e2": b"", "x": "x"})
        self.tree_of("B", {"e3": b"", "x": "x"})
        self.tree_of("C", {"only-empty": b""})
        self.tree_of("D", {"only-empty": b""})
        self.scan("--min-size", "0")
        self.assertEqual(self.groups(), [["A/x", "B/x"]])
        fams, _ = self.families()
        self.assertEqual(fams, {"A": [("renamed", "B")]})    # empty files: names only

    def test_overlapping_roots_are_scanned_once(self):
        self.tree_of("a", {"x": "x", "b/y": "y"})
        self.tree_of("c", {"x": "x", "y": "y"})
        a = os.path.join(self.root, "a")
        r = self.scan(roots=[a, self.root, os.path.join(a, "b"), self.root + "/"])
        d = self.doc()
        self.assertEqual(d["roots"], [self.root])
        self.assertEqual(d["total_files"], 4)
        self.assertEqual(self.groups(), [["a/b/y", "c/y"], ["a/x", "c/x"]])
        self.assertIn("Files scanned:   4", r.stdout.decode())

    def test_several_roots(self):
        one, two = os.path.join(self.tmp, "one"), os.path.join(self.tmp, "two")
        for base in (one, two):
            os.makedirs(os.path.join(base, "Pictures"))
            for name, tag in PHOTOS.items():
                path = os.path.join(base, "Pictures", name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(blob(tag))
        self.scan(roots=[two, one])
        d = self.doc()
        self.assertEqual(d["roots"], [two, one])
        paths = report_data.paths_of(d["dirs"])
        f, = d["families"]
        # the first root given holds the kept copy
        self.assertEqual(paths[f["anchor"]], two)
        self.assertEqual(paths[f["members"][0]["folder"]], one)

    def test_hidden_files(self):
        self.tree_of("", {".config/a": "a", "visible/a": "a", "b1": "b", ".b2": "b"})
        self.scan()
        self.assertEqual(self.groups(), [])
        self.scan("--hidden")
        self.assertEqual(self.groups(), [[".b2", "b1"], [".config/a", "visible/a"]])


class TestFilters(FolderBase):
    def setUp(self):
        super().setUp()
        self.tree_of("A", {"p.jpg": "p", "s.mp3": "s", "d.pdf": "d", "big.bin": blob("big", 20_000)})
        self.tree_of("B", {"p.JPG": "p", "s.mp3": "s", "d.pdf": "d", "big.bin": blob("big", 20_000)})

    def check(self, *args, expect):
        r = self.scan(*args)
        names = sorted(os.path.basename(g[0]) for g in self.groups())
        self.assertEqual(names, expect, args)
        return r

    def test_types(self):
        r = self.check("--type", "images", expect=["p.jpg"])
        self.assertIn("Filters:         types: images\n", r.stdout.decode())
        self.assertIn("Skipped:         6 filtered-out files", r.stdout.decode())
        self.check("--type", "audio,documents", expect=["d.pdf", "s.mp3"])
        self.check("--type", "images", "--type", "audio", expect=["p.jpg", "s.mp3"])
        r = self.dupes(self.root, "--type", "pictures")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"images", r.stderr)
        # A filtered-out file means the folder is not compared as a whole
        self.assertEqual(self.families()[0], {})
        self.check(expect=["big.bin", "d.pdf", "p.jpg", "s.mp3"])
        self.assertEqual(self.families()[0], {"A": [("renamed", "B")]})

    def test_extensions(self):
        self.check("--ext", "pdf,.MP3", expect=["d.pdf", "s.mp3"])
        self.check("--not-ext", "bin", "--not-ext", "jpg", expect=["d.pdf", "s.mp3"])
        self.check("--type", "images", "--ext", "bin", expect=["big.bin", "p.jpg"])

    def test_sizes(self):
        self.check("--min-size", "10K", expect=["big.bin"])
        self.check("--max-size", "10K", expect=["d.pdf", "p.jpg", "s.mp3"])
        self.check("--min-size", "1K", "--max-size", "3000", expect=["d.pdf", "p.jpg", "s.mp3"])
        r = self.dupes(self.root, "--max-size", "huge")
        self.assertEqual(r.returncode, 2)

    def test_dates(self):
        old = time.time() - 40 * 86400
        for top in ("A", "B"):
            os.utime(os.path.join(self.root, top, "d.pdf"), (old, old))
        self.check("--older-than", "30d", expect=["d.pdf"])
        self.check("--newer-than", "30d", expect=["big.bin", "p.jpg", "s.mp3"])
        day = time.strftime("%Y-%m-%d", time.localtime(old + 86400))
        self.check("--older-than", day, expect=["d.pdf"])
        r = self.dupes(self.root, "--newer-than", "last tuesday")
        self.assertEqual(r.returncode, 2)

    def test_names(self):
        self.check("--name", "*.jpg", expect=["p.jpg"])          # ignoring case
        self.check("--name", "?.*", "--name", "big*", expect=["big.bin", "d.pdf", "p.jpg", "s.mp3"])

    def test_max_depth(self):
        self.tree_of("A/deep", {"q": "q"})
        self.tree_of("B/deep", {"q": "q"})
        self.tree_of("", {"top1": "t", "top2": "t"})
        self.check("--max-depth", "0", expect=["top1"])
        self.check("--max-depth", "1", expect=["big.bin", "d.pdf", "p.jpg", "s.mp3", "top1"])
        self.assertEqual(self.families()[0], {})                  # A and B go deeper
        self.check("--max-depth", "2", expect=["big.bin", "d.pdf", "p.jpg", "q", "s.mp3", "top1"])
        self.assertEqual(self.families()[0], {"A": [("renamed", "B")]})


class TestMatching(FolderBase):
    def test_same_name(self):
        self.tree_of("", {"a/photo.jpg": "p", "b/PHOTO.jpg": "p", "c/other.jpg": "p"})
        self.scan()
        self.assertEqual(self.groups(), [["a/photo.jpg", "b/PHOTO.jpg", "c/other.jpg"]])
        r = self.scan("--same-name")
        self.assertEqual(self.groups(), [["a/photo.jpg", "b/PHOTO.jpg"]])
        self.assertIn("same content + same name", r.stdout.decode())

    def test_same_date(self):
        self.tree_of("", {"a": "p", "b": "p", "c": "p"})
        t = OLD
        os.utime(os.path.join(self.root, "a"), (t, t))
        os.utime(os.path.join(self.root, "b"), (t + 0.3, t + 0.3) if int(t + 0.3) == int(t)
                 else (t, t))
        os.utime(os.path.join(self.root, "c"), (t + 5, t + 5))
        self.scan("--same-date")
        self.assertEqual(self.groups(), [["a", "b"]])
        self.scan("--same-date", "--same-name")
        self.assertEqual(self.groups(), [])


class TestCache(FolderBase):
    def make(self):
        for top in ("A", "B"):
            self.tree_of(top, PHOTOS)
        self.tree_of("", {"big1": blob("big", 100_000), "big2": blob("big", 100_000),
                          "lonely": blob("lonely", 100_001)})
        for dirpath, _, files in os.walk(self.root):
            for name in files:
                os.utime(os.path.join(dirpath, name), (OLD, OLD))

    def hashing(self):
        return self.doc()["hashing"]

    def test_a_warm_rescan_reads_nothing(self):
        self.make()
        self.scan()
        cold = self.hashing()
        self.assertEqual(cold["cache"], "on")
        self.assertEqual(cold["cached"], 0)
        self.assertEqual(cold["read"], 12)          # 'lonely' has no same-size partner
        self.assertEqual(cold["bytes"], 10 * 3000 + 2 * 4096 + 2 * 100_000)
        self.assertGreaterEqual(cold["stored"], 12)
        before = self.groups()
        r = self.scan()
        warm = self.hashing()
        self.assertEqual((warm["read"], warm["bytes"], warm["cached"]), (0, 0, 12))
        self.assertEqual(self.groups(), before)
        self.assertIn("Hashing:         0 files read (0 B), 12 hashes from the cache",
                      r.stdout.decode())

    def test_a_rescan_renews_seen_only_when_old(self):
        # A scan stamps `seen` on the entries it uses (trimming the cache drops
        # the ones met longest ago), but rewrites a stamp only once it is over
        # an hour old: a warm rescan doesn't rewrite every row.
        self.make()
        self.scan()
        cache = os.path.join(self.tmp, "xdg", "cache", "clean", "hashes.sqlite")
        recent = int(time.time()) - 60
        db = sqlite3.connect(cache)
        paths = [p for p, in db.execute("SELECT path FROM hashes ORDER BY path")]
        db.execute("UPDATE hashes SET seen = 1000")
        db.execute("UPDATE hashes SET seen = ? WHERE path = ?", (recent, paths[0]))
        db.commit()
        db.close()
        start = int(time.time())
        self.scan()
        self.assertEqual(self.hashing()["cached"], 12)
        db = sqlite3.connect(cache)
        seen = dict(db.execute("SELECT path, seen FROM hashes"))
        db.close()
        self.assertEqual(seen[paths[0]], recent)
        self.assertEqual(sorted(p for p in paths if seen[p] >= start), paths[1:])

    def test_changed_and_deleted_files(self):
        self.make()
        self.scan()
        path = os.path.join(self.root, "big2")
        with open(path, "r+b") as f:        # the same size and first 4 KiB
            f.seek(90_000)
            f.write(b"X")
        os.utime(path, (OLD + 1, OLD + 1))
        os.unlink(os.path.join(self.root, "B", "notes.txt"))
        self.scan()
        h = self.hashing()
        # big2 is read again (all of it); big1 and the 9 photos come from the cache
        self.assertEqual((h["read"], h["bytes"], h["cached"]), (1, 4096 + 100_000, 10))
        self.assertEqual(h["pruned"], 1)            # B/notes.txt (big2's entry was replaced)
        self.assertNotIn(["big1", "big2"], self.groups())
        self.scan()
        self.assertEqual((self.hashing()["read"], self.hashing()["pruned"]), (0, 0))

    def test_a_moved_folder_is_read_again(self):
        # Keyed by the path inside the drive: a moved file is a new one
        self.make()
        self.scan()
        os.rename(os.path.join(self.root, "A"), os.path.join(self.root, "A2"))
        self.scan()
        h = self.hashing()
        self.assertEqual((h["read"], h["cached"], h["pruned"]), (5, 7, 5))
        self.scan()
        h = self.hashing()
        self.assertEqual((h["read"], h["cached"], h["pruned"]), (0, 12, 0))

    def test_entries_of_folders_not_entered_are_kept(self):
        self.make()
        self.scan()
        # Excluded, too deep, or a deleted file in an excluded folder: kept;
        # a folder deleted for real: dropped
        self.scan("-e", "A", "--max-depth", "1")
        self.assertEqual(self.hashing()["pruned"], 0)
        self.scan()
        self.assertEqual((self.hashing()["read"], self.hashing()["cached"]), (0, 12))
        import shutil
        shutil.rmtree(os.path.join(self.root, "B", "2019"))
        self.scan("-e", "A")
        self.assertEqual(self.hashing()["pruned"], 3)

    def test_new_files_are_not_stored(self):
        self.tree_of("", {"a": "a", "b": "a"})          # changed just now
        self.scan()
        self.scan()
        self.assertEqual(self.hashing()["cached"], 0)

    def test_no_cache(self):
        self.make()
        cache_dir = os.path.join(self.tmp, "xdg", "cache", "clean")
        r = self.scan("--no-cache")
        self.assertFalse(os.path.exists(cache_dir))
        self.assertEqual(self.hashing()["cache"], "off")
        self.assertNotIn("from the cache", r.stdout.decode())
        r = self.run_clean("dupes", self.root, env={"CLEAN_CACHE_ENABLED": "off"})
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(cache_dir))
        self.scan("--cache", "--no-cache", "--cache")
        self.assertTrue(os.path.exists(os.path.join(cache_dir, "hashes.sqlite")))

    def test_a_broken_cache_warns_and_goes_on(self):
        self.make()
        cache = os.path.join(self.tmp, "xdg", "cache", "clean", "hashes.sqlite")
        os.makedirs(os.path.dirname(cache))
        with open(cache, "wb") as f:
            f.write(b"this is not a database" * 100)
        r = self.scan()                     # results are complete: not an error
        self.assertIn(b"clean: warning:", r.stderr)
        self.assertIn(b"hash cache", r.stderr)
        self.assertEqual(self.hashing()["cache"], "failed")
        self.assertEqual(len(self.groups()), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
