"""
Content hashing: the blake2b digest of a whole file or its first bytes.

Big files are read with a sequential read-ahead hint and dropped from the page
cache afterwards, so a 300 GB scan doesn't push everything else out of RAM.
"""

import hashlib
import os

from cleanup.errors import Cancelled
from cleanup.fsutil import CANCEL, human

HEAD_BYTES = 4096         # the partial read that rules out most same-size files
CHUNK = 1 << 20           # 1 MiB streaming read for full hashing


class FileChanged(Exception):
    """The file changed (size or modification time) between listing and reading it."""


def digest(path, size, nbytes, progress=None):
    """blake2b of the first `nbytes` of the file (all of it when nbytes ==
    size), as hex. Raises FileChanged if the file isn't `size` bytes any more."""
    return read_hash(path, size, None, nbytes, 64, progress).hex()


def read_hash(path, size, mtime_ns, nbytes, digest_size, progress=None):
    """The blake2b digest (raw bytes, `digest_size` long) of the first
    `nbytes` of the file. Raises FileChanged if the file isn't `size` bytes
    any more, or (when mtime_ns is given) was modified since it was listed:
    the result then wouldn't describe the file the scan saw."""
    h = hashlib.blake2b(digest_size=digest_size)
    buf = memoryview(bytearray(max(1, min(nbytes, CHUNK))))
    left = nbytes
    big = nbytes > HEAD_BYTES
    with open(path, "rb", buffering=0) as f:
        if big:
            _advise(f, "SEQUENTIAL")  # bigger read-ahead on the full pass
        while left > 0:
            if CANCEL.is_set():
                raise Cancelled
            n = f.readinto(buf[:min(left, len(buf))])
            if not n:
                break
            h.update(buf[:n])
            left -= n
            if big and progress is not None:
                progress.bytes += n
                if progress.due():
                    progress.show(f"comparing: {human(progress.bytes)} of "
                                  f"{human(progress.total_bytes)} read")
        if nbytes == size and left == 0 and f.read(1):
            left = -1  # it grew
        if mtime_ns is not None and not left:
            st = os.fstat(f.fileno())
            if st.st_size != size or st.st_mtime_ns != mtime_ns:
                left = -1
        if big:
            _advise(f, "DONTNEED")  # don't flush everything else out of RAM
    if left:
        raise FileChanged
    return h.digest()


def _advise(f, what):
    try:
        os.posix_fadvise(f.fileno(), 0, 0, getattr(os, "POSIX_FADV_" + what))
    except (AttributeError, OSError):
        pass  # not available here; purely a performance hint
