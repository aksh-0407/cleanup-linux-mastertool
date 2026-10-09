#!/usr/bin/env python3
"""Settings: value parsing, the settings file (precedence, broken files,
atomic writes), the XDG locations, and the `clean config` command."""

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from helpers import IS_ROOT, Base

from cleanup import paths
from cleanup import settings as S


def key(name):
    return S.KEYS[name]


class TestValues(unittest.TestCase):
    def test_parse_text_per_kind(self):
        self.assertEqual(S.parse_text(key("empty.jobs"), ["4"]), 4)
        self.assertEqual(S.parse_text(key("empty.jobs"), [" 2 "]), 2)
        for bad in ("0", "-1", "two", "1.5", ""):
            with self.assertRaises(ValueError, msg=bad):
                S.parse_text(key("empty.jobs"), [bad])
        self.assertEqual(S.parse_text(key("dupes.min_size"), ["1M"]), 1 << 20)
        self.assertEqual(S.parse_text(key("dupes.min_size"), ["0"]), 0)
        with self.assertRaises(ValueError):
            S.parse_text(key("dupes.min_size"), ["lots"])
        for text, want in (("true", True), ("YES", True), ("on", True), ("1", True),
                           ("false", False), ("No", False), ("off", False), ("0", False)):
            self.assertIs(S.parse_text(key("scan.one_file_system"), [text]), want, text)
        with self.assertRaises(ValueError):
            S.parse_text(key("scan.one_file_system"), ["maybe"])
        with self.assertRaises(ValueError):
            S.parse_text(key("empty.jobs"), ["1", "2"])

    def test_parse_text_lists(self):
        k = key("scan.exclude")
        self.assertEqual(S.parse_text(k, ["a", "b", "a"]), ["a", "b"])
        self.assertEqual(S.parse_text(k, ['["x,y", "z"]']), ["x,y", "z"])
        self.assertEqual(S.parse_text(k, ["a", ""]), ["a"])
        with self.assertRaises(ValueError):
            S.parse_text(k, ["[not json"])
        with self.assertRaises(ValueError):
            S.parse_text(k, ['["ok", 3]'])

    def test_parse_text_paths(self):
        k = key("paths.data")
        self.assertEqual(S.parse_text(k, ["/srv/clean"]), "/srv/clean")
        self.assertEqual(S.parse_text(k, ["~/clean-data"]), "~/clean-data")
        self.assertEqual(S.parse_text(k, [""]), "")
        with self.assertRaises(ValueError):
            S.parse_text(k, ["relative/folder"])

    def test_parse_env(self):
        self.assertEqual(S.parse_env(key("scan.exclude"), "a, b ,,c"), ["a", "b", "c"])
        self.assertEqual(S.parse_env(key("scan.exclude"), '["a,b"]'), ["a,b"])
        self.assertEqual(S.parse_env(key("scan.exclude"), ""), [])
        self.assertEqual(S.parse_env(key("dupes.min_size"), "10k"), 10240)
        self.assertIs(S.parse_env(key("scan.default_excludes"), "off"), False)

    def test_check_value(self):
        self.assertEqual(S.check_value(key("empty.jobs"), 3), 3)
        for bad in (0, -2, True, "3", 2.0, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                S.check_value(key("empty.jobs"), bad)
        self.assertEqual(S.check_value(key("dupes.min_size"), "2K"), 2048)
        self.assertEqual(S.check_value(key("dupes.min_size"), 0), 0)
        for bad in (-1, True, 1.5, [], "huge"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                S.check_value(key("dupes.min_size"), bad)
        for bad in (1, "true", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                S.check_value(key("scan.one_file_system"), bad)
        self.assertEqual(S.check_value(key("scan.exclude"), ["a", "a", "b"]), ["a", "b"])
        for bad in ("a", ["a", ""], ["a", 1], {"a": 1}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                S.check_value(key("scan.exclude"), bad)
        for bad in (1, "rel", ["/x"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                S.check_value(key("paths.data"), bad)

    def test_format_value(self):
        self.assertEqual(S.format_value(key("dupes.min_size"), 1), "1")
        self.assertEqual(S.format_value(key("dupes.min_size"), 1 << 20), "1.0 MB (1048576)")
        self.assertEqual(S.format_value(key("scan.exclude"), []), "(none)")
        self.assertEqual(S.format_value(key("scan.exclude"), ["a", "b"]), "a, b")
        self.assertEqual(S.format_value(key("paths.data"), ""), "(default)")
        self.assertEqual(S.format_value(key("scan.one_file_system"), True), "true")

    def test_every_key_has_a_valid_default_and_help(self):
        for name, k in S.KEYS.items():
            self.assertEqual(S.check_value(k, k.default), k.default, name)
            self.assertTrue(k.help.strip(), name)
            self.assertEqual(k.env, "CLEAN_" + name.upper().replace(".", "_"))


class FileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clean-test-")
        self.path = os.path.join(self.tmp, "conf", "settings.json")

    def tearDown(self):
        os.chmod(self.tmp, 0o755)
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for n in dirnames + filenames:
                try:
                    os.chmod(os.path.join(dirpath, n), 0o755)
                except OSError:
                    pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def settings(self, env=None):
        return S.Settings(self.path, env or {})


class TestSettingsFile(FileCase):
    def test_missing_file_means_defaults(self):
        s = self.settings()
        self.assertFalse(s.exists)
        self.assertEqual(s.check_all(), [])
        for name, k in S.KEYS.items():
            self.assertEqual(s.get(name), k.default)
            self.assertEqual(s.source(name), "default")
        self.assertFalse(os.path.exists(self.path))  # reading never creates it

    def test_precedence(self):
        self.write('{"dupes.min_size": 4096, "empty.jobs": 3}')
        s = self.settings({"CLEAN_DUPES_MIN_SIZE": "1M"})
        self.assertEqual(s.get("dupes.min_size"), 1 << 20)
        self.assertEqual(s.source("dupes.min_size"), "environment (CLEAN_DUPES_MIN_SIZE)")
        self.assertEqual(s.get("empty.jobs"), 3)
        self.assertEqual(s.source("empty.jobs"), "settings file")
        self.assertEqual(s.get("dupes.jobs"), 1)
        self.assertEqual(s.source("dupes.jobs"), "default")
        self.assertEqual(s.check_all(), [])

    def test_bad_environment_value_is_ignored_with_one_warning(self):
        self.write('{"empty.jobs": 3}')
        s = self.settings({"CLEAN_EMPTY_JOBS": "lots", "CLEAN_SCAN_EXCLUDE": "a,b"})
        self.assertEqual(s.get("empty.jobs"), 3)          # falls back to the file
        self.assertEqual(s.source("empty.jobs"), "settings file")
        self.assertEqual(s.get("scan.exclude"), ["a", "b"])
        warnings = s.check_all()
        s.check_all()
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("CLEAN_EMPTY_JOBS='lots'", warnings[0])

    def test_empty_file_is_fine(self):
        self.write("  \n")
        s = self.settings()
        self.assertTrue(s.exists)
        self.assertEqual(s.check_all(), [])
        s.set("empty.jobs", 2)
        self.assertEqual(self.read(), {"empty.jobs": 2})

    def test_corrupt_file_gives_defaults_and_is_never_overwritten(self):
        for text in ('{"empty.jobs": 2,', "[1, 2]", '"text"', b"\xff\xfe{}"):
            with self.subTest(text=text):
                self.write(text)
                s = self.settings()
                self.assertEqual(s.get("empty.jobs"), 1)
                warnings = s.check_all()
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("using the defaults", warnings[0])
                with self.assertRaises(S.SettingsError) as cm:
                    s.set("empty.jobs", 4)
                self.assertIn("clean config edit", str(cm.exception))
                with self.assertRaises(S.SettingsError):
                    s.reset(["empty.jobs"])
                with open(self.path, "rb") as f:
                    raw = f.read()
                self.assertEqual(raw, text if isinstance(text, bytes) else text.encode())

    @unittest.skipIf(IS_ROOT, "root can read anything")
    def test_unreadable_file(self):
        self.write('{"empty.jobs": 2}')
        os.chmod(self.path, 0)
        s = self.settings()
        self.assertEqual(s.get("empty.jobs"), 1)
        self.assertIn("cannot read the settings file", s.check_all()[0])
        with self.assertRaises(S.SettingsError):
            s.set("empty.jobs", 3)

    def test_invalid_and_unknown_entries_are_kept_on_save(self):
        self.write(json.dumps({"empty.jobs": 0, "dupes.jobs": "x", "future.key": [1],
                               "dupes.min_size": "1K"}))
        s = self.settings()
        warnings = s.check_all()
        self.assertEqual(len(warnings), 3, warnings)
        self.assertTrue(any("unknown setting 'future.key'" in w for w in warnings))
        self.assertEqual(s.get("empty.jobs"), 1)
        self.assertEqual(s.get("dupes.min_size"), 1024)
        s.set("empty.jobs", 2)   # replaces the invalid value of that key only
        self.assertEqual(self.read(), {"dupes.jobs": "x", "dupes.min_size": 1024,
                                       "empty.jobs": 2, "future.key": [1]})
        s.reset(["dupes.jobs"])  # resetting an invalid entry removes it
        self.assertEqual(self.read(), {"dupes.min_size": 1024, "empty.jobs": 2,
                                       "future.key": [1]})
        self.assertEqual(self.settings().check_all(), [
            f"unknown setting 'future.key' in {self.settings().shown_path}; ignored "
            f"(see `clean config` for the valid names)"])

    def test_set_and_reset_round_trip(self):
        s = self.settings()
        s.set("scan.exclude", ["node_modules", "*.tmp"])
        s.set("scan.one_file_system", True)
        s.set("dupes.min_size", 1 << 20)
        again = self.settings()
        self.assertEqual(again.get("scan.exclude"), ["node_modules", "*.tmp"])
        self.assertIs(again.get("scan.one_file_system"), True)
        self.assertEqual(again.get("dupes.min_size"), 1 << 20)
        self.assertEqual(again.check_all(), [])
        again.reset(["scan.exclude", "dupes.min_size"])
        self.assertEqual(self.read(), {"scan.one_file_system": True})
        again.reset(list(S.KEYS))
        self.assertEqual(self.read(), {})

    def test_file_is_sorted_readable_json(self):
        s = self.settings()
        s.set("scan.exclude", ["ünï"])
        s.set("dupes.jobs", 2)
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, '{\n  "dupes.jobs": 2,\n  "scan.exclude": [\n    "ünï"\n  ]\n}\n')


class TestAtomicWrite(FileCase):
    def test_creates_folder_and_leaves_no_temp_file(self):
        S.atomic_write(self.path, b"one")
        S.atomic_write(self.path, b"two")
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"two")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["settings.json"])

    def test_mode_follows_umask(self):
        for umask, mode in ((0o022, 0o644), (0o077, 0o600), (0o002, 0o664)):
            old = os.umask(umask)
            try:
                S.atomic_write(self.path, b"{}")
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), mode, oct(umask))

    def test_failure_keeps_the_old_file(self):
        S.atomic_write(self.path, b"old")
        for fail in ("os.replace", "os.fsync"):
            with self.subTest(fail=fail), mock.patch(fail, side_effect=OSError(28, "No space")):
                with self.assertRaises(OSError):
                    S.atomic_write(self.path, b"new")
            with open(self.path, "rb") as f:
                self.assertEqual(f.read(), b"old")
            self.assertEqual(os.listdir(os.path.dirname(self.path)), ["settings.json"])

    def test_symlinked_settings_file_is_replaced_not_followed(self):
        # Like editors do: the link itself becomes the new file, the target is untouched.
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as f:
            f.write("{}")
        os.makedirs(os.path.dirname(self.path))
        os.symlink(target, self.path)
        s = self.settings()
        s.set("empty.jobs", 2)
        with open(target) as f:
            self.assertEqual(f.read(), "{}")
        self.assertEqual(self.read(), {"empty.jobs": 2})


class TestPaths(unittest.TestCase):
    def env(self, **kw):
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("XDG_", "CLEAN_"))}
        return mock.patch.dict(os.environ, dict(base, HOME="/home/u", **kw), clear=True)

    def test_defaults(self):
        with self.env():
            self.assertEqual(paths.settings_file(), "/home/u/.config/clean/settings.json")
            self.assertEqual(paths.default_data_dir(), "/home/u/.local/share/clean")

    def test_xdg_variables(self):
        with self.env(XDG_CONFIG_HOME="/cfg", XDG_DATA_HOME="/data"):
            self.assertEqual(paths.settings_file(), "/cfg/clean/settings.json")
            self.assertEqual(paths.default_data_dir(), "/data/clean")
        with self.env(XDG_CONFIG_HOME="rel/cfg", XDG_DATA_HOME=""):  # spec: ignore these
            self.assertEqual(paths.settings_file(), "/home/u/.config/clean/settings.json")
            self.assertEqual(paths.default_data_dir(), "/home/u/.local/share/clean")

    def test_clean_config_dir(self):
        with self.env(CLEAN_CONFIG_DIR="~/my-clean", XDG_CONFIG_HOME="/cfg"):
            self.assertEqual(paths.settings_file(), "/home/u/my-clean/settings.json")

    def test_data_dir_setting(self):
        with self.env(XDG_DATA_HOME="/data"):
            self.assertEqual(paths.data_dir(S.Settings("/nonexistent/s.json", {})),
                             "/data/clean")
            s = S.Settings("/nonexistent/s.json", {"CLEAN_PATHS_DATA": "~/reports"})
            self.assertEqual(paths.data_dir(s), "/home/u/reports")

    def test_tilde(self):
        with self.env():
            self.assertEqual(paths.tilde("/home/u/x/y"), "~/x/y")
            self.assertEqual(paths.tilde("/home/u"), "~")
            self.assertEqual(paths.tilde("/home/user2/x"), "/home/user2/x")
            self.assertEqual(paths.tilde("/srv"), "/srv")


