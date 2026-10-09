#!/usr/bin/env python3
"""Drives: reading /proc/self/mountinfo, matching mounts to filesystem UUIDs
and labels, and finding a drive again after it is mounted somewhere else."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import TOOL, needs_unshare

from cleanup import volumes
from cleanup.volumes import Volumes, parse_mountinfo


def line(mount_id, point, source, dev="8:17", root="/", fstype="ext4",
         options="rw,relatime", optional="shared:1"):
    opt = f" {optional}" if optional else ""
    return f"{mount_id} 1 {dev} {root} {point} {options}{opt} - {fstype} {source} rw"


ROOT_LINE = line(1, "/", "/dev/fake-root", dev="259:4", optional="")


class TestParse(unittest.TestCase):
    def test_fields_and_escapes(self):
        text = "\n".join([
            ROOT_LINE,
            line(30, r"/media/u/My\040Drive", r"/dev/sd\134b1", dev="8:17",
                 options="ro,nosuid", optional="shared:5 master:2", fstype="exfat"),
            line(31, "/srv/tab\011name", "server:/share", dev="0:55", fstype="nfs4",
                 optional=""),
        ])
        root, drive, nfs = parse_mountinfo(text)
        self.assertEqual((root.point, root.dev, root.fstype), ("/", (259, 4), "ext4"))
        self.assertEqual(drive.point, "/media/u/My Drive")
        self.assertEqual(drive.source, "/dev/sd\\b1")
        self.assertEqual(drive.dev, (8, 17))
        self.assertEqual(drive.fstype, "exfat")
        self.assertTrue(drive.read_only)
        self.assertFalse(root.read_only)
        self.assertEqual(nfs.point, "/srv/tab\tname")
        self.assertEqual((nfs.source, nfs.fstype), ("server:/share", "nfs4"))

    def test_malformed_lines_are_skipped(self):
        text = "\n".join([
            "",
            "garbage",
            "12 1 8:1 / /no-separator rw shared:1 ext4 /dev/sda1 rw",
            "x 1 8:1 / /bad-id rw - ext4 /dev/sda1 rw",
            "13 1 81 / /bad-dev rw - ext4 /dev/sda1 rw",
            "14 1 8:1 / /short rw -",
            ROOT_LINE,
        ])
        self.assertEqual([m.point for m in parse_mountinfo(text)], ["/"])


class FakeSystem(unittest.TestCase):
    """Fake /dev/disk/by-uuid and by-label folders: links to regular files
    that stand in for the devices (matched by link target, like btrfs)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clean-test-")
        self.by_uuid = os.path.join(self.tmp, "by-uuid")
        self.by_label = os.path.join(self.tmp, "by-label")
        os.mkdir(self.by_uuid)
        os.mkdir(self.by_label)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def device(self, name, uuid, label=None):
        dev = os.path.join(self.tmp, "dev", name)
        os.makedirs(os.path.dirname(dev), exist_ok=True)
        open(dev, "w").close()
        os.symlink(f"../dev/{name}", os.path.join(self.by_uuid, uuid))
        if label:
            os.symlink(f"../dev/{name}", os.path.join(self.by_label, label))
        return dev

    def volumes(self, *lines):
        return Volumes("\n".join((ROOT_LINE,) + lines), self.by_uuid, self.by_label)


