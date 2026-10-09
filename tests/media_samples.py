"""
Small but well-formed photo and song files for the look-alike tests, built
byte by byte: only the headers the readers look at are real; the picture or
sound itself is filler.
"""

import struct

# ---------------------------------------------------------------- photos


def tiff(make="Canon", model="EOS 80D", taken="2019:06:12 10:15:30", sub=None,
         pixel=None, dims=None, order="<", sub_ifd_dims=None, magic=42):
    """A TIFF structure as EXIF holds it: IFD0 (make, model, optional width
    and height, a pointer to the EXIF IFD, optional SubIFDs) and the EXIF IFD
    (date taken, fraction of a second, pixel size)."""
    e = order
    mark = b"II" if e == "<" else b"MM"

    def ascii(s):
        return s.encode() + b"\0"

    ifd0 = []
    if dims:
        ifd0 += [(0x100, 4, 1, struct.pack(e + "I", dims[0])),
                 (0x101, 4, 1, struct.pack(e + "I", dims[1]))]
    if make is not None:
        ifd0.append((0x10F, 2, len(ascii(make)), ascii(make)))
    if model is not None:
        ifd0.append((0x110, 2, len(ascii(model)), ascii(model)))
    exif = []
    if taken is not None:
        exif.append((0x9003, 2, len(ascii(taken)), ascii(taken)))
    if sub is not None:
        exif.append((0x9291, 2, len(ascii(sub)), ascii(sub)))
    if pixel:
        exif += [(0xA002, 3, 1, struct.pack(e + "H", pixel[0]) + b"\0\0"),
                 (0xA003, 3, 1, struct.pack(e + "H", pixel[1]) + b"\0\0")]
    subs = []
    if sub_ifd_dims:
        subs = [[(0x100, 4, 1, struct.pack(e + "I", w)), (0x101, 4, 1, struct.pack(e + "I", h))]
                for w, h in sub_ifd_dims]

    # Layout: header, IFD0, EXIF IFD, SubIFDs, then the values that don't fit.
    def pack(n):
        return struct.pack(e + "I", n)

    n0 = len(ifd0) + 1 + (1 if subs else 0)
    pos = 8 + 2 + 12 * n0 + 4
    off_exif = pos
    pos += 2 + 12 * len(exif) + 4
    off_subs = []
    for entries in subs:
        off_subs.append(pos)
        pos += 2 + 12 * len(entries) + 4
    data = bytearray()
    data_start = pos

    def place(value):
        off = data_start + len(data)
        data.extend(value)
        if len(data) & 1:
            data.append(0)
        return off

    def build(entries):
        rows = []
        for tag, typ, count, value in sorted(entries):
            if len(value) <= 4:
                rows.append(struct.pack(e + "HHI", tag, typ, count) + value.ljust(4, b"\0"))
            else:
                rows.append(struct.pack(e + "HHII", tag, typ, count, place(value)))
        return struct.pack(e + "H", len(rows)) + b"".join(rows) + pack(0)

    extra = [(0x8769, 4, 1, pack(off_exif))]
    if subs:
        extra.append((0x14A, 4, len(subs), b"".join(pack(o) for o in off_subs)))
    body = build(ifd0 + extra) + build(exif) + b"".join(build(x) for x in subs)
    return mark + struct.pack(e + "HI", magic, 8) + body + bytes(data)


def jpeg(w=6000, h=4000, exif=None, body=b"\x55" * 2000, xmp_first=False):
    seg = b""
    if xmp_first:
        x = b"http://ns.adobe.com/xap/1.0/\0<x/>"
        seg += b"\xff\xe1" + struct.pack(">H", len(x) + 2) + x
    if exif is not None:
        payload = b"Exif\0\0" + exif
        seg += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    app0 = b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    dqt = b"\0" + bytes(64)
    sof = b"\x08" + struct.pack(">HH", h, w) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0 + seg
            + b"\xff\xdb" + struct.pack(">H", len(dqt) + 2) + dqt
            + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
            + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + body + b"\xff\xd9")


