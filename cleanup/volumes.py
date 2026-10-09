"""
Drives: which filesystem a path lives on, and where that filesystem is mounted
now.

A drive's mount point can change between plug-ins: /media/me/1A2B-3C4D one
day, /media/me/1A2B-3C4D1 the next (when a stale folder blocks the old
name). So anything clean remembers about a path on a drive is stored as
(filesystem UUID, path inside that filesystem) and turned back into a real
path with resolve() when needed. A different drive that happens to sit at the
old mount point has another UUID, so it is never mistaken for the old one.

Everything comes from /proc/self/mountinfo and /dev/disk/by-uuid (and
by-label); no external commands and no root needed.
"""

import os
import re
import stat

MOUNTINFO = "/proc/self/mountinfo"
BY_UUID = "/dev/disk/by-uuid"
BY_LABEL = "/dev/disk/by-label"


class Mount:
    """One line of /proc/self/mountinfo."""

    def __init__(self, mount_id, parent_id, dev, root, point, options, fstype, source):
        self.mount_id = mount_id
        self.parent_id = parent_id
        self.dev = dev              # (major, minor): st_dev of files on it
        self.root = root            # folder of the filesystem mounted here ("/" usually)
        self.point = point          # where it is mounted
        self.options = options      # per-mount options, e.g. {"ro", "nosuid"}
        self.fstype = fstype
        self.source = source        # e.g. /dev/sda1, or "tmpfs"
        self.uuid = None
        self.label = None

    @property
    def read_only(self):
        return "ro" in self.options

    def __repr__(self):
        return (f"Mount({self.point!r}, {self.fstype}, {self.source!r}, "
                f"uuid={self.uuid!r}, root={self.root!r})")


_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(field):
    """mountinfo writes space, tab, newline and backslash as \\040 \\011 \\012 \\134."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text):
    """[Mount] from the text of /proc/self/mountinfo, in file order (a later
    mount on the same point hides the earlier one). Malformed lines are skipped."""
    mounts = []
    for line in text.splitlines():
        fields = line.split(" ")
        try:
            sep = fields.index("-", 6)
            major, minor = fields[2].split(":")
            mounts.append(Mount(
                mount_id=int(fields[0]), parent_id=int(fields[1]),
                dev=(int(major), int(minor)),
                root=_unescape(fields[3]), point=_unescape(fields[4]),
                options=set(fields[5].split(",")),
                fstype=fields[sep + 1], source=_unescape(fields[sep + 2])))
        except (ValueError, IndexError):
            continue
    return mounts


_HEX = re.compile(rb"\\x([0-9a-fA-F]{2})")


def mount_points():
    """Every place something is mounted now (a set of paths); a bind mount
    of a folder on the same drive counts too, which os.path.ismount misses.
    Empty when the mount table can't be read."""
    try:
        with open(MOUNTINFO, encoding="utf-8", errors="surrogateescape") as f:
            return {m.point for m in parse_mountinfo(f.read())}
    except OSError:
        return set()


def _udev_name(name):
    """udev writes unusual bytes in /dev/disk/by-label names as \\xHH
    ("My\\x20Drive" is the label "My Drive")."""
    return os.fsdecode(_HEX.sub(lambda m: bytes([int(m.group(1), 16)]), os.fsencode(name)))


def _links(folder):
    """{name: (rdev or None, realpath)} for the device links in `folder`."""
    out = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        link = os.path.join(folder, name)
        try:
            st = os.stat(link)
        except OSError:
            continue
        rdev = (os.major(st.st_rdev), os.minor(st.st_rdev)) if stat.S_ISBLK(st.st_mode) else None
        out[name] = (rdev, os.path.realpath(link))
    return out


def _match(mount, links):
    """The name of the link in `links` that is the device of `mount`, if any.

    Matched by device number first. Some filesystems (btrfs) report an
    anonymous device number instead, so the link target is also compared with
    the mount's source."""
    source = os.path.realpath(mount.source) if mount.source.startswith("/") else None
    for name, (rdev, real) in sorted(links.items()):
        if rdev is not None and rdev == mount.dev:
            return name
    for name, (rdev, real) in sorted(links.items()):
        if source is not None and real == source:
            return name
    return None


def _is_within(path, parent):
    return parent == "/" or path == parent or path.startswith(parent.rstrip("/") + "/")


