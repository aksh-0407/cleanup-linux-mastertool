#!/usr/bin/env python3
"""Look-alikes (`clean dupes --media`): the same photo or song in files whose
bytes differ.

Covered: every format the tag readers know (JPEG, TIFF-based RAW, PNG, WebP,
HEIC/AVIF; MP3 with ID3v1 and v2.2-2.4 in every text encoding, FLAC, M4A,
Ogg Vorbis and Opus), read from small files built byte by byte
(media_samples.py); the readers fed truncated, corrupt, random and lying
files (never an exception, never slow, never a result the rest can't use);
what counts as the same photo or song; the tags kept in the hash cache; and
a scan with --media through the text summary, data.json.gz, the manifest,
report.html, the dashboard (its tab, never selected in bulk, the remove
dialog) and a removal (only what you tick, never the last version, the best
one stays; Trash, undo, and --link refused) in a sealed namespace."""

import os
import random
import shutil
import sqlite3
import time
import tempfile
import unittest
from unittest import mock

import media_samples as M
from helpers import SLOW, Base, needs_unshare
from test_web import WebBase

from cleanup import choices as _choices
from cleanup.dupes import manifest as _manifest
from cleanup.dupes import media
from cleanup.dupes.lookalikes import _label, _letter
from cleanup.report import data as _data

EX = M.tiff(sub="123", pixel=(6000, 4000))
WHEN = "2019-06-12 10:15:30"


def P(data, ext):
    return media.parse(media._Src(data=data), ext)


def photo(w, h, fmt, sub=None):
    return {"kind": "photo", "taken": WHEN, "sub": sub, "make": "Canon",
            "model": "EOS 80D", "w": w, "h": h, "format": fmt}


# ---------------------------------------------------------------- reading


