"""In-process CENC (Widevine) decryption — the pure-Python replacement for
`mp4decrypt`. Decrypts the AES-CTR fragmented MP4 in place using the recovered
content key, renaming the encryption-signalling boxes to `free` so no byte ever
moves."""

import logging
import mmap
import shutil
import struct

from Crypto.Cipher import AES

from amzdl.remux.mp4 import (
    AUDIO_SAMPLE_ENTRY_HEADER,
    find_box,
    find_path,
    iter_boxes,
    parse_tfhd,
    parse_trun,
)

_log = logging.getLogger("downloader.decrypt")

_SENC_SUBSAMPLES = 0x000002

_PROTECTION_BOXES = (b"senc", b"saiz", b"saio")


class DecryptError(Exception):
    pass


def _parse_tenc(buf, content_start, box_end):
    c = content_start
    if c + 8 + 16 > box_end:
        raise DecryptError("tenc box truncated")
    is_protected = buf[c + 6]
    iv_size = buf[c + 7]
    constant_iv = b""
    if iv_size == 0 and is_protected:
        off = c + 8 + 16
        if off < box_end:
            civ_size = buf[off]
            constant_iv = bytes(buf[off + 1 : off + 1 + civ_size])
    return iv_size, constant_iv


def _prepare_moov(buf, moov_content, moov_end):
    stsd = find_path(
        buf, moov_content, moov_end,
        b"trak", b"mdia", b"minf", b"stbl", b"stsd",
    )
    if stsd is None:
        raise DecryptError("no moov/trak/mdia/minf/stbl/stsd box")

    iv_size = None
    constant_iv = b""
    for _btype, entry_start, _entry_content, entry_end in iter_boxes(
        buf, stsd[1] + 8, stsd[2]
    ):
        child_start = entry_start + AUDIO_SAMPLE_ENTRY_HEADER
        if child_start >= entry_end:
            continue
        sinf = find_box(buf, child_start, entry_end, b"sinf")
        if sinf is None:
            continue

        frma = find_box(buf, sinf[1], sinf[2], b"frma")
        if frma is not None:
            buf[entry_start + 4 : entry_start + 8] = buf[frma[1] : frma[1] + 4]

        schm = find_box(buf, sinf[1], sinf[2], b"schm")
        if schm is not None:
            scheme = bytes(buf[schm[1] + 4 : schm[1] + 8])
            if scheme != b"cenc":
                raise DecryptError(
                    f"unsupported protection scheme {scheme!r}; "
                    "only cenc (AES-CTR) is supported"
                )

        tenc = find_path(buf, sinf[1], sinf[2], b"schi", b"tenc")
        if tenc is not None:
            iv_size, constant_iv = _parse_tenc(buf, tenc[1], tenc[2])

        buf[sinf[0] + 4 : sinf[0] + 8] = b"free"

    if iv_size is None:
        raise DecryptError("no protected sample entry / tenc found in moov")
    return iv_size, constant_iv


def _parse_senc(buf, content_start, box_end, iv_size):
    flags = struct.unpack(">I", b"\x00" + buf[content_start + 1 : content_start + 4])[0]
    sample_count = struct.unpack(">I", buf[content_start + 4 : content_start + 8])[0]
    off = content_start + 8
    entries = []
    for _ in range(sample_count):
        iv = bytes(buf[off : off + iv_size]) if iv_size else b""
        off += iv_size
        subsamples = []
        if flags & _SENC_SUBSAMPLES:
            subsample_count = struct.unpack(">H", buf[off : off + 2])[0]
            off += 2
            for _ in range(subsample_count):
                clear = struct.unpack(">H", buf[off : off + 2])[0]
                enc = struct.unpack(">I", buf[off + 2 : off + 6])[0]
                subsamples.append((clear, enc))
                off += 6
        entries.append((iv, subsamples))
    return entries


def _ctr_iv(iv, constant_iv):
    if not iv:
        iv = constant_iv
    if len(iv) == 16:
        return iv
    if len(iv) == 8:
        return iv + b"\x00" * 8
    if len(iv) < 16:
        return iv + b"\x00" * (16 - len(iv))
    return iv[:16]


def _decrypt_sample(buf, pos, size, iv16, subsamples, key):
    cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv16)
    if not subsamples:
        buf[pos : pos + size] = cipher.decrypt(bytes(buf[pos : pos + size]))
        return
    off = pos
    for clear, enc in subsamples:
        off += clear
        if enc:
            buf[off : off + enc] = cipher.decrypt(bytes(buf[off : off + enc]))
            off += enc


def _process_traf(
    buf, traf_start, traf_content, traf_end, moof_start, iv_size, constant_iv, key
):
    tfhd = find_box(buf, traf_content, traf_end, b"tfhd")
    if tfhd is None:
        return
    base_data_offset, _default_dur, default_size = parse_tfhd(buf, tfhd[1])

    senc = find_box(buf, traf_content, traf_end, b"senc")
    if senc is None:
        return
    senc_entries = _parse_senc(buf, senc[1], senc[2], iv_size)
    if not senc_entries:
        return

    base = base_data_offset if base_data_offset is not None else moof_start

    sample_index = 0
    for btype, _bs, b_content, _b_end in iter_boxes(buf, traf_content, traf_end):
        if btype != b"trun":
            continue
        samples, data_offset = parse_trun(buf, b_content, _default_dur, default_size)
        pos = base + (data_offset or 0)
        for size, _dur in samples:
            if sample_index < len(senc_entries):
                iv, subsamples = senc_entries[sample_index]
            else:
                iv, subsamples = b"", []
            if iv or constant_iv:
                _decrypt_sample(
                    buf, pos, size, _ctr_iv(iv, constant_iv), subsamples, key
                )
            pos += size
            sample_index += 1

    for btype, box_start, b_content, _b_end in iter_boxes(buf, traf_content, traf_end):
        if btype in _PROTECTION_BOXES or (
            btype in (b"sbgp", b"sgpd")
            and bytes(buf[b_content + 4 : b_content + 8]) == b"seig"
        ):
            buf[box_start + 4 : box_start + 8] = b"free"


def decrypt_mp4(encrypted_path, key, output_path):
    key_hex = key.split(":")[-1].strip()
    key_bytes = bytes.fromhex(key_hex)

    # Fast streaming copy to output destination (minimal RAM footprint)
    shutil.copyfile(encrypted_path, output_path)

    with open(output_path, "r+b") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_WRITE) as buf:
            end = len(buf)

            moov = find_box(buf, 0, end, b"moov")
            if moov is None:
                raise DecryptError("no moov box found")
            iv_size, constant_iv = _prepare_moov(buf, moov[1], moov[2])

            fragment_count = 0
            pending_moof = None
            for btype, box_start, content_start, box_end in iter_boxes(buf, 0, end):
                if btype == b"moof":
                    pending_moof = (box_start, content_start, box_end)
                elif btype == b"mdat" and pending_moof is not None:
                    moof_start, moof_content, moof_end = pending_moof
                    for tb, ts, tc, te in iter_boxes(buf, moof_content, moof_end):
                        if tb == b"traf":
                            _process_traf(
                                buf, ts, tc, te, moof_start, iv_size, constant_iv, key_bytes
                            )
                    fragment_count += 1
                    pending_moof = None

            if fragment_count == 0:
                raise DecryptError("no moof/mdat fragments found to decrypt")

            buf.flush()

    _log.debug("decrypted %s fragment(s) -> %s", fragment_count, output_path)
    return output_path
