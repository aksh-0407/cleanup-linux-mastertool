#!/usr/bin/env python3
"""`clean empty`: what counts as empty, what is never deleted, the record in
folders.tsv, and odd names, permissions, mounts and huge trees."""

import os
import shutil
import subprocess
import sys
import unittest

from helpers import CLEAN, IS_ROOT, Base, needs_unshare


class TestEmpty(Base):
    def test_cascade_and_root_kept(self):
        self.mk("a", "b", "c")
        self.mk("a", "d")
        self.mk("keep", "deep", files=["f.txt"])
        self.mk("keep", "gone")
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(), {"keep/", "keep/deep/", "keep/deep/f.txt"})
        self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(self.deleted(), ["a", "a/b", "a/b/c", "a/d", "keep/gone"])

    def test_root_left_empty_is_kept_and_noted(self):
        self.mk("a", "b")
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(os.listdir(self.root), [])
        self.assertIn(b"is now empty", r.stdout)

    def test_dry_run_is_default_and_deletes_nothing(self):
        self.mk("a", "b")
        self.mk("c")
        before = self.tree()
        for extra in ([], ["--dry-run"], ["-n"]):
            r = self.empty(self.root, *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.tree(), before)
            self.assertIn(b"WOULD be deleted: 3", r.stdout)
            self.assertEqual(len(self.folder_rows("would-delete")), 3)
            self.assertEqual(self.folder_rows("deleted"), [])
            self.assertEqual(self.run_meta()["mode"], "dry-run")
        self.assertEqual(len(self.runs()), 3)

    def test_dry_run_matches_delete(self):
        for i in range(30):
            self.mk(*[f"d{i % 4}", f"e{i % 7}", f"f{i}"])
        self.mk("d1", "e1", "x", files=["keep"])
        self.empty(self.root)
        would = {r[2] for r in self.folder_rows("would-delete")}
        self.empty(self.root, "--delete")
        did = {r[2] for r in self.folder_rows("deleted")}
        self.assertEqual(would, did)
        self.assertTrue(would)

    def test_any_file_keeps_folder_and_ancestors(self):
        self.mk("a", "b", files=[".DS_Store"])
        self.mk("c", files=["Thumbs.db"])
        before = self.tree()
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.tree(), before)

    def test_special_files_count_as_content(self):
        d = self.mk("fifo")
        os.mkfifo(os.path.join(d, "pipe"))
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0)
        self.assertIn("fifo/pipe", self.tree())

    def test_symlinks_never_followed_and_count_as_content(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(outside, "empty_target"))
        d = self.mk("haslink")
        os.symlink(outside, os.path.join(d, "link_to_dir"))
        d2 = self.mk("hasbroken")
        os.symlink("/nonexistent/xyz", os.path.join(d2, "broken"))
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.islink(os.path.join(self.root, "haslink", "link_to_dir")))
        self.assertTrue(os.path.islink(os.path.join(self.root, "hasbroken", "broken")))
        self.assertTrue(os.path.isdir(os.path.join(outside, "empty_target")))

    def test_symlink_root_refused(self):
        self.mk("a")
        link = os.path.join(self.tmp, "link")
        os.symlink(self.root, link)
        r = self.empty(link, "--delete")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"symlink", r.stderr)
        self.assertIn("a/", self.tree())

    def test_bad_roots(self):
        f = self.write("file.txt", "x")
        for path, msg in [(f, b"not a folder"), (os.path.join(self.tmp, "nope"), b"no such folder"),
                          ("/", b"refusing"), ("/proc", b"refusing"), ("/sys/kernel", b"refusing")]:
            r = self.empty(path)
            self.assertEqual(r.returncode, 2, path)
            self.assertIn(msg, r.stderr, path)
            self.assertNotIn(b"Traceback", r.stderr)

    def test_home_and_system_folders_need_force(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, "Templates"))
        r = self.empty(home, "--delete", env={"HOME": home})
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"home folder", r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(home, "Templates")))
        r = self.empty(home, env={"HOME": home})  # a dry run is fine
        self.assertEqual(r.returncode, 0)
        r = self.empty(home, "--delete", "--force", env={"HOME": home})
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(home, "Templates")))
        r = self.empty(self.tmp, "--delete", env={"HOME": home})  # contains ~
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"contains your home folder", r.stderr)
        self.assertTrue(os.path.isdir(self.root))
        for system in ("/usr/share", "/etc", "/var/tmp"):
            r = self.empty(system, "--delete")
            self.assertEqual(r.returncode, 2, system)
            self.assertIn(b"system folder", r.stderr)

    def test_git_repo_is_protected_by_default(self):
        repo = self.mk("proj")
        os.makedirs(os.path.join(repo, ".git", "refs", "heads"))
        os.makedirs(os.path.join(repo, ".git", "refs", "tags"))
        os.makedirs(os.path.join(repo, ".git", "objects", "pack"))
        os.makedirs(os.path.join(repo, ".git", "branches"))
        self.write("proj/.git/HEAD", "ref: refs/heads/main\n")
        self.mk("proj", "src", "empty")
        self.mk("skeleton", ".git", "refs")  # a .git with no files at all
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0)
        t = self.tree()
        for kept in ("proj/.git/refs/heads/", "proj/.git/refs/tags/",
                     "proj/.git/objects/pack/", "proj/.git/branches/", "skeleton/.git/refs/"):
            self.assertIn(kept, t)
        self.assertNotIn("proj/src/", t)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_real_git_repo_after_gc_still_works(self):
        repo = self.mk("proj")
        git = ["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        self.write("proj/f.txt", "hello")
        subprocess.run(git + ["add", "f.txt"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        subprocess.run(git + ["gc", "-q", "--prune=now"], check=True, capture_output=True)
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0)
        log = subprocess.run(["git", "-C", repo, "log", "--oneline"], capture_output=True)
        self.assertEqual(log.returncode, 0, log.stderr)

    def test_no_default_excludes(self):
        self.mk("proj", ".git", "refs", "tags")
        self.write("proj/.git/HEAD", "x")
        r = self.empty(self.root, "--delete", "--no-default-excludes")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.tree(), {"proj/", "proj/.git/", "proj/.git/HEAD"})

    def test_lost_found_and_stfolder_kept(self):
        self.mk("lost+found")
        self.mk("sync", ".stfolder")
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.tree(), {"lost+found/", "sync/", "sync/.stfolder/"})

    def test_trash_folders_kept(self):
        uid = os.getuid()
        self.mk(f".Trash-{uid}", "files", "old", "empty-inside")
        self.mk(f".Trash-{uid}", "info")
        self.mk(".Trash", str(uid), "files")
        self.mk("share", "Trash", "files")
        self.mk("share", "Trash", "info")
        self.mk("share", "notatrash", "Trash")
        env = {"XDG_DATA_HOME": os.path.join(self.root, "share")}
        for extra in ((), ("--no-default-excludes",)):
            r = self.empty(self.root, "--delete", *extra, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        left = {p for p in self.tree() if not p.startswith("share/clean/")}  # its data
        self.assertEqual(left, {
            f".Trash-{uid}/", f".Trash-{uid}/files/", f".Trash-{uid}/files/old/",
            f".Trash-{uid}/files/old/empty-inside/", f".Trash-{uid}/info/",
            ".Trash/", f".Trash/{uid}/", f".Trash/{uid}/files/",
            "share/", "share/Trash/", "share/Trash/files/", "share/Trash/info/"})

    def test_exclude_name_glob_and_path(self):
        self.mk("a", "node_modules", "x")
        self.mk("b", "cache-1")
        self.mk("c", "keepme")
        self.mk("d", "gone")
        r = self.empty(self.root, "--delete", "-e", "node_modules", "--exclude", "cache-*",
                       "-e", os.path.join(self.root, "c", "keep*"))
        self.assertEqual(r.returncode, 0, r.stderr)
        t = self.tree()
        self.assertIn("a/node_modules/x/", t)
        self.assertIn("b/cache-1/", t)
        self.assertIn("c/keepme/", t)
        self.assertNotIn("d/", t)

    def test_multiple_and_nested_roots(self):
        self.mk("one", "e1")
        self.mk("two", "e2", "deeper")
        self.mk("one", "nested")
        r = self.empty(os.path.join(self.root, "one"), os.path.join(self.root, "two"),
                       os.path.join(self.root, "one", "nested"), "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        # nested was named, so it is a root and kept; e1, e2 (and deeper) go.
        self.assertEqual(self.tree(), {"one/", "two/", "one/nested/"})

    def test_unreadable_folder_is_reported_and_kept(self):
        if IS_ROOT:
            self.skipTest("root can read anything")
        self.mk("locked", "inner")
        self.mk("fine")
        os.chmod(os.path.join(self.root, "locked"), 0o000)
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"cannot read folder", r.stdout)
        os.chmod(os.path.join(self.root, "locked"), 0o755)
        self.assertEqual(self.tree(), {"locked/", "locked/inner/"})

    def test_undeletable_folder_blocks_parents_quietly(self):
        if IS_ROOT:
            self.skipTest("root can delete anything")
        self.mk("a", "b", "c", "d")
        os.chmod(os.path.join(self.root, "a", "b", "c"), 0o555)  # d can't be removed
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 1)
        out = r.stdout.decode()
        self.assertIn("Errors:          1", out)
        self.assertIn("Kept:            3", out)  # c, b, a kept quietly, not 4 errors
        self.assertIn("Permission denied", out)
        os.chmod(os.path.join(self.root, "a", "b", "c"), 0o755)
        self.assertEqual(self.tree(), {"a/", "a/b/", "a/b/c/", "a/b/c/d/"})

    def test_strange_names(self):
        names = ["with space", "tab\there", "new\nline", b"bad\xffbyte", "esc\x1b[31mred",
                 "ünïcødé 漢字", "-dash", "back\\slash", "'quote\"s"]
        for n in names:
            raw = os.fsencode(self.root) + b"/" + (n if isinstance(n, bytes) else n.encode())
            os.makedirs(raw + b"/sub")
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(b"Traceback", r.stderr)
        self.assertEqual(os.listdir(self.root), [])
        run = self.last_run()
        for name in ("folders.tsv", "summary.txt", "report.html"):
            with open(run.file(name), "rb") as f:
                data = f.read()
            data.decode("utf-8")  # valid UTF-8
            self.assertNotIn(b"\x1b", data, name)
        rows = self.folder_rows("deleted")
        self.assertEqual(len(rows), 2 * len(names))
        self.assertTrue(all(len(r) == 4 for r in rows))
        self.assertIn(os.path.join(self.root, "new\\nline"), [r[2] for r in rows])
        self.assertIn(os.path.join(self.root, "bad\\xffbyte"), [r[2] for r in rows])
        r = self.empty(self.root)
        self.assertEqual(r.returncode, 0)

    def test_deep_tree(self):
        path = self.root
        for _ in range(400):
            path = os.path.join(path, "d")
        os.makedirs(path)
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.root), [])

    def test_parallel_matches_serial(self):
        for i in range(60):
            self.mk(f"t{i % 6}", f"m{i % 5}", f"l{i}")
            if i % 9 == 0:
                self.mk(f"t{i % 6}", f"m{i % 5}", files=[f"f{i}"])
        self.empty(self.root, "-j1")
        serial = [r[2] for r in self.folder_rows("would-delete")]
        self.empty(self.root, "-j8")
        parallel = [r[2] for r in self.folder_rows("would-delete")]
        self.assertEqual(serial, parallel)
        self.assertTrue(serial)

    def test_folders_tsv(self):
        self.mk("a", "b")
        self.mk("c")
        self.empty(self.root)
        self.assertEqual(self.folder_rows(), [
            ["would-delete", "1", os.path.join(self.root, "a"), ""],
            ["would-delete", "2", os.path.join(self.root, "a", "b"), ""],
            ["would-delete", "1", os.path.join(self.root, "c"), ""]])
        self.empty(self.root, "--delete")
        # Rows are written as each folder goes: children before parents.
        self.assertEqual(self.folder_rows(), [
            ["deleted", "1", os.path.join(self.root, "c"), ""],
            ["deleted", "2", os.path.join(self.root, "a", "b"), ""],
            ["deleted", "1", os.path.join(self.root, "a"), ""]])

    def test_summary_lists_outermost_folders(self):
        for i in range(5):
            self.mk("old", f"e{i}", "x")
        self.mk("other")
        r = self.empty(self.root)
        out = r.stdout.decode()
        self.assertIn("Empty folders that WOULD be deleted (12, in 2 places):", out)
        self.assertIn(f"  {self.root}/old  (+10 inside)\n", out)
        self.assertIn(f"  {self.root}/other\n", out)
        self.assertIn(f"Run:             {self.last_run().name}", out)
        with open(self.last_run().file("summary.txt")) as f:
            saved = f.read()
        self.assertIn(f"  {self.root}/old  (+10 inside)\n", saved)
        r = self.empty(self.root, "--delete")
        out = r.stdout.decode()
        self.assertNotIn("(+10 inside)", out)   # the terminal keeps --delete short
        with open(self.last_run().file("summary.txt")) as f:
            self.assertIn("Deleted (12, in 2 places):", f.read())

    def test_unwritable_data_folder_fails_before_deleting(self):
        self.mk("a")
        for args in (["--delete"], []):
            r = self.run_clean("empty", self.root, *args,
                               env={"CLEAN_PATHS_DATA": "/proc/nope"})
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn(b"cannot", r.stderr)
            self.assertIn(b"/proc/nope", r.stderr)
            self.assertNotIn(b"Traceback", r.stderr)
            self.assertIn("a/", self.tree())

    def test_bad_jobs(self):
        for bad in ("0", "-3", "x"):
            r = self.empty(self.root, "-j", bad)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b"at least 1", r.stderr)

    def test_quiet(self):
        self.mk("a")
        r = self.empty(self.root, "-q")
        self.assertNotIn(b"  " + os.fsencode(self.root), r.stdout.split(b"=" * 60)[0])
        self.assertIn(b"WOULD be deleted: 1", r.stdout)

    def test_broken_pipe(self):
        for i in range(3000):
            self.mk(f"d{i}")
        p = subprocess.run(f'"{sys.executable}" "{CLEAN}" empty "{self.root}" | head -1',
                           shell=True, capture_output=True, env=self.env)
        self.assertNotIn(b"Traceback", p.stderr)
        self.assertNotIn(b"Error", p.stderr)

    @needs_unshare
    def test_mount_points(self):
        mnt = self.mk("mnt")
        self.mk("plain")
        script = (f'mount -t tmpfs none "{mnt}" && mkdir -p "{mnt}/inner/deep" && '
                  f'"{sys.executable}" "{CLEAN}" empty "{self.root}" --delete; '
                  f'echo "rc=$?"; ls -a "{mnt}"; echo ---; '
                  f'mkdir -p "{mnt}/inner/deep" && '
                  f'"{sys.executable}" "{CLEAN}" empty "{self.root}" --delete -x; '
                  f'echo "rc=$?"; ls -a "{mnt}"')
        r = self.run_shell(script)
        out = r.stdout.decode()
        crossing, one_fs = out.split("---")
        self.assertIn("rc=0", crossing)            # mount point itself not touched
        self.assertNotIn("inner", crossing.split("rc=0")[1])  # but cleaned inside
        self.assertIn("rc=0", one_fs)
        self.assertIn("inner", one_fs.split("rc=0")[1])       # -x: left alone
        self.assertTrue(os.path.isdir(mnt))
        self.assertNotIn("plain/", self.tree())

    @needs_unshare
    def test_read_only_drive_refused(self):
        self.mk("a", "b")
        script = (f'mount --bind "{self.root}" "{self.root}" && '
                  f'mount -o remount,bind,ro "{self.root}" && '
                  f'"{sys.executable}" "{CLEAN}" empty "{self.root}" --delete')
        r = self.run_shell(script)
        if b"read-only" not in r.stderr and r.returncode != 2:
            self.skipTest(f"could not set up a read-only mount: {r.stderr!r}")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"read-only", r.stderr)
        self.assertIn("a/b/", self.tree())


if __name__ == "__main__":
    unittest.main(verbosity=2)