def png(w=1920, h=1080, exif=None, exif_prefix=False, body=b"\0" * 500):
    def chunk(typ, data):
        return struct.pack(">I", len(data)) + typ + data + b"\0\0\0\0"
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    if exif is not None:
        out += chunk(b"eXIf", (b"Exif\0\0" if exif_prefix else b"") + exif)
    return out + chunk(b"IDAT", body) + chunk(b"IEND", b"")


def webp(w=1600, h=1200, exif=None, kind="VP8X"):
    def chunk(typ, data):
        return typ + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")
    if kind == "VP8X":
        body = chunk(b"VP8X", bytes([0x08, 0, 0, 0]) + (w - 1).to_bytes(3, "little")
                     + (h - 1).to_bytes(3, "little"))
        body += chunk(b"VP8 ", b"\0\0\0\x9d\x01\x2a" + struct.pack("<HH", w, h) + bytes(20))
    elif kind == "VP8L":
        bits = (w - 1) | (h - 1) << 14
        body = chunk(b"VP8L", b"\x2f" + struct.pack("<I", bits) + bytes(20))
    else:
        body = chunk(b"VP8 ", b"\0\0\0\x9d\x01\x2a" + struct.pack("<HH", w, h) + bytes(20))
    if exif is not None:
        body += chunk(b"EXIF", exif)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def _box(typ, data):
    return struct.pack(">I", 8 + len(data)) + typ + data


def _full(typ, data, version=0, flags=0):
    return _box(typ, bytes([version]) + flags.to_bytes(3, "big") + data)


def heic(w=4032, h=3024, exif=None, brand=b"heic", thumb=(320, 240), iloc_version=1,
         infe_version=2):
    """An HEIF file: an image item and an Exif item whose bytes sit in mdat."""
    ftyp = _box(b"ftyp", brand + b"\0\0\0\0" + b"mif1" + brand)
    hdlr = _full(b"hdlr", b"\0\0\0\0pict" + bytes(12) + b"\0")
    pitm = _full(b"pitm", struct.pack(">H", 1))

    def infe(item, typ):
        id_bytes = struct.pack(">H" if infe_version == 2 else ">I", item)
        return _full(b"infe", id_bytes + b"\0\0" + typ + b"\0", version=infe_version)
    items = [infe(1, b"hvc1")]
    if exif is not None:
        items.append(infe(2, b"Exif"))
    iinf = _full(b"iinf", struct.pack(">H", len(items)) + b"".join(items))
    ispes = [_full(b"ispe", struct.pack(">II", w, h))]
    if thumb:
        ispes.insert(0, _full(b"ispe", struct.pack(">II", *thumb)))
    iprp = _box(b"iprp", _box(b"ipco", b"".join(ispes)))
    exif_item = (b"\0\0\0\x06" + b"Exif\0\0" + exif) if exif is not None else b""
    image = b"\x42" * 300

    def build_iloc(base):
        # offset_size 4, length_size 4, base_offset_size 0, index_size 0
        rows = []
        for item, off, n in ((1, base, len(image)), (2, base + len(image), len(exif_item))):
            if item == 2 and exif is None:
                continue
            wide = ">I" if iloc_version == 2 else ">H"
            row = struct.pack(wide, item)
            if iloc_version:
                row += struct.pack(">H", 0)
            row += struct.pack(">H", 0) + struct.pack(">H", 1) + struct.pack(">II", off, n)
            rows.append(row)
        return _full(b"iloc", bytes([0x44, 0x00]) + struct.pack(wide, len(rows)) + b"".join(rows),
                     version=iloc_version)

    def meta(base):
        return _full(b"meta", hdlr + pitm + iinf + build_iloc(base) + iprp)
    size = len(ftyp) + len(meta(0)) + 8
    return ftyp + meta(size) + _box(b"mdat", image + exif_item)


# ---------------------------------------------------------------- songs

_KBPS_INDEX = {32: 1, 40: 2, 48: 3, 56: 4, 64: 5, 80: 6, 96: 7, 112: 8, 128: 9, 160: 10,
               192: 11, 224: 12, 256: 13, 320: 14}


