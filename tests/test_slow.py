#!/usr/bin/env python3
"""Big trees: tens of thousands of folders, thousands of duplicate groups,
large files and very deep nesting. Skipped unless CLEAN_SLOW_TESTS=1
(./install.sh test --slow)."""

import hashlib
import os
import random
import subprocess
import unittest

from helpers import Base, slow


@slow
class TestBigTrees(Base):
    def test_wide_empty_tree(self):
        kept = set()
        for a in range(30):
            for b in range(30):
                for c in range(20):
                    os.makedirs(os.path.join(self.root, f"a{a}", f"b{b}", f"c{c}"))
        rng = random.Random(1)
        picks = rng.sample([(a, b, c) for a in range(30) for b in range(30)
                            for c in range(20)], 200)
        for a, b, c in picks:
            self.write(f"a{a}/b{b}/c{c}/keep", "x")
            kept.update({f"a{a}/", f"a{a}/b{b}/", f"a{a}/b{b}/c{c}/", f"a{a}/b{b}/c{c}/keep"})
        r = self.empty(self.root, "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.tree()), 30 + 900 + 18000 + 200)  # nothing deleted
        r = self.empty(self.root, "--delete", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(), kept)
        rows = self.folder_rows("deleted")
        self.assertEqual(len(rows), 30 + 900 + 18000 - (len(kept) - 200))

    def test_very_deep_nesting(self):
        # Deeper than Python's recursion limit; made and removed with mkdir/rm
        # because os.makedirs and os.walk are recursive themselves.
        deep = os.path.join(self.root, *["d"] * 1500)
        side = os.path.join(self.root, *["d"] * 600, "keep")
        subprocess.run(["mkdir", "-p", deep, side], check=True)
        try:
            with open(os.path.join(side, "f"), "w") as f:
                f.write("x")
            self.write("x1", "same")
            self.write("/".join(["d"] * 900) + "/x2", "same")
            r = self.dupes(self.root, "-q")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(self.report_groups()), 1)
            r = self.empty(self.root, "--delete", "-q")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.root, *["d"] * 901)))
            self.assertTrue(os.path.exists(os.path.join(side, "f")))
            self.assertEqual(len(self.folder_rows("deleted")), 1500 - 900)
        finally:
            subprocess.run(["rm", "-rf", self.root])

    def test_many_duplicate_groups(self):
        rng = random.Random(2)
        expected = []
        n = 0
        for g in range(1000):
            size = rng.choice((8, 100, 4095, 4096, 4097, 9000))
            data = (b"%06d" % g + rng.randbytes(size))[:size]  # unique per group
            copies = []
            for _ in range(rng.choice((2, 3, 5))):
                rel = f"g{g % 37}/s{n % 11}/f{n}.bin"
                self.write(rel, data)
                copies.append(rel)
                n += 1
            expected.append(sorted(copies))
            # Same size, same first 4 KB, different last byte: never a duplicate.
            twin = bytearray(data)
            twin[-1] ^= 0xFF
            self.write(f"twins/t{g}.bin", bytes(twin))
        r = self.dupes(self.root, "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.report_groups(), sorted(expected))

    def test_large_files(self):
        size = 256 << 20
        for name, last in (("a.img", b"\0"), ("b.img", b"\0"), ("c.img", b"\1")):
            with open(os.path.join(self.root, name), "wb") as f:
                f.truncate(size - 1)  # sparse: costs no disk space
                f.seek(size - 1)
                f.write(last)
        r = self.dupes(self.root, "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.report_groups(), [["a.img", "b.img"]])
        with open(self.last_run().file("files.tsv"), encoding="utf-8") as f:
            next(f)
            digest = next(f).split("\t")[2]
        h = hashlib.blake2b()
        with open(os.path.join(self.root, "a.img"), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        self.assertEqual(digest, h.hexdigest())


if __name__ == "__main__":
    unittest.main(verbosity=2)