class TestRead(unittest.TestCase):
    def test_photos(self):
        cases = [
            ("jpg", M.jpeg(exif=EX), photo(6000, 4000, "JPEG", "123")),
            # big-endian EXIF after an XMP segment; the size from the frame
            ("jpg", M.jpeg(exif=M.tiff(order=">"), xmp_first=True),
             photo(6000, 4000, "JPEG")),
            # a RAW: the EXIF pixel size wins over IFD0's and the previews'
            ("cr2", M.tiff(dims=(6024, 4024), pixel=(6000, 4000),
                           sub_ifd_dims=[(160, 120), (6024, 4024)]), photo(6000, 4000, "TIFF")),
            ("dng", M.tiff(dims=(6024, 4024), sub_ifd_dims=[(160, 120)]),
             photo(6024, 4024, "TIFF")),
            ("orf", M.tiff(magic=0x4F52, pixel=(4608, 3456)), photo(4608, 3456, "ORF")),
            ("nef", M.tiff(order=">", pixel=(6016, 4016)), photo(6016, 4016, "TIFF")),
            ("png", M.png(exif=EX), photo(1920, 1080, "PNG", "123")),
            ("png", M.png(exif=EX, exif_prefix=True), photo(1920, 1080, "PNG", "123")),
            ("webp", M.webp(exif=EX), photo(1600, 1200, "WebP", "123")),
            ("webp", M.webp(exif=EX, kind="VP8L"), photo(1600, 1200, "WebP", "123")),
            ("webp", M.webp(exif=EX, kind="VP8 "), photo(1600, 1200, "WebP", "123")),
            ("heic", M.heic(exif=EX), photo(4032, 3024, "HEIC", "123")),
            ("heic", M.heic(exif=EX, iloc_version=2), photo(4032, 3024, "HEIC", "123")),
            ("avif", M.heic(exif=EX, brand=b"avif", iloc_version=0, infe_version=3),
             photo(4032, 3024, "AVIF", "123")),
        ]
        for ext, data, want in cases:
            with self.subTest(ext=ext, format=want["format"], w=want["w"]):
                self.assertEqual(P(data, ext), want)

    def test_photos_without_what_matching_needs(self):
        cases = {
            "no EXIF (jpg)": ("jpg", M.jpeg()),
            "no EXIF (png)": ("png", M.png()),
            "no EXIF (webp)": ("webp", M.webp()),
            "no EXIF (heic)": ("heic", M.heic()),
            "no date": ("jpg", M.jpeg(exif=M.tiff(taken=None))),
            "a date that isn't one": ("jpg", M.jpeg(exif=M.tiff(taken="0000:00:00 00:00:00"))),
            "a date of spaces": ("jpg", M.jpeg(exif=M.tiff(taken="    :  :     :  :  "))),
            "no camera": ("jpg", M.jpeg(exif=M.tiff(make=None, model=None))),
            "no size": ("jpg", M.jpeg(w=0, h=0, exif=M.tiff())),
            "a song's extension": ("mp3", M.jpeg(exif=EX)),
            "empty": ("jpg", b""),
            "TIFF with a bad magic": ("tif", M.tiff(magic=7, pixel=(10, 10))),
        }
        for why, (ext, data) in cases.items():
            with self.subTest(why):
                self.assertIsNone(P(data, ext))
        # only one of make and model is enough
        t = P(M.jpeg(exif=M.tiff(make=None, model="DSC-RX100")), "jpg")
        self.assertEqual((t["make"] or None, t["model"]), (None, "DSC-RX100"))

    def test_songs(self):
        mp3 = {"kind": "song", "artist": "Artist", "title": "Title", "format": "MP3",
               "kbps": 128, "lossless": False}
        self.assertEqual(P(M.mp3(secs=10), "mp3"), dict(mp3, secs=10.01))
        cases = [
            ("mp3", M.mp3(secs=215.3, kbps=320, vbr=True), "MP3", 215.3, 320, False),
            ("mp3", M.mp3(secs=12, tag_version=2), "MP3", 12, 128, False),
            ("mp3", M.mp3(secs=12, tag_version=4, picture=50000, encoding=1), "MP3", 12, 128,
             False),
            ("mp3", M.mp3(secs=12, encoding=0, unsync=True, picture=300), "MP3", 12, 128, False),
            ("mp3", M.mp3(secs=12, tag_version=4, footer=True, encoding=2), "MP3", 12, 128, False),
            ("mp3", M.mp3(secs=12, tag=False, v1=True), "MP3", 12, 128, False),
            ("mp3", M.mp3(secs=12, v1=True, mono=True, kbps=64), "MP3", 12, 64, False),
            ("flac", M.flac(secs=200.5), "FLAC", 200.5, None, True),
            ("flac", M.flac(secs=10, picture=5000, id3_first=True), "FLAC", 10, None, True),
            ("flac", M.flac(secs=10, rate=96000), "FLAC", 10, None, True),
            ("m4a", M.m4a(secs=180), "AAC", 180, None, False),
            ("m4a", M.m4a(secs=10, codec=b"alac"), "ALAC", 10, None, True),
            ("m4a", M.m4a(secs=180, version=1, scale=1000), "AAC", 180, None, False),
            ("m4a", M.m4a(secs=180, quicktime_meta=True), "AAC", 180, None, False),
            ("ogg", M.ogg(secs=10), None, 10, None, False),
            ("opus", M.ogg(secs=10, codec="opus"), "Opus", 10, None, False),
            ("ogg", M.ogg(secs=10, picture=70000), None, 10, None, False),
        ]
        for ext, data, fmt, secs, kbps, lossless in cases:
            with self.subTest(ext=ext, format=fmt, secs=secs):
                t = P(data, ext)
                self.assertIsNotNone(t)
                self.assertEqual((t["kind"], t["artist"], t["title"], t["lossless"]),
                                 ("song", "Artist", "Title", lossless))
                self.assertAlmostEqual(t["secs"], secs, delta=0.05)
                if fmt:
                    self.assertEqual(t["format"], fmt)
                if kbps:
                    self.assertEqual(t["kbps"], kbps)

    def test_tag_text_in_every_encoding(self):
        for version, encodings in ((2, (0, 1)), (3, (0, 1, 3)), (4, (0, 1, 2, 3))):
            for enc in encodings:
                with self.subTest(version=version, encoding=enc):
                    t = P(M.mp3("Björk", "Jóga", secs=5, tag_version=version, encoding=enc),
                          "mp3")
                    self.assertEqual((t["artist"], t["title"]), ("Björk", "Jóga"))
        # whitespace tidied, control characters dropped, kept short
        t = P(M.mp3("  The\tBand \x01", "x" * 1000, secs=5), "mp3")
        self.assertEqual(t["artist"], "The Band")
        self.assertEqual(len(t["title"]), media.MAX_TEXT)

    def test_songs_without_what_matching_needs(self):
        cases = {
            "no tag": ("mp3", M.mp3(secs=5, tag=False)),
            "no artist": ("mp3", M.mp3(None, "Title", secs=5)),
            "no title": ("mp3", M.mp3("Artist", None, secs=5)),
            "a blank artist": ("mp3", M.mp3("  ", "Title", secs=5)),
            "punctuation only": ("mp3", M.mp3("Artist", "?!", secs=5)),
            "no frames": ("mp3", M.id3()),
            "no length (flac)": ("flac", M.flac(secs=0)),
            "a photo's extension": ("jpg", M.mp3(secs=5)),
        }
        for why, (ext, data) in cases.items():
            with self.subTest(why):
                self.assertIsNone(P(data, ext))

    def test_reading_a_file(self):
        tmp = tempfile.mkdtemp(prefix="clean-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "IMG.JPG")
        with open(path, "wb") as f:
            f.write(M.jpeg(exif=EX))
        st = os.stat(path)
        self.assertEqual(media.read(path, st.st_size, st.st_mtime_ns)["format"], "JPEG")
        self.assertEqual(media.read(path, st.st_size)["format"], "JPEG")
        # changed since it was listed: no tags rather than wrong ones
        self.assertIsNone(media.read(path, st.st_size + 1, st.st_mtime_ns))
        self.assertIsNone(media.read(path, st.st_size, st.st_mtime_ns + 1))
        # names: only a known extension counts, in any case
        for name in ("noext", ".jpg", "photo.txt"):
            other = os.path.join(tmp, name)
            with open(other, "wb") as f:
                f.write(M.jpeg(exif=EX))
            self.assertIsNone(media.read(other, st.st_size), name)
        # can't be read: the caller is told
        with self.assertRaises(OSError):
            media.read(os.path.join(tmp, "gone.jpg"), 10)

    def test_no_readahead(self):
        # The kernel is told to read only what the readers ask for; the hint
        # failing or missing (not Linux) changes nothing else.
        tmp = tempfile.mkdtemp(prefix="clean-test-")
        path = os.path.join(tmp, "IMG.JPG")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(path, "wb") as f:
            f.write(M.jpeg(exif=EX))
        size = os.path.getsize(path)
        calls = []
        with mock.patch.object(media.os, "posix_fadvise",
                               lambda *a: calls.append(a[1:]), create=True):
            self.assertEqual(media.read(path, size)["format"], "JPEG")
        self.assertEqual(calls, [(0, 0, os.POSIX_FADV_RANDOM)])

        def fail(*a):
            raise OSError(22, "Invalid argument")
        with mock.patch.object(media.os, "posix_fadvise", fail, create=True):
            self.assertEqual(media.read(path, size)["format"], "JPEG")
        with mock.patch.object(media, "os", mock.Mock(wraps=os, spec=["fstat", "path"])):
            self.assertEqual(media.read(path, size)["format"], "JPEG")


# ---------------------------------------------------------------- fuzzing


def samples():
    """{name: (ext, bytes)}: one of each format and variant."""
    ex = M.tiff(sub="5", pixel=(6000, 4000))
    return {
        "jpg": ("jpg", M.jpeg(exif=ex)),
        "jpg-be-xmp": ("jpg", M.jpeg(exif=M.tiff(order=">"), xmp_first=True)),
        "cr2": ("cr2", M.tiff(dims=(6024, 4024), pixel=(6000, 4000),
                              sub_ifd_dims=[(160, 120), (6024, 4024)])),
        "orf": ("orf", M.tiff(magic=0x4F52, pixel=(4608, 3456))),
        "png": ("png", M.png(exif=ex)),
        "webp": ("webp", M.webp(exif=ex)),
        "webp-vp8l": ("webp", M.webp(exif=ex, kind="VP8L")),
        "heic": ("heic", M.heic(exif=ex)),
        "heic-v2": ("heic", M.heic(exif=ex, iloc_version=2, infe_version=3)),
        "mp3": ("mp3", M.mp3(secs=3)),
        "mp3-vbr": ("mp3", M.mp3(secs=3, vbr=True, tag_version=4, picture=3000)),
        "mp3-v22": ("mp3", M.mp3(secs=3, tag_version=2, v1=True)),
        "mp3-unsync": ("mp3", M.mp3(secs=3, unsync=True, picture=200)),
        "mp3-v1": ("mp3", M.mp3(secs=3, tag=False, v1=True)),
        "flac": ("flac", M.flac(secs=3, picture=500)),
        "flac-id3": ("flac", M.flac(secs=3, id3_first=True)),
        "m4a": ("m4a", M.m4a()),
        "m4a-alac": ("m4a", M.m4a(codec=b"alac", version=1)),
        "ogg": ("ogg", M.ogg(picture=70000)),
        "opus": ("opus", M.ogg(codec="opus")),
    }


MAGIC = [b"\xff\xd8\xff\xe1", b"\x89PNG\r\n\x1a\n", b"RIFF\0\0\0\0WEBP", b"II*\0", b"MM\0*",
         b"IIRO", b"\0\0\0\x18ftypheic", b"\0\0\0\x18ftypM4A ", b"fLaC", b"OggS",
         b"ID3\x03\0\0", b"ID3\x04\0\x10", b"ID3\x02\0\0", b"\xff\xfb\x90\0"]


class TestFuzz(unittest.TestCase):
    """Whatever a file holds, reading it never raises (other than OSError
    for a file that can't be read), never takes long, and any tags it gives
    are complete enough for the matching, the cache and the reports."""

    ROUNDS = 6 if SLOW else 1
    LIMIT = 0.5             # seconds for one file, generously

    def check(self, data, ext, why):
        start = time.perf_counter()
        try:
            t = P(data, ext)
        except Exception as e:      # noqa: BLE001 - the failure under test
            self.fail(f"{why}: {type(e).__name__}: {e}")
        took = time.perf_counter() - start
        self.assertLess(took, self.LIMIT, why)
        if t is not None:
            self.assertIn(t["kind"], ("photo", "song"), why)
            self.assertIsNotNone(media.key_of(t), why)
            media.describe(t)
            media.quality(t, len(data))
            self.assertEqual(media.decode(media.encode(t)), t, why)
        return t

    def test_every_sample_reads(self):
        for name, (ext, data) in samples().items():
            with self.subTest(name):
                self.assertIsNotNone(self.check(data, ext, name))

    def test_truncated(self):
        rnd = random.Random(1)
        for name, (ext, data) in samples().items():
            cuts = set(range(min(len(data), 700)))
            cuts.update(rnd.randrange(len(data)) for _ in range(150 * self.ROUNDS))
            cuts.update(len(data) - k for k in range(1, 300) if k < len(data))
            for c in sorted(cuts):
                self.check(data[:c], ext, f"{name} cut at {c} of {len(data)}")

    def test_corrupt(self):
        rnd = random.Random(2)
        for name, (ext, data) in samples().items():
            # most of what matters is in the first few KB
            hot = min(len(data), 4096)
            for k in range(400 * self.ROUNDS):
                b = bytearray(data)
                how = k % 5
                if how == 0:                # a few random bytes
                    for _ in range(rnd.randint(1, 8)):
                        b[rnd.randrange(hot)] = rnd.randrange(256)
                elif how == 1:              # a length or offset gone huge, or zero
                    i = rnd.randrange(max(1, hot - 4))
                    b[i:i + 4] = rnd.choice([b"\xff\xff\xff\xff", b"\0\0\0\0", b"\x7f\xff\xff\xff",
                                             b"\0\0\0\x01", b"\x80\0\0\0"])
                elif how == 2:              # a piece left out
                    i = rnd.randrange(hot)
                    del b[i:i + rnd.randint(1, 64)]
                elif how == 3:              # a piece repeated
                    i = rnd.randrange(hot)
                    b[i:i] = b[i:i + rnd.randint(1, 64)]
                else:                       # bits flipped
                    for _ in range(rnd.randint(1, 16)):
                        b[rnd.randrange(hot)] ^= 1 << rnd.randrange(8)
                self.check(bytes(b), ext, f"{name} corruption {k}")

    def test_random_bytes_behind_every_signature(self):
        rnd = random.Random(3)
        exts = ["jpg", "png", "webp", "tif", "heic", "mp3", "flac", "m4a", "ogg"]
        for k in range(300 * self.ROUNDS):
            head = rnd.choice(MAGIC)
            body = bytes(rnd.randrange(256) for _ in range(rnd.choice([0, 3, 40, 500, 5000])))
            for ext in exts:
                self.check(head + body, ext, f"{head!r} + {len(body)} random bytes as .{ext}")

    def test_files_that_lie_about_sizes(self):
        """Lengths and counts that point far past the end, loops, and long
        runs of nothing: answered quickly, without reading much."""
        big = b"\0" * (8 << 20)
        cases = {
            "an ID3 tag claiming 256 MB": ("mp3", b"ID3\x03\0\0\x7f\x7f\x7f\x7f" + b"\0" * 100),
            "8 MB of nothing after the tag": ("mp3", M.id3() + big),
            "8 MB of nothing before the frames": ("mp3", M.id3() + big + M.mp3(secs=1,
                                                                                tag=False)),
            "a JPEG segment claiming 64 KB": ("jpg", b"\xff\xd8\xff\xe1\xff\xff" + b"\0" * 50),
            "a JPEG of 8 MB with no frame": ("jpg", b"\xff\xd8\xff\xfe\0\x04ab" + big),
            "a PNG chunk claiming 4 GB": ("png", b"\x89PNG\r\n\x1a\n\xff\xff\xff\xffeXIf"),
            "a box claiming a 64-bit size": ("heic", b"\0\0\0\x18ftypheic\0\0\0\0mif1heic"
                                             + b"\0\0\0\x01meta" + b"\xff" * 8 + b"\0" * 64),
            "boxes of size 8, many": ("heic", b"\0\0\0\x18ftypheic\0\0\0\0mif1heic"
                                      + b"\0\0\0\x08free" * 100000),
            "FLAC blocks, many": ("flac", b"fLaC" + b"\x01\0\0\0" * 200000),
            "Ogg pages with nothing in them": ("ogg", (b"OggS\0\x02" + b"\0" * 20 + b"\0") * 50000),
            "an IFD looping to itself": ("tif", b"II*\0\x08\0\0\0\x01\0\x69\x87\x04\0\x01\0\0\0"
                                         b"\x08\0\0\0\x08\0\0\0"),
            "a WebP chunk claiming 4 GB": ("webp", b"RIFF\xff\xff\xff\xffWEBPVP8X\xff\xff\xff\xff"),
        }
        for why, (ext, data) in cases.items():
            with self.subTest(why):
                self.check(data, ext, why)

    def test_damaged_files_on_disk(self):
        """Through read() (the file, not bytes), where every read counts."""
        tmp = tempfile.mkdtemp(prefix="clean-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        rnd = random.Random(4)
        for name, (ext, data) in samples().items():
            for k in range(20 * self.ROUNDS):
                b = bytearray(data[:rnd.randrange(1, len(data) + 1)])
                for _ in range(rnd.randint(0, 6)):
                    b[rnd.randrange(len(b))] = rnd.randrange(256)
                path = os.path.join(tmp, f"{name}-{k}.{ext}")
                with open(path, "wb") as f:
                    f.write(b)
                start = time.perf_counter()
                t = media.read(path, len(b))
                self.assertLess(time.perf_counter() - start, self.LIMIT, path)
                self.assertTrue(t is None or t["kind"] in ("photo", "song"), path)
                os.unlink(path)


# ---------------------------------------------------------------- matching


def song(artist="Artist", title="Title", secs=200.0, **kw):
    return dict({"kind": "song", "artist": artist, "title": title, "secs": secs,
                 "format": "MP3", "kbps": 128, "lossless": False}, **kw)


def shot(sub=None, w=6000, h=4000, **kw):
    return dict(photo(w, h, "JPEG", sub), **kw)


class TestMatch(unittest.TestCase):
    def sets(self, items):
        return sorted(sorted(s) for s in media.group(items))

    def test_photos(self):
        self.assertEqual(self.sets([("a", shot()), ("b", shot()), ("c", shot(w=4000, h=6000)),
                                    ("d", shot(w=1920, h=1080)), ("e", shot(model="EOS R5")),
                                    ("f", shot(taken="2019-06-12 10:15:31")),
                                    ("g", shot(make="CANON ", model="eos-80d"))]),
                         [["a", "b", "c", "g"]])

    def test_photos_taken_in_one_second(self):
        # several fractions: each its own set, files without one left out
        self.assertEqual(self.sets([("a", shot("12")), ("b", shot("12")), ("c", shot("57")),
                                    ("d", shot("57")), ("e", shot()), ("f", shot("99"))]),
                         [["a", "b"], ["c", "d"]])
        # one fraction: files without one may be the same shot
        self.assertEqual(self.sets([("a", shot("12")), ("b", shot())]), [["a", "b"]])

    def test_songs(self):
        items = [("a", song()), ("b", song("ARTIST", "title.", 201.5)),
                 ("c", song("Ártist", "Title", 200.0)), ("d", song(title="Title (Live)")),
                 ("e", song("The Artist")), ("f", song(secs=202.1)), ("g", song(secs=260)),
                 ("h", song(secs=261))]
        # case and punctuation don't count, accents and words do; f is more
        # than LENGTH_SLACK from a, and alone in its own set
        self.assertEqual(self.sets(items), [["a", "b"], ["g", "h"]])

    def test_song_lengths(self):
        # within LENGTH_SLACK of the first in the set, not of the one before
        got = self.sets([("a", song(secs=100)), ("b", song(secs=101.5)), ("c", song(secs=102.5)),
                         ("d", song(secs=104.4)), ("e", song(secs=300))])
        self.assertEqual(got, [["a", "b"], ["c", "d"]])
        self.assertEqual(self.sets([("a", song(secs=100)), ("b", song(secs=102.0))]),
                         [["a", "b"]])
        self.assertEqual(self.sets([("a", song(secs=100)), ("b", song(secs=102.01))]), [])

    def test_photos_and_songs_never_mix(self):
        self.assertEqual(self.sets([("a", shot()), ("b", song("Canon", "EOS 80D"))]), [])

    def test_keys(self):
        self.assertEqual(media.key_of(song("AC/DC", "T.N.T.")), ("song", "acdc", "tnt"))
        self.assertEqual(media.key_of(song("Ｂｅｙｏｎｃé", "x")), ("song", "beyoncé", "x"))
        self.assertEqual(media.key_of(shot(w=4000, h=6000)),
                         ("photo", WHEN, "canon", "eos80d", 6000, 4000))
        self.assertIsNone(media.key_of({"kind": "video"}))

    def test_cache_text(self):
        for t in (shot("5"), song(), song(format="FLAC", kbps=900, lossless=True),
                  shot(make=None, model="Pixel 7"), song("Sigur Rós", "Ágætis byrjun")):
            with self.subTest(t=t):
                self.assertEqual(media.decode(media.encode(t)), t)
        self.assertEqual(media.encode(None), "")
        for text in ("", None, "{", "[1, 2]", "null", "3", '"x"', '{"kind": "photo"}',
                     '{"kind": "song", "artist": "a", "title": "b", "secs": "200"}',
                     '{"kind": "song", "artist": "a", "title": "b", "secs": 0}',
                     '{"kind": "song", "artist": 5, "title": "b", "secs": 10}',
                     '{"kind": "photo", "taken": "x", "make": "m", "w": "a", "h": 2}',
                     '{"kind": "photo", "taken": "x", "make": "m", "w": 2}',
                     '{"kind": "photo", "taken": "x", "make": "m", "w": 1e300, "h": 2}'):
            with self.subTest(text=text):
                self.assertIsNone(media.decode(text))

    def test_describe(self):
        self.assertEqual(media.describe(shot()), "6000×4000 JPEG")
        self.assertEqual(media.describe(shot(format=None)), "6000×4000")
        self.assertEqual(media.describe(song(secs=215.4, kbps=320)), "MP3 · 320 kbps · 3:35")
        self.assertEqual(media.describe(song(format="FLAC", lossless=True, kbps=900, secs=59.6)),
                         "FLAC · lossless · 1:00")
        self.assertEqual(media.describe(song(format="Opus", kbps=None, secs=3725)),
                         "Opus · 62:05")

    def test_quality(self):
        def best(*ts):
            return max(range(len(ts)), key=lambda k: media.quality(ts[k][0], ts[k][1]))
        self.assertEqual(best((shot(format="JPEG"), 9000), (shot(format="TIFF"), 10)), 1)
        self.assertEqual(best((shot(w=1920, h=1080), 9000), (shot(), 10)), 1)
        self.assertEqual(best((shot(), 10), (shot(), 9000)), 1)
        self.assertEqual(best((song(kbps=320), 9000), (song(format="FLAC", lossless=True,
                                                               kbps=0), 10)), 1)
        self.assertEqual(best((song(kbps=128), 9000), (song(kbps=320), 10)), 1)

    def test_letters_and_labels(self):
        self.assertEqual([_letter(k) for k in (0, 1, 25, 26, 27, 51, 52, 701, 702)],
                         ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"])
        cases = [(shot(), f"Canon EOS 80D · {WHEN}"),
                 (shot(make="Canon", model="Canon EOS 5D"), f"Canon EOS 5D · {WHEN}"),
                 (shot(make="NIKON CORPORATION", model="NIKON D750"), f"NIKON D750 · {WHEN}"),
                 (shot(make="Apple", model="iPhone 12"), f"Apple iPhone 12 · {WHEN}"),
                 (shot(make=None, model="Pixel 7"), f"Pixel 7 · {WHEN}"),
                 (shot(make="Sony", model=None), f"Sony · {WHEN}"),
                 (song("Nina Simone", "Sinnerman"), "Nina Simone – Sinnerman")]
        for t, want in cases:
            self.assertEqual(_label(t), want)


# ---------------------------------------------------------------- scanning


class Library:
    """A small photo and music library with look-alikes in it."""

    PHOTO_A = "Photos/IMG_0001.jpg"
    PHOTO_B = "Backup/IMG_0001 edited.jpg"
    PHOTO_B2 = "Backup/IMG_0001 copy.jpg"
    FLAC = "Music/Nina - Song.flac"
    MP3_320 = "Rips/song.mp3"
    MP3_128 = "Rips/song 128.mp3"

    def library(self):
        self.write(self.PHOTO_A, M.jpeg(exif=EX, body=b"\x11" * 4000))
        edited = M.jpeg(exif=EX, body=b"\x22" * 2500)
        self.write(self.PHOTO_B, edited)
        self.write(self.PHOTO_B2, edited)
        # the same second, another fraction: another shot
        self.write("Photos/IMG_0002.jpg", M.jpeg(exif=M.tiff(sub="456", pixel=(6000, 4000))))
        self.write("Photos/other.jpg", M.jpeg(exif=M.tiff(taken="2020:01:01 00:00:00")))
        self.write("Photos/no-exif.jpg", M.jpeg())
        self.write(self.FLAC, M.flac("Nina", "Song", secs=8))
        self.write(self.MP3_320, M.mp3("NINA", "song", secs=8.4, kbps=320))
        self.write(self.MP3_128, M.mp3("Nina", "Song!", secs=8, kbps=128))
        self.write("Rips/live.mp3", M.mp3("Nina", "Song", secs=12))
        self.write("Docs/notes.txt", "not a photo")
        self.age()

    def age(self):
        """Back-date every file (always to the same time), so the hash cache
        keeps what it learns."""
        old = 1_600_000_000
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                os.utime(os.path.join(dirpath, n), (old, old))

    def p(self, rel):
        return os.path.join(self.root, rel)

    def looks(self, run=None):
        """The look-alikes in data.json.gz: [(kind, label, [(rel, letter, best)])]."""
        d = _data.read((run or self.last_run()).file("data.json.gz"))
        dirs = _data.paths_of(d["dirs"])
        return [(g["kind"], g["label"],
                 [(os.path.relpath(os.path.join(dirs[f[0]], f[1]), self.root), f[4], f[5])
                  for f in g["files"]]) for g in d["lookalikes"]]

    def file_ref(self, run, rel):
        m = _manifest.Manifest.read(run.file(_manifest.NAME))
        found = [f for f in range(len(m.files)) if m.file_path(f) == self.p(rel)]
        self.assertEqual(len(found), 1, rel)
        return f"f{found[0]}"

    def choose(self, run, *rels):
        _choices.save(run, _choices.Saved(choices={self.file_ref(run, r): "remove"
                                                   for r in rels}))


SONGS = ("song", "Nina – Song", [("Music/Nina - Song.flac", "A", True),
                                 ("Rips/song.mp3", "B", False),
                                 ("Rips/song 128.mp3", "C", False)])
PHOTOS = ("photo", f"Canon EOS 80D · {WHEN}", [("Photos/IMG_0001.jpg", "A", True),
                                               ("Backup/IMG_0001 copy.jpg", "B", False),
                                               ("Backup/IMG_0001 edited.jpg", "B", False)])


class TestScan(Library, Base):
    def scan(self, *args):
        r = self.dupes(self.root, *args)
        self.assertEqual(r.returncode, 0, r.stderr.decode() + r.stdout.decode())
        return r.stdout.decode()

    def test_found_and_reported(self):
        self.library()
        out = self.scan("--media")
        self.assertEqual(self.looks(), [SONGS, PHOTOS])
        # the summary
        self.assertIn("LOOK-ALIKES (2): the same photo or song in files whose bytes differ; "
                      "never picked for removal", out)
        self.assertIn("LOOK-ALIKE 1 — song: Nina – Song, 3 versions:", out)
        self.assertIn(f"   A* {self.p(self.FLAC)}  (FLAC · lossless · 0:08, ", out)
        self.assertIn(f"   C  {self.p(self.MP3_128)}  (MP3 · 128 kbps · 0:08, ", out)
        self.assertIn(f"LOOK-ALIKE 2 — photo: Canon EOS 80D · {WHEN}, 2 versions:", out)
        self.assertIn(f"   B  {self.p(self.PHOTO_B)}  (6000×4000 JPEG, ", out)
        self.assertRegex(out, r"Look-alikes: +2 sets among 9 tagged photos and songs "
                              r"\(10 read, 0 from the cache\)")
        self.assertNotIn("IMG_0002", out.split("LOOK-ALIKES")[1].split("====")[0])
        run = self.last_run()
        self.assertIn("2 look-alike sets", run.headline)
        with open(run.file("summary.txt"), encoding="utf-8") as f:
            self.assertIn("LOOK-ALIKE 2 — photo", f.read())
        # the manifest: best first, and which versions each file has
        m = _manifest.Manifest.read(run.file(_manifest.NAME))
        self.assertEqual([(lid, kind) for lid, kind, _ in m.lookalikes],
                         [(1, "song"), (2, "photo")])
        flac = int(self.file_ref(run, self.FLAC)[1:])
        self.assertEqual(m.lookalikes[0][2][0], flac)
        self.assertEqual(sorted(m.file_path(f) for f in m.versions(flac)),
                         [self.p(self.MP3_128), self.p(self.MP3_320)])
        # the static report
        with open(run.file("report.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("Look-alikes", html)
        self.assertIn("Nina – Song", html)
        self.assertIn("likely the best", html)
        self.assertIn("2 versions in 3 files, not byte-identical", html)
        # the copies of B are still a duplicate group
        self.assertEqual(self.report_groups(), [[self.PHOTO_B2, self.PHOTO_B]])

    def test_only_when_asked(self):
        self.library()
        out = self.scan()
        self.assertNotIn("LOOK-ALIKE", out)
        self.assertNotIn("Look-alikes:", out)
        self.assertEqual(self.looks(), [])
        with open(self.last_run().file("report.html"), encoding="utf-8") as f:
            self.assertNotIn("look-alike", f.read().lower())
        # the setting turns it on; --no-media off again
        r = self.run_clean("config", "set", "dupes.media", "true")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.scan()
        self.assertEqual(len(self.looks()), 2)
        self.scan("--no-media")
        self.assertEqual(self.looks(), [])

    def test_tags_are_kept_in_the_hash_cache(self):
        self.library()
        self.scan("--media")
        out = self.scan("--media")
        self.assertRegex(out, r"\(0 read, 10 from the cache\)")
        self.assertEqual(len(self.looks()), 2)
        db = sqlite3.connect(os.path.join(self.tmp, "xdg", "cache", "clean", "hashes.sqlite"))
        self.addCleanup(db.close)

        def tags():
            return {os.path.basename(os.fsdecode(p)): t
                    for p, t in db.execute("SELECT path, tags FROM hashes")}
        by_name = tags()
        self.assertEqual(media.decode(by_name["IMG_0001.jpg"])["sub"], "123")
        self.assertEqual(by_name["no-exif.jpg"], "")         # read: nothing to go by
        self.assertIsNone(by_name.get("notes.txt"))
        # a scan without --media keeps the tags beside the hashes
        self.scan()
        self.assertEqual(tags()["IMG_0001.jpg"], by_name["IMG_0001.jpg"])
        both = db.execute("SELECT count(*) FROM hashes WHERE full IS NOT NULL "
                          "AND tags IS NOT NULL").fetchone()[0]
        self.assertGreaterEqual(both, 2, "the copies of B have a hash and tags")
        # a file that changed is read again
        self.write(self.MP3_128, M.mp3("Nina", "Song", secs=8, kbps=96))
        self.age()
        out = self.scan("--media")
        self.assertRegex(out, r"\(1 read, 9 from the cache\)")
        self.assertIn("MP3 · 96 kbps", out)

    def test_files_that_cant_be_read(self):
        self.library()
        os.chmod(self.p(self.MP3_320), 0)
        if os.access(self.p(self.MP3_320), os.R_OK):
            self.skipTest("running as root")
        r = self.dupes(self.root, "--media")
        out = r.stdout.decode()
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertRegex(out, r"Look-alikes: +2 sets among 8 tagged photos and songs "
                              r"\(9 read, 0 from the cache, 1 unreadable\)")
        self.assertEqual(self.looks()[0][2], [SONGS[2][0], ("Rips/song 128.mp3", "B", False)])

    def test_hidden_files_and_filters(self):
        self.library()
        self.write(".hidden/IMG_0001.jpg", M.jpeg(exif=EX, body=b"\x33" * 100))
        self.age()
        self.scan("--media")
        self.assertEqual(self.looks(), [SONGS, PHOTOS])
        self.scan("--media", "--hidden")
        self.assertIn((".hidden/IMG_0001.jpg", "C", False), self.looks()[1][2])
        # only what the filters let in is compared
        self.scan("--media", "--type", "audio")
        self.assertEqual(self.looks(), [SONGS])
        self.scan("--media", "--ext", "jpg")
        self.assertEqual(self.looks(), [PHOTOS])

    def test_hard_links_are_one_file(self):
        self.library()
        os.link(self.p(self.FLAC), self.p("Music/linked.flac"))
        self.scan("--media")
        files = [rel for rel, _, _ in self.looks()[0][2]]
        self.assertEqual(len([f for f in files if f.endswith(".flac")]), 1, files)

    def test_help(self):
        # the option that changes what the settings say is the one shown
        out = self.run_clean("dupes", "-h").stdout.decode()
        self.assertIn("--media ", out)
        self.assertNotIn("--no-media", out)
        out = self.run_clean("dupes", "-h", env={"CLEAN_DUPES_MEDIA": "1"}).stdout.decode()
        self.assertIn("--no-media", out)
        self.assertIn("setting dupes.media", out)


# ---------------------------------------------------------------- dashboard


class TestDashboard(Library, WebBase):
    def scan(self, *args):
        self.library()
        r = self.dupes(self.root, "--media", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.last_run().name

    def choose(self, name, rel, value="remove"):
        return self.api(f"/api/selection/{name}", {"op": "set", "ref": self.ref(name, rel, "f"),
                                                   "value": value, "q": {"tab": "lookalikes"}})

    def dialog(self, name):
        return self.api("/api/confirm", {"action": "remove", "run": name, "method": "trash"})

    def test_the_tab(self):
        name = self.scan()
        page = self.page(f"/runs/{name}")
        self.assertIn("Look-alikes", page)
        out = self.api(f"/api/results/{name}?tab=lookalikes")
        self.assertEqual(out["tab"], "lookalikes")
        html = out["html"]
        self.assertIn("Nothing here is ticked for you", html)
        self.assertIn("Nina – Song", html)
        self.assertIn("likely the best", html)
        self.assertIn("FLAC · lossless", html)
        self.assertIn("not byte-identical", html)
        for rel in (self.FLAC, self.MP3_320, self.MP3_128, self.PHOTO_A, self.PHOTO_B):
            self.assertIn(f'data-ref="{self.ref(name, rel, "f")}"', html)
        self.assertLess(html.index("Nina – Song"), html.index("Canon EOS 80D"))
        # one card by its id, as the links to it ask
        one = self.api(f"/api/results/{name}?card=L2")
        self.assertEqual(one["tab"], "lookalikes")
        self.assertIn("Canon EOS 80D", one["html"])
        self.assertNotIn("Nina – Song", one["html"])
        # the searches and filters work on them as on the others
        self.assertNotIn("Nina", self.api(f"/api/results/{name}?tab=lookalikes&type=Images")
                         ["html"])
        self.assertIn("Nina", self.api(f"/api/results/{name}?tab=lookalikes&q=nina")["html"])
        self.page(f"/runs/{name}?tab=lookalikes&sort=path")

    def test_no_tab_without_media(self):
        self.library()
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        name = self.last_run().name
        self.assertNotIn("Look-alikes", self.page(f"/runs/{name}"))
        # nor in the wizard's answer: the option is offered there
        self.assertIn("Also find look-alike photos and songs", self.page("/scan?kind=dupes"))

    def test_never_selected_for_you(self):
        name = self.scan()
        songs = [self.ref(name, r, "f") for r in (self.FLAC, self.MP3_320, self.MP3_128)]
        for r in songs:
            self.assertNotIn(self.state(name, r), ("go", "merge", "blocked"))
        c = self.dialog(name)
        self.assertNotIn("same song", c["html"])
        self.assertNotIn("look-alike", c["html"])
        # "invert" doesn't tick them either
        out = self.api(f"/api/selection/{name}", {"op": "invert", "q": {"tab": "lookalikes"}})
        self.assertEqual(out["message"], "Look-alikes are never selected in bulk; tick each "
                                         "file to remove.")
        self.assertNotIn(self.state(name, songs[2]), ("go", "merge"))
        out = self.api(f"/api/selection/{name}", {"op": "invert", "q": {"tab": "groups"}})
        self.assertEqual(out["message"], "Inverted 1 result.")
        # nor the rules
        self.api(f"/api/selection/{name}", {"op": "rules", "values": {
            "remove_from": [self.p("Rips")]}, "q": {"tab": "lookalikes"}})
        self.assertNotIn(self.state(name, songs[2]), ("go", "merge"))

    def test_ticked_and_blocked(self):
        name = self.scan()
        self.choose(name, self.MP3_128)
        self.assertEqual(self.state(name, self.ref(name, self.MP3_128, "f")), "go")
        c = self.dialog(name)
        self.assertIn(f"same song as {self.p(self.FLAC)}, which stays (not byte-identical)",
                      c["html"])
        self.assertIn("1 look-alike chosen to go: not copies", c["html"])
        # every version ticked: the best stays
        self.choose(name, self.MP3_320)
        self.choose(name, self.FLAC)
        self.assertEqual(self.state(name, self.ref(name, self.FLAC, "f")), "blocked")
        self.assertEqual(self.state(name, self.ref(name, self.MP3_320, "f")), "go")
        c = self.dialog(name)
        self.assertIn("it is the last version left", c["html"])
        self.assertIn("2 look-alikes chosen to go", c["html"])
        # undecided again, card by card
        self.api(f"/api/selection/{name}", {"op": "reset_card", "card": "L1",
                                            "q": {"tab": "lookalikes"}})
        self.assertNotIn(self.state(name, self.ref(name, self.MP3_128, "f")), ("go", "blocked"))

    def test_photos_with_copies(self):
        """B has two copies (a file group); A is a look-alike of both."""
        name = self.scan()
        a, b, b2 = (self.ref(name, r, "f") for r in (self.PHOTO_A, self.PHOTO_B, self.PHOTO_B2))
        # the rules take one copy of B (the group), never A
        self.assertEqual(sorted(self.state(name, r) for r in (b, b2)).count("go"), 1)
        self.assertNotEqual(self.state(name, a), "go")
        # A ticked: B stays (a copy of it), so A may go
        self.choose(name, self.PHOTO_A)
        self.assertEqual(self.state(name, a), "go")
        self.assertIn(f"same photo as {self.p(self.PHOTO_B2)}", self.dialog(name)["html"])

    def test_keep_all_on_a_card(self):
        name = self.scan()
        self.choose(name, self.MP3_128)
        self.api(f"/api/selection/{name}", {"op": "keep_all", "card": "L1",
                                            "q": {"tab": "lookalikes"}})
        self.assertEqual(self.state(name, self.ref(name, self.MP3_128, "f")), "keep")


# ---------------------------------------------------------------- removing


@needs_unshare
class TestRemove(Library, Base):
    def setUp(self):
        super().setUp()
        self.trash = os.path.join(self.tmp, "xdg", "data", "Trash")

    def scan(self):
        self.library()
        r = self.dupes(self.root, "--media", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.last_run()

    def test_a_ticked_look_alike_goes_and_comes_back(self):
        run = self.scan()
        before = self.snapshot()
        r = self.clean_sealed("remove", "-y")
        self.assertEqual(r.rc, 0, r)
        # by the rules: only a copy of B went, no look-alike
        gone = sorted(set(before) - set(self.snapshot()))
        self.assertEqual(len(gone), 1)
        self.assertIn(gone[0], (self.PHOTO_B, self.PHOTO_B2))
        self.assertEqual(self.clean_sealed("undo", "-y").rc, 0)
        self.assertEqual(self.snapshot(), before)
        self.choose(run, self.MP3_128)
        r = self.clean_sealed("remove", "-y", run.name)
        self.assertEqual(r.rc, 0, r)
        self.assertIn("1 look-alike chosen to go", r.out + r.err)
        self.assertNotIn(self.MP3_128, self.tree())
        with open(os.path.join(self.trash, "files", "song 128.mp3"), "rb") as f:
            self.assertEqual(f.read(), before[self.MP3_128])
        self.assertEqual(self.clean_sealed("undo", "-y").rc, 0)
        self.assertEqual(self.snapshot(), before)

    def test_the_last_version_stays(self):
        run = self.scan()
        before = self.snapshot()
        self.choose(run, self.FLAC, self.MP3_320, self.MP3_128)
        r = self.clean_sealed("remove", "-y", run.name)
        self.assertIn(r.rc, (0, 1), r)
        self.assertIn("it is the last version left", r.out + r.err)
        after = self.snapshot()
        self.assertEqual(after[self.FLAC], before[self.FLAC])
        self.assertNotIn(self.MP3_320, after)
        self.assertNotIn(self.MP3_128, after)
        self.assertEqual(self.clean_sealed("undo", "-y").rc, 0)
        self.assertEqual(self.snapshot(), before)

    def test_the_version_that_stays_is_checked(self):
        run = self.scan()
        self.choose(run, self.MP3_128)
        # the FLAC and the 320 kbps rip both changed after the scan
        self.write(self.FLAC, M.flac("Nina", "Song", secs=8.5))
        self.write(self.MP3_320, b"gone bad")
        r = self.clean_sealed("remove", "-y", run.name)
        self.assertIn(self.MP3_128, self.tree())
        self.assertNotEqual(r.rc, 0, r)

    def test_dry_run_changes_nothing(self):
        run = self.scan()
        before = self.snapshot()
        self.choose(run, self.MP3_128)
        r = self.clean_sealed("remove", "-n", run.name)
        self.assertEqual(r.rc, 0, r)
        self.assertIn("song 128.mp3", r.out)
        # rules given here don't quietly mix with what was ticked
        r = self.clean_sealed("remove", "-n", "--only", "folders", run.name)
        self.assertEqual(r.rc, 2, r)
        self.assertIn("--no-choices", r.err)
        self.assertEqual(self.snapshot(), before)

    def test_no_hard_link_to_a_look_alike(self):
        run = self.scan()
        before = self.snapshot()
        self.choose(run, self.MP3_128)
        r = self.clean_sealed("remove", "-y", "--link", run.name)
        self.assertIn("its look-alike is not the same file, so it can't be replaced with a "
                      "hard link to it", r.out + r.err)
        self.assertEqual(self.snapshot()[self.MP3_128], before[self.MP3_128])
        self.assertEqual(os.stat(self.p(self.MP3_128)).st_nlink, 1)


if __name__ == "__main__":
    unittest.main()
