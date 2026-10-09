"""The Trash module: putting things in, listing, restoring, emptying; the
home Trash in a temp folder, drive Trashes on ramfs mounts inside a sealed
namespace (where `gio trash` must agree with us)."""

import os
import shutil
import stat
import textwrap
import unittest

from helpers import HAVE_UNSHARE, Base, needs_unshare

from cleanup.actions import trash


def read(path):
    with open(path) as f:
        return f.read()


class HomeTrash(Base):
    def setUp(self):
        super().setUp()
        self._old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.env["XDG_DATA_HOME"]
        self.home = os.path.join(self.env["XDG_DATA_HOME"], "Trash")

    def tearDown(self):
        os.environ["XDG_DATA_HOME"] = self._old
        super().tearDown()

    def put(self, path):
        return trash.for_path(path).put(path)

    def test_file_goes_to_home_trash_with_info(self):
        path = self.write("a b%c.txt", "hello")
        e = self.put(path)
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(e.file, os.path.join(self.home, "files", "a b%c.txt"))
        with open(e.file) as f:
            self.assertEqual(f.read(), "hello")
        with open(e.info) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "[Trash Info]")
        self.assertEqual(lines[1], "Path=" + path.replace(" ", "%20").replace("%c", "%25c"))
        self.assertRegex(lines[2], r"^DeletionDate=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)

    def test_folder_moves_in_one_rename(self):
        folder = self.mk("photos", "2019", files=["a.jpg", "b.jpg"])
        top = os.path.join(self.root, "photos")
        ino = os.stat(top).st_ino
        e = self.put(top)
        self.assertEqual(os.stat(e.file).st_ino, ino)       # same folder, not a copy
        self.assertTrue(os.path.isfile(os.path.join(e.file, "2019", "a.jpg")))
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(e.size(), (2, 2))

    def test_list_and_restore(self):
        path = self.write("deep/er/x.txt", "x")
        e = self.put(path)
        shutil.rmtree(os.path.join(self.root, "deep"))
        [listed] = trash.Trash(self.home, None).entries()
        self.assertEqual((listed.name, listed.original), ("x.txt", path))
        self.assertEqual(listed.restore(), path)            # parents are made again
        with open(path) as f:
            self.assertEqual(f.read(), "x")
        self.assertFalse(os.path.lexists(e.info))
        self.assertEqual(trash.Trash(self.home, None).entries(), [])

    def test_restore_never_overwrites(self):
        path = self.write("x.txt", "old")
        e = self.put(path)
        self.write("x.txt", "new")
        with self.assertRaises(FileExistsError):
            e.restore()
        with open(path) as f:
            self.assertEqual(f.read(), "new")
        self.assertTrue(os.path.isfile(e.file) and os.path.isfile(e.info))

    def test_same_name_twice(self):
        names = []
        for content in "123":
            names.append(self.put(self.write("x.txt", content)).name)
        self.assertEqual(names, ["x.txt", "x.txt.2", "x.txt.3"])
        found = {e.name: read(e.file) for e in trash.Trash(self.home, None).entries()}
        self.assertEqual(found, {"x.txt": "1", "x.txt.2": "2", "x.txt.3": "3"})

    def test_orphan_item_name_is_skipped(self):
        os.makedirs(os.path.join(self.home, "files"))
        os.makedirs(os.path.join(self.home, "info"))
        self.write("../xdg/data/Trash/files/x.txt", "someone else's")
        e = self.put(self.write("x.txt", "mine"))
        self.assertEqual(e.name, "x.txt.2")
        with open(os.path.join(self.home, "files", "x.txt")) as f:
            self.assertEqual(f.read(), "someone else's")
        self.assertFalse(os.path.exists(os.path.join(self.home, "info", "x.txt.trashinfo")))

    def test_long_and_odd_names(self):
        long_name = "é" * 120 + ".jpeg"                       # 245 bytes
        raw = os.fsdecode(b"bad\xff\xfename\n.txt")
        for name in (long_name, raw):
            path = self.write(name, name.encode("utf-8", "surrogateescape"))
            e = self.put(path)
            self.assertLessEqual(len(os.fsencode(e.name + ".trashinfo")), 255)
            self.assertEqual(trash.Trash(self.home, None).entry(e.name).original, path)
            e.restore()
            self.assertTrue(os.path.isfile(path))
        e = self.put(self.write(long_name, "1"))
        e2 = self.put(self.write(long_name, "2"))
        self.assertTrue(e.name.endswith(".jpeg") and e2.name.endswith(".jpeg.2"))
        e.name.encode("utf-8")                                  # not cut mid-character

    def test_failed_rename_leaves_no_info(self):
        path = self.write("x.txt", "x")
        t = trash.for_path(path)
        real = trash.rename_noreplace
        trash.rename_noreplace = lambda a, b: (_ for _ in ()).throw(OSError(5, "I/O error"))
        try:
            with self.assertRaises(OSError):
                t.put(path)
        finally:
            trash.rename_noreplace = real
        self.assertEqual(os.listdir(t.info), [])
        self.assertTrue(os.path.isfile(path))

    def test_delete_and_orphans(self):
        top = os.path.join(self.root, "ro")
        self.mk("ro", "inner", files=["f"])
        os.chmod(os.path.join(top, "inner"), 0o500)
        e = self.put(top)
        e.delete()
        self.assertFalse(os.path.lexists(e.file) or os.path.lexists(e.info))
        e2 = self.put(self.write("y", "y"))
        os.unlink(e2.file)
        [orphan] = trash.Trash(self.home, None).entries()
        self.assertTrue(orphan.missing)
        orphan.delete()
        self.assertEqual(os.listdir(os.path.join(self.home, "info")), [])

    def test_garbage_info_ignored(self):
        os.makedirs(os.path.join(self.home, "info"))
        os.makedirs(os.path.join(self.home, "files"))
        for name, text in (("a", "nonsense"), ("b", "[Trash Info]\nPath=relative\n"),
                           ("c", "[Other]\nPath=/x\n")):
            with open(os.path.join(self.home, "info", name + ".trashinfo"), "w") as f:
                f.write(text)
        self.assertEqual(trash.Trash(self.home, None).entries(), [])

    def test_trash_folder_that_is_a_symlink_is_refused(self):
        os.makedirs(os.path.join(self.tmp, "elsewhere"))
        os.makedirs(self.env["XDG_DATA_HOME"])
        os.symlink(os.path.join(self.tmp, "elsewhere"), self.home)
        path = self.write("x", "x")
        with self.assertRaises(trash.TrashError):
            trash.for_path(path)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "elsewhere")), [])

    def test_parse_info(self):
        self.assertEqual(trash.parse_info(b"[Trash Info]\nPath=/a%20b/%C3%A9\nDeletionDate=X\n"),
                         ("/a b/\u00e9", "X"))
        self.assertEqual(trash.parse_info(b"[Trash Info]\nPath=/a%FF\n")[0],
                         os.fsdecode(b"/a\xff"))


