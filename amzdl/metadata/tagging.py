"""Tag a downloaded track and embed its cover art. `tag_track` dispatches on the
container chosen by `fetch_track._output_spec` — Vorbis comments for FLAC/Opus,
MP4 atoms for spatial `.mp4`, or skip for raw `.ac4`. Mirrors the tag set
OrpheusDL's Amazon Music module writes: core fields, an Explicit/Clean RATING,
the music.amazon URL (WWW), MERCHANT, reference loudness, a
LABEL/PUBLISHER/ORGANIZATION fan-out, and per-role credits parsed from the track
xray; plus the project's own ALBUM_REVIEW_AVERAGE/ALBUM_REVIEW_COUNT
(album-level customer reviews). All tag names are normalized to
UPPERCASE_SNAKE_CASE."""

import re
import tempfile

import requests
from mutagen.flac import FLAC, Picture

from amzdl.metadata.metadata import TrackMetadata

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_FRONT_COVER = 3
_CREDIT_NAME_SPLIT = re.compile(r" & |, | - | / | feat\. ")


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _split_credit_names(name: str) -> list[str]:
    return [part for part in _CREDIT_NAME_SPLIT.split(str(name)) if part]


def _credit_key(name: str) -> str:
    spaced = str(name).replace("&", " and ")
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return "_".join(w.upper() for w in re.findall(r"[A-Za-z0-9]+", spaced))


def _prepare_credits(
    credits: dict | None, track: TrackMetadata
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for credit_type, names in (credits or {}).items():
        key = _credit_key(credit_type)
        if not key:
            continue
        for name in names or []:
            grouped.setdefault(key, []).extend(_split_credit_names(name))

    album_artist_lower = (track.album_artist or "").lower()
    track_artist_lower = (track.artist or "").lower()
    for key in list(grouped):
        norm = key.replace("_", " ").replace("-", " ").strip().lower()
        if norm == "music publisher":
            del grouped[key]
            continue
        if norm in ("main artist", "primary artist"):
            names_lower = [n.lower() for n in grouped[key]]
            if album_artist_lower in names_lower or names_lower == [track_artist_lower]:
                del grouped[key]

    return {k: list(dict.fromkeys(v)) for k, v in grouped.items() if v}


def _extra_tags(track: TrackMetadata, track_url, reference_loudness) -> dict[str, str]:
    extra: dict[str, str] = {}
    if track_url:
        extra["WWW"] = track_url
    if track.merchant:
        extra["MERCHANT"] = track.merchant
    if reference_loudness:
        extra["REPLAYGAIN_REFERENCE_LOUDNESS"] = str(reference_loudness)
    if track.review_average is not None:
        extra["ALBUM_REVIEW_AVERAGE"] = track.review_average
    if track.review_count is not None:
        extra["ALBUM_REVIEW_COUNT"] = track.review_count
    return extra


def download_artwork(url: str, directory: str):
    if not url:
        return None
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg", dir=directory) as tmp:
        response = requests.get(
            url,
            timeout=10,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                ),
                "User-Agent": _UA,
            },
        )
        response.raise_for_status()
        tmp.write(response.content)
        return tmp.name


def tag_track(media_path: str, track: TrackMetadata, lyrics, temp_dir: str,
              tag_mode: str = "flac", artwork_path=None,
              track_url=None, credits=None, reference_loudness=None):
    if tag_mode is None:
        return
    prepared_credits = _prepare_credits(credits, track)
    extra = _extra_tags(track, track_url, reference_loudness)
    if tag_mode == "mp4":
        embed_metadata_and_cover_mp4(
            media_path, track, lyrics, artwork_path, extra, prepared_credits
        )
    elif tag_mode == "opus":
        embed_metadata_and_cover_opus(
            media_path, track, lyrics, artwork_path, extra, prepared_credits
        )
    else:
        embed_metadata_and_cover(
            media_path, track, lyrics, artwork_path, extra, prepared_credits
        )


