"""Choosing what to remove: the default choices, the rules, and the hard
block on removing the last copy of anything, checked on scanned trees and
on random manifests whose claims may be plain wrong."""

import os
import random
import unittest

from helpers import SLOW, Base
from test_folders import PHOTOS, FolderBase, analyse, blob

from cleanup import selection
from cleanup.dupes import manifest
from cleanup.report import data
from cleanup.scan.filters import JUNK_NAMES
from cleanup.selection import Rules, parse_rank, select


def manifest_of(roots, **kw):
    """Scan in this process; the manifest as `clean remove` would read it."""
    tree, matcher, rel, _ = analyse(roots, **kw)
    obj = manifest.build(tree, matcher, rel, JUNK_NAMES)
    path = os.path.join(os.path.dirname(roots[0]), "m.json.gz")
    with open(path, "wb") as f:
        f.write(data.encode(obj))
    return manifest.Manifest.read(path)


class SelBase(FolderBase):
    def plan(self, rules=None, roots=None, **kw):
        self.m = manifest_of(roots or [self.root], **kw)
        return select(self.m, rules or Rules())

    def picked(self, plan):
        return [(it.kind, self.rel(it.path)) for it in plan.items]

    def blocked(self, plan):
        return [(self.rel(it.path), why) for it, why in plan.blocked]

    def at(self, *rel):
        return os.path.join(self.root, *rel)


class TestDefaults(SelBase):
    def test_manifest_round_trip(self):
        self.tree_of("A", PHOTOS)
        self.tree_of("B", PHOTOS)
        self.write("lone.txt", blob("lone"))
        os.link(self.at("A", "notes.txt"), self.at("hard.txt"))
        m = manifest_of([self.root])
        self.assertEqual(m.roots, [self.root])
        [[fid, anchor, members]] = m.families
        self.assertEqual((m.paths[anchor], [(m.paths[d], r) for d, r in members]),
                         (self.at("A"), [(self.at("B"), "identical")]))
        listed = sorted(self.rel(m.paths[d]) for d in m.listed)
        self.assertEqual(listed, ["A", "B"])
        paths = {self.rel(m.file_path(f)): f for f in range(len(m.files))}
        self.assertNotIn("lone.txt", paths)                 # no copy: not needed
        self.assertIn("hard.txt", paths)                    # a name of a copy
        self.assertEqual(m.inode_key(paths["hard.txt"]), m.inode_key(paths["A/notes.txt"]))
        self.assertNotEqual(m.inode_key(paths["B/notes.txt"]), m.inode_key(paths["A/notes.txt"]))
        f = paths["B/2019/jan/a.jpg"]
        st = os.stat(self.at("B/2019/jan/a.jpg"))
        self.assertEqual(m.files[f][2:4], [st.st_size, st.st_mtime_ns])
        import hashlib
        self.assertEqual(m.hashes[m.files[f][4]],
                         hashlib.blake2b(blob("a")).hexdigest())

    def test_family_keeps_the_anchor(self):
        for top in ("Backup1", "Backup2", "Backup3"):
            self.tree_of(top, PHOTOS)
        p = self.plan()
        self.assertEqual(self.picked(p), [("folder", "Backup2"), ("folder", "Backup3")])
        self.assertEqual(p.items[0].why, f"identical copy of {self.at('Backup1')}")
        self.assertEqual(p.blocked, [])
        self.assertEqual(p.totals(), (2 * 5 * 3000, 10, 2))
        c = self.m.files[p.files_of(p.items[0])[0]][4]
        self.assertEqual([self.rel(self.m.file_path(f)) for f in p.survivors(c)][:1],
                         ["Backup1/" + self.rel(self.m.file_path(p.files_of(p.items[0])[0]))
                          .split("/", 1)[1]])

    def test_partial_goes_diverged_stays(self):
        self.tree_of("A", {"a": "a", "b": "b", "c": "c", "d": "d", "n": "n"})
        self.tree_of("Part", {"a": "a", "b": "b", "c": "c"})
        self.tree_of("Div", {"a": "a", "b": "b", "c": "c", "d": "d", "e": "e1"})
        p = self.plan(similar=50)
        self.assertEqual(self.picked(p), [("folder", "Part")])
        p = select(self.m, Rules(merge=True))
        self.assertEqual(self.picked(p), [("merge", "Div"), ("folder", "Part")])
        self.assertEqual(self.rel(self.m.paths[p.items[0].into]), "A")

    def test_covered_folder(self):
        self.tree_of("X", {"a": "a", "b": "b"})
        self.tree_of("Y", {"c": "c", "d": "d"})
        self.tree_of("Mixed", {"a": "a", "c": "c"})
        p = self.plan()
        self.assertEqual(self.picked(p), [("folder", "Mixed")])
        self.assertEqual(p.items[0].why, "every file has a copy elsewhere")

    def test_file_groups_keep_one(self):
        self.write("one/a.jpg", blob("a"))
        self.write("two/deeper/a.jpg", blob("a"))
        self.write("two/deeper/x", blob("x"))
        self.write("one/y", blob("y"))
        p = self.plan()
        self.assertEqual(self.picked(p), [("file", "two/deeper/a.jpg")])
        self.assertEqual(p.items[0].why, f"copy of {self.at('one/a.jpg')}")

    def test_kept_folder_not_hollowed_out(self):
        # A's copies are kept; a stray copy of one file elsewhere goes, A's stays
        self.tree_of("A", PHOTOS)
        self.tree_of("B", PHOTOS)
        self.write("zz/a.jpg", blob("a"))
        self.write("zz/own", blob("own"))
        p = self.plan(rules=Rules(rank=parse_rank(["newest"])))
        self.assertEqual(self.picked(p), [("folder", "B"), ("file", "zz/a.jpg")])