def _syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3(artist="Artist", title="Title", version=3, tlen=None, picture=0, encoding=3,
        unsync=False, padding=64, footer=False):
    def text(s):
        if encoding == 0:
            return b"\0" + s.encode("latin-1")
        if encoding == 1:
            return b"\1" + s.encode("utf-16")
        if encoding == 2:
            return b"\2" + s.encode("utf-16-be")
        return b"\3" + s.encode() + b"\0"

    frames = []
    names = ({"artist": b"TP1", "title": b"TT2", "tlen": b"TLE", "pic": b"PIC"} if version == 2
             else {"artist": b"TPE1", "title": b"TIT2", "tlen": b"TLEN", "pic": b"APIC"})
    if picture:
        frames.append((names["pic"], b"\0image/jpeg\0\3\0" + b"\xff" * picture))
    if artist is not None:
        frames.append((names["artist"], text(artist)))
    if title is not None:
        frames.append((names["title"], text(title)))
    if tlen is not None:
        frames.append((names["tlen"], b"\0" + str(tlen).encode()))
    body = b""
    for fid, data in frames:
        if version == 2:
            body += fid + len(data).to_bytes(3, "big") + data
        elif version == 3:
            body += fid + struct.pack(">I", len(data)) + b"\0\0" + data
        else:
            body += fid + _syncsafe(len(data)) + b"\0\0" + data
    if unsync:
        body = body.replace(b"\xff", b"\xff\x00")
    body += b"\0" * padding
    flags = (0x80 if unsync else 0) | (0x10 if footer else 0)
    out = b"ID3" + bytes([version, 0, flags]) + _syncsafe(len(body)) + body
    if footer:
        out += b"3DI" + bytes([version, 0, flags]) + _syncsafe(len(body))
    return out


def mp3(artist="Artist", title="Title", secs=10.0, kbps=128, tag_version=3, vbr=False,
        v1=False, mono=False, picture=0, tlen=None, tag=True, **tag_args):
    """MPEG-1 layer III at 44.1 kHz: an ID3v2 tag, then frames (a Xing frame
    first when vbr), then an ID3v1 tag when v1."""
    rate, spf = 44100, 1152
    head = bytes([0xFF, 0xFB, _KBPS_INDEX[kbps] << 4, 0xC0 if mono else 0x00])
    size = 144 * kbps * 1000 // rate
    out = id3(artist, title, tag_version, tlen, picture, **tag_args) if tag else b""
    if vbr:
        frames = int(round(secs * rate / spf))
        x = 4 + (17 if mono else 32)
        first = bytearray(head + bytes(size - 4))
        first[x:x + 16] = b"Xing" + struct.pack(">III", 3, frames, frames * size)
        out += bytes(first) + (head + bytes(size - 4)) * 4
    else:
        n = max(2, int(round(secs * kbps * 1000 / 8 / size)))
        out += (head + bytes(size - 4)) * n
    if v1:
        out += (b"TAG" + (title or "").encode("latin-1")[:30].ljust(30, b"\0")
                + (artist or "").encode("latin-1")[:30].ljust(30, b"\0") + bytes(65))
    return out


def _vorbis_comments(artist, title, extra=()):
    entries = []
    if artist is not None:
        entries.append(b"ARTIST=" + artist.encode())
    if title is not None:
        entries.append(b"TITLE=" + title.encode())
    entries += list(extra)
    vendor = b"test"
    return (struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(entries))
            + b"".join(struct.pack("<I", len(x)) + x for x in entries))


def flac(artist="Artist", title="Title", secs=10.0, rate=44100, picture=0, id3_first=False):
    total = int(secs * rate)
    si = bytearray(34)
    si[0:2] = struct.pack(">H", 4096)
    si[2:4] = struct.pack(">H", 4096)
    v = rate << 44 | 1 << 41 | 15 << 36 | total     # rate, 2 channels, 16 bits, samples
    si[10:18] = v.to_bytes(8, "big")
    blocks = [(0, bytes(si)), (4, _vorbis_comments(artist, title))]
    if picture:
        blocks.append((6, b"\0" * picture))
    out = b"fLaC"
    for i, (typ, data) in enumerate(blocks):
        last = 0x80 if i == len(blocks) - 1 else 0
        out += bytes([last | typ]) + len(data).to_bytes(3, "big") + data
    out += b"\xff\xf8" + bytes(2000)
    if id3_first:
        out = id3(artist, title) + out
    return out


