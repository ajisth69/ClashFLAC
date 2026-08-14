"""Shared MP4 (ISOBMFF) box parsing — the box-walking and fragment-header
helpers common to `decrypt` and `remux`: generic box iteration/lookup
(`iter_boxes`/`find_box`/`find_path`), the audio sample-entry header size, and
`tfhd`/`trun` parsing (track-fragment defaults + per-sample sizes and 48 kHz
durations)."""

import struct

AUDIO_SAMPLE_ENTRY_HEADER = 36

_TFHD_BASE_DATA_OFFSET = 0x000001
_TFHD_SAMPLE_DESC_INDEX = 0x000002
_TFHD_DEFAULT_DURATION = 0x000008
_TFHD_DEFAULT_SIZE = 0x000010

_TRUN_DATA_OFFSET = 0x000001
_TRUN_FIRST_SAMPLE_FLAGS = 0x000004
_TRUN_SAMPLE_DURATION = 0x000100
_TRUN_SAMPLE_SIZE = 0x000200
_TRUN_SAMPLE_FLAGS = 0x000400
_TRUN_SAMPLE_CTO = 0x000800


def iter_boxes(buf, start, end):
    off = start
    while off + 8 <= end:
        size = struct.unpack(">I", buf[off : off + 4])[0]
        btype = bytes(buf[off + 4 : off + 8])
        header = 8
        if size == 1:
            if off + 16 > end:
                break
            size = struct.unpack(">Q", buf[off + 8 : off + 16])[0]
            header = 16
        elif size == 0:
            size = end - off
        if size < header or off + size > end:
            break
        yield btype, off, off + header, off + size
        off += size


def find_box(buf, start, end, target):
    for btype, box_start, content_start, box_end in iter_boxes(buf, start, end):
        if btype == target:
            return box_start, content_start, box_end
    return None


def find_path(buf, start, end, *path):
    cur = (start, start, end)
    for target in path:
        found = find_box(buf, cur[1], cur[2], target)
        if found is None:
            return None
        cur = found
    return cur


def parse_tfhd(buf, content_start):
    flags = struct.unpack(">I", b"\x00" + buf[content_start + 1 : content_start + 4])[0]
    base = None
    default_dur = 0
    default_size = 0
    off = content_start + 8
    if flags & _TFHD_BASE_DATA_OFFSET:
        base = struct.unpack(">Q", buf[off : off + 8])[0]
        off += 8
    if flags & _TFHD_SAMPLE_DESC_INDEX:
        off += 4
    if flags & _TFHD_DEFAULT_DURATION:
        default_dur = struct.unpack(">I", buf[off : off + 4])[0]
        off += 4
    if flags & _TFHD_DEFAULT_SIZE:
        default_size = struct.unpack(">I", buf[off : off + 4])[0]
        off += 4
    return base, default_dur, default_size


def parse_trun(buf, content_start, default_duration, default_size):
    flags = struct.unpack(">I", b"\x00" + buf[content_start + 1 : content_start + 4])[0]
    count = struct.unpack(">I", buf[content_start + 4 : content_start + 8])[0]
    off = content_start + 8
    data_offset = None
    if flags & _TRUN_DATA_OFFSET:
        data_offset = struct.unpack(">i", buf[off : off + 4])[0]
        off += 4
    if flags & _TRUN_FIRST_SAMPLE_FLAGS:
        off += 4
    samples = []
    for _ in range(count):
        dur = default_duration
        size = default_size
        if flags & _TRUN_SAMPLE_DURATION:
            dur = struct.unpack(">I", buf[off : off + 4])[0]
            off += 4
        if flags & _TRUN_SAMPLE_SIZE:
            size = struct.unpack(">I", buf[off : off + 4])[0]
            off += 4
        if flags & _TRUN_SAMPLE_FLAGS:
            off += 4
        if flags & _TRUN_SAMPLE_CTO:
            off += 4
        samples.append((size, dur))
    return samples, data_offset
