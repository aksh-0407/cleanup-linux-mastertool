"""
Photo and song tags, for finding look-alikes: the same photo or song in files
whose bytes differ, so they are not duplicates. Examples are an edited or
re-saved photo, a HEIC and its JPEG export, or one song ripped twice at
different bitrates.

    photos  the same capture time (EXIF DateTimeOriginal), the same camera
            (make and model) and the same dimensions, either way round
    songs   the same artist and title, ignoring case, spaces and punctuation,
            with lengths within LENGTH_SLACK seconds

Only the headers are read: a few KB to a few hundred KB per file, seeking
past cover art and picture data, never the image or the sound itself. Every
reader is bounded (MAX_READS reads of at most MAX_PIECE bytes each) and treats
anything unexpected as "no tags", so a truncated or corrupt file never stops
a scan.

Formats:
  photos  JPEG; TIFF and the raw formats built on it (DNG, CR2, NEF, ARW, ORF,
          RW2, ...); HEIC, HEIF and AVIF; PNG; WebP
  songs   MP3 (ID3v2.2 to 2.4, ID3v1, Xing/Info/VBRI or constant bitrate);
          FLAC; M4A (MP4 audio); Ogg Vorbis and Opus

read() gives a small dict, which the hash cache stores as JSON:
    {"kind": "photo", "taken": "2019-06-12 10:15:30", "sub": "123" or None,
     "make": "Apple", "model": "iPhone XS", "w": 4032, "h": 3024,
     "format": "JPEG"}
    {"kind": "song", "artist": "...", "title": "...", "secs": 215.3,
     "format": "MP3", "kbps": 320, "lossless": False}
"""

import json
import os
import re
import struct
import unicodedata
from collections import defaultdict

PHOTO_EXTS = frozenset(
    "jpg jpeg jpe jfif tif tiff dng cr2 nef nrw arw srf sr2 orf rw2 pef srw erf kdc mrw 3fr "
    "heic heif avif png webp".split())
SONG_EXTS = frozenset("mp3 flac m4a m4b ogg oga opus".split())
EXTS = PHOTO_EXTS | SONG_EXTS

LENGTH_SLACK = 2.0      # seconds two copies of a song may differ by
MAX_READS = 400         # reads per file
MAX_PIECE = 1 << 20     # bytes per read
MAX_TEXT = 200          # characters kept of a tag
HEAD = 1 << 16          # first read of an MP3 tag (more only if needed)
BUFFER = 1 << 14        # bytes Python reads at a time (the tags are near the start)


class _Bad(Exception):
    """Not a file whose tags can be read."""


# ---------------------------------------------------------------- reading

class _Src:
    """Bounded random access to an open file, or to bytes."""

    def __init__(self, f=None, size=0, data=None):
        self.f = f
        self.data = data
        self.size = len(data) if data is not None else size
        self.reads = 0

    def at(self, off, n):
        """Up to n bytes at off (fewer at the end)."""
        if off < 0 or n <= 0 or off >= self.size:
            return b""
        n = min(n, self.size - off)
        if n > MAX_PIECE:
            raise _Bad
        if self.data is not None:
            return bytes(self.data[off:off + n])
        self.reads += 1
        if self.reads > MAX_READS:
            raise _Bad
        self.f.seek(off)
        return self.f.read(n)

    def need(self, off, n):
        """Exactly n bytes at off."""
        b = self.at(off, n)
        if len(b) != n:
            raise _Bad
        return b


class _Cur:
    """A cursor over bytes; reading past the end raises _Bad."""

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise _Bad
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def uint(self, n, order="big"):
        return int.from_bytes(self.take(n), order) if n else 0


def read(path, size, mtime_ns=None):
    """The tags of one file (see the module's docstring), or None when it
    has none to go by, isn't a photo or song, or changed since it was
    listed. Raises OSError when it can't be read."""
    stem, dot, ext = os.path.basename(path).rpartition(".")
    if not (dot and stem) or ext.lower() not in EXTS:     # ".jpg" has no extension
        return None
    ext = ext.lower()
    with open(path, "rb", buffering=BUFFER) as f:
        _no_readahead(f)
        st = os.fstat(f.fileno())
        if st.st_size != size or (mtime_ns is not None and st.st_mtime_ns != mtime_ns):
            return None
        return parse(_Src(f, size), ext)


def _no_readahead(f):
    """Have the kernel read only the bytes asked for. By default it reads
    ahead (100-250 KB a file), while the tags take a few KB: on a slow USB
    drive with many photos that was most of the time --media took."""
    try:
        os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
    except (AttributeError, OSError):
        pass                    # only a hint


