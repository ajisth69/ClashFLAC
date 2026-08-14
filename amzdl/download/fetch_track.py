"""Download, decrypt, remux, and tag a single track. Each codec is stream-copied
into its natural container (HD/UHD FLAC, lossy Opus, or the spatial
`.mp4`/`.ac4` tiers) and filed at
`<output_dir>/<album_artist>/<album>/<disc> - <track> <title>.<ext>`."""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

import requests

from amzdl.download.keys import Keys
from amzdl.download.mpd_info import _AUDIO_EXTENSIONS, find_representation
from amzdl.metadata.lyrics import Lyrics
from amzdl.metadata.metadata import TrackMetadata, resolve_track_cover
from amzdl.metadata.tagging import download_artwork, tag_track
from amzdl.remux.decrypt import decrypt_mp4
from amzdl.remux.remux import remux_ac4, remux_flac, remux_mp4, remux_opus
from amzdl.utils import build_output_filename, safe_filename

_log = logging.getLogger("downloader.track")

_TEMP_SUBDIR = ".downloader"

def _output_spec(codec):
    c = str(codec or "").lower()
    if c.startswith("opus"):
        return ".opus", "opus"
    if c.startswith(("ec-3", "ec3", "eac3", "ac-3", "ac3", "mha", "mhm")):
        return ".mp4", "mp4"
    if c.startswith(("ac-4", "ac4")):
        return ".ac4", None
    return ".flac", "flac"


def _track_url(session, track: TrackMetadata) -> str | None:
    if not track.album_asin:
        return None
    region = session.credentials.account_region
    return (
        f"https://music.amazon.{region.domain_tld}/albums/{track.album_asin}"
        f"?trackAsin={track.asin}&musicTerritory={region.country}"
    )


def _fetch_credits(session, asin: str) -> dict:
    try:
        return session.get_track_xray(
            asin, region_to_use=session.credentials.account_region, parse_credits=True
        ) or {}
    except Exception:
        _log.warning(
            "credits lookup failed for %s; tagging without credits", asin, exc_info=True
        )
        return {}


def _existing_download(track_output_dir: Path, output_filename: str):
    for ext in _AUDIO_EXTENSIONS:
        candidate = track_output_dir / (output_filename + ext)
        if candidate.exists():
            return candidate
    return None


def purge_temp_dir(output_dir: Path) -> None:
    shutil.rmtree(Path(output_dir) / _TEMP_SUBDIR, ignore_errors=True)


_DOWNLOAD_CHUNK = 1024 * 1024


def download_full_file(base_url: str, output_path):
    r = requests.get(base_url, stream=True)
    if r.status_code != 200:
        _log.error("download failed. Status code: %s", r.status_code)
        return None
    r.raw.decode_content = True
    with open(output_path, "wb") as f:
        shutil.copyfileobj(r.raw, f, length=_DOWNLOAD_CHUNK)
    return output_path


_REMUXERS = {
    ".flac": remux_flac,
    ".opus": remux_opus,
    ".mp4": remux_mp4,
    ".ac4": remux_ac4,
}


def remux_copy(src_mp4: Path, dst: Path) -> None:
    remuxer = _REMUXERS.get(dst.suffix)
    if remuxer is None:
        raise RuntimeError(f"no remuxer for {dst.suffix!r} output")
    remuxer(src_mp4, dst)


async def process_track(
    session,
    track: TrackMetadata,
    representation,
    output_dir: Path,
    build_folder_structure: bool = True,
    lyrics_resp=None,
    on_step=None,
    wvd_path: str | None = None,
    resolve_hi_res_cover: bool = False,
):
    def step(desc):
        _log.debug(desc)
        if on_step:
            on_step(desc)

    if representation is None:
        _log.warning("no playable representation for %s; skipping.", track.title)
        return

    rep = representation.mpd_representation
    extension, tag_mode = _output_spec(rep.get("codec"))

    if build_folder_structure:
        safe_album_artist_name = safe_filename(track.album_artist, False)
        safe_album_name = safe_filename(track.album_name, False)
        track_output_dir = output_dir / safe_album_artist_name / safe_album_name
    else:
        track_output_dir = output_dir

    output_filename = build_output_filename(track.disc, track.track_number, track.title)
    output_file = track_output_dir / (output_filename + extension)

    existing = _existing_download(track_output_dir, output_filename)
    if existing is not None:
        _log.info(
            "file %s already exists (%s); skipping.", output_filename, existing.suffix
        )
        return True

    base_temp = output_dir / _TEMP_SUBDIR
    base_temp.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="dl-", dir=base_temp))
    encrypted_file = temp_dir / "encrypted.mp4"

    def fetch_cover():
        url = (
            resolve_track_cover(session, track)
            if resolve_hi_res_cover
            else track.cover_url
        )
        return download_artwork(url, str(temp_dir))

    step("downloading track")
    coros = [
        asyncio.to_thread(
            Keys.getContentKeys, session, track.asin, rep["pssh"], wvd_path
        ),
        asyncio.to_thread(download_full_file, rep["base_url"], encrypted_file),
        asyncio.to_thread(fetch_cover),
        asyncio.to_thread(_fetch_credits, session, track.asin),
    ]
    fetch_lyrics = lyrics_resp is None
    if fetch_lyrics:
        coros.append(asyncio.to_thread(session.get_track_lyrics, track.asin))
    results = await asyncio.gather(*coros)
    content_key = results[0]
    artwork_path = results[2]
    credits = results[3]
    if fetch_lyrics:
        lyrics_resp = results[4]

    step("decrypting")
    decrypted_mp4 = temp_dir / "decrypted_temp.mp4"
    try:
        await asyncio.to_thread(decrypt_mp4, encrypted_file, content_key, decrypted_mp4)
    except Exception:
        _log.error("decryption failed for %s", track.title, exc_info=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    step(f"remuxing to {extension.lstrip('.')}")
    media_temp = temp_dir / ("decoded_temp" + extension)
    await asyncio.to_thread(remux_copy, decrypted_mp4, media_temp)

    step("tagging metadata")
    lyrics_obj = Lyrics.from_xray(lyrics_resp)
    await asyncio.to_thread(
        tag_track, str(media_temp), track, lyrics_obj, str(temp_dir), tag_mode,
        artwork_path,
        _track_url(session, track), credits, rep.get("reference_loudness"),
    )

    track_output_dir.mkdir(parents=True, exist_ok=True)
    media_temp.rename(output_file)
    shutil.rmtree(temp_dir, ignore_errors=True)

    _log.info("saved to: %s", output_file)


async def fetch_track(
    session,
    track: TrackMetadata,
    output_dir: Path,
    quality,
    build_folder_structure: bool = True,
    on_step=None,
    wvd_path: str | None = None,
):
    if on_step:
        on_step("fetching manifest")
    representation = await asyncio.to_thread(
        find_representation, session, track.asin, quality
    )
    return await process_track(
        session, track, representation, output_dir, build_folder_structure,
        on_step=on_step, wvd_path=wvd_path,
    )