class TestConfigCommand(Base):
    def config(self, *args, env=None):
        r = self.run_clean("config", *args, env=env)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def saved(self):
        with open(self.settings_file, encoding="utf-8") as f:
            return json.load(f)

    def test_list_without_a_settings_file(self):
        code, out, err = self.config()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("(not created yet; all defaults)", out)
        self.assertIn(self.data_dir, out)
        for name in S.KEYS:
            self.assertIn(name, out)
        self.assertFalse(os.path.exists(self.settings_file))
        self.assertEqual(self.config("list")[1], out)

    def test_set_get_and_sources(self):
        code, out, err = self.config("set", "dupes.min_size", "1M")
        self.assertEqual((code, out, err), (0, "dupes.min_size = 1.0 MB (1048576)\n", ""))
        self.assertEqual(self.saved(), {"dupes.min_size": 1048576})
        self.assertEqual(self.config("get", "dupes.min_size")[1], "1048576\n")
        out = self.config()[1]
        self.assertRegex(out, r"\ndupes\.min_size +1\.0 MB \(1048576\)\n")
        self.assertRegex(out, r"\[settings\s+file\] default: 0\n")
        out = self.config(env={"CLEAN_DUPES_MIN_SIZE": "5"})[1]
        self.assertIn("[environment (CLEAN_DUPES_MIN_SIZE)]", out)
        self.assertEqual(self.config("get", "dupes.min_size",
                                     env={"CLEAN_DUPES_MIN_SIZE": "5"})[1], "5\n")
        # Saving while the environment overrides it says so.
        code, out, err = self.config("set", "dupes.min_size", "2K",
                                     env={"CLEAN_DUPES_MIN_SIZE": "5"})
        self.assertEqual(code, 0)
        self.assertIn("CLEAN_DUPES_MIN_SIZE is set in the environment", err)
        self.assertEqual(self.config("get", "scan.one_file_system")[1], "false\n")

    def test_bad_values_and_names_are_refused(self):
        for args, msg in ((["set", "empty.jobs", "0"], "empty.jobs must be a whole number"),
                          (["set", "empty.jobs", "1", "2"], "takes exactly one value"),
                          (["set", "dupes.min_size", "big"], "dupes.min_size"),
                          (["set", "scan.one_file_system", "maybe"], "must be true or false"),
                          (["set", "paths.data", "rel"], "must be an absolute path"),
                          (["set", "dupe.min_size", "1"], "did you mean dupes.min_size?"),
                          (["get", "nope"], "unknown setting 'nope'"),
                          (["add", "empty.jobs", "2"], "is not a list"),
                          (["remove", "dupes.jobs", "2"], "is not a list"),
                          (["reset"], "name the settings to reset, or use --all"),
                          (["reset", "empty.jobs", "--all"], "name the settings")):
            with self.subTest(args=args):
                code, out, err = self.config(*args)
                self.assertEqual(code, 2, (out, err))
                self.assertIn(msg, err)
                self.assertFalse(os.path.exists(self.settings_file))

    def test_list_settings(self):
        self.assertEqual(self.config("add", "scan.exclude", "node_modules", "*.tmp")[0], 0)
        self.assertEqual(self.config("add", "scan.exclude", "*.tmp", "build")[0], 0)
        self.assertEqual(self.saved(), {"scan.exclude": ["node_modules", "*.tmp", "build"]})
        self.assertEqual(self.config("get", "scan.exclude")[1], "node_modules\n*.tmp\nbuild\n")
        code, out, err = self.config("remove", "scan.exclude", "*.tmp", "gone")
        self.assertEqual(code, 0)
        self.assertIn("'gone' is not in scan.exclude", err)
        self.assertEqual(self.saved(), {"scan.exclude": ["node_modules", "build"]})
        self.config("set", "scan.exclude", '["a,b", "c"]')
        self.assertEqual(self.saved(), {"scan.exclude": ["a,b", "c"]})
        self.assertEqual(self.config("get", "scan.exclude",
                                     env={"CLEAN_SCAN_EXCLUDE": "x, y"})[1], "x\ny\n")

    def test_word_and_rank_settings(self):
        self.assertEqual(self.config("set", "remove.method", " Permanent")[0], 0)
        self.assertEqual(self.saved(), {"remove.method": "permanent"})
        code, _, err = self.config("set", "remove.method", "shred")
        self.assertEqual(code, 2)
        self.assertIn("must be one of trash, permanent, not 'shred'", err)
        self.assertEqual(self.config("set", "remove.rank", "Newest,path-contains=Cam",
                                     "shortest")[0], 0)
        self.assertEqual(self.saved()["remove.rank"],
                         ["newest", "path-contains=cam", "shortest"])
        for bad, msg in ((["eldest"], "unknown rank rule 'eldest'"),
                         (["path-contains"], "needs a text after path-contains="),
                         (["oldest=1"], "oldest takes no value")):
            code, _, err = self.config("set", "remove.rank", *bad)
            self.assertEqual(code, 2, bad)
            self.assertIn(msg, err)
        self.assertEqual(self.config("get", "remove.rank",
                                     env={"CLEAN_REMOVE_RANK": "oldest, first-root"})[1],
                         "oldest\nfirst-root\n")
        code, out, err = self.config("get", "remove.method",
                                     env={"CLEAN_REMOVE_METHOD": "nope"})
        self.assertEqual(out, "permanent\n")
        self.assertIn("CLEAN_REMOVE_METHOD", err)
        # a hand-edited file with a bad value warns and uses the default
        with open(self.settings_file, "w") as f:
            json.dump({"remove.rank": ["oldest", 3], "remove.method": "TRASH"}, f)
        code, out, err = self.config("get", "remove.rank")
        self.assertEqual(out, "first-root\noldest\nshortest\n")
        self.assertIn("remove.rank", err)
        self.assertEqual(self.config("get", "remove.method")[1], "trash\n")

    def test_reset(self):
        self.config("set", "empty.jobs", "3")
        self.config("set", "dupes.jobs", "2")
        code, out, _ = self.config("reset", "empty.jobs")
        self.assertEqual((code, out), (0, "empty.jobs = 1  (default)\n"))
        self.assertEqual(self.saved(), {"dupes.jobs": 2})
        self.assertEqual(self.config("reset", "--all")[0], 0)
        self.assertEqual(self.saved(), {})

    def test_path(self):
        code, out, err = self.config("path")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"settings: {self.settings_file}\ndata:     {self.data_dir}\n")
        other = os.path.join(self.tmp, "cfg")
        out = self.config("path", env={"CLEAN_CONFIG_DIR": other,
                                       "CLEAN_PATHS_DATA": "/srv/x"})[1]
        self.assertEqual(out, f"settings: {other}/settings.json\ndata:     /srv/x\n")

    def test_corrupt_file(self):
        os.makedirs(os.path.dirname(self.settings_file))
        with open(self.settings_file, "w") as f:
            f.write('{"empty.jobs": ')
        code, out, err = self.config()
        self.assertEqual(code, 0)
        self.assertIn("is not valid JSON", err)
        self.assertEqual(err.count("warning"), 1, err)
        code, out, err = self.config("set", "empty.jobs", "2")
        self.assertEqual(code, 2)
        self.assertIn("can't be changed because it is not valid JSON", err)
        with open(self.settings_file) as f:
            self.assertEqual(f.read(), '{"empty.jobs": ')
        # Other commands still run, on the defaults.
        r = self.empty(self.root)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"warning: the settings file", r.stderr)

    def editor(self, body):
        script = os.path.join(self.tmp, "editor.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(script, 0o755)
        return {"VISUAL": "", "EDITOR": script}

    def test_edit(self):
        env = self.editor('printf \'{"empty.jobs": 4}\\n\' > "$1"\n')
        code, out, err = self.config("edit", env=env)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Settings OK", out)
        self.assertEqual(self.saved(), {"empty.jobs": 4})

    def test_edit_creates_the_file_and_reports_problems(self):
        seen = os.path.join(self.tmp, "seen")
        env = self.editor(f'cat "$1" > {seen}; echo \'{{"empty.jobs": "many"}}\' > "$1"\n')
        code, out, err = self.config("edit", env=env)
        self.assertEqual(code, 1)
        self.assertIn("setting empty.jobs", err)
        self.assertEqual(err.count("warning"), 1, err)
        with open(seen) as f:
            self.assertEqual(f.read(), "{\n}\n")
        # A broken file before editing is reported once, after the edit.
        env = self.editor('echo "{" > "$1"\n')
        code, out, err = self.config("edit", env=env)
        self.assertEqual(code, 1)
        self.assertEqual(err.count("not valid JSON"), 1, err)

    def test_edit_with_a_failing_or_missing_editor(self):
        env = self.editor("exit 3\n")
        code, out, err = self.config("edit", env=env)
        self.assertIn("the editor exited with status 3", err)
        env = {"VISUAL": "", "EDITOR": os.path.join(self.tmp, "no-such-editor")}
        code, out, err = self.config("edit", env=env)
        self.assertEqual(code, 2)
        self.assertIn("cannot start the editor", err)
        env = {"VISUAL": "'unclosed", "EDITOR": ""}
        code, out, err = self.config("edit", env=env)
        self.assertEqual(code, 2, err)
        self.assertIn("cannot understand $VISUAL", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