class TestDriveIdentity(FakeSystem):
    def test_remounted_at_a_new_path(self):
        sdb1 = self.device("sdb1", "1A2B-3C4D", label=r"Backup\x20Drive")
        before = self.volumes(line(40, "/fake/media/u/1A2B-3C4D", sdb1))
        uuid, inside, mount = before.locate("/fake/media/u/1A2B-3C4D/archive/x.zip")
        self.assertEqual((uuid, inside), ("1A2B-3C4D", "/archive/x.zip"))
        self.assertEqual(mount.label, "Backup Drive")
        self.assertEqual(before.name("1A2B-3C4D"), "Backup Drive")

        after = self.volumes(line(52, "/fake/media/u/1A2B-3C4D1", sdb1))
        self.assertEqual(after.resolve(uuid, inside), "/fake/media/u/1A2B-3C4D1/archive/x.zip")
        self.assertEqual(after.resolve(uuid, "/"), "/fake/media/u/1A2B-3C4D1")
        self.assertEqual([m.point for m in after.by_uuid(uuid)], ["/fake/media/u/1A2B-3C4D1"])

    def test_another_drive_at_the_old_mount_point_is_not_mistaken(self):
        sdb1 = self.device("sdb1", "1A2B-3C4D")
        sdc1 = self.device("sdc1", "BEEF-0001")
        now = self.volumes(line(40, "/fake/media/u/1A2B-3C4D", sdc1, dev="8:33"))
        self.assertIsNone(now.resolve("1A2B-3C4D", "/archive/x.zip"))
        uuid, inside, _ = now.locate("/fake/media/u/1A2B-3C4D/archive/x.zip")
        self.assertEqual((uuid, inside), ("BEEF-0001", "/archive/x.zip"))
        self.assertTrue(os.path.exists(sdb1))
        self.assertEqual(now.name("1A2B-3C4D"), "1A2B-3C4D")  # no label: the UUID

    def test_label_of_a_drive_that_is_not_mounted(self):
        self.device("sdd1", "AAAA-BBBB", label=r"Backup\x202019\x2f\x5c")
        self.assertEqual(self.volumes().name("AAAA-BBBB"), "Backup 2019/\\")
        self.assertEqual(self.volumes().name("NOPE"), "NOPE")

    def test_deepest_and_latest_mount_wins(self):
        sdb1 = self.device("sdb1", "U-B")
        sdc1 = self.device("sdc1", "U-C")
        sdd1 = self.device("sdd1", "U-D")
        v = self.volumes(line(40, "/fake/m", sdb1, dev="8:17"),
                         line(41, "/fake/m/inner", sdc1, dev="8:33"),
                         line(42, "/fake/m/inner", sdd1, dev="8:49"))  # stacked on top
        self.assertEqual(v.find_mount("/fake/m/x").uuid, "U-B")
        self.assertEqual(v.find_mount("/fake/m/innerx").uuid, "U-B")
        self.assertEqual(v.find_mount("/fake/m/inner").uuid, "U-D")
        self.assertEqual(v.find_mount("/fake/m/inner/f").uuid, "U-D")
        self.assertEqual(v.find_mount("/fake").source, "/dev/fake-root")
        # U-C is hidden under U-D; U-B's own "inner" folder is hidden too.
        self.assertIsNone(v.resolve("U-C", "/f"))
        self.assertEqual(v.by_uuid("U-C"), [])
        self.assertIsNone(v.resolve("U-B", "/inner/f"))
        self.assertEqual(v.resolve("U-B", "/innerx/f"), "/fake/m/innerx/f")
        self.assertEqual(v.resolve("U-D", "/f"), "/fake/m/inner/f")

    def test_bind_mount_of_a_subfolder(self):
        sdb1 = self.device("sdb1", "U-B")
        whole = line(40, "/fake/media/D", sdb1)
        bind = line(41, "/fake/home/u/photos", sdb1, root="/Photos/2019")
        v = self.volumes(whole, bind)
        for path in ("/fake/media/D/Photos/2019/a.jpg", "/fake/home/u/photos/a.jpg"):
            self.assertEqual(v.locate(path)[:2], ("U-B", "/Photos/2019/a.jpg"), path)
        # The mount of the whole filesystem is preferred when resolving...
        self.assertEqual(v.resolve("U-B", "/Photos/2019/a.jpg"),
                         "/fake/media/D/Photos/2019/a.jpg")
        # ...but the bind mount is used when it is all there is.
        only_bind = self.volumes(bind)
        self.assertEqual(only_bind.resolve("U-B", "/Photos/2019/a.jpg"),
                         "/fake/home/u/photos/a.jpg")
        self.assertEqual(only_bind.resolve("U-B", "/Photos/2019"), "/fake/home/u/photos")
        self.assertIsNone(only_bind.resolve("U-B", "/Photos/2020/b.jpg"))
        self.assertIsNone(only_bind.resolve("U-B", "/Photos/2019x"))

    def test_filesystems_without_a_uuid(self):
        v = self.volumes(line(40, "/fake/run", "tmpfs", dev="0:30", fstype="tmpfs"))
        uuid, inside, mount = v.locate("/fake/run/x")
        self.assertEqual((uuid, inside, mount.fstype), (None, "/x", "tmpfs"))

    def test_unreadable_link_folders(self):
        v = Volumes(ROOT_LINE, os.path.join(self.tmp, "missing"), "/nonexistent")
        self.assertIsNone(v.locate("/x")[0])

    def test_dangling_links_are_ignored(self):
        os.symlink("../dev/gone", os.path.join(self.by_uuid, "DEAD-0000"))
        self.assertIsNone(self.volumes().locate("/x")[0])