@needs_unshare
class DriveTrash(Base):
    """A second drive: ramfs mounted at $T/disk (gio treats tmpfs as a system
    mount and won't trash there; ramfs it accepts)."""

    def py(self, code, prelude="", mount="mount -t ramfs r \"$T/disk\"\n"):
        script = os.path.join(self.tmp, "t.py")
        with open(script, "w") as f:
            f.write(textwrap.dedent("""\
                import os, sys, json
                from cleanup.actions import trash
                T = os.environ["T"]
                D = T + "/disk"
                def w(p, data="x"):
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w") as f:
                        f.write(data)
                    return p
            """) + textwrap.dedent(code))
        os.makedirs(os.path.join(self.tmp, "disk"))
        r = self.run_sealed(mount + textwrap.dedent(prelude) + '"$PY" -B "$T/t.py"\n')
        self.assertEqual(r.returncode, 0, r.stderr.decode() + r.stdout.decode())
        return r.stdout.decode()

    def test_topdir_trash(self):
        out = self.py("""
            p = w(D + "/sub/a b.txt", "hi")
            t = trash.for_path(p)
            assert t.path == D + "/.Trash-0" and t.top == D, t
            e = t.put(p)
            assert open(e.info).read().splitlines()[1] == "Path=sub/a%20b.txt"
            assert oct(os.stat(t.path).st_mode & 0o777) == "0o700"
            [x] = trash.Trash(t.path, D).entries()
            assert x.original == p, x.original
            assert [tt.path for tt in trash.all_trashes()][-1] == t.path
            x.restore()
            assert open(p).read() == "hi"
            print("ok")
        """)
        self.assertEqual(out, "ok\n")

    def test_shared_trash_folder(self):
        self.py("""
            os.mkdir(D + "/.Trash"); os.chmod(D + "/.Trash", 0o1777)
            t = trash.for_path(w(D + "/a"))
            assert t.path == D + "/.Trash/0", t
            t.put(D + "/a")
            assert os.path.isfile(D + "/.Trash/0/files/a")
            # not sticky, or a symlink: never used
            os.chmod(D + "/.Trash", 0o777)
            assert trash.for_path(w(D + "/b")).path == D + "/.Trash-0"
            os.rename(D + "/.Trash", D + "/real"); os.symlink("real", D + "/.Trash")
            os.chmod(D + "/real", 0o1777)
            assert trash.for_path(w(D + "/c")).path == D + "/.Trash-0"
        """)

    def test_symlinked_trash_refused(self):
        self.py("""
            os.mkdir(T + "/outside")
            os.symlink(T + "/outside", D + "/.Trash-0")
            p = w(D + "/a")
            try:
                trash.for_path(p)
            except trash.TrashError as e:
                assert "no usable Trash" in str(e), e
            else:
                raise AssertionError("used a symlinked Trash")
            assert os.path.isfile(p) and os.listdir(T + "/outside") == []
        """)

    def test_read_only_drive_has_no_trash(self):
        self.py("""
            p = D + "/a"
            try:
                trash.for_path(p)
            except trash.TrashError as e:
                assert "cannot create" in str(e), e
            else:
                raise AssertionError("no error")
            assert open(p).read() == "x\\n"
        """, prelude="""
            echo x > "$T/disk/a"
            mount -o remount,ro,bind "$T/disk"
        """)

    def test_never_moves_across_drives(self):
        self.py("""
            t = trash.for_path(D + "/x")
            try:
                trash.Trash(T + "/xdg/data/Trash", None).put(D + "/x")
            except trash.TrashError as e:
                assert "another drive" in str(e)
            else:
                raise AssertionError("moved across drives")
            assert os.path.isfile(D + "/x") and t.path == D + "/.Trash-0"
        """, prelude="""
            echo x > "$T/disk/x"
            mkdir -p "$T/xdg/data/Trash/files" "$T/xdg/data/Trash/info"
        """)

    @unittest.skipUnless(HAVE_UNSHARE and shutil.which("gio") and shutil.which("dbus-run-session"),
                         "needs gio and dbus-run-session")
    def test_gio_agrees(self):
        """What we trash, gio lists and restores; what gio trashes, we list
        and restore. gio runs on a private session bus in the sealed
        namespace, so it never sees the real Trash."""
        out = self.py("""
            import subprocess
            ours = [w(D + "/dir/ours.txt", "ours"), w(T + "/root/home.txt", "home")]
            os.makedirs(D + "/folder/inner"); w(D + "/folder/inner/f", "f")
            for p in ours + [D + "/folder"]:
                trash.for_path(p).put(p)
            gio = [w(D + "/gio one.txt", "g1"), w(T + "/root/gio2", "g2")]
            env = dict(os.environ, GIO_USE_VFS="local")
            subprocess.run(["gio", "trash"] + gio, check=True, env=env)
            listed = subprocess.run(
                ["dbus-run-session", "--", "gio", "trash", "--list"],
                capture_output=True, text=True, check=True).stdout
            seen = dict(line.split("\\t")[::-1] for line in listed.splitlines())
            for p in ours + [D + "/folder"] + gio:
                assert p in seen, (p, listed)
            # gio restores ours ...
            subprocess.run(["dbus-run-session", "--", "sh", "-c",
                            "gio trash --restore '%s' && gio trash --restore '%s'"
                            % (seen[ours[0]], seen[D + "/folder"])],
                           capture_output=True, check=True)
            assert open(ours[0]).read() == "ours"
            assert open(D + "/folder/inner/f").read() == "f"
            # ... and we restore gio's
            mine = {e.original: e for t in trash.all_trashes() for e in t.entries()}
            for p in gio:
                mine[p].restore()
            assert open(gio[0]).read() == "g1" and open(gio[1]).read() == "g2"
            print(sorted(os.path.relpath(p, T) for p in mine))
        """)
        self.assertEqual(out, "['disk/gio one.txt', 'root/gio2', 'root/home.txt']\n")


if __name__ == "__main__":
    unittest.main()