def embed_metadata_and_cover_mp4(
    mp4_path: str, track: TrackMetadata, lyrics, artwork_path,
    extra: dict, prepared_credits: dict,
):
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(mp4_path)
    audio.delete()

    def setv(key, value):
        if value is not None and value != "":
            audio[key] = [str(value)]

    def freeform(name, value):
        if value is not None and value != "":
            audio[f"----:com.apple.iTunes:{name}"] = [str(value).encode("utf-8")]

    setv("\xa9nam", track.title)
    setv("\xa9ART", track.artist)
    setv("\xa9alb", track.album_name)
    setv("aART", track.album_artist)
    setv("\xa9day", track.release_date)
    setv("cprt", track.copyright)
    setv("\xa9gen", track.genre)
    setv("\xa9wrt", track.composers)

    if track.track_number:
        audio["trkn"] = [(_as_int(track.track_number), _as_int(track.total_tracks))]
    if track.disc:
        audio["disk"] = [(_as_int(track.disc), _as_int(track.total_discs))]

    audio["rtng"] = [1 if track.is_explicit else 0]

    freeform("ISRC", track.isrc)

    for name, value in extra.items():
        freeform(name, value)
    for credit_type, names in prepared_credits.items():
        audio[f"----:com.apple.iTunes:{credit_type}"] = [
            n.encode("utf-8") for n in names
        ]

    if track.label:
        setv("\xa9pub", track.label)
        freeform("LABEL", track.label)

    if lyrics and lyrics.has_content():
        setv("\xa9lyr", lyrics.to_mp4_lyrics())

    if artwork_path:
        with open(artwork_path, "rb") as img:
            audio["covr"] = [MP4Cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()


def _set_vorbis_fields(
    audio, track: TrackMetadata, lyrics, extra: dict, prepared_credits: dict
):
    def setv(key, value):
        if value is not None and value != "":
            audio[key] = str(value)

    setv("TITLE", track.title)
    setv("ARTIST", track.artist)
    setv("ALBUM", track.album_name)
    setv("ALBUMARTIST", track.album_artist)
    setv("TRACKNUMBER", track.track_number)
    setv("TOTALTRACKS", track.total_tracks)
    setv("DISCNUMBER", track.disc)
    setv("TOTALDISCS", track.total_discs)
    setv("DATE", track.release_date)
    setv("COPYRIGHT", track.copyright)
    setv("GENRE", track.genre)
    setv("ISRC", track.isrc)
    setv("COMPOSER", track.composers)
    setv("RATING", "Explicit" if track.is_explicit else "Clean")

    for key, value in extra.items():
        setv(key, value)
    for credit_type, names in prepared_credits.items():
        audio[credit_type] = names

    if track.label:
        audio["LABEL"] = track.label
        audio["PUBLISHER"] = track.label
        audio["ORGANIZATION"] = track.label

    if lyrics and lyrics.has_content():
        lrc_content = lyrics.to_lrc()
        if lrc_content:
            setv("LYRICS", lrc_content)
            setv("UNSYNCEDLYRICS", lyrics.to_mp4_lyrics())
        else:
            setv("LYRICS", lyrics.to_mp4_lyrics())


def _build_cover_picture(artwork_path) -> Picture:
    with open(artwork_path, "rb") as img:
        cover_data = img.read()
    pic = Picture()
    pic.type = _FRONT_COVER
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = cover_data
    return pic


def embed_metadata_and_cover(flac_path: str, track: TrackMetadata, lyrics, artwork_path,
                             extra: dict, prepared_credits: dict):
    audio = FLAC(flac_path)
    audio.delete()
    _set_vorbis_fields(audio, track, lyrics, extra, prepared_credits)
    if artwork_path:
        audio.add_picture(_build_cover_picture(artwork_path))
    audio.save()


def embed_metadata_and_cover_opus(
    opus_path: str, track: TrackMetadata, lyrics, artwork_path,
    extra: dict, prepared_credits: dict,
):
    import base64

    from mutagen.oggopus import OggOpus

    audio = OggOpus(opus_path)
    audio.delete()
    _set_vorbis_fields(audio, track, lyrics, extra, prepared_credits)
    if artwork_path:
        pic = _build_cover_picture(artwork_path)
        audio["METADATA_BLOCK_PICTURE"] = [
            base64.b64encode(pic.write()).decode("ascii")
        ]
    audio.save()