def parse(src, ext):
    """The tags of `src` (an open _Src), or None."""
    try:
        head = src.at(0, 16)
        kind = "song" if ext in SONG_EXTS else "photo"
        reader = _sniff(head, kind)
        if reader is None:
            return None
        out = reader(src)
    except OSError:
        raise
    except Exception:       # whatever a corrupt file makes the readers trip on
        return None
    return _valid(out)


def _sniff(head, kind):
    if kind == "photo":
        if head[:3] == b"\xff\xd8\xff":
            return _jpeg
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return _png
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return _webp
        if head[:4] in (b"II*\0", b"MM\0*", b"IIRO", b"IIRS", b"IIU\0"):
            return _tiff_file
        if head[4:8] == b"ftyp":
            return _heif
        return None
    if head[4:8] == b"ftyp":
        return _mp4
    if head[:4] == b"fLaC":
        return _flac
    if head[:4] == b"OggS":
        return _ogg
    if head[:3] == b"ID3":
        return _id3_then
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return _mp3
    return None


def _valid(t):
    """t, if it has everything the matching needs."""
    if not isinstance(t, dict):
        return None
    if t.get("kind") == "photo":
        if not t.get("taken") or not (t.get("make") or t.get("model")):
            return None
        w, h = t.get("w") or 0, t.get("h") or 0
        if not (0 < w < 1 << 20 and 0 < h < 1 << 20):
            return None
        return t
    if t.get("kind") == "song":
        secs = t.get("secs")
        if not _norm(t.get("artist")) or not _norm(t.get("title")):
            return None
        if not isinstance(secs, (int, float)) or not 0 < secs < 86400:
            return None
        t["secs"] = round(float(secs), 2)
        if not t["secs"]:
            return None
        if t.get("kbps") is not None:
            t["kbps"] = int(round(t["kbps"]))
        return t
    return None


def _text(raw, encoding="utf-8"):
    """Tag text: up to the first NUL, whitespace tidied, at most MAX_TEXT long."""
    if isinstance(raw, bytes):
        raw = raw.split(b"\0", 1)[0]
        try:
            raw = raw.decode(encoding)
        except UnicodeDecodeError:
            raw = raw.decode("latin-1")
    raw = raw.split("\0", 1)[0].replace("﻿", "")
    out = " ".join("".join(ch if ch.isprintable() else " " if ch.isspace() else ""
                           for ch in raw).split())
    return out[:MAX_TEXT]


