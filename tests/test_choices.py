#!/usr/bin/env python3
"""The dashboard's choices (selection.json in a dupes run's folder): reading
and checking the file, and `clean remove` acting on them: what it removes,
when it refuses (rules given as well, a broken file, a plan that is not the
one confirmed), and --no-choices. Also `clean history --delete`.

Every run that may move or delete anything happens in a sealed namespace on
trees in the test's temp folder, as in test_remove."""

import json
import os
import unittest
from unittest import mock

from helpers import Base, needs_unshare
from test_remove import RemoveBase

from cleanup import choices, cli, runs
from cleanup import settings as _settings
from cleanup.commands import remove as _remove
from cleanup.dupes import manifest as _manifest


class TestFile(Base):
    def setUp(self):
        super().setUp()
        self.run = runs.Run(self.mk("runs", "2026-10-08_120000-dupes"), {})

    def saved_text(self, obj):
        with open(self.run.file(choices.NAME), "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_round_trip(self):
        self.assertIsNone(choices.load(self.run))
        s = choices.Saved()
        self.assertTrue(s.is_default())
        s.rules.update(keep_in=["/a/b/"], exts=[".JPG", "png", " "], rank=["newest"])
        s.rules = choices.Saved.from_obj({"format": 1, "rules": s.rules}).rules
        s.choices = {"f10": "keep", "d2": "remove", "f9": "remove"}
        choices.save(self.run, s)
        back = choices.load(self.run)
        self.assertEqual(back.rules, dict(choices.DEFAULT_RULES, keep_in=["/a/b"],
                                          exts=["jpg", "png"], rank=["newest"]))
        self.assertEqual(list(back.to_obj()["choices"]), ["d2", "f9", "f10"])
        self.assertRegex(back.updated, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertFalse(back.is_default())
        self.assertEqual(os.listdir(self.run.folder), [choices.NAME])   # no temp files left
        choices.clear(self.run)
        choices.clear(self.run)
        self.assertIsNone(choices.load(self.run))

    def test_bad_files(self):
        for obj, text in (
                ("{", "selection.json is not valid JSON"),
                ([], "it is not a selection file this version of clean can read"),
                ({"format": 2}, "it is not a selection file"),
                ({"format": 1, "rules": []}, "its rules are not a JSON object"),
                ({"format": 1, "rules": {"keep_in": ["rel/path"]}}, "its keep_in is not a list "
                                                                     "of full paths"),
                ({"format": 1, "rules": {"keep_in": "/a"}}, "its keep_in is not a list"),
                ({"format": 1, "rules": {"rank": ["bogus"]}}, "bogus"),
                ({"format": 1, "rules": {"merge": 1}}, "its merge is not true or false"),
                ({"format": 1, "rules": {"base": "all"}}, "its base is 'all', not one of "
                                                          "rules, none"),
                ({"format": 1, "rules": {"conflicts": "x"}}, "its conflicts is 'x'"),
                ({"format": 1, "rules": {"per_group": 2}}, "its per_group is 2"),
                ({"format": 1, "rules": {"exts": "jpg"}}, "its exts is not a list"),
                ({"format": 1, "choices": {"d1": "delete"}}, "has a choice 'd1': 'delete'"),
                ({"format": 1, "choices": {"../x": "keep"}}, "has a choice '../x'"),
                ({"format": 1, "choices": ["d1"]}, "its choices are not a JSON object")):
            with self.subTest(obj=obj):
                self.saved_text(obj)
                with self.assertRaises(ValueError) as e:
                    choices.load(self.run)
                self.assertTrue(str(e.exception).startswith("selection.json"), e.exception)
                self.assertIn(text, str(e.exception))
        with open(self.run.file(choices.NAME), "wb") as f:
            f.write(b"\xff\xfe")
        self.assertRaises(ValueError, choices.load, self.run)

    def test_newer_rules_are_ignored(self):
        self.saved_text({"format": 1, "rules": {"someday": 1, "merge": True}, "updated": 3})
        s = choices.load(self.run)
        self.assertEqual(s.rules, dict(choices.DEFAULT_RULES, merge=True))
        self.assertIsNone(s.updated)

    def test_rules_follow_a_moved_drive(self):
        s = choices.Saved({"keep_in": ["/media/old/Photos", "/home/x"],
                           "remove_from": ["/media/old"], "rank": None, "base": "none",
                           "per_group": "one", "exts": ["jpg"]}, {"d1": "keep"})
        r = s.to_rules(["oldest"], ["/media/new"], {"/media/old": "/media/new"})
        self.assertEqual((r.keep_in, r.remove_from), (["/media/new/Photos", "/home/x"],
                                                      ["/media/new"]))
        self.assertEqual((r.rank, r.base, r.per_group, r.exts, r.choices, r.mounts),
                         ([("oldest", None)], "none", "one", {"jpg"}, {"d1": "keep"},
                          ["/media/new"]))
        self.assertEqual(choices.moved("/media/older/x", {"/media/old": "/n"}),
                         "/media/older/x")
        self.assertEqual(choices.moved("/media/old", {"/media/old": "/n"}), "/n")


class ChoicesBase(RemoveBase):
    def manifest(self, run):
        return _manifest.Manifest.read(run.file(_manifest.NAME))

    def choose(self, run, picks, **rules):
        """Save choices for `run`: {relative path: keep/remove}, a folder if
        it is a folder in the scan, else a file."""
        m = self.manifest(run)
        out = {}
        for rel, v in picks.items():
            path = self.p(rel)
            if path in m.paths:
                out[f"d{m.paths.index(path)}"] = v
            else:
                out[f"f{[m.file_path(f) for f in range(len(m.files))].index(path)}"] = v
        choices.save(run, choices.Saved(rules, out))

    def dupes_run(self):
        return [r for r in self.runs() if r.command == "dupes"][0]

    def prepared(self, *argv):
        """`clean remove ARGV` worked out in this process, as the dashboard
        does, against this test's data."""
        with mock.patch.dict(os.environ, self.env, clear=True):
            _settings.reload()
            try:
                args = cli.build_parser().parse_args(["remove", *argv])
                return _remove.prepare(args, _remove._source(args.run))
            finally:
                _settings.reload()


@needs_unshare
class TestRemoveChoices(ChoicesBase):
    def test_removes_what_was_chosen(self):
        self.standard()
        before = self.snapshot()
        self.scan()
        run = self.dupes_run()
        # B stays after all; of the two u's, the older one goes instead
        self.choose(run, {"B": "keep", "C/u.bin": "remove"})
        r = self.clean_sealed("remove", "-n")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("Note: using the choices made in the dashboard (`clean ui`); "
                      "--no-choices ignores them\n", r.out)
        self.assertIn(f"To go ahead: clean remove {run.name}\n", r.out)
        self.assertEqual(self.snapshot(), before)
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(set(before) - set(self.snapshot()), {"C/u.bin"})
        meta = self.run_meta()
        self.assertEqual((meta["choices"], meta["merge"]), (True, False))
        self.assertEqual(self.in_trash(), {"u.bin": self.p("C/u.bin")})

    def test_choosing_nothing_but_the_choices(self):
        self.standard()
        self.scan()
        run = self.dupes_run()
        self.choose(run, {"A/a1": "remove"}, base="none")
        before = self.snapshot()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(set(before) - set(self.snapshot()), {"A/a1"})

    def test_the_last_copy_still_stays(self):
        self.standard()
        self.scan()
        self.choose(self.dupes_run(), {"A": "remove", "B": "remove"}, base="none")
        before = self.snapshot()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        # the one the rules would keep is the one that stays
        self.assertEqual(set(before) - set(self.snapshot()), {"B/a1", "B/a2", "B/sub/a3"})
        self.assertIn(f"it holds the last copy left of {self.p('A')}/", r.out)

    def test_rules_and_choices_dont_mix(self):
        self.standard()
        self.scan()
        run = self.dupes_run()
        self.choose(run, {"B": "keep"})
        before = self.snapshot()
        for args in (["--keep-in", self.p("A")], ["--rank", "newest"], ["--merge"],
                     ["--only", "files"], ["--rank-folders"]):
            r = self.clean_sealed("remove", "-y", *args)
            self.assertEqual(r.rc, 2, (args, r))
            self.assertIn(f"run {run.name} has choices you made in the dashboard (`clean ui`), "
                          f"and {args[0]} would mix with them: add --no-choices", r.err)
        # --no-choices: the rules alone, so B goes again
        r = self.clean_sealed("remove", "-n", "--no-choices", "--keep-in", self.p("A"))
        self.assertEqual(r.rc, 0, r)
        self.assertIn(f"  folder  {self.p('B')}\n", r.out)
        self.assertIn(f"To go ahead: clean remove {run.name} --no-choices --keep-in "
                      f"{self.p('A')}", r.out)
        self.assertEqual(self.snapshot(), before)

    def test_choices_like_the_defaults_are_no_choices(self):
        self.standard()
        self.scan()
        choices.save(self.dupes_run(), choices.Saved())
        r = self.clean_sealed("remove", "-n", "--keep-in", self.p("B"))
        self.assertEqual(r.rc, 0, r)
        self.assertNotIn("using the choices", r.out)

    def test_broken_choices_are_refused(self):
        self.standard()
        self.scan()
        run = self.dupes_run()
        with open(run.file(choices.NAME), "w") as f:
            f.write('{"format": 1, "choices": {"d1": "burn"}}')
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 2, r)
        self.assertIn(f"the choices saved in the dashboard for run {run.name} can't be used: "
                      f"selection.json: has a choice 'd1': 'burn'", r.err)
        self.assertIn("add --no-choices to go by the rules alone", r.err)
        r = self.clean_sealed("remove", "-n", "--no-choices")
        self.assertEqual(r.rc, 0, r)

    def test_merge_choices_and_link(self):
        self.standard()
        self.scan()
        self.choose(self.dupes_run(), {}, merge=True)
        r = self.clean_sealed("remove", "-y", "--link")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("the choices made in the dashboard merge folders, which --link can't do",
                      r.err)
        self.choose(self.dupes_run(), {}, merge=True, conflicts="newer")
        r = self.clean_sealed("remove", "-y", "--permanent")
        self.assertEqual(r.rc, 2, r)
        self.assertIn("can't be used with --permanent", r.err)

    def test_plan_digest(self):
        self.standard()
        self.scan()
        run = self.dupes_run()
        self.choose(run, {"B": "keep"})
        p = self.prepared(run.name)
        self.assertEqual([it.path for it in p.plan.items], [self.p("C/u-copy.bin")])
        before = self.snapshot()
        r = self.clean_sealed("remove", "-y", run.name, "--plan-digest", "0" * 24)
        self.assertEqual(r.rc, 2, r)
        self.assertIn("what would be removed is not what was confirmed in the dashboard any "
                      "more", r.err)
        self.assertEqual(self.snapshot(), before)
        # the choices changed after the confirm: refused too
        self.choose(run, {})
        r = self.clean_sealed("remove", "-y", run.name, "--plan-digest", p.digest)
        self.assertEqual(r.rc, 2, r)
        self.assertEqual(self.snapshot(), before)
        self.choose(run, {"B": "keep"})
        r = self.clean_sealed("remove", "-y", run.name, "--plan-digest", p.digest)
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(set(before) - set(self.snapshot()), {"C/u-copy.bin"})

    def test_dupes_remove_ignores_choices(self):
        # a fresh scan has none, and its --remove goes by the rules it is given
        self.standard()
        r = self.clean_sealed("dupes", self.root, "-q", "--remove", "-n", "--keep-in",
                              self.p("B"))
        self.assertEqual(r.rc, 0, r)
        self.assertNotIn("using the choices", r.out)


class TestHistoryDelete(Base):
    def test_delete(self):
        self.fake_run("2026-10-01_100000-dupes", headline="3 groups")
        self.fake_run("2026-10-02_100000-empty", headline="none")
        r = self.run_clean("history", "--delete", "2")
        self.assertEqual(r.returncode, 2, r)
        self.assertIn(b"there is no terminal to ask on: add -y", r.stderr)
        self.assertEqual(len(self.runs()), 2)
        r = self.run_clean("history", "--delete", "2", "-y")
        self.assertEqual(r.returncode, 0, r)
        self.assertIn(b"Deleted run 2026-10-01_100000-dupes.", r.stdout)
        self.assertEqual([x.name for x in self.runs()], ["2026-10-02_100000-empty"])
        with open(os.path.join(self.data_dir, runs.INDEX), encoding="utf-8") as f:
            self.assertNotIn("2026-10-01_100000-dupes", f.read())
        r = self.run_clean("history", "--delete", "2026-10-01", "-y")
        self.assertEqual(r.returncode, 2, r)
        r = self.run_clean("history", "--delete", "1", "--prune", "-y")
        self.assertEqual(r.returncode, 2, r)
        self.assertIn(b"--delete can't be combined with --prune", r.stderr)

    def test_running_and_undo_records(self):
        self.fake_run("2026-10-01_100000-dupes", status="running", pid=os.getpid(),
                      proc_start=runs.proc_start(os.getpid()))
        r = self.run_clean("history", "--delete", "1", "-y")
        self.assertEqual(r.returncode, 2, r)
        self.assertIn(b"is still running: it can't be deleted until it ends", r.stderr)
        self.fake_run("2026-10-02_100000-remove", mode="trash", files=("actions.tsv",))
        run = self.last_run()
        self.assertTrue(runs.keeps_undo(run), self.run_meta(run))
        r = self.run_clean("history", "--delete", "1", "-y")
        self.assertEqual(r.returncode, 0, r)
        self.assertIn(b"Note: it is the record `clean undo` uses", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
