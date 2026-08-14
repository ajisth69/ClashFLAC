"""Pure-Python remux — FLAC: lifts the `dfLa` metadata blocks out of the
decrypted fragmented MP4 into a native FLAC metadata section (STREAMINFO first,
last-block flag fixed up) and concatenates every sample (one coded FLAC frame).
Opus: rebuilds the OpusHead from the `dOps` box and re-frames each sample (one
Opus packet) into Ogg pages (proper lacing, Ogg CRC32, 48 kHz granule
positions). MP4 (spatial E-AC-3/MPEG-H): flattens the fragmented MP4 into a
plain MP4 — keeps the codec `stsd`, rebuilds the `stts`/`stsc`/`stsz`/`stco`
sample tables from the fragments into one chunk, patches the movie/track/media
durations, and drops `mvex`. AC-4: writes the raw `.ac4` elementary stream,
wrapping each sample in an `0xAC40` sync frame (syncword + frame size). All
reuse the MP4 box/sample parsing from `mp4`."""

import struct

from amzdl.remux.mp4 import (
    AUDIO_SAMPLE_ENTRY_HEADER,
    find_box,
    find_path,
    iter_boxes,
    parse_tfhd,
    parse_trun,
)

_FLAC_MAGIC = b"fLaC"
_STREAMINFO = 0

_OGG_BOS = 0x02
_OGG_EOS = 0x04
_OGG_SERIAL = 0x0A2D4C00


class RemuxError(Exception):
    pass


def _codec_box(buf, moov_content, moov_end, box_type):
    stsd = find_path(
        buf, moov_content, moov_end,
        b"trak", b"mdia", b"minf", b"stbl", b"stsd",
    )
    if stsd is None:
        raise RemuxError("no moov/trak/mdia/minf/stbl/stsd box")
    for _bt, entry_start, _ec, entry_end in iter_boxes(buf, stsd[1] + 8, stsd[2]):
        child_start = entry_start + AUDIO_SAMPLE_ENTRY_HEADER
        if child_start >= entry_end:
            continue
        box = find_box(buf, child_start, entry_end, box_type)
        if box is not None:
            return box
    return None


def _iter_traf_samples(buf, traf_content, traf_end, moof_start):
    tfhd = find_box(buf, traf_content, traf_end, b"tfhd")
    if tfhd is None:
        return
    base, default_dur, default_size = parse_tfhd(buf, tfhd[1])
    if base is None:
        base = moof_start
    for btype, _bs, b_content, _be in iter_boxes(buf, traf_content, traf_end):
        if btype != b"trun":
            continue
        samples, data_offset = parse_trun(buf, b_content, default_dur, default_size)
        pos = base + (data_offset or 0)
        for size, dur in samples:
            yield pos, size, dur
            pos += size


def _iter_samples(buf, end):
    pending_moof = None
    for btype, box_start, content_start, box_end in iter_boxes(buf, 0, end):
        if btype == b"moof":
            pending_moof = (box_start, content_start, box_end)
        elif btype == b"mdat" and pending_moof is not None:
            moof_start, moof_content, moof_end = pending_moof
            for tb, _ts, tc, te in iter_boxes(buf, moof_content, moof_end):
                if tb == b"traf":
                    yield from _iter_traf_samples(buf, tc, te, moof_start)
            pending_moof = None


def _read_mp4(src_mp4):
    with open(src_mp4, "rb") as f:
        buf = f.read()
    end = len(buf)
    moov = find_box(buf, 0, end, b"moov")
    if moov is None:
        raise RemuxError("no moov box found")
    return buf, end, moov


def _normalize_metadata(blocks):
    parsed = []
    off = 0
    n = len(blocks)
    while off + 4 <= n:
        btype = blocks[off] & 0x7F
        length = int.from_bytes(blocks[off + 1 : off + 4], "big")
        parsed.append((btype, bytes(blocks[off + 4 : off + 4 + length])))
        off += 4 + length
    if not parsed or parsed[0][0] != _STREAMINFO:
        raise RemuxError("dfLa box missing STREAMINFO")
    out = bytearray()
    for i, (btype, data) in enumerate(parsed):
        last = 0x80 if i == len(parsed) - 1 else 0
        out.append(last | btype)
        out += len(data).to_bytes(3, "big")
        out += data
    return bytes(out)


def remux_flac(src_mp4, dst):
    buf, end, moov = _read_mp4(src_mp4)
    dfla = _codec_box(buf, moov[1], moov[2], b"dfLa")
    if dfla is None:
        raise RemuxError("no dfLa box found (track is not FLAC)")
    metadata = _normalize_metadata(buf[dfla[1] + 4 : dfla[2]])
    written = 0
    with open(dst, "wb") as out:
        out.write(_FLAC_MAGIC)
        out.write(metadata)
        for pos, size, _dur in _iter_samples(buf, end):
            out.write(buf[pos : pos + size])
            written += 1
    if written == 0:
        raise RemuxError("no audio samples found")
    return dst