class Volumes:
    """A snapshot of the mounted filesystems."""

    def __init__(self, mountinfo=None, by_uuid=BY_UUID, by_label=BY_LABEL):
        if mountinfo is None:
            with open(MOUNTINFO, encoding="utf-8", errors="surrogateescape") as f:
                mountinfo = f.read()
        self.mounts = parse_mountinfo(mountinfo)
        uuids, labels = _links(by_uuid), _links(by_label)
        self._labels = {}   # uuid -> label, for drives that aren't mounted too
        for uuid, (_, real) in uuids.items():
            for label, (_, lreal) in labels.items():
                if lreal == real:
                    self._labels[uuid] = _udev_name(label)
        for m in self.mounts:
            m.uuid = _match(m, uuids)
            label = _match(m, labels)
            m.label = _udev_name(label) if label else None

    def find_mount(self, path):
        """The mount that `path` (absolute, symlinks resolved) is on: the
        deepest mount point containing it, the latest one if several are
        stacked on the same point."""
        best = None
        for m in self.mounts:
            if _is_within(path, m.point) and (best is None or len(m.point) >= len(best.point)):
                best = m
        return best

    def locate(self, path):
        """(uuid or None, path inside the filesystem, Mount) for `path`.

        A bind mount and the original give the same answer, since both show
        the same files. uuid is None for filesystems without one (tmpfs,
        network shares)."""
        path = os.path.realpath(path)
        m = self.find_mount(path)
        if m is None:
            return None, path, None
        rel = path[len(m.point.rstrip("/")):]
        inside = (m.root.rstrip("/") + rel) or "/"
        return m.uuid, inside, m

    def resolve(self, uuid, inside):
        """Where the path `inside` the filesystem `uuid` is now, or None if
        that drive isn't mounted (or that part of it isn't visible)."""
        found = []
        for m in self.mounts:
            if m.uuid != uuid or not _is_within(inside, m.root):
                continue
            rel = inside[len(m.root.rstrip("/")):]
            path = (m.point.rstrip("/") + ("" if rel == "/" else rel)) or "/"
            if self.find_mount(path) is m:  # not hidden under a later mount
                found.append((len(m.root), len(m.point), path))
        if not found:
            return None
        # Prefer the mount of the whole filesystem, then the shortest path.
        found.sort(key=lambda f: (f[0], f[1]))
        return found[0][2]

    def by_uuid(self, uuid):
        """The visible mounts of filesystem `uuid`."""
        return [m for m in self.mounts if m.uuid == uuid and self.find_mount(m.point) is m]

    def name(self, uuid):
        """A drive's name for messages: its label if it has one, else the UUID."""
        for m in self.mounts:
            if m.uuid == uuid and m.label:
                return m.label
        return self._labels.get(uuid, uuid)


def usage(path):
    """(total, used, free) bytes of the filesystem holding `path`; free is what
    a normal user can still write."""
    st = os.statvfs(path)
    return (st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize,
            st.f_bavail * st.f_frsize)


PSEUDO_FS = frozenset((
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "securityfs", "debugfs",
    "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "pstore", "bpf", "autofs",
    "binfmt_misc", "efivarfs", "squashfs", "overlay", "nsfs", "ramfs", "fuse.gvfsd-fuse",
    "fuse.portal", "rpc_pipefs", "nfsd", "tmpfs"))
REMOVABLE = ("/media/", "/run/media/", "/mnt/")


def drives():
    """[(name, mount point, fstype, total, used, free, read-only)] of the
    disks worth showing: the one holding home, / and removable drives."""
    try:
        vols = Volumes()
    except (OSError, ValueError):
        return []
    home = os.path.realpath(os.path.expanduser("~"))
    home_m = vols.find_mount(home)
    out, seen = [], set()
    for m in sorted(vols.mounts, key=lambda m: (m is not home_m, m.point != "/", m.point)):
        mine = m is home_m or m.point == "/"
        if not mine and not m.point.startswith(REMOVABLE):
            continue
        if m.fstype in PSEUDO_FS and not (m.fstype == "tmpfs" and m.point.startswith(REMOVABLE)):
            continue
        if m.dev in seen:
            continue
        try:
            total, used, free = usage(m.point)
        except OSError:
            continue
        if total <= 0:
            continue
        seen.add(m.dev)
        if mine:
            name = "This computer"
        else:
            name = (vols.name(m.uuid) if m.uuid else None) or m.label or os.path.basename(m.point)
        out.append((name, m.point, m.fstype, total, used, free, m.read_only))
    return out