class TestRules(SelBase):
    def test_keep_in_wins_over_the_anchor(self):
        for top in ("Backup1", "Backup2", "Backup3"):
            self.tree_of(top, PHOTOS)
        p = self.plan(Rules(keep_in=[self.at("Backup3")]))
        self.assertEqual(self.picked(p), [("folder", "Backup1"), ("folder", "Backup2")])
        self.assertEqual(p.items[0].why, f"identical copy of {self.at('Backup3')}")

    def test_keep_in_inside_a_copy_keeps_that_copy(self):
        for top in ("Backup1", "Backup2"):
            self.tree_of(top, PHOTOS)
        p = self.plan(Rules(keep_in=[self.at("Backup2", "2019")]))
        self.assertEqual(self.picked(p), [("folder", "Backup1")])

    def test_keep_in_a_backup_of_nested_copies(self):
        # Pictures = Backup (which only wraps a copy of it); Pictures/2019
        # has a renamed copy and a partial one. Keeping the backup, all of
        # them go: 2019's family keeps Backup/Pictures/2019, found by its
        # files, instead of the one in Pictures.
        pics = {f"2019/IMG_{i}.jpg": "p" + n for i, n in enumerate("abcde")}
        pics.update({"2020/IMG_100.jpg": "pf", "2020/IMG_101.jpg": "pg"})
        self.tree_of("Pictures", pics)
        self.tree_of("Backup/Pictures", pics)
        self.write("Backup/Pictures/2019/Thumbs.db", blob("thumbs"))
        self.tree_of("Phone dump", {f"2019061{i}.jpg": "p" + n for i, n in enumerate("abcde")})
        self.tree_of("Old laptop/Pictures 2019",
                     {f"IMG_{i}.jpg": "p" + n for i, n in enumerate("abc")})
        self.write("Old laptop/todo.txt", blob("todo"))
        for rules in (Rules(keep_in=[self.at("Backup")], rank=parse_rank(["newest"])),
                      Rules(keep_in=[self.at("Backup")])):
            p = self.plan(rules)
            twin = self.at("Backup", "Pictures", "2019")
            self.assertEqual([(self.rel(it.path), it.why) for it in p.items],
                             [("Phone dump", f"same files as {twin}"),
                              ("Pictures", f"same files as {self.at('Backup')}"),
                              ("Old laptop/Pictures 2019", f"all its files are in {twin}")])
            self.assertEqual(p.blocked, [])
        # without the rule, Pictures stays and the backup goes
        p = self.plan()
        self.assertEqual(self.picked(p), [("folder", "Phone dump"), ("folder", "Backup"),
                                          ("folder", "Old laptop/Pictures 2019")])

    def test_keep_in_everything_removes_nothing(self):
        self.tree_of("A", PHOTOS)
        self.tree_of("B", PHOTOS)
        self.write("c/a.jpg", blob("a"))
        p = self.plan(Rules(keep_in=[self.root]))
        self.assertEqual((p.items, p.blocked), ([], []))

    def test_remove_from_only_there(self):
        self.tree_of("A", PHOTOS)
        self.tree_of("B", PHOTOS)
        self.tree_of("Old/C", PHOTOS)
        self.write("loose/a.jpg", blob("a"))
        self.write("loose/own", blob("own"))
        self.write("Old/a.jpg", blob("a"))
        self.write("Old/own", blob("own2"))
        p = self.plan(Rules(remove_from=[self.at("Old")]))
        self.assertEqual(self.picked(p), [("folder", "Old/C"), ("file", "Old/a.jpg")])
        # only Old: A, the anchor, would stay even though B is identical
        p = select(self.m, Rules(remove_from=[self.at("A")]))
        self.assertEqual(self.picked(p), [("folder", "A")])
        self.assertEqual(p.items[0].why, f"identical copy of {self.at('B')}")

    def test_remove_from_hollows_out_a_kept_folder(self):
        self.tree_of("E", {"three": "3", "one": "1", "own": "own"})
        self.tree_of("D", {"three": "3"})
        self.write("A/one", blob("1"))
        self.write("A/two", blob("2"))
        p = self.plan(Rules(remove_from=[self.at("E")]))
        self.assertEqual(sorted(self.picked(p)), [("file", "E/one"), ("file", "E/three")])

    def test_rank_rules(self):
        # same content, three places; each rule picks a different keeper
        old = 1_000_000_000
        for rel, age in (("zz/deep/er/a.jpg", 3), ("b/a.jpg", 2), ("Camera/x/a.jpg", 1)):
            path = self.write(rel, blob("a"))
            os.utime(path, (old + age, old + age))
        for k in range(3):
            self.write(f"zz/deep/er/other{k}", blob(f"o{k}"))
        self.write("b/own", blob("b-own"))
        self.write("Camera/x/own", blob("c-own"))
        roots = [self.at("zz"), self.at("Camera"), self.at("b")]
        m = manifest_of(roots)
        for rank, kept in (("first-root", "zz/deep/er/a.jpg"), ("oldest", "Camera/x/a.jpg"),
                           ("newest", "zz/deep/er/a.jpg"), ("shortest", "b/a.jpg"),
                           ("largest-folder", "zz/deep/er/a.jpg"),
                           ("path-contains=CAMERA", "Camera/x/a.jpg")):
            with self.subTest(rank=rank):
                p = select(m, Rules(rank=parse_rank([rank])))
                gone = {self.rel(it.path) for it in p.items}
                left = {"zz/deep/er/a.jpg", "b/a.jpg", "Camera/x/a.jpg"} - gone
                self.assertEqual(left, {kept})

    def test_rank_parsing(self):
        self.assertEqual(parse_rank(["Oldest", " path-contains=Foo Bar"]),
                         [("oldest", None), ("path-contains", "foo bar")])
        for bad in (["eldest"], ["path-contains"], ["path-contains="], ["oldest=1"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_rank(bad)
        self.assertEqual(selection.check_rank(["first-root", "path-contains=X"]),
                         ["first-root", "path-contains=x"])
        # commas separate rules, as in --rank newest,shortest; repeats go
        self.assertEqual(selection.check_rank(["newest,path-contains=Cam", "newest"]),
                         ["newest", "path-contains=cam"])
        with self.assertRaises(ValueError):
            parse_rank(["oldest,"])

    def test_family_rank(self):
        for top in ("A", "Newer"):
            self.tree_of(top, PHOTOS)
        for dp, _, names in os.walk(self.at("A")):
            for n in names:
                os.utime(os.path.join(dp, n), (1_000_000_000, 1_000_000_000))
        p = self.plan(Rules(rank=parse_rank(["newest"]), family_rank=True))
        self.assertEqual(self.picked(p), [("folder", "A")])
        p = select(self.m, Rules(rank=parse_rank(["newest"])))
        self.assertEqual(self.picked(p), [("folder", "Newer")])

    def test_only(self):
        self.tree_of("A", PHOTOS)
        self.tree_of("B", PHOTOS)
        self.write("x/a.jpg", blob("a"))
        self.write("x/own", blob("own"))
        self.assertEqual(self.picked(self.plan(Rules(only="folders"))), [("folder", "B")])
        files = self.picked(select(self.m, Rules(only="files")))
        self.assertEqual(sorted(files), sorted([("file", "B/" + k) for k in PHOTOS]
                                               + [("file", "x/a.jpg")]))


class TestChoices(SelBase):
    """The dashboard's choices win over the rules, but never over the last
    copy of anything or a keep-in folder."""

    def row(self, rel):
        return "d%d" % self.m.paths.index(self.at(rel))

    def file(self, rel):
        return "f%d" % next(f for f in range(len(self.m.files))
                            if self.m.file_path(f) == self.at(rel))

    def three(self, **more):
        for top in ("Backup1", "Backup2", "Backup3"):
            self.tree_of(top, PHOTOS)
        for rel, tag in more.items():
            self.write(rel, blob(tag))
        return self.plan()

    def choose(self, choices, **kw):
        rules = Rules(choices=choices, **kw)
        p = select(self.m, rules)
        check_plan(self, self.m, p, rules)
        return p

    def test_keeping_a_copy_the_rules_remove(self):
        self.three()
        p = self.choose({self.row("Backup2"): "keep"})
        self.assertEqual(self.picked(p), [("folder", "Backup3")])
        self.assertEqual((p.blocked, p.items[0].chosen), ([], False))

    def test_removing_the_anchor_keeps_another_copy(self):
        self.three()
        p = self.choose({self.row("Backup1"): "remove"})
        self.assertEqual(self.picked(p), [("folder", "Backup1"), ("folder", "Backup3")])
        self.assertEqual([it.chosen for it in p.items], [True, False])
        self.assertEqual(p.items[0].why, f"identical copy of {self.at('Backup2')}")
        self.assertEqual([self.rel(self.m.paths[d]) for d in p.keepers.values()], ["Backup2"])

    def test_every_copy_chosen_to_go_keeps_the_anchor(self):
        self.three()
        p = self.choose({self.row(t): "remove" for t in ("Backup1", "Backup2", "Backup3")})
        self.assertEqual(self.picked(p), [("folder", "Backup2"), ("folder", "Backup3")])
        [(path, why)] = self.blocked(p)
        self.assertEqual(path, "Backup1")
        self.assertTrue(why.startswith(f"it holds the last copy left of {self.at('Backup1')}/"))

    def test_a_choice_merges_a_diverged_copy(self):
        self.tree_of("A", {"a": "a", "b": "b", "c": "c", "d": "d", "n": "n"})
        self.tree_of("Div", {"a": "a", "b": "b", "c": "c", "d": "d", "e": "e1"})
        self.assertEqual(self.picked(self.plan(similar=50)), [])
        p = self.choose({self.row("Div"): "remove"})
        self.assertEqual(self.picked(p), [("merge", "Div")])
        self.assertTrue(p.items[0].chosen)
        self.assertEqual(self.rel(self.m.paths[p.items[0].into]), "A")
        # the anchor can't be merged anywhere: the folder goes, if it may
        p = self.choose({self.row("A"): "remove"})
        self.assertEqual(self.blocked(p)[0][0], "A")

    def test_nothing_but_the_choices(self):
        self.three(**{"one/z": "z", "two/z": "z", "one/o": "o1", "two/o": "o2"})
        self.assertEqual(self.picked(self.plan()),
                         [("folder", "Backup2"), ("folder", "Backup3"), ("file", "two/z")])
        self.assertEqual(self.picked(self.choose({}, base="none")), [])
        p = self.choose({self.row("Backup3"): "remove", self.file("one/z"): "remove"},
                        base="none")
        self.assertEqual(self.picked(p), [("folder", "Backup3"), ("file", "one/z")])
        self.assertEqual(p.items[1].why, f"copy of {self.at('two/z')}")
        self.assertEqual([it.chosen for it in p.items], [True, True])

    def test_a_kept_file_keeps_its_folder(self):
        self.three()
        kept = self.at("Backup3/2020/d.jpg")
        p = self.choose({self.file("Backup3/2020/d.jpg"): "keep"})
        self.assertEqual(self.picked(p), [("folder", "Backup2")])
        self.assertEqual(self.blocked(p), [("Backup3", f"it holds {kept}, which you chose "
                                                      f"to keep")])

    def test_a_kept_folder_keeps_what_holds_it(self):
        self.three()
        p = self.choose({self.row("Backup3/2019"): "keep"})
        self.assertEqual(self.picked(p), [("folder", "Backup2")])
        self.assertEqual(self.blocked(p), [("Backup3", f"it holds {self.at('Backup3/2019')}, "
                                                      f"which you chose to keep")])

    def test_keep_in_beats_a_choice(self):
        self.three()
        p = self.choose({self.row("Backup2"): "remove", self.file("Backup2/notes.txt"): "remove",
                         self.file("Backup1/2020/d.jpg"): "remove"},
                        keep_in=[self.at("Backup2")])
        self.assertEqual(self.picked(p), [("folder", "Backup3"), ("file", "Backup1/2020/d.jpg")])
        self.assertEqual(self.blocked(p), [
            ("Backup2", "it is a keep-in folder: its copies are always kept"),
            ("Backup2/notes.txt", f"it is in {self.at('Backup2')}, whose copies are always "
                                  f"kept")])

    def test_a_choice_outside_remove_from(self):
        self.three(**{"one/z": "z", "two/z": "z", "one/o": "o1", "two/o": "o2"})
        p = self.choose({self.file("one/z"): "remove"}, remove_from=[self.at("Backup3")])
        self.assertEqual(self.picked(p), [("folder", "Backup3"), ("file", "one/z")])

    def test_one_per_group_and_extensions(self):
        for d in "abcd":
            self.write(f"{d}/x.jpg", blob("x"))
            self.write(f"{d}/y.PNG", blob("y"))
            self.write(f"{d}/own", blob("own" + d))
        names = lambda p: sorted(os.path.basename(x) for _, x in self.picked(p))  # noqa
        self.assertEqual(names(self.plan()), ["x.jpg"] * 3 + ["y.PNG"] * 3)
        p = self.choose({}, per_group="one")
        self.assertEqual(names(p), ["x.jpg", "y.PNG"])
        self.assertEqual(names(self.choose({}, exts=[".png"])), ["y.PNG"] * 3)
        self.assertEqual(names(self.choose({}, exts=["jpg"], per_group="one")), ["x.jpg"])
        # a copy chosen to stay stays too
        p = self.choose({self.file("c/x.jpg"): "keep"}, exts=["jpg"])
        self.assertEqual(sorted(x for _, x in self.picked(p)), ["b/x.jpg", "d/x.jpg"])

    def test_every_file_chosen_to_go_keeps_the_best(self):
        for d in "abc":
            self.write(f"{d}/x.jpg", blob("x"))
            self.write(f"{d}/own", blob("own" + d))
        self.plan()
        p = self.choose({self.file(f"{d}/x.jpg"): "remove" for d in "abc"}, base="none")
        self.assertEqual(sorted(self.picked(p)), [("file", "b/x.jpg"), ("file", "c/x.jpg")])
        self.assertEqual(self.blocked(p), [("a/x.jpg", "it is the last copy left")])

    def test_digest(self):
        self.three()
        a = selection.digest(select(self.m, Rules()))
        self.assertEqual(len(a), 24)
        self.assertEqual(a, selection.digest(select(self.m, Rules())))
        self.assertNotEqual(a, selection.digest(select(self.m, Rules(
            choices={self.row("Backup2"): "keep"}))))

    def test_bad_choices(self):
        for bad in ({"x1": "keep"}, {"d1": "maybe"}, {"d-1": "keep"}, {"d": "keep"},
                    {"d²": "keep"}, {1: "keep"}, ["d1"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                selection.parse_choices(bad)
        self.assertEqual(selection.parse_choices({"d3": "keep", "f0": "remove"}),
                         ({3: "keep"}, {0: "remove"}))
        self.three()
        p = self.choose({"d99999": "remove", "f99999": "keep"})      # stale: ignored
        self.assertEqual(self.picked(p), [("folder", "Backup2"), ("folder", "Backup3")])


class TestBlocks(Base):
    """Hand-made manifests with wrong claims: selection must still never
    take the last copy."""

    def make(self, files, families=(), covered=(), groups=()):
        """files: {"dir/sub/name": (content, inode)}: a manifest over them."""
        rows, row = [[-1, "/r"]], {"": 0}

        def dir_row(path):
            if path not in row:
                parent, _, name = path.rpartition("/")
                p = dir_row(parent)
                row[path] = len(rows)
                rows.append([p, name])
            return row[path]
        entries = []
        for path, (content, node) in sorted(files.items()):
            d, _, name = path.rpartition("/")
            entries.append([dir_row(d), name, 10, 0, content, node])
        ncontent = 1 + max([c for c, _ in files.values()] + [0])
        obj = {"format": 1, "roots": ["/r"], "junk": [], "dirs": rows,
               "dir_files": [0] * len(rows), "listed": list(range(len(rows))),
               "files": entries, "hashes": [None] + ["00"] * (ncontent - 1),
               "families": [[f"F{i}", dir_row(a), [[dir_row(m), r] for m, r in ms]]
                            for i, (a, ms) in enumerate(families)],
               "covered": [[f"C{i}", dir_row(c)] for i, c in enumerate(covered)],
               "groups": [[i, False, [sorted(files).index(p) for p in g]]
                          for i, g in enumerate(groups)]}
        return manifest.Manifest(obj)

    def test_wrong_family_claim_is_blocked(self):
        m = self.make({"a/1": (1, 0), "b/1": (1, 0), "b/2": (2, 0)},
                      families=[("a", [("b", "identical")])])
        p = select(m, Rules())
        self.assertEqual(p.items, [])
        [(it, why)] = p.blocked
        self.assertEqual((it.path, why), ("/r/b", "it holds the last copy left of /r/b/2"))

    def test_two_copies_claiming_each_other(self):
        m = self.make({"a/1": (1, 0), "b/1": (1, 0)},
                      covered=["a", "b"])
        p = select(m, Rules())
        self.assertEqual([it.path for it in p.items], ["/r/a"])
        self.assertEqual([it.path for it, _ in p.blocked], ["/r/b"])

    def test_hard_link_twin_is_not_a_copy(self):
        m = self.make({"a/1": (1, 7), "b/1": (1, 7)}, covered=["b"],
                      groups=[["a/1", "b/1"]])
        p = select(m, Rules())
        self.assertEqual(p.items, [])
        # b stays whole, so its name is the one kept, and the twin can't go
        self.assertEqual([(it.path, why) for it, why in p.blocked],
                         [("/r/b", "it holds the last copy left of /r/b/1"),
                          ("/r/a/1", "it is the last copy left")])

    def test_keeper_inside_a_folder_that_goes(self):
        # x goes (covered), so the family keeps b rather than x/a in it
        m = self.make({"x/a/1": (1, 0), "b/1": (1, 0), "y/1": (1, 0)},
                      families=[("x/a", [("b", "identical")])], covered=["x"])
        p = select(m, Rules())
        self.assertEqual([(it.path, it.why) for it in p.items],
                         [("/r/x", "every file has a copy elsewhere")])
        self.assertEqual(p.blocked, [])

    def test_folder_holding_a_kept_folder(self):
        # x/a is the only folder its family can keep
        m = self.make({"x/a/1": (1, 0), "x/a/2": (2, 0), "b/1": (1, 0), "y/2": (2, 0)},
                      families=[("x/a", [("b", "partial")])], covered=["x"])
        p = select(m, Rules())
        self.assertEqual([it.path for it in p.items], ["/r/b"])
        self.assertEqual([(it.path, why) for it, why in p.blocked],
                         [("/r/x", "it holds /r/x/a, which is kept")])

    def test_twin_in_the_copy_that_stays(self):
        # P = Q, and P/a = c; keeping Q, the family of P/a keeps Q/a
        files = {"P/a/1": (1, 0), "P/2": (2, 0), "Q/a/1": (1, 0), "Q/2": (2, 0),
                 "c/1": (1, 0)}
        m = self.make(files, families=[("P/a", [("c", "renamed")]),
                                       ("P", [("Q", "identical")])])
        p = select(m, Rules(keep_in=["/r/Q"]))
        self.assertEqual([(it.path, it.why) for it in p.items],
                         [("/r/c", "same files as /r/Q/a"),
                          ("/r/P", "identical copy of /r/Q")])
        self.assertEqual(p.blocked, [])
        # the twin is found by its files when it has another name
        files = {k.replace("Q/a/", "Q/b/"): v for k, v in files.items()}
        m = self.make(files, families=[("P/a", [("c", "renamed")]),
                                       ("P", [("Q", "renamed")])])
        p = select(m, Rules(keep_in=["/r/Q"]))
        self.assertEqual([(it.path, it.why) for it in p.items],
                         [("/r/c", "same files as /r/Q/b"),
                          ("/r/P", "same files as /r/Q")])
        # no twin (Q/b lacks a file): c is kept instead of P/a, and P goes
        del files["Q/b/1"]
        files["Q/b/3"] = (3, 0)
        files["z/3"] = (3, 0)
        m = self.make(files, families=[("P/a", [("c", "renamed")]),
                                       ("P", [("Q", "renamed")])])
        p = select(m, Rules(keep_in=["/r/Q"]))
        self.assertEqual([(it.path, it.why) for it in p.items],
                         [("/r/P", "same files as /r/Q")])
        self.assertEqual(p.blocked, [])

    def test_kept_by_a_folder_holding_its_files(self):
        # P = Q, Q flattened: no folder in Q is P/a's twin, and P/a's other
        # copy is partial, but Q holds all of P/a's files
        m = self.make({"P/a/1": (1, 0), "P/a/2": (2, 0), "P/3": (3, 0), "c/1": (1, 0),
                       "Q/1": (1, 0), "Q/2": (2, 0), "Q/3": (3, 0)},
                      families=[("P/a", [("c", "partial")]), ("P", [("Q", "renamed")])])
        p = select(m, Rules(keep_in=["/r/Q"]))
        self.assertEqual([(it.path, it.why) for it in p.items],
                         [("/r/c", "all its files are in /r/Q"),
                          ("/r/P", "same files as /r/Q")])
        self.assertEqual(p.blocked, [])

    def test_nested_picks_counted_once(self):
        m = self.make({"a/1": (1, 0), "b/c/1": (1, 0), "b/2": (2, 0), "d/2": (2, 0)},
                      covered=["b", "b/c"], groups=[["a/1", "b/c/1"]])
        p = select(m, Rules())
        self.assertEqual([it.path for it in p.items], ["/r/b"])

    def test_random_manifests_never_lose_a_copy(self):
        rng = random.Random(11)
        for case in range(3000 if SLOW else 400):
            with self.subTest(case=case):
                self.random_manifest(rng)

    def random_manifest(self, rng):
        dirs = [""]
        for _ in range(rng.randint(1, 10)):
            dirs.append((rng.choice(dirs) + "/" if rng.random() < 0.6 else "")
                        .lstrip("/") + f"d{len(dirs)}")
        dirs = [d for d in dirs if d]
        files = {}
        for k in range(rng.randint(1, 25)):
            files[f"{rng.choice(dirs)}/f{k}"] = (rng.randint(0, 6),
                                                 rng.choice([0, 0, 0, 1, 2]))
        # an inode is one content
        node_content = {}
        for p, (c, node) in list(files.items()):
            if node:
                files[p] = (node_content.setdefault(node, c), node)
        used = sorted({p.rpartition("/")[0] for p in files})
        pick = lambda: rng.choice(used)     # noqa: E731
        roles = ["identical", "renamed", "partial", "diverged"]
        families = [(pick(), [(pick(), rng.choice(roles)) for _ in range(rng.randint(1, 3))])
                    for _ in range(rng.randint(0, 3))]
        covered = [pick() for _ in range(rng.randint(0, 3))]
        paths = sorted(files)
        groups = [rng.sample(paths, rng.randint(2, len(paths))) for _ in range(rng.randint(0, 4))
                  if len(paths) >= 2]
        m = self.make(files, families, covered, groups)
        sample = lambda: ["/r/" + rng.choice(used) for _ in range(rng.randint(0, 2))]  # noqa
        choices = {}
        if rng.random() < 0.5:
            for _ in range(rng.randint(1, 6)):
                key = (f"d{rng.randrange(len(m.rows))}" if rng.random() < 0.5
                       else f"f{rng.randrange(len(m.files))}")
                choices[key] = rng.choice(selection.CHOICES)
        rules = Rules(keep_in=sample() if rng.random() < 0.3 else [],
                      remove_from=sample() if rng.random() < 0.3 else [],
                      rank=parse_rank(rng.sample(["first-root", "oldest", "newest", "shortest",
                                                  "largest-folder", "path-contains=d1"],
                                                 rng.randint(0, 3))),
                      family_rank=rng.random() < 0.3,
                      only=rng.choice([None, None, "files", "folders"]),
                      merge=rng.random() < 0.3,
                      base=rng.choice(["rules", "rules", "none"]),
                      per_group=rng.choice(selection.PER_GROUP),
                      exts=rng.choice([(), (), ("",), ("x",)]),
                      choices=choices)
        p = select(m, rules)
        check_plan(self, m, p, rules)


def check_plan(test, m, p, rules):
    """Whatever the plan, every content keeps a file on disk nothing removes;
    nothing chosen to stay goes; without the rules, only what was chosen."""
    gone_keys, moved = set(), set()
    for it in p.items:
        for f in p.files_of(it):
            gone_keys.add(m.inode_key(f))
        if it.kind == "merge":
            into = {m.files[f][4] for f in m.files_in(it.into)}
            moved |= {m.files[f][4] for f in p.files_of(it)} - into
    for c in range(1, len(m.copies)):
        if m.copies[c] and c not in moved:
            test.assertTrue(any(m.inode_key(f) not in gone_keys for f in m.copies[c]),
                            f"content {c} lost: {[m.file_path(f) for f in m.copies[c]]}")
        if m.copies[c] and any(m.inode_key(f) in gone_keys for f in m.copies[c]) \
                and c not in moved:
            test.assertTrue(p.survivors(c))
    paths = [it.path for it in p.items]
    for it in p.items:
        test.assertFalse(any(q != it.path and it.path.startswith(q + "/") for q in paths),
                         "an item inside another")
        for k in rules.keep_in:
            test.assertFalse(it.path == k or it.path.startswith(k + "/")
                             or (it.kind != "file" and k.startswith(it.path + "/")),
                             f"{it.path} touches keep-in {k}")
        if rules.remove_from and not it.chosen:
            test.assertTrue(any(it.path == d or it.path.startswith(d + "/")
                                for d in rules.remove_from))
        key = ("f" if it.kind == "file" else "d") + str(it.target)
        test.assertEqual(it.chosen, rules.choices.get(key) == "remove", it)
        if rules.base == "none":
            test.assertTrue(it.chosen, f"{it.path} picked though only choices count")
        if it.kind == "merge":
            test.assertFalse(any(m.inside(it.into, x.target) for x in p.items
                                 if x.kind != "file"), "merged into a folder that goes")
    dirs, files = selection.parse_choices(rules.choices)
    for it in p.items:
        if it.kind != "file":
            test.assertFalse(any(v == "keep" and d < len(m.rows) and m.inside(d, it.target)
                                 for d, v in dirs.items()), f"{it.path} holds a kept folder")
        test.assertFalse(any(files.get(f) == "keep" for f in p.files_of(it)),
                         f"{it.path} takes a file chosen to stay")


class TestRandomTrees(SelBase):
    def test_random_trees_and_rules(self):
        """Scanned random trees (test_folders' generator) under random rules:
        on disk, every content still has a copy outside what would go."""
        rng = random.Random(5)
        gen = __import__("test_folders").TestFamilies("test_random_trees_never_lose_the_last_copy")
        for case in range(150 if SLOW else 30):
            with self.subTest(case=case):
                base = os.path.join(self.tmp, f"case{case}")
                captured = {}
                real = __import__("test_folders").analyse

                def spy(roots, **kw):
                    captured["roots"] = roots
                    captured["kw"] = kw
                    return real(roots, **kw)
                import test_folders
                test_folders.analyse = spy
                try:
                    gen.random_case(rng, base)
                finally:
                    test_folders.analyse = real
                m = manifest_of(captured["roots"], **captured["kw"])
                dirs = sorted({m.paths[d] for d in range(len(m.rows))})
                rules = Rules(keep_in=rng.sample(dirs, 1) if rng.random() < 0.3 else [],
                              remove_from=rng.sample(dirs, 1) if rng.random() < 0.2 else [],
                              rank=parse_rank(rng.sample(list(selection.DEFAULT_RANK)
                                                         + ["newest", "largest-folder"], 2)),
                              only=rng.choice([None, None, "files"]),
                              merge=rng.random() < 0.3)
                p = select(m, rules)
                check_plan(self, m, p, rules)
                self.check_disk(base, p)

    def test_random_nested_copies_keeping_one(self):
        """A tree, whole copies of it (one renamed or reorganised), and loose
        and partial copies of its folders. Keeping one whole copy, the other
        whole copies go and nothing is blocked; on disk, nothing is lost."""
        rng = random.Random(8)
        tags = iter(range(10 ** 6))

        def tree(depth=0):
            out = {f"f{i}.dat": f"t{next(tags)}" for i in range(rng.randint(1, 3))}
            for j in range(rng.randint(1 if depth == 0 else 0, 2 if depth < 2 else 0)):
                out.update({f"s{j}/{k}": v for k, v in tree(depth + 1).items()})
            return out

        def put(top, files):
            for rel, tag in files.items():
                path = os.path.join(top, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(blob(tag, 3000 + 2000 * (int(tag[1:]) % 2)))

        def renamed(files):
            if rng.random() < 0.5:                  # new names, same places
                return {os.path.join(os.path.dirname(k), "r_" + os.path.basename(k)): v
                        for k, v in files.items()}
            return {k.replace("/", "_"): v for k, v in files.items()}    # flattened

        for case in range(80 if SLOW else 15):
            with self.subTest(case=case):
                base = os.path.join(self.tmp, f"nest{case}")
                files = tree()
                put(os.path.join(base, "X"), files)
                put(os.path.join(base, "Y"), files)
                put(os.path.join(base, "Z"), renamed(files))
                subs = sorted({k.rsplit("/", 1)[0] for k in files if "/" in k})
                for k in range(rng.randint(1, 3)):
                    sub = rng.choice(subs)
                    part = {r[len(sub) + 1:]: v for r, v in files.items()
                            if r.startswith(sub + "/")}
                    if rng.random() < 0.5:
                        part = renamed(part)
                    put(os.path.join(base, f"loose{k}"), part)
                for k in range(rng.randint(0, 2)):
                    sub = rng.choice(subs + [""])
                    part = sorted(r for r in files if r.startswith(sub))
                    if len(part) > 1:
                        part = rng.sample(part, rng.randint(1, len(part) - 1))
                        put(os.path.join(base, f"part{k}"),
                            {r.replace("/", "_"): files[r] for r in part})
                keep = os.path.join(base, rng.choice("XYZ"))
                m = manifest_of([base])
                rules = Rules(keep_in=[keep],
                              rank=parse_rank(rng.sample(list(selection.DEFAULT_RANK)
                                                         + ["newest", "largest-folder"], 2)),
                              family_rank=rng.random() < 0.3)
                p = select(m, rules)
                self.assertEqual([(it.path, why) for it, why in p.blocked], [])
                gone = [it.path for it in p.items if it.kind != "file"]
                for top in "XYZ":
                    path = os.path.join(base, top)
                    if path != keep:
                        self.assertTrue(any(path == g or path.startswith(g + "/")
                                            for g in gone), f"{top} stays: {gone}")
                check_plan(self, m, p, rules)
                self.check_disk(base, p)

    def check_disk(self, base, p):
        junk = {n.lower() for n in JUNK_NAMES}
        everything = {}
        for dp, _, names in os.walk(base):
            for n in names:
                path = os.path.join(dp, n)
                if os.path.isfile(path) and not os.path.islink(path) and n.lower() not in junk:
                    with open(path, "rb") as f:
                        everything[path] = f.read()
        gone = set()
        moved = set()
        for it in p.items:
            if it.kind == "file":
                gone.add(it.path)
                continue
            for path in everything:
                if path.startswith(it.path + "/"):
                    gone.add(path)
                    if it.kind == "merge":
                        moved.add(everything[path])
        before = {v for v in everything.values() if v}
        after = {v for k, v in everything.items() if v and k not in gone} | moved
        self.assertEqual(before - after, set(), "the last copy of something would go")


if __name__ == "__main__":
    unittest.main()