def _opus_head(dops):
    if len(dops) < 11:
        raise RemuxError("dOps box too short")
    channels = dops[1]
    preskip = int.from_bytes(dops[2:4], "big")
    input_rate = int.from_bytes(dops[4:8], "big")
    gain = int.from_bytes(dops[8:10], "big", signed=True)
    mapping_family = dops[10]
    out = bytearray(b"OpusHead")
    out.append(1)
    out.append(channels)
    out += struct.pack("<H", preskip)
    out += struct.pack("<I", input_rate)
    out += struct.pack("<h", gain)
    out.append(mapping_family)
    if mapping_family != 0:
        out += dops[11 : 11 + 2 + channels]
    return bytes(out), preskip


def _opus_tags():
    vendor = b"amzdl"
    return b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)


def _ogg_crc_table():
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if (r & 0x80000000) else (r << 1)
            r &= 0xFFFFFFFF
        table.append(r)
    return tuple(table)


_CRC_TABLE = _ogg_crc_table()


def _ogg_crc(data):
    crc = 0
    for b in data:
        crc = (((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ b])
    return crc


def _ogg_page(seq, granule, header_type, packets):
    segtable = bytearray()
    body = bytearray()
    for pkt in packets:
        for _ in range(len(pkt) // 255):
            segtable.append(255)
        segtable.append(len(pkt) % 255)
        body += pkt
    if len(segtable) > 255:
        raise RemuxError("page segment table overflow")
    page = bytearray(b"OggS")
    page.append(0)
    page.append(header_type)
    page += struct.pack("<q", granule)
    page += struct.pack("<I", _OGG_SERIAL)
    page += struct.pack("<I", seq)
    page += struct.pack("<I", 0)
    page.append(len(segtable))
    page += segtable
    page += body
    crc = _ogg_crc(page)
    page[22:26] = struct.pack("<I", crc)
    return bytes(page)


def _write_ogg_opus(out, head, preskip, samples):
    out.write(_ogg_page(0, 0, _OGG_BOS, [head]))
    out.write(_ogg_page(1, 0, 0, [_opus_tags()]))
    seq = 2
    granule = preskip
    page_pkts = []
    page_segs = 0
    page_granule = granule
    for pkt, dur in samples:
        nseg = len(pkt) // 255 + 1
        if page_pkts and page_segs + nseg > 255:
            out.write(_ogg_page(seq, page_granule, 0, page_pkts))
            seq += 1
            page_pkts = []
            page_segs = 0
        page_pkts.append(pkt)
        page_segs += nseg
        granule += dur
        page_granule = granule
    out.write(_ogg_page(seq, page_granule, _OGG_EOS, page_pkts))


def remux_opus(src_mp4, dst):
    buf, end, moov = _read_mp4(src_mp4)
    dops = _codec_box(buf, moov[1], moov[2], b"dOps")
    if dops is None:
        raise RemuxError("no dOps box found (track is not Opus)")
    head, preskip = _opus_head(buf[dops[1] : dops[2]])
    samples = [
        (buf[pos : pos + size], dur) for pos, size, dur in _iter_samples(buf, end)
    ]
    if not samples:
        raise RemuxError("no audio samples found")
    with open(dst, "wb") as out:
        _write_ogg_opus(out, head, preskip, samples)
    return dst


def _box(box_type, content):
    return struct.pack(">I", len(content) + 8) + box_type + content


def _full_box(box_type, version, flags, content):
    return _box(box_type, bytes([version]) + flags.to_bytes(3, "big") + content)


_MP4_FTYP = _box(
    b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isom" + b"iso2" + b"mp41"
)


def _rebuild(buf, content_start, end, transforms, drop=()):
    out = bytearray()
    for btype, bs, _bc, be in iter_boxes(buf, content_start, end):
        if btype in drop:
            continue
        repl = transforms.get(btype)
        out += repl if repl is not None else buf[bs:be]
    return bytes(out)


def _box_timescale(box):
    return struct.unpack(">I", box[20:24] if box[8] == 0 else box[28:32])[0]


def _patch_duration(box, off_v0, off_v1, duration):
    data = bytearray(box)
    if data[8] == 0:
        struct.pack_into(">I", data, off_v0, duration)
    else:
        struct.pack_into(">Q", data, off_v1, duration)
    return bytes(data)


def _build_stts(durs):
    entries = []
    for d in durs:
        if entries and entries[-1][1] == d:
            entries[-1][0] += 1
        else:
            entries.append([1, d])
    content = struct.pack(">I", len(entries))
    for count, delta in entries:
        content += struct.pack(">II", count, delta)
    return _full_box(b"stts", 0, 0, content)


def _build_stsz(sizes):
    content = struct.pack(">II", 0, len(sizes)) + b"".join(
        struct.pack(">I", s) for s in sizes
    )
    return _full_box(b"stsz", 0, 0, content)


_CODEC_CONFIG_BOXES = (b"mhaC", b"dfLa", b"dOps", b"dec3", b"dac3", b"dac4", b"esds")


def _clean_sample_entry(buf, entry_start, entry_end):
    box_type = bytes(buf[entry_start + 4 : entry_start + 8])
    audio_fields = bytes(buf[entry_start + 8 : entry_start + AUDIO_SAMPLE_ENTRY_HEADER])
    children = bytearray()
    has_config = False
    for btype, bs, _bc, be in iter_boxes(
        buf, entry_start + AUDIO_SAMPLE_ENTRY_HEADER, entry_end
    ):
        if btype == b"free":
            continue
        children += buf[bs:be]
        if btype in _CODEC_CONFIG_BOXES:
            has_config = True
    content = audio_fields + bytes(children)
    return _box(box_type, content), has_config


def _build_stsd(buf, stsd):
    for _bt, entry_start, _ec, entry_end in iter_boxes(buf, stsd[1] + 8, stsd[2]):
        if entry_start + AUDIO_SAMPLE_ENTRY_HEADER >= entry_end:
            continue
        entry, has_config = _clean_sample_entry(buf, entry_start, entry_end)
        if has_config:
            return _full_box(b"stsd", 0, 0, struct.pack(">I", 1) + entry)
    raise RemuxError("no codec-config sample entry in stsd")


def remux_mp4(src_mp4, dst):
    buf, end, moov = _read_mp4(src_mp4)
    mvhd = find_box(buf, moov[1], moov[2], b"mvhd")
    trak = find_box(buf, moov[1], moov[2], b"trak")
    if mvhd is None or trak is None:
        raise RemuxError("no mvhd/trak in moov")
    mdia = find_box(buf, trak[1], trak[2], b"mdia")
    tkhd = find_box(buf, trak[1], trak[2], b"tkhd")
    minf = find_box(buf, mdia[1], mdia[2], b"minf") if mdia else None
    mdhd = find_box(buf, mdia[1], mdia[2], b"mdhd") if mdia else None
    stbl = find_box(buf, minf[1], minf[2], b"stbl") if minf else None
    stsd = find_box(buf, stbl[1], stbl[2], b"stsd") if stbl else None
    if not all((mdia, tkhd, minf, mdhd, stbl, stsd)):
        raise RemuxError("incomplete moov structure for mp4 flatten")

    sizes = []
    durs = []
    for _pos, size, dur in _iter_samples(buf, end):
        sizes.append(size)
        durs.append(dur)
    if not sizes:
        raise RemuxError("no audio samples found")

    media_dur = sum(durs)
    movie_ts = _box_timescale(buf[mvhd[0] : mvhd[2]])
    media_ts = _box_timescale(buf[mdhd[0] : mdhd[2]])
    movie_dur = round(media_dur * movie_ts / media_ts) if media_ts else media_dur

    stbl_content = (
        _build_stsd(buf, stsd)
        + _build_stts(durs)
        + _full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(sizes), 1))
        + _build_stsz(sizes)
        + _full_box(b"stco", 0, 0, struct.pack(">II", 1, 0))
    )
    new_stbl = _box(b"stbl", stbl_content)
    new_minf = _box(b"minf", _rebuild(buf, minf[1], minf[2], {b"stbl": new_stbl}))
    new_mdia = _box(b"mdia", _rebuild(buf, mdia[1], mdia[2], {
        b"minf": new_minf,
        b"mdhd": _patch_duration(buf[mdhd[0] : mdhd[2]], 24, 32, media_dur),
    }))
    new_trak = _box(b"trak", _rebuild(buf, trak[1], trak[2], {
        b"mdia": new_mdia,
        b"tkhd": _patch_duration(buf[tkhd[0] : tkhd[2]], 28, 36, movie_dur),
    }))
    new_moov = bytearray(_box(b"moov", _rebuild(buf, moov[1], moov[2], {
        b"mvhd": _patch_duration(buf[mvhd[0] : mvhd[2]], 24, 32, movie_dur),
        b"trak": new_trak,
    }, drop=(b"mvex", b"pssh"))))

    mdat_offset = len(_MP4_FTYP) + len(new_moov) + 8
    stco_off = new_moov.rfind(b"stco")
    struct.pack_into(">I", new_moov, stco_off + 12, mdat_offset)

    with open(dst, "wb") as out:
        out.write(_MP4_FTYP)
        out.write(new_moov)
        out.write(struct.pack(">I", sum(sizes) + 8) + b"mdat")
        for pos, size, _dur in _iter_samples(buf, end):
            out.write(buf[pos : pos + size])
    return dst


_AC4_SYNCWORD = b"\xac\x40"


def remux_ac4(src_mp4, dst):
    buf, end, moov = _read_mp4(src_mp4)
    written = 0
    with open(dst, "wb") as out:
        for pos, size, _dur in _iter_samples(buf, end):
            out.write(_AC4_SYNCWORD)
            if size < 0xFFFF:
                out.write(struct.pack(">H", size))
            else:
                out.write(b"\xff\xff" + size.to_bytes(3, "big"))
            out.write(buf[pos : pos + size])
            written += 1
    if written == 0:
        raise RemuxError("no audio samples found")
    return dst