def _norm(text):
    """What two tags are compared by: letters and digits only, ignoring case."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum())


# ---------------------------------------------------------------- EXIF (TIFF)

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_MAKE, _MODEL, _WIDTH, _HEIGHT = 0x010F, 0x0110, 0x0100, 0x0101
_EXIF_IFD, _SUB_IFDS = 0x8769, 0x014A
_TAKEN, _SUBSEC, _PIXEL_X, _PIXEL_Y = 0x9003, 0x9291, 0xA002, 0xA003
_WANTED = {_MAKE, _MODEL, _WIDTH, _HEIGHT, _EXIF_IFD, _SUB_IFDS, _TAKEN, _SUBSEC,
           _PIXEL_X, _PIXEL_Y}
_DATE = re.compile(r"\s*(\d{4})[:\-](\d\d)[:\-](\d\d)[ T](\d\d):(\d\d):(\d\d)")


def _tiff(src, base):
    """Photo facts from the TIFF structure at `base` in src (the EXIF of
    every format): {taken, sub, make, model, w, h}; w and h are the
    largest image the IFDs describe, else the EXIF pixel size."""
    head = src.need(base, 8)
    if head[:2] == b"II":
        e = "<"
    elif head[:2] == b"MM":
        e = ">"
    else:
        raise _Bad
    magic, first = struct.unpack(e + "HI", head[2:8])
    if magic not in (42, 0x4F52, 0x5352, 0x55):
        raise _Bad
    out = {"taken": None, "sub": None, "make": "", "model": "", "w": 0, "h": 0}
    todo, seen, n = [(first, False)], set(), 0
    pixel = (0, 0)
    while todo and n < 12:
        off, is_exif = todo.pop(0)
        if not off or off in seen:
            continue
        seen.add(off)
        n += 1
        tags, nxt = _ifd(src, base, off, e)
        if is_exif:
            t = tags.get(_TAKEN)
            if t:
                out["taken"] = _when(_text(t[2], "ascii"))
            s = tags.get(_SUBSEC)
            if s:
                digits = "".join(ch for ch in _text(s[2], "ascii") if ch.isdigit())
                out["sub"] = (digits.rstrip("0") or "0") if digits else None
            pixel = (_num(e, tags.get(_PIXEL_X)) or 0, _num(e, tags.get(_PIXEL_Y)) or 0)
            continue
        for key, tag in (("make", _MAKE), ("model", _MODEL)):
            if not out[key] and tags.get(tag):
                out[key] = _text(tags[tag][2])
        w, h = _num(e, tags.get(_WIDTH)) or 0, _num(e, tags.get(_HEIGHT)) or 0
        if w * h > out["w"] * out["h"]:
            out["w"], out["h"] = w, h
        if _EXIF_IFD in tags:
            todo.append((_num(e, tags[_EXIF_IFD]), True))
        if _SUB_IFDS in tags:
            todo.extend((x, False) for x in _nums(e, tags[_SUB_IFDS])[:8])
        todo.append((nxt, False))
    if not out["w"] or not out["h"]:
        out["w"], out["h"] = pixel
    out["pixel"] = pixel
    return out


def _ifd(src, base, off, e):
    """({tag: (type, count, value bytes)} for the tags wanted, next IFD)."""
    count = struct.unpack(e + "H", src.need(base + off, 2))[0]
    if not 0 < count <= 1000:
        raise _Bad
    raw = src.need(base + off + 2, count * 12 + 4)
    tags = {}
    for i in range(count):
        tag, typ, n = struct.unpack_from(e + "HHI", raw, i * 12)
        size = _TYPE_SIZE.get(typ)
        if tag not in _WANTED or size is None or not 0 < n <= 4096:
            continue
        total = size * n
        if total <= 4:
            value = raw[i * 12 + 8:i * 12 + 8 + total]
        else:
            ptr = struct.unpack_from(e + "I", raw, i * 12 + 8)[0]
            value = src.at(base + ptr, total)
            if len(value) != total:
                continue
        tags[tag] = (typ, n, value)
    return tags, struct.unpack_from(e + "I", raw, count * 12)[0]


def _nums(e, entry):
    if entry is None:
        return []
    typ, n, value = entry
    fmt = {1: "B", 3: "H", 4: "I", 13: "I", 9: "i", 8: "h", 6: "b"}.get(typ)
    if fmt is None:
        return []
    return list(struct.unpack(e + fmt * n, value[:struct.calcsize(fmt) * n]))


def _num(e, entry):
    values = _nums(e, entry)
    return values[0] if values else None


def _when(text):
    """'2019-06-12 10:15:30' from an EXIF date, or None."""
    m = _DATE.match(text or "")
    if not m:
        return None
    y, mo, d, h, mi, s = map(int, m.groups())
    if not (1800 < y < 2200 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60
            and s <= 60):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


def _photo(fmt, exif, w=0, h=0):
    """The tags of a photo, from its EXIF and its own dimensions."""
    if not exif:
        return None
    if not w or not h:
        w, h = exif["w"], exif["h"]
    return {"kind": "photo", "taken": exif["taken"], "sub": exif["sub"],
            "make": exif["make"], "model": exif["model"], "w": w, "h": h, "format": fmt}


def _exif_bytes(data):
    """EXIF found in a chunk of its own, with or without the 'Exif\\0\\0'
    of JPEG in front."""
    if data[:6] == b"Exif\0\0":
        data = data[6:]
    return _tiff(_Src(data=data), 0)


# ---------------------------------------------------------------- photo formats

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg(src):
    off, exif, w, h = 2, None, 0, 0
    for _ in range(512):
        b = src.need(off, 2)
        if b[0] != 0xFF:
            raise _Bad
        marker = b[1]
        if marker == 0xFF:              # fill byte
            off += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            off += 2
            continue
        if marker in (0xD9, 0xDA):      # end of image, start of scan
            break
        n = struct.unpack(">H", src.need(off + 2, 2))[0]
        if n < 2:
            raise _Bad
        if marker == 0xE1 and exif is None:
            seg = src.at(off + 4, n - 2)
            if seg[:6] == b"Exif\0\0":
                exif = _tiff(_Src(data=seg), 6)
        elif marker in _SOF:
            h, w = struct.unpack(">HH", src.need(off + 5, 4))
            break
        off += 2 + n
    return _photo("JPEG", exif, w, h)


def _tiff_file(src):
    head = src.need(0, 4)
    fmt = {b"IIRO": "ORF", b"IIRS": "ORF", b"IIU\0": "RW2"}.get(head, "TIFF")
    exif = _tiff(src, 0)
    # A raw file's largest image is the sensor's, a little bigger than the
    # photo; the EXIF pixel size, when given, is the photo's.
    w, h = exif["pixel"] if all(exif["pixel"]) else (exif["w"], exif["h"])
    return _photo(fmt, exif, w, h)


def _png(src):
    off, exif, w, h = 8, None, 0, 0
    for _ in range(512):
        head = src.at(off, 8)
        if len(head) < 8:
            break
        n, typ = struct.unpack(">I4s", head)
        if typ == b"IHDR":
            w, h = struct.unpack(">II", src.need(off + 8, 8))
        elif typ == b"eXIf":
            exif = _exif_bytes(src.need(off + 8, n))
        elif typ in (b"IDAT", b"IEND"):
            break
        off += 12 + n
    return _photo("PNG", exif, w, h)


def _webp(src):
    off, exif, w, h = 12, None, 0, 0
    for _ in range(64):
        head = src.at(off, 8)
        if len(head) < 8:
            break
        typ, n = struct.unpack("<4sI", head)
        if typ == b"VP8X":
            d = src.need(off + 8, 10)
            w, h = 1 + int.from_bytes(d[4:7], "little"), 1 + int.from_bytes(d[7:10], "little")
        elif typ == b"VP8 " and not w:
            d = src.need(off + 8, 10)
            if d[3:6] != b"\x9d\x01\x2a":
                raise _Bad
            w = int.from_bytes(d[6:8], "little") & 0x3FFF
            h = int.from_bytes(d[8:10], "little") & 0x3FFF
        elif typ == b"VP8L" and not w:
            d = src.need(off + 8, 5)
            if d[0] != 0x2F:
                raise _Bad
            bits = int.from_bytes(d[1:5], "little")
            w, h = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        elif typ == b"EXIF":
            exif = _exif_bytes(src.need(off + 8, n))
        off += 8 + n + (n & 1)
    return _photo("WebP", exif, w, h)


def _boxes(src, start, end, limit=512):
    """(type, payload start, payload end) of the ISO-BMFF boxes in
    [start, end). A box running past the end is cut short, and is the last."""
    off, n = start, 0
    while off + 8 <= end:
        n += 1
        if n > limit:
            raise _Bad
        size, typ = struct.unpack(">I4s", src.need(off, 8))
        head = 8
        if size == 1:
            size = struct.unpack(">Q", src.need(off + 8, 8))[0]
            head = 16
        elif size == 0:
            size = end - off
        if size < head:
            raise _Bad
        yield typ, off + head, min(off + size, end)
        off += size


def _find(src, start, end, typ):
    for t, a, b in _boxes(src, start, end):
        if t == typ:
            return a, b
    return None


def _heif(src):
    ftyp = src.need(8, 4)
    fmt = "AVIF" if ftyp in (b"avif", b"avis") else "HEIC"
    meta = _find(src, 0, src.size, b"meta")
    if meta is None:
        return None
    a, b = meta
    exif_id, items, dims = None, {}, (0, 0)
    for typ, p, q in _boxes(src, a + 4, b):
        if typ == b"iinf":
            exif_id = _iinf(src.need(p, q - p))
        elif typ == b"iloc":
            items = _iloc(src.need(p, q - p))
        elif typ == b"iprp":
            ipco = _find(src, p, q, b"ipco")
            if ipco:
                for t, x, y in _boxes(src, ipco[0], ipco[1]):
                    if t == b"ispe" and y - x >= 12:
                        w, h = struct.unpack(">II", src.need(x + 4, 8))
                        if w * h > dims[0] * dims[1]:
                            dims = (w, h)
    if exif_id is None or exif_id not in items:
        return None
    method, extents = items[exif_id]
    if method != 0 or not extents:
        return None
    off, length = extents[0]
    data = src.need(off, min(length or src.size - off, MAX_PIECE))
    skip = struct.unpack(">I", data[:4])[0]
    return _photo(fmt, _tiff(_Src(data=data), 4 + skip), *dims)


def _iinf(data):
    """The item id of the Exif item, or None."""
    c = _Cur(data)
    version = c.uint(1)
    c.take(3)
    c.uint(2 if version == 0 else 4)
    inner = _Src(data=data)
    for typ, p, q in _boxes(inner, c.pos, len(data)):
        if typ != b"infe":
            continue
        e = _Cur(data[p:q])
        v = e.uint(1)
        e.take(3)
        if v < 2:
            continue
        item = e.uint(2 if v == 2 else 4)
        e.uint(2)
        if e.take(4) == b"Exif":
            return item
    return None


def _iloc(data):
    """{item id: (construction method, [(offset, length)])}."""
    c = _Cur(data)
    version = c.uint(1)
    if version > 2:
        raise _Bad
    c.take(3)
    x, y = c.uint(1), c.uint(1)
    off_size, len_size, base_size = x >> 4, x & 15, y >> 4
    idx_size = y & 15 if version else 0
    if any(s not in (0, 4, 8) for s in (off_size, len_size, base_size, idx_size)):
        raise _Bad
    count = c.uint(2 if version < 2 else 4)
    if count > 4096:
        raise _Bad
    out = {}
    for _ in range(count):
        item = c.uint(2 if version < 2 else 4)
        method = c.uint(2) & 15 if version else 0
        c.uint(2)
        base = c.uint(base_size)
        n = c.uint(2)
        if n > 64:
            raise _Bad
        extents = []
        for _ in range(n):
            if version and idx_size:
                c.uint(idx_size)
            extents.append((base + c.uint(off_size), c.uint(len_size)))
        out[item] = (method, extents)
    return out


# ---------------------------------------------------------------- MP3

_FRAMES = {b"TPE1": "artist", b"TIT2": "title", b"TLEN": "tlen",
           b"TP1": "artist", b"TT2": "title", b"TLE": "tlen"}
_BITRATES = {
    (1, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_FRAME_ID = re.compile(rb"[A-Z0-9]{3,4}")
_RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 25: (11025, 12000, 8000)}


def _syncsafe(b):
    return (b[0] & 0x7F) << 21 | (b[1] & 0x7F) << 14 | (b[2] & 0x7F) << 7 | (b[3] & 0x7F)


def _id3_header(src):
    """(version, end of the ID3v2 tag, tag flags) or None when there is none."""
    head = src.at(0, 10)
    if len(head) < 10 or head[:3] != b"ID3" or head[3] not in (2, 3, 4):
        return None
    flags = head[5]
    end = 10 + _syncsafe(head[6:10]) + (10 if head[3] == 4 and flags & 0x10 else 0)
    return head[3], end, flags


def _id3_then(src):
    """A file starting with an ID3 tag: an MP3, or a FLAC tagged the wrong way."""
    tag = _id3_header(src)
    if tag is not None and src.at(tag[1], 4) == b"fLaC":
        return _flac(src)
    return _mp3(src)


def _mp3(src):
    out, start = {}, 0
    tag = _id3_header(src)
    if tag is not None:
        version, start, flags = tag
        if flags & 0x80 and version < 4:     # the whole tag is unsynchronised
            body = src.at(10, min(start - 10, MAX_PIECE)).replace(b"\xff\x00", b"\xff")
            out = _id3_frames(_Src(data=body), version, 0, len(body), flags)
        else:
            out = _id3_frames(src, version, 10, min(start, src.size), flags)
    if not (out.get("artist") and out.get("title")):
        v1 = src.at(src.size - 128, 128) if src.size >= 128 else b""
        if v1[:3] == b"TAG":
            out["title"] = out.get("title") or _text(v1[3:33], "latin-1")
            out["artist"] = out.get("artist") or _text(v1[33:63], "latin-1")
    secs, kbps = _mpeg(src, start)
    if not secs and out.get("tlen"):
        digits = "".join(ch for ch in out["tlen"] if ch.isdigit())[:9]
        secs = int(digits) / 1000 if digits else None
    return {"kind": "song", "artist": out.get("artist"), "title": out.get("title"),
            "secs": secs, "format": "MP3", "kbps": kbps, "lossless": False}


def _id3_frames(src, version, pos, end, flags):
    """{artist, title, tlen} from the frames of an ID3v2 tag in [pos, end)."""
    out = {}
    if version > 2 and flags & 0x40:        # an extended header comes first
        size = src.need(pos, 4)
        pos += _syncsafe(size) if version == 4 else 4 + int.from_bytes(size, "big")
    hl = 6 if version == 2 else 10
    for _ in range(256):
        if pos + hl > end or len(out) == 3:
            break
        h = src.need(pos, hl)
        fid = h[:3] if version == 2 else h[:4]
        if version == 2:
            n, fflags = int.from_bytes(h[3:6], "big"), 0
        else:
            n = _syncsafe(h[4:8]) if version == 4 else int.from_bytes(h[4:8], "big")
            fflags = int.from_bytes(h[8:10], "big")
        if not _FRAME_ID.fullmatch(fid):
            break                           # padding, or not a frame
        key = _FRAMES.get(fid)
        if key and key not in out and 1 < n <= 8192 and pos + hl + n <= end:
            data = src.need(pos + hl, n)
            ok = True
            if version == 4:
                ok = not fflags & 0x000C    # compressed or encrypted
                if fflags & 0x0002:
                    data = data.replace(b"\xff\x00", b"\xff")
                if fflags & 0x0001:
                    data = data[4:]
            elif version == 3:
                ok = not fflags & 0x00C0
            if ok and data:
                text = _id3_text(data)
                if text:
                    out[key] = text
        pos += hl + n
    return out


def _id3_text(data):
    enc, raw = data[0], data[1:]
    if enc == 0:
        s = raw.decode("latin-1")
    elif enc == 1:
        s = raw.decode("utf-16", "replace")
    elif enc == 2:
        s = raw.decode("utf-16-be", "replace")
    elif enc == 3:
        s = raw.decode("utf-8", "replace")
    else:
        return ""
    return _text(s)


def _frame(b):
    """(version, layer, kbps, rate, padding, mono, frame bytes, samples per
    frame) of an MPEG audio frame header, or None."""
    if len(b) < 4 or b[0] != 0xFF or b[1] & 0xE0 != 0xE0:
        return None
    vbits, lbits = (b[1] >> 3) & 3, (b[1] >> 1) & 3
    bindex, rindex = b[2] >> 4, (b[2] >> 2) & 3
    if vbits == 1 or lbits == 0 or bindex in (0, 15) or rindex == 3:
        return None
    version = {3: 1, 2: 2, 0: 25}[vbits]
    layer = 4 - lbits
    kbps = _BITRATES[(1 if version == 1 else 2, layer if version == 1 or layer == 1 else 2)][
        bindex - 1]
    rate = _RATES[version][rindex]
    pad = (b[2] >> 1) & 1
    mono = (b[3] >> 6) == 3
    if layer == 1:
        size, spf = (12 * kbps * 1000 // rate + pad) * 4, 384
    elif layer == 3 and version != 1:
        size, spf = 72 * kbps * 1000 // rate + pad, 576
    else:
        size, spf = 144 * kbps * 1000 // rate + pad, 1152
    return version, layer, kbps, rate, pad, mono, size, spf


def _mpeg(src, start):
    """(seconds, kbps) of the MPEG audio from `start`, or (None, None)."""
    buf = src.at(start, HEAD)
    i = 0
    while True:
        i = buf.find(b"\xff", i)
        if i < 0 or i + 4 > len(buf):
            return None, None
        f = _frame(buf[i:i + 4])
        if f is not None:
            j = i + f[6]
            if j + 4 > len(buf) or _frame(buf[j:j + 4]) is not None:
                break
        i += 1
    version, layer, kbps, rate, _, mono, _, spf = f
    frame = buf[i:]
    audio = src.size - start - i
    x = 4 + ((17 if mono else 32) if version == 1 else (9 if mono else 17))
    if frame[x:x + 4] in (b"Xing", b"Info") and len(frame) >= x + 16:
        flags = int.from_bytes(frame[x + 4:x + 8], "big")
        p, frames, nbytes = x + 8, None, None
        if flags & 1:
            frames = int.from_bytes(frame[p:p + 4], "big")
            p += 4
        if flags & 2:
            nbytes = int.from_bytes(frame[p:p + 4], "big")
        if frames:
            secs = frames * spf / rate
            return secs, (nbytes or audio) * 8 / secs / 1000
    if frame[36:40] == b"VBRI" and len(frame) >= 54:
        nbytes = int.from_bytes(frame[46:50], "big")
        frames = int.from_bytes(frame[50:54], "big")
        if frames:
            secs = frames * spf / rate
            return secs, (nbytes or audio) * 8 / secs / 1000
    return audio * 8 / (kbps * 1000), kbps


# ---------------------------------------------------------------- FLAC, Ogg, MP4

def _flac(src):
    off = 0
    tag = _id3_header(src)
    if tag is not None:
        off = tag[1]
    if src.need(off, 4) != b"fLaC":
        raise _Bad
    off += 4
    rate = total = 0
    tags = {}
    for _ in range(128):
        h = src.need(off, 4)
        last, typ, n = h[0] & 0x80, h[0] & 0x7F, int.from_bytes(h[1:4], "big")
        if typ == 0:
            si = src.need(off + 4, 34)
            rate = si[10] << 12 | si[11] << 4 | si[12] >> 4
            total = (si[13] & 0x0F) << 32 | int.from_bytes(si[14:18], "big")
        elif typ == 4:
            tags = _comments(src.at(off + 4, min(n, 1 << 18)), 0)
        elif typ == 127:
            raise _Bad
        off += 4 + n
        if last:
            break
    secs = total / rate if rate and total else None
    return {"kind": "song", "artist": tags.get("artist"), "title": tags.get("title"),
            "secs": secs, "format": "FLAC",
            "kbps": src.size * 8 / secs / 1000 if secs else None, "lossless": True}


def _comments(data, pos):
    """{artist, title} from a Vorbis comment block; a cut-off end is fine."""
    c = _Cur(data, pos)
    out = {}
    try:
        c.take(c.uint(4, "little"))
        n = c.uint(4, "little")
        for _ in range(min(n, 10000)):
            entry = c.take(c.uint(4, "little"))
            key, _, value = entry.partition(b"=")
            key = key.decode("ascii", "replace").upper()
            if key in ("ARTIST", "TITLE") and key.lower() not in out:
                out[key.lower()] = _text(value)
            if len(out) == 2:
                break
    except _Bad:
        pass
    return out


def _ogg(src):
    packets, serial = _ogg_packets(src)
    first, second = packets
    if first[:7] == b"\x01vorbis" and second[:7] == b"\x03vorbis":
        rate, skip, fmt, at = struct.unpack_from("<I", first, 12)[0], 0, "Vorbis", 7
    elif first[:8] == b"OpusHead" and second[:8] == b"OpusTags":
        rate, skip, fmt, at = 48000, struct.unpack_from("<H", first, 10)[0], "Opus", 8
    else:
        raise _Bad
    tags = _comments(second, at)
    granule = _ogg_last_granule(src, serial)
    secs = (granule - skip) / rate if granule and rate and granule > skip else None
    return {"kind": "song", "artist": tags.get("artist"), "title": tags.get("title"),
            "secs": secs, "format": fmt,
            "kbps": src.size * 8 / secs / 1000 if secs else None, "lossless": False}


def _ogg_packets(src, want=2, budget=1 << 18):
    """The first `want` packets of the first stream (each cut at `budget`
    bytes), and its serial number."""
    off, packets, cur, serial = 0, [], bytearray(), None
    for _ in range(128):
        head = src.at(off, 27 + 255)
        if len(head) < 27 or head[:4] != b"OggS" or head[4] != 0:
            raise _Bad
        nseg = head[26]
        segs = head[27:27 + nseg]
        if len(segs) != nseg:
            raise _Bad
        s = struct.unpack_from("<I", head, 14)[0]
        if serial is None:
            serial = s
        body = src.need(off + 27 + nseg, sum(segs))
        if s == serial:
            pos = 0
            for n in segs:
                cur += body[pos:pos + n]
                pos += n
                if n < 255 or len(cur) >= budget:
                    packets.append(bytes(cur[:budget]))
                    cur = bytearray()
                    if len(packets) == want:
                        return packets, serial
                    if n == 255:        # the rest of a packet cut short
                        raise _Bad
        off += 27 + nseg + sum(segs)
    raise _Bad


def _ogg_last_granule(src, serial):
    start = max(0, src.size - HEAD)
    tail = src.at(start, src.size - start)
    i = len(tail)
    while True:
        i = tail.rfind(b"OggS", 0, i)
        if i < 0:
            return None
        if i + 27 > len(tail):
            continue
        granule = struct.unpack_from("<q", tail, i + 6)[0]
        if struct.unpack_from("<I", tail, i + 14)[0] == serial and granule > 0:
            return granule


def _mp4(src):
    moov = _find(src, 0, src.size, b"moov")
    if moov is None:
        return None
    secs, tags, codec = None, {}, None
    for typ, a, b in _boxes(src, *moov):
        if typ == b"mvhd":
            version = src.need(a, 1)[0]
            if version == 1:
                scale, length = struct.unpack(">IQ", src.need(a + 20, 12))
            else:
                scale, length = struct.unpack(">II", src.need(a + 12, 8))
            secs = length / scale if scale else None
        elif typ == b"udta":
            meta = _find(src, a, b, b"meta")
            if meta:
                p, q = meta
                if src.need(p + 4, 4) != b"hdlr":   # a full box (QuickTime's isn't)
                    p += 4
                ilst = _find(src, p, q, b"ilst")
                if ilst:
                    tags = _ilst(src, *ilst)
        elif typ == b"trak" and codec is None:
            codec = _codec(src, a, b)
    lossless = codec in (b"alac", b"fLaC")
    fmt = {b"alac": "ALAC", b"fLaC": "FLAC", b"mp4a": "AAC", b"Opus": "Opus"}.get(codec, "M4A")
    return {"kind": "song", "artist": tags.get("artist"), "title": tags.get("title"),
            "secs": secs, "format": fmt,
            "kbps": src.size * 8 / secs / 1000 if secs else None, "lossless": lossless}


def _ilst(src, a, b):
    out = {}
    for typ, p, q in _boxes(src, a, b):
        key = {b"\xa9ART": "artist", b"\xa9nam": "title"}.get(typ)
        if key is None or key in out:
            continue
        data = _find(src, p, q, b"data")
        if data and data[1] - data[0] > 8:
            out[key] = _text(src.need(data[0] + 8, min(data[1] - data[0] - 8, 4096)))
    return out


def _codec(src, a, b):
    """The sample entry type of an audio track (b'mp4a', b'alac', ...)."""
    mdia = _find(src, a, b, b"mdia")
    if not mdia:
        return None
    hdlr = _find(src, *mdia, b"hdlr")
    if not hdlr or src.need(hdlr[0] + 8, 4) != b"soun":
        return None
    minf = _find(src, *mdia, b"minf")
    stbl = minf and _find(src, *minf, b"stbl")
    stsd = stbl and _find(src, *stbl, b"stsd")
    if not stsd:
        return None
    return src.need(stsd[0] + 12, 4)


# ---------------------------------------------------------------- matching

def key_of(t):
    """What look-alikes have in common, or None."""
    if t.get("kind") == "photo":
        w, h = t["w"], t["h"]
        return ("photo", t["taken"], _norm(t.get("make")), _norm(t.get("model")),
                max(w, h), min(w, h))
    if t.get("kind") == "song":
        return ("song", _norm(t["artist"]), _norm(t["title"]))
    return None


def group(items):
    """[(file, tags)] -> [[file, ...]]: the sets of files that look alike,
    two or more each. Photos taken in the same second are told apart by the
    fraction of a second when they have it; songs by their length."""
    by = defaultdict(list)
    for f, t in items:
        k = key_of(t)
        if k is not None:
            by[k].append((f, t))
    out = []
    for k, members in by.items():
        if len(members) < 2:
            continue
        split = _by_fraction(members) if k[0] == "photo" else _by_length(members)
        out.extend([f for f, _ in s] for s in split if len(s) > 1)
    return out


def _by_fraction(members):
    subs = {t.get("sub") for _, t in members if t.get("sub")}
    if len(subs) <= 1:
        return [members]
    # Several shots in one second: keep each fraction apart, and leave out
    # files without one (they could be any of them).
    by = defaultdict(list)
    for f, t in members:
        if t.get("sub"):
            by[t["sub"]].append((f, t))
    return list(by.values())


def _by_length(members):
    members = sorted(members, key=lambda x: x[1]["secs"])
    out, cur = [], []
    for f, t in members:
        if cur and t["secs"] - cur[0][1]["secs"] > LENGTH_SLACK:
            out.append(cur)
            cur = []
        cur.append((f, t))
    out.append(cur)
    return out


def encode(t):
    """Tags as the cache stores them ('' for none)."""
    return "" if t is None else json.dumps(t, separators=(",", ":"), ensure_ascii=False)


def decode(text):
    """Tags from the cache; None for none, or for anything odd."""
    if not text:
        return None
    try:
        t = json.loads(text)
    except ValueError:
        return None
    try:
        return _valid(t) if isinstance(t, dict) else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def describe(t):
    """What a file is, for the reports: '4032×3024 JPEG' or 'MP3 · 320 kbps · 3:35'."""
    if t.get("kind") == "photo":
        return f"{t['w']}×{t['h']} {t.get('format') or ''}".strip()
    bits = [t.get("format") or ""]
    if t.get("lossless"):
        bits.append("lossless")
    elif t.get("kbps"):
        bits.append(f"{t['kbps']} kbps")
    secs = int(round(t["secs"]))
    bits.append(f"{secs // 60}:{secs % 60:02d}")
    return " · ".join(b for b in bits if b)


def quality(t, size):
    """Bigger is better: for picking which version to point out as best."""
    if t.get("kind") == "song":
        return (bool(t.get("lossless")), t.get("kbps") or 0, size)
    raw = t.get("format") not in ("JPEG", "PNG", "WebP", "HEIC", "AVIF")
    return (raw, t["w"] * t["h"], size)
