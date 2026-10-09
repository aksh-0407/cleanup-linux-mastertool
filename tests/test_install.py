#!/usr/bin/env python3
"""install.sh: install, re-install, uninstall and their refusals, all into a
temporary BIN_DIR and HOME; and the Files (Nautilus) menu entries, in a fake
desktop: a temporary scripts folder, a stand-in for `clean`, and stand-ins
for the terminal and notifications. Nothing here touches the real
~/.local/share/nautilus."""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest

from helpers import CLEAN, TOOL, Base, pretend

INSTALL = os.path.join(TOOL, "install.sh")
SCRIPT = os.path.join(TOOL, "nautilus", "clean-nautilus")
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
ENTRIES = {"Find duplicates here": "dupes", "Find empty folders here": "empty",
           "Where the space goes": "space"}


class TestInstall(Base):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.bin_dir = os.path.join(self.tmp, "bin")
        self.bin = os.path.join(self.bin_dir, "clean")
        os.mkdir(self.home)

    def sh(self, *args, path=SYSTEM_PATH, bin_dir=None, env=None):
        env = dict(self.env, HOME=self.home, BIN_DIR=bin_dir or self.bin_dir, PATH=path,
                   **(env or {}))
        r = subprocess.run(["sh", INSTALL, *args], capture_output=True, text=True,
                           env=env, cwd="/")
        return r.returncode, r.stdout, r.stderr

    def test_install_and_reinstall(self):
        code, out, err = self.sh()
        self.assertEqual(code, 0, err)
        self.assertEqual(os.readlink(self.bin), CLEAN)
        self.assertIn(f"Installed: {self.bin} -> {CLEAN}", out)
        self.assertIn(f"{self.bin_dir} is not in your PATH", out)
        r = subprocess.run([self.bin, "--version"], capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Again: same link, no complaint; this time with BIN_DIR in PATH.
        code, out, err = self.sh(path=f"{self.bin_dir}:{SYSTEM_PATH}")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("not in your PATH", out)
        self.assertEqual(os.readlink(self.bin), CLEAN)
        self.assertFalse(os.path.exists(os.path.join(TOOL, "reports")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config")))

    def test_default_bin_dir_is_in_home(self):
        env = dict(self.env, HOME=self.home, PATH=SYSTEM_PATH)
        env.pop("BIN_DIR", None)
        r = subprocess.run(["sh", INSTALL], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.readlink(os.path.join(self.home, ".local/bin/clean")), CLEAN)

    def test_notes_an_earlier_clean_in_path(self):
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        with open(os.path.join(other, "clean"), "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(os.path.join(other, "clean"), 0o755)
        code, out, err = self.sh(path=f"{other}:{self.bin_dir}:{SYSTEM_PATH}")
        self.assertEqual(code, 0, err)
        self.assertIn("comes earlier in PATH", err)

    def test_refuses_to_replace_something_else(self):
        os.mkdir(self.bin_dir)
        with open(self.bin, "w") as f:
            f.write("mine")
        code, out, err = self.sh()
        self.assertEqual(code, 1)
        self.assertIn("not overwriting it", err)
        with open(self.bin) as f:
            self.assertEqual(f.read(), "mine")
        os.unlink(self.bin)
        os.symlink("/bin/true", self.bin)
        code, out, err = self.sh()
        self.assertEqual(code, 1)
        self.assertIn("which is another program", err)
        self.assertEqual(os.readlink(self.bin), "/bin/true")

    def test_replaces_a_dangling_link(self):
        os.mkdir(self.bin_dir)
        os.symlink(os.path.join(self.tmp, "moved-away", "clean"), self.bin)
        code, out, err = self.sh()
        self.assertEqual(code, 0, err)
        self.assertEqual(os.readlink(self.bin), CLEAN)

    def test_uninstall(self):
        code, out, err = self.sh("uninstall")
        self.assertEqual((code, out), (0, f"clean is not installed in {self.bin_dir}.\n"))
        self.sh()
        code, out, err = self.sh("uninstall")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.lexists(self.bin))
        self.assertIn(f"clean removed from {self.bin_dir}.", out)
        self.assertIn(f"code:     {TOOL}", out)
        self.assertIn(f"settings: {self.settings_file}", out)
        self.assertIn(f"data:     {self.data_dir}", out)
        self.assertTrue(os.path.exists(CLEAN))

    def test_uninstall_leaves_other_things_alone(self):
        os.mkdir(self.bin_dir)
        os.symlink("/bin/true", self.bin)
        code, out, err = self.sh("uninstall")
        self.assertEqual(code, 1)
        self.assertIn("is not a link to this tool", err)
        self.assertEqual(os.readlink(self.bin), "/bin/true")
        os.unlink(self.bin)
        with open(self.bin, "w") as f:
            f.write("mine")
        self.assertEqual(self.sh("uninstall")[0], 1)
        self.assertTrue(os.path.isfile(self.bin))

    def test_bad_arguments(self):
        for args in (["bogus"], ["test", "--fast"], ["test", "--slow", "x"], ["--nautilus", "x"],
                     ["install", "--slow"], ["uninstall", "--bogus"], ["test", "--nautilus"],
                     ["install", "--nautilus", "--nautilus"]):
            with self.subTest(args=args):
                code, out, err = self.sh(*args)
                self.assertEqual(code, 2)
                self.assertIn("usage:", err)
        self.assertFalse(os.path.exists(self.bin_dir))
        self.assertFalse(os.path.exists(self.scripts))

    def test_refuses_where_clean_cannot_run(self):
        for code, reason in (("sys.platform = 'darwin'", "Linux only; this system is darwin."),
                             ("sys.version_info = (3, 9, 0)", "this is Python 3.9.")):
            for args in ([], ["--nautilus"], ["test"]):
                with self.subTest(code=code, args=args):
                    rc, out, err = self.sh(*args, env=pretend(self.tmp, code))
                    self.assertEqual((rc, out), (1, ""))
                    self.assertIn(reason, err)
        self.assertFalse(os.path.exists(self.bin_dir))
        self.assertFalse(os.path.exists(self.scripts))
        # Uninstalling still works there.
        self.assertEqual(self.sh("uninstall", env=pretend(self.tmp, "sys.platform = 'darwin'"))[0], 0)

    # ------------------------------------------------------------ Files menu

    @property
    def scripts(self):
        return os.path.join(self.env["XDG_DATA_HOME"], "nautilus", "scripts", "clean")

    def entries(self):
        return sorted(os.listdir(self.scripts)) if os.path.isdir(self.scripts) else []

    def test_nautilus_install_and_uninstall(self):
        self.assertTrue(self.scripts.startswith(self.tmp + os.sep))
        for args in (["--nautilus"], ["install", "--nautilus"]):     # (again: no change)
            code, out, err = self.sh(*args)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(os.readlink(self.bin), CLEAN)
            self.assertIn(f"Files menu: right-click a folder > Scripts > clean ({self.scripts})",
                          out)
            self.assertEqual(self.entries(), sorted(ENTRIES))
            for name in ENTRIES:
                link = os.path.join(self.scripts, name)
                self.assertEqual(os.readlink(link), SCRIPT)
                self.assertTrue(os.access(link, os.X_OK))
        # Only the entries go; the command stays.
        code, out, err = self.sh("uninstall", "--nautilus")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"Removed the Files menu entries from {self.scripts}.\n")
        self.assertFalse(os.path.exists(self.scripts))
        self.assertTrue(os.path.isdir(os.path.dirname(self.scripts)))
        self.assertEqual(os.readlink(self.bin), CLEAN)
        code, out, err = self.sh("uninstall", "--nautilus")
        self.assertEqual((code, out), (0, "The Files menu entries are not installed.\n"))
        # A plain uninstall takes both.
        self.sh("--nautilus")
        code, out, err = self.sh("uninstall")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Removed the Files menu entries from {self.scripts}.", out)
        self.assertIn(f"clean removed from {self.bin_dir}.", out)
        self.assertFalse(os.path.lexists(self.bin))
        self.assertFalse(os.path.exists(self.scripts))
        self.assertTrue(os.path.exists(SCRIPT))
        # Without XDG_DATA_HOME: under HOME.
        env = dict(self.env, HOME=self.home, BIN_DIR=self.bin_dir, PATH=SYSTEM_PATH)
        del env["XDG_DATA_HOME"]
        r = subprocess.run(["sh", INSTALL, "--nautilus"], capture_output=True, text=True,
                           env=env, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        where = os.path.join(self.home, ".local/share/nautilus/scripts/clean")
        self.assertEqual(sorted(os.listdir(where)), sorted(ENTRIES))
        r = subprocess.run(["sh", INSTALL, "uninstall"], capture_output=True, text=True,
                           env=env, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(where))

    def test_nautilus_leaves_other_things_alone(self):
        os.makedirs(self.scripts)
        mine = os.path.join(self.scripts, "Find duplicates here")
        with open(mine, "w") as f:
            f.write("mine")
        os.symlink("/bin/true", os.path.join(self.scripts, "Find empty folders here"))
        gone = os.path.join(self.tmp, "moved-away", "clean-nautilus")
        os.symlink(gone, os.path.join(self.scripts, "Where the space goes"))
        code, out, err = self.sh("--nautilus")
        self.assertEqual(code, 1)
        self.assertIn(f"{mine} already exists and is not this tool's; leaving it alone.", err)
        self.assertIn("Find empty folders here already exists", err)
        self.assertNotIn("Try:", out)
        with open(mine) as f:
            self.assertEqual(f.read(), "mine")
        self.assertEqual(os.readlink(os.path.join(self.scripts, "Find empty folders here")),
                         "/bin/true")
        # a link to a copy that is gone is replaced, like the command's
        self.assertEqual(os.readlink(os.path.join(self.scripts, "Where the space goes")), SCRIPT)
        code, out, err = self.sh("uninstall", "--nautilus")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{mine} is not this tool's; leaving it alone.", err)
        self.assertEqual(self.entries(), ["Find duplicates here", "Find empty folders here"])
        self.assertTrue(os.path.isfile(mine))


class FakeDesktop(Base):
    """A copy of the Files script next to a stand-in `clean` that notes how it
    was run, linked in under the three entry names, with stand-ins for
    notify-send and the terminal; PATH holds only those."""

    FAKE = """#!{py}
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0]), os.getcwd(), *sys.argv[1:]]) + "\\n")
name = os.path.basename(sys.argv[0])
if name == "clean":
    if os.environ.get("FAKE_ERR"):
        sys.exit(print(os.environ["FAKE_ERR"], file=sys.stderr) or 2)
    sys.exit(0 if "--if-running" not in sys.argv or os.environ.get("FAKE_RUNNING") else 1)
"""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        self.menu = os.path.join(self.tmp, "menu")
        self.log = os.path.join(self.tmp, "log")
        for d in (os.path.join(self.repo, "nautilus"), self.fakebin, self.menu):
            os.makedirs(d)
        self.script = os.path.join(self.repo, "nautilus", "clean-nautilus")
        shutil.copy(SCRIPT, self.script)
        fake = self.FAKE.format(py=sys.executable)
        self.write_fake(os.path.join(self.repo, "clean"), fake)
        self.fake_clean = os.path.join(self.repo, "clean")
        for name in ("notify-send", "x-terminal-emulator"):
            self.write_fake(os.path.join(self.fakebin, name), fake)
        os.symlink(sys.executable, os.path.join(self.fakebin, "python3"))
        os.symlink("/bin/sh", os.path.join(self.fakebin, "sh"))
        for name in ENTRIES:
            os.symlink(self.script, os.path.join(self.menu, name))
        self.here = os.path.join(self.tmp, "Photos")
        os.makedirs(os.path.join(self.here, "Summer 2019"))
        os.mkdir(os.path.join(self.here, "Winter"))
        with open(os.path.join(self.here, "notes.txt"), "w") as f:
            f.write("x")

    @staticmethod
    def write_fake(path, text):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def entry(self, name, *args, cwd=None, uri=None, wait_for=0, **env):
        """Run a menu entry as Files does; the calls it made, once there are
        `wait_for` of them (the terminal starts on its own)."""
        e = {"PATH": self.fakebin, "HOME": self.tmp, "FAKE_LOG": self.log, **env}
        if uri is not None:
            e["NAUTILUS_SCRIPT_CURRENT_URI"] = uri
        r = subprocess.run([os.path.join(self.menu, name), *args], capture_output=True,
                           env=e, cwd=cwd or self.here, timeout=60)
        for _ in range(400):
            calls = self.calls()
            if len(calls) >= wait_for:
                break
            time.sleep(0.025)
        return r, calls

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


class TestNautilusScript(FakeDesktop):
    @staticmethod
    def ui(kind, *folders, running=True):
        """The arguments an entry gives `clean`."""
        return ["ui", *(["--if-running"] if running else []), "--scan", kind, "--", *folders]

    def test_selected_folders_in_a_running_dashboard(self):
        summer = os.path.join(self.here, "Summer 2019")
        winter = os.path.join(self.here, "Winter")
        uri = "file://" + self.here.replace(" ", "%20")
        for name, kind in ENTRIES.items():
            with self.subTest(name=name):
                os.unlink(self.log) if os.path.exists(self.log) else None
                # names relative to the open folder; a file counts as its folder
                r, calls = self.entry(name, "Summer 2019", "notes.txt", "Winter", "Summer 2019",
                                      uri=uri, FAKE_RUNNING="1", wait_for=1)
                self.assertEqual((r.returncode, r.stderr), (0, b""))
                self.assertEqual([c[0] for c in calls], ["clean"])
                self.assertEqual(calls[0][2:], self.ui(kind, summer, self.here, winter))

    def test_the_open_folder_when_nothing_is_selected(self):
        odd = os.path.join(self.tmp, "50% off & more")
        os.mkdir(odd)
        uri = "file://" + odd.replace("%", "%25").replace(" ", "%20").replace("&", "%26")
        for env_uri, want in ((uri, odd), ("file://localhost" + self.here, self.here),
                              (None, self.here)):
            with self.subTest(uri=env_uri):
                os.unlink(self.log) if os.path.exists(self.log) else None
                r, calls = self.entry("Where the space goes", uri=env_uri, FAKE_RUNNING="1",
                                      wait_for=1)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(calls[0][2:], self.ui("space", want))
        # Search results and the like: full paths, or file:// URIs
        os.unlink(self.log)
        r, calls = self.entry("Find empty folders here", odd, "file://" + self.here + "/Winter",
                              uri="x-nautilus-search://0/", FAKE_RUNNING="1", wait_for=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls[0][2:], self.ui("empty", odd, os.path.join(self.here, "Winter")))

    def test_names_that_are_not_text(self):
        odd = os.path.join(os.fsencode(self.here), b"caf\xe9")
        os.mkdir(odd)
        r, calls = self.entry("Find duplicates here", uri="file://" + self.here + "/caf%E9",
                              FAKE_RUNNING="1", wait_for=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.fsencode(calls[0][-1]), odd)

    def test_not_on_this_computer(self):
        for args, uri in (([], "smb://server/share/"), ([], "trash:///"), ([], "recent:///"),
                          (["sftp://host/home/me"], "file://" + self.here)):
            with self.subTest(args=args, uri=uri):
                os.unlink(self.log) if os.path.exists(self.log) else None
                r, calls = self.entry("Find duplicates here", *args, uri=uri, wait_for=1)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(calls, [["notify-send", self.here, "--app-name=clean", "clean",
                                          "This works on folders on this computer only."]])

    def test_starts_a_dashboard_in_a_terminal(self):
        r, calls = self.entry("Where the space goes", "Winter", wait_for=2)
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        winter = os.path.join(self.here, "Winter")
        self.assertEqual(calls[0][2:], self.ui("space", winter))
        term = calls[1]
        self.assertEqual(term[0], "x-terminal-emulator")
        self.assertEqual(term[2:6], ["-e", "sh", "-c", term[5]])
        self.assertEqual(term[6:], ["sh", self.fake_clean, *self.ui("space", winter,
                                                                    running=False)])
        # In the terminal: the dashboard itself; if it can't start, the
        # window stays open until Enter, so the reason can be read.
        os.unlink(self.log)
        e = {"PATH": self.fakebin, "FAKE_LOG": self.log}
        r = subprocess.run(term[3:], input=b"", capture_output=True, env=e, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual([c[2:] for c in self.calls()], [self.ui("space", winter, running=False)])
        r = subprocess.run(term[3:], input=b"\n", capture_output=True, timeout=60,
                           env=dict(e, FAKE_ERR="clean: error: port 8765 is in use"))
        self.assertEqual(r.stderr, b"clean: error: port 8765 is in use\n")
        self.assertEqual(r.stdout, b"\nPress Enter to close this window. ")

    def test_other_terminals(self):
        os.unlink(os.path.join(self.fakebin, "x-terminal-emulator"))
        for name, flag in (("konsole", "-e"), ("gnome-terminal", "--"), ("xterm", "-e"),
                           ("xfce4-terminal", "-x")):
            with self.subTest(name=name):
                os.unlink(self.log) if os.path.exists(self.log) else None
                fake = os.path.join(self.fakebin, name)
                self.write_fake(fake, self.FAKE.format(py=sys.executable))
                r, calls = self.entry("Find duplicates here", wait_for=2)
                os.unlink(fake)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(calls[1][0], name)
                self.assertEqual(calls[1][2:4], [flag, "sh"])
        # the first in the list that is there: gnome-terminal before xterm
        for name in ("xterm", "gnome-terminal"):
            self.write_fake(os.path.join(self.fakebin, name), self.FAKE.format(py=sys.executable))
        os.unlink(self.log)
        self.assertEqual(self.entry("Find duplicates here", wait_for=2)[1][1][0], "gnome-terminal")

    def test_no_terminal(self):
        os.unlink(os.path.join(self.fakebin, "x-terminal-emulator"))
        r, calls = self.entry("Find empty folders here", wait_for=2)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(calls[1][4:], ["No terminal program found to run the dashboard in. "
                                        "Start it with `clean ui` in a terminal, then try "
                                        "again."])
        # ... and without notify-send either: zenity, else only stderr
        os.unlink(os.path.join(self.fakebin, "notify-send"))
        os.unlink(self.log)
        r, calls = self.entry("Find empty folders here", wait_for=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"No terminal program found", r.stderr)
        self.write_fake(os.path.join(self.fakebin, "zenity"), self.FAKE.format(py=sys.executable))
        os.unlink(self.log)
        r, calls = self.entry("Find empty folders here", wait_for=2)
        self.assertEqual(calls[1][:5], ["zenity", self.here, "--error", "--title=clean",
                                        "--no-markup"])

    def test_errors_are_shown(self):
        r, calls = self.entry("Find duplicates here", "notes.txt", wait_for=2,
                              FAKE_ERR="clean: error: not a folder: x")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2:], ["--app-name=clean", "clean",
                                        "clean: error: not a folder: x"])
        os.unlink(self.fake_clean)
        os.unlink(self.log)
        r, calls = self.entry("Find duplicates here", wait_for=1)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(calls[0][4].startswith(f"Couldn't run {self.fake_clean}: "), calls)


class TestNautilusInstalled(FakeDesktop):
    """The entries install.sh links in, run with the real `clean`: with no
    dashboard running, they hand it to the terminal."""

    def test_through_install(self):
        home = os.path.join(self.tmp, "home")
        os.mkdir(home)
        env = dict(self.env, HOME=home, BIN_DIR=os.path.join(self.tmp, "bin"), PATH=SYSTEM_PATH)
        r = subprocess.run(["sh", INSTALL, "--nautilus"], capture_output=True, env=env, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        scripts = os.path.join(self.env["XDG_DATA_HOME"], "nautilus", "scripts", "clean")
        e = dict(self.env, PATH=self.fakebin, HOME=home, FAKE_LOG=self.log,
                 NAUTILUS_SCRIPT_CURRENT_URI="file://" + self.here.replace(" ", "%20"))
        r = subprocess.run([os.path.join(scripts, "Find duplicates here"), "Winter"],
                           capture_output=True, env=e, cwd=self.here, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        for _ in range(400):
            if self.calls():
                break
            time.sleep(0.025)
        (term,) = self.calls()
        self.assertEqual(term[0], "x-terminal-emulator")
        self.assertEqual(term[6:], ["sh", CLEAN, "ui", "--scan", "dupes", "--",
                                    os.path.join(self.here, "Winter")])
        self.assertFalse(os.path.exists(os.path.join(self.env["XDG_DATA_HOME"], "clean",
                                                     "ui.json")))
        r = subprocess.run(["sh", INSTALL, "uninstall"], capture_output=True, env=env, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