def m4a(artist="Artist", title="Title", secs=10.0, codec=b"mp4a", quicktime_meta=False,
        version=0, scale=44100):
    if version == 1:
        mvhd = _full(b"mvhd", bytes(16) + struct.pack(">IQ", scale, int(secs * scale)) + bytes(80),
                     version=1)
    else:
        mvhd = _full(b"mvhd", bytes(8) + struct.pack(">II", scale, int(secs * scale)) + bytes(80))
    hdlr = _full(b"hdlr", b"\0\0\0\0soun" + bytes(12) + b"\0")
    entry = _box(codec, bytes(28))
    stsd = _full(b"stsd", struct.pack(">I", 1) + entry)
    trak = _box(b"trak", _box(b"tkhd", bytes(84))
                + _box(b"mdia", _full(b"mdhd", bytes(20)) + hdlr
                       + _box(b"minf", _box(b"stbl", stsd))))

    def item(typ, s):
        return _box(typ, _box(b"data", struct.pack(">II", 1, 0) + s.encode()))
    items = b""
    if artist is not None:
        items += item(b"\xa9ART", artist)
    if title is not None:
        items += item(b"\xa9nam", title)
    meta_hdlr = _full(b"hdlr", b"\0\0\0\0mdirappl" + bytes(9))
    inner = meta_hdlr + _box(b"ilst", items)
    meta = _box(b"meta", inner) if quicktime_meta else _full(b"meta", inner)
    moov = _box(b"moov", mvhd + trak + _box(b"udta", meta))
    return (_box(b"ftyp", b"M4A \0\0\0\0M4A mp42isom") + _box(b"mdat", b"\x21" * 3000)
            + moov)


def _page(serial, seq, packets, granule=0, kind=0, continued_last=False):
    segs, body = [], b""
    for i, p in enumerate(packets):
        n = len(p)
        segs += [255] * (n // 255)
        if not (continued_last and i == len(packets) - 1):
            segs.append(n % 255)
        body += p
    return (b"OggS" + bytes([0, kind]) + struct.pack("<qIII", granule, serial, seq, 0)
            + bytes([len(segs)]) + bytes(segs) + body)


def ogg(artist="Artist", title="Title", secs=10.0, codec="vorbis", serial=0x1234,
        picture=0):
    if codec == "opus":
        rate, skip = 48000, 312
        head = b"OpusHead" + bytes([1, 2]) + struct.pack("<HIhB", skip, 44100, 0, 0)
        tags = b"OpusTags" + _vorbis_comments(artist, title)
        pages = [_page(serial, 0, [head], kind=2), _page(serial, 1, [tags])]
        end = int(secs * rate) + skip
    else:
        rate = 44100
        head = (b"\x01vorbis" + struct.pack("<IBIiii", 0, 2, rate, 0, 128000, 0)
                + b"\xb8\x01")
        extra = [b"METADATA_BLOCK_PICTURE=" + b"A" * picture] if picture else []
        tags = b"\x03vorbis" + _vorbis_comments(artist, title, extra) + b"\x01"
        setup = b"\x05vorbis" + bytes(100)
        pages = [_page(serial, 0, [head], kind=2)]
        # The comment packet may span pages: split it in 255*255-byte pieces.
        packet = tags
        seq = 1
        while len(packet) > 255 * 255:
            pages.append(_page(serial, seq, [packet[:255 * 255]], continued_last=True,
                               kind=1 if seq > 1 else 0))
            packet = packet[255 * 255:]
            seq += 1
        pages.append(_page(serial, seq, [packet, setup], kind=1 if seq > 1 else 0))
        end = int(secs * rate)
    audio = [_page(serial, 10 + i, [b"\x11" * 900], granule=end * (i + 1) // 4)
             for i in range(4)]
    return b"".join(pages) + b"".join(audio[:-1]) + audio[-1][:5] + bytes([4]) + audio[-1][6:]
