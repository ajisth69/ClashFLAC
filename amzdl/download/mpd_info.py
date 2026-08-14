"""DASH manifest retrieval, parsing, and stream selection. Fetches the manifest
from `getDashManifestsV2`, parses it into representations, and picks one by
quality tier (using the web TRACK_PSSH block, the one with no `value`)."""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape

_log = logging.getLogger("downloader.mpd")

_LINEAR_TIER_RANK = {"LD": 0, "SD": 1, "HD": 2, "UHD": 3}
_SUB_LEVEL = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_DEFAULT_QUALITY = "HD"

_ATMOS_CODECS = ("ec-3", "ec3", "eac3", "ac-3", "ac3", "ac-4", "ac4")
_RA360_CODECS = ("mha1", "mhm1", "mha", "mhm")

_AUDIO_EXTENSIONS = (".flac", ".opus", ".mp4", ".ac4")


def _is_linear_rep(rep) -> bool:
    return str(rep.get("track_type") or "").upper() in _LINEAR_TIER_RANK


def _spatial_family(rep):
    codec = str(rep.get("codec") or "").lower()
    if codec.startswith(_ATMOS_CODECS):
        return "ATMOS"
    if codec.startswith(_RA360_CODECS):
        return "RA360"
    return None


def _parse_quality(quality):
    q = str(quality or "").upper()
    if q.startswith("SPATIAL_ATMOS"):
        return "ATMOS", None, _SUB_LEVEL.get(q[len("SPATIAL_ATMOS"):].lstrip("_"))
    if q.startswith("SPATIAL_RA360"):
        suffix = q[len("SPATIAL_RA360"):].lstrip("_")
        level = int(suffix[1:]) if suffix[:1] == "L" and suffix[1:].isdigit() else None
        return "RA360", None, level
    tier, _, sub = q.partition("_")
    if tier in _LINEAR_TIER_RANK:
        return "LINEAR", tier, _SUB_LEVEL.get(sub)
    return None, None, None


def is_spatial_quality(quality) -> bool:
    return _parse_quality(quality)[0] in ("ATMOS", "RA360")

_WIDEVINE_SCHEME = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

_NS = {
    "mpd": "urn:mpeg:dash:schema:mpd:2011",
    "cenc": "urn:mpeg:cenc:2013",
}


@dataclass
class TrackRepresentation:
    track_asin: str
    mpd_representation: dict
    quality: str


def _fetch_manifest_xml(session, track_asin: str, force_3d: bool = False) -> str:
    region = session.credentials.account_region
    responses = session._get_tracks_manifest((track_asin,), region, force_3d or None)
    if not responses:
        raise ValueError(f"no manifest returned for {track_asin}")
    return responses[0]["manifest"]


def fetch_representations(session, track_asin: str, quality=None) -> list:
    _log.debug("fetching track MPD streams for %s", track_asin)
    manifest_xml = _fetch_manifest_xml(session, track_asin, is_spatial_quality(quality))
    return parse_mpd(manifest_xml)


def _select_linear(representations: list, ceiling_tier: str, ceiling_level):
    linear = [r for r in representations if _is_linear_rep(r)]
    if not linear:
        return None

    step = {}
    for tier in {str(r["track_type"]).upper() for r in linear}:
        same = sorted((r for r in linear if str(r["track_type"]).upper() == tier),
                      key=lambda r: int(r["bandwidth"]))
        for i, r in enumerate(same):
            step[id(r)] = i

    def grank(r):
        return (_LINEAR_TIER_RANK[str(r["track_type"]).upper()], step[id(r)])

    ceiling_tr = _LINEAR_TIER_RANK[ceiling_tier]
    ceiling_step = 99 if ceiling_level is None else ceiling_level
    eligible = [
        r for r in linear
        if grank(r)[0] < ceiling_tr
        or (grank(r)[0] == ceiling_tr and grank(r)[1] <= ceiling_step)
    ]
    return max(eligible or linear, key=grank)


def _select_spatial(representations: list, family: str, level):
    cands = sorted(
        (r for r in representations if _spatial_family(r) == family),
        key=lambda r: int(r["bandwidth"]),
    )
    if not cands:
        return None
    return cands[-1] if level is None else cands[min(level, len(cands) - 1)]


def select_representation(track_asin: str, representations: list, quality):
    if not representations:
        _log.warning("no available representations for %s", track_asin)
        return None

    kind, tier, level = _parse_quality(quality)
    if kind is None:
        _log.warning(
            "unknown quality %r; falling back to %s", quality, _DEFAULT_QUALITY
        )
        kind, tier, level = "LINEAR", _DEFAULT_QUALITY, None

    if kind == "LINEAR":
        rep = _select_linear(representations, tier, level)
    else:
        rep = _select_spatial(representations, kind, level)
        if rep is None:
            _log.warning(
                "no %s stream for %s; falling back to best FLAC", kind, track_asin
            )
            rep = _select_linear(representations, "UHD", None)

    if rep is None:
        _log.warning("no playable representation for %s", track_asin)
        return None

    return TrackRepresentation(
        track_asin=track_asin,
        mpd_representation=rep,
        quality=quality,
    )


def find_representation(session, track_asin: str, quality) -> TrackRepresentation:
    reps = fetch_representations(session, track_asin, quality)
    return select_representation(track_asin, reps, quality)


def _web_pssh(adaptation) -> str:
    for cp in adaptation.findall("mpd:ContentProtection", _NS):
        if cp.attrib.get("schemeIdUri") != _WIDEVINE_SCHEME:
            continue
        if cp.attrib.get("value"):
            continue
        pssh_elem = cp.find("cenc:pssh", _NS)
        if pssh_elem is not None and pssh_elem.text:
            return pssh_elem.text.strip()
    return None


def parse_mpd(raw_xml: str):
    root = ET.fromstring(raw_xml)
    representations = []

    for adaptation in root.findall(".//mpd:AdaptationSet", _NS):
        track_type = None
        reference_loudness = None
        for prop in adaptation.findall("mpd:SupplementalProperty", _NS):
            if prop.attrib.get("schemeIdUri") == "amz-music:trackType":
                track_type = prop.attrib.get("value")
            elif (
                prop.attrib.get("schemeIdUri")
                == "urn:mpeg:mpegB:cicp:ProgramLoudness"
            ):
                reference_loudness = prop.attrib.get("value")

        adaptation_pssh = _web_pssh(adaptation)

        for rep in adaptation.findall("mpd:Representation", _NS):
            base_url = rep.find("mpd:BaseURL", _NS)
            if base_url is None or not base_url.text:
                continue

            seglist = rep.find("mpd:SegmentList", _NS)
            segments = (
                seglist.findall("mpd:SegmentURL", _NS) if seglist is not None else []
            )

            representations.append({
                "id": rep.attrib.get("id"),
                "track_type": track_type,
                "codec": rep.attrib.get("codecs"),
                "bandwidth": int(rep.attrib.get("bandwidth", 0)),
                "sample_rate": rep.attrib.get("audioSamplingRate"),
                "bit_depth": next(
                    (
                        sp.attrib.get("value")
                        for sp in rep.findall("mpd:SupplementalProperty", _NS)
                        if sp.attrib.get("schemeIdUri") == "amz-music:bitDepth"
                    ),
                    None,
                ),
                "base_url": unescape(base_url.text.strip()),
                "first_segment_range": (
                    segments[0].attrib.get("mediaRange") if segments else None
                ),
                "pssh": adaptation_pssh,
                "reference_loudness": reference_loudness,
            })

    return representations