class TestRealSystem(unittest.TestCase):
    """Read-only: this machine's own mounts."""

    def test_tool_folder_round_trips(self):
        v = Volumes()
        self.assertIsNotNone(v.find_mount("/"))
        uuid, inside, mount = v.locate(TOOL)
        self.assertIsNotNone(mount)
        self.assertEqual(os.stat(TOOL).st_dev, os.makedev(*mount.dev))
        if uuid is None:
            self.skipTest(f"{TOOL} is on {mount.fstype} without a UUID")
        self.assertEqual(v.resolve(uuid, inside), os.path.realpath(TOOL))
        self.assertTrue(v.name(uuid))

    def test_block_devices_match_by_device_number(self):
        if not os.path.isdir(volumes.BY_UUID):
            self.skipTest("no /dev/disk/by-uuid")
        v = Volumes()
        matched = [m for m in v.mounts if m.uuid]
        for m in matched:
            real = os.path.realpath(os.path.join(volumes.BY_UUID, m.uuid))
            self.assertTrue(real.startswith("/dev/"), m)

    def test_usage(self):
        total, used, free = volumes.usage(TOOL)
        du = shutil.disk_usage(TOOL)
        self.assertEqual(total, du.total)
        self.assertLessEqual(used, total)
        self.assertLessEqual(free, total - used)


SCENARIO = r"""
import json, os, subprocess, sys
from cleanup.volumes import Volumes

T, src = sys.argv[1], sys.argv[2]
by_uuid = os.path.join(T, "by-uuid")
out = {}

def sh(*cmd):
    subprocess.run(cmd, check=True)

def vols():
    return Volumes(by_uuid=by_uuid)

# A "drive": a tmpfs whose source is a fake device that by-uuid links to.
old, new = os.path.join(T, "media", "Drive 1"), os.path.join(T, "media", "Drive 2")
sh("mount", "-t", "tmpfs", os.path.join(T, "dev", "sdz1"), old)
os.makedirs(os.path.join(old, "archive"))
with open(os.path.join(old, "archive", "x.zip"), "w") as f:
    f.write("drive data")
out["before"] = vols().locate(os.path.join(old, "archive", "x.zip"))[:2]

sh("mount", "--move", old, new)          # unplugged and mounted elsewhere
out["after"] = vols().resolve(*out["before"])
with open(out["after"]) as f:
    out["content"] = f.read()

sh("mount", "-t", "tmpfs", os.path.join(T, "dev", "sdy1"), old)  # another drive
out["old_point"] = vols().locate(os.path.join(old, "archive", "x.zip"))[:2]
out["still"] = vols().resolve(*out["before"])

sh("umount", new)                        # unplugged for good
out["gone"] = vols().resolve(*out["before"])

# A bind mount of a folder on a real drive: same UUID, same inside path.
bound = os.path.join(T, "bound")
sh("mount", "--bind", src, bound)
real = Volumes()
out["bind"] = [real.locate(os.path.join(p, "f"))[:2] for p in (src, bound)]
out["bind_resolved"] = real.resolve(*out["bind"][1])
print(json.dumps(out))
"""


@needs_unshare
class TestNamespaceMounts(unittest.TestCase):
    """Real mounts in a private user+mount namespace: nothing outside it changes."""

    def test_remount_bind_and_replacement(self):
        tmp = tempfile.mkdtemp(prefix="clean-test-")
        try:
            for d in ("by-uuid", "dev", "media/Drive 1", "media/Drive 2", "src", "bound"):
                os.makedirs(os.path.join(tmp, d))
            for name, uuid in (("sdz1", "1234-ABCD"), ("sdy1", "9999-0000")):
                open(os.path.join(tmp, "dev", name), "w").close()
                os.symlink(f"../dev/{name}", os.path.join(tmp, "by-uuid", uuid))
            src = os.path.join(tmp, "src")
            open(os.path.join(src, "f"), "w").close()
            script = os.path.join(tmp, "scenario.py")
            with open(script, "w") as f:
                f.write(SCENARIO)
            env = dict(os.environ, PYTHONPATH=TOOL)
            r = subprocess.run(["unshare", "-rm", sys.executable, "-B", script, tmp, src],
                               capture_output=True, env=env, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["before"], ["1234-ABCD", "/archive/x.zip"])
            self.assertEqual(out["after"], os.path.join(tmp, "media/Drive 2/archive/x.zip"))
            self.assertEqual(out["content"], "drive data")
            self.assertEqual(out["old_point"], ["9999-0000", "/archive/x.zip"])
            self.assertEqual(out["still"], out["after"])
            self.assertIsNone(out["gone"])
            a, b = out["bind"]
            self.assertEqual(a, b)
            self.assertTrue(a[1].endswith("/src/f"), a)
            self.assertEqual(out["bind_resolved"], os.path.join(os.path.realpath(src), "f"))
            # Nothing leaked out of the namespace.
            self.assertEqual(os.listdir(os.path.join(tmp, "media", "Drive 2")), [])
            self.assertEqual(os.listdir(os.path.join(tmp, "bound")), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
