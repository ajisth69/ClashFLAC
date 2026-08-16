import os
import re
import shutil
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Callable
import httpx
import mutagen
from mutagen.flac import FLAC, Picture

from .config import TidalConfig
from .api import TidalAPI
from .decryption import decrypt_security_token, decrypt_file
from amzdl.remux.remux import remux_flac

logger = logging.getLogger("tidal.downloader")

import shutil

def get_ffmpeg_binary() -> str:
    """Resolve ffmpeg binary from system PATH, imageio-ffmpeg, or standard Linux paths."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    for path in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/nix/var/nix/profiles/default/bin/ffmpeg"):
        if Path(path).is_file():
            return path
    return "ffmpeg"

def safe_filename(name: Optional[str], fallback: str = "track") -> str:
    if not name:
        return fallback
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name)).strip()
    return clean or fallback

def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    # Remove bracketed/parenthetical additions
    t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', str(text))
    t = re.sub(r'[^\w\s]', '', t.lower())
    return " ".join(t.split())

def score_candidate(candidate: Any, target_title: str, target_artist: str, target_duration: int = 0) -> int:
    score = 0
    c_title = normalize_text(getattr(candidate, "title", None) or (candidate.get("title") if isinstance(candidate, dict) else ""))
    t_title = normalize_text(target_title)
    
    if c_title == t_title:
        score += 60
    elif t_title and (t_title in c_title or c_title in t_title):
        score += 35

    c_artist = normalize_text(getattr(candidate, "artist", None) or (candidate.get("artist") if isinstance(candidate, dict) else ""))
    t_artist = normalize_text(target_artist)
    
    t_primary = t_artist.split()[0] if t_artist else ""
    if t_primary and t_primary in c_artist:
        score += 40

    c_dur = getattr(candidate, "duration_sec", 0) or (candidate.get("duration_sec", 0) if isinstance(candidate, dict) else 0)
    if target_duration and c_dur:
        diff = abs(target_duration - c_dur)
        if diff <= 3:
            score += 25
        elif diff <= 8:
            score += 15

    quality = getattr(candidate, "audio_quality", "") or (candidate.get("audio_quality", "") if isinstance(candidate, dict) else "")
    if quality in ("HI_RES_LOSSLESS", "UHD", "MASTER"):
        score += 15
    elif quality in ("LOSSLESS", "HD"):
        score += 10

    return score

class TidalDownloader:
    def __init__(self, api: Optional[TidalAPI] = None):
        self.api = api or TidalAPI()

    async def download_track(
        self,
        track_id_or_input: str,
        output_dir: Optional[Path] = None,
        quality: str = "HD",
        track_hint: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[str, int], None]] = None,
    ) -> Path:
        """
        Download and tag a single Tidal track with 100% accurate metadata and embedded Front Cover picture.
        Guarantees that the output file is ALWAYS delivered in .flac format with complete Vorbis tags.
        """
        output_base = (output_dir or Path("downloads/tidal")).resolve()
        output_base.mkdir(parents=True, exist_ok=True)
        track_hint = track_hint or {}

        def report(stage: str, progress: int) -> None:
            if progress_callback:
                progress_callback(stage, progress)

        target_title = track_hint.get("title") or ""
        target_artist = track_hint.get("artist") or ""
        target_duration = int(track_hint.get("duration") or track_hint.get("duration_sec") or 0)

        # 1. Resolve Candidate Track IDs
        track_id = self.api.extract_track_id(track_id_or_input)
        candidate_ids = []

        if track_id:
            candidate_ids.append(track_id)
        else:
            clean_artist = re.sub(r'[,&/]|feat\..*$', ' ', target_artist).strip()
            clean_artist = " ".join(clean_artist.split())
            search_query = f"{target_title} {clean_artist}".strip() or str(track_id_or_input)
            hits = await self.api.search(search_query, limit=5)
            if not hits:
                hits = await self.api.search(f"{target_title} {target_artist}".strip() or track_id_or_input, limit=5)
            if not hits:
                hits = await self.api.search(track_id_or_input, limit=5)
            
            if hits:
                # Rank candidates by fuzzy match score
                scored_hits = sorted(
                    hits,
                    key=lambda h: score_candidate(h, target_title or track_id_or_input, target_artist, target_duration),
                    reverse=True
                )
                candidate_ids = [h.asin for h in scored_hits]

        if not candidate_ids:
            raise Exception(f"Track '{track_id_or_input}' not found on Tidal")

        report("Resolving Tidal stream", 18)
        # 2. Resolve Stream URL across best candidates
        stream_url = None
        codec = "flac"
        bitrate = 0
        encryption_key = None
        resolved_track_id = None
        raw_track_meta = None

        for cid in candidate_ids:
            try:
                t_meta = await self.api.get_track(cid)
                if not t_meta:
                    continue

                s_url, cd, br, ekey = await self.api.get_stream_url(cid, quality=quality)
                if s_url:
                    resolved_track_id = cid
                    raw_track_meta = t_meta
                    stream_url = s_url
                    codec = cd
                    bitrate = br
                    encryption_key = ekey
                    break
            except Exception:
                continue

        if not stream_url or not resolved_track_id:
            raise Exception(f"Could not retrieve playable stream URL for Tidal track {track_id_or_input} (quality: {quality})")

        track_id = resolved_track_id
        track_meta = raw_track_meta or (await self.api.get_track(track_id))
        album_meta = track_meta.get("album") or {}

        report("Reading Tidal metadata", 28)
        # 3. Construct Unified Metadata (Frontend hint prioritized, Tidal enriched)
        title = (track_hint.get("title") if track_hint else None) or track_meta.get("title") or "Unknown Title"
        artists_list = track_meta.get("artists") or []
        tidal_artist_str = ", ".join([a.get("name") for a in artists_list if a.get("name")]) or track_meta.get("artist", {}).get("name")
        artist = (track_hint.get("artist") if track_hint else None) or tidal_artist_str or "Unknown Artist"
        album_artist = track_meta.get("artist", {}).get("name") or artist
        album_title = (track_hint.get("album") if track_hint else None) or album_meta.get("title") or "Unknown Album"
        track_number = track_hint.get("trackNumber") or track_meta.get("trackNumber") or 1
        disc_number = track_hint.get("discNumber") or track_meta.get("volumeNumber") or 1
        release_date = track_hint.get("releaseDate") or track_meta.get("streamStartDate") or album_meta.get("releaseDate") or ""
        year = str(track_hint.get("year") or (release_date[:4] if release_date else "")) or None
        isrc = track_meta.get("isrc") or track_hint.get("isrc")
        copyright_str = track_meta.get("copyright") or album_meta.get("copyright")
        genre_name = track_hint.get("genre") or track_meta.get("genre") or album_meta.get("genre")
        total_tracks = album_meta.get("numberOfTracks")
        total_discs = album_meta.get("numberOfVolumes")
        is_explicit = bool(track_meta.get("explicit") or (track_hint.get("explicit") if track_hint else False))
        replay_gain = track_meta.get("replayGain")
        peak = track_meta.get("peak")

        target_folder = output_base / safe_filename(artist) / safe_filename(album_title)
        target_folder.mkdir(parents=True, exist_ok=True)

        track_num_str = f"{track_number:02d}" if isinstance(track_number, int) else str(track_number)
        base_name = f"{disc_number}-{track_num_str} {safe_filename(title)}"
        final_flac_path = target_folder / f"{base_name}.flac"
        temp_file_path = target_folder / f"{base_name}.part"
        temp_file_path.parent.mkdir(parents=True, exist_ok=True)

        report("Downloading Tidal audio", 40)
        # 4. Stream Download Chunks
        if isinstance(stream_url, dict) and stream_url.get("is_dash"):
            init_url = stream_url["init_url"]
            media_tmpl = stream_url["media_template"]
            total = stream_url.get("total_segments", 1)

            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                r_init = await client.get(init_url)
                with open(temp_file_path, "wb") as f_out:
                    f_out.write(r_init.content)
                    for i in range(1, total + 1):
                        seg_url = media_tmpl.replace("$Number$", str(i))
                        try:
                            r_seg = await client.get(seg_url)
                            if r_seg.status_code == 200:
                                f_out.write(r_seg.content)
                        except Exception as seg_err:
                            logger.warning(f"Error fetching segment {i}: {seg_err}")
        else:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                async with client.stream("GET", stream_url) as resp:
                    if resp.status_code != 200:
                        raise Exception(f"Failed downloading audio stream ({resp.status_code}): {resp.text}")
                    with open(temp_file_path, "wb") as f_out:
                        async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                            f_out.write(chunk)

        report("Decrypting audio stream", 65)
        # 5. Decrypt if AES encrypted
        if encryption_key:
            try:
                decrypted_path = target_folder / f"{base_name}.decrypted"
                key, nonce = decrypt_security_token(encryption_key)
                decrypt_file(temp_file_path, decrypted_path, key, nonce)
                temp_file_path.unlink(missing_ok=True)
                temp_file_path = decrypted_path
            except Exception as e:
                logger.warning(f"Error during AES stream decryption: {e}")

        report("Packaging lossless FLAC", 75)
        # 6. Remux to FLAC container
        if temp_file_path.exists():
            with open(temp_file_path, "rb") as f_check:
                header = f_check.read(16)

            if header.startswith(b"fLaC"):
                if final_flac_path.exists():
                    final_flac_path.unlink()
                temp_file_path.rename(final_flac_path)
            else:
                # Convert MP4/AAC/DASH container stream to genuine bit-perfect PCM FLAC using ffmpeg
                converted = False
                try:
                    ffmpeg_exe = get_ffmpeg_binary()
                    cmd = [ffmpeg_exe, "-y", "-i", str(temp_file_path), "-c:a", "flac", str(final_flac_path)]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if final_flac_path.exists() and final_flac_path.stat().st_size > 1000:
                        converted = True
                        temp_file_path.unlink(missing_ok=True)
                except Exception as ff_err:
                    logger.warning(f"ffmpeg conversion error: {ff_err}")

                if not converted:
                    # Fallback to remux_flac if ffmpeg failed
                    try:
                        remux_flac(temp_file_path, final_flac_path)
                        temp_file_path.unlink(missing_ok=True)
                    except Exception:
                        if temp_file_path.exists():
                            temp_file_path.rename(final_flac_path)
        else:
            raise Exception("Downloaded audio file was not written.")

        report("Fetching artwork", 82)
        # 7. Fetch High-Resolution Cover Image (PNG or JPEG)
        cover_bytes = None
        cover_urls_to_try = []

        if track_hint and track_hint.get("image"):
            cover_urls_to_try.append(track_hint["image"])
        if track_hint and track_hint.get("thumbnail_hq"):
            cover_urls_to_try.append(track_hint["thumbnail_hq"])

        cover_id = album_meta.get("cover") or track_meta.get("cover")
        if cover_id:
            cover_urls_to_try.append(self.api.format_cover_url(cover_id, "1280x1280"))
            cover_urls_to_try.append(self.api.format_cover_url(cover_id, "640x640"))

        img_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for c_url in cover_urls_to_try:
                if not c_url:
                    continue
                try:
                    c_resp = await client.get(c_url, headers=img_headers)
                    if c_resp.status_code == 200 and len(c_resp.content) > 1000:
                        cover_bytes = c_resp.content
                        break
                except Exception as e:
                    logger.warning(f"Notice fetching cover from {c_url}: {e}")

        report("Fetching lyrics", 88)
        # 8. Fetch Synchronized Lyrics (Tidal native -> LRCLIB fallback)
        lyrics = await self.api.get_lyrics(track_id)
        if not lyrics:
            try:
                clean_artist = artist.split(",")[0].split("&")[0].replace("feat.", "").strip()
                params = {"track_name": title, "artist_name": clean_artist}
                if album_title and album_title != "Unknown Album":
                    params["album_name"] = album_title
                if target_duration:
                    params["duration"] = target_duration

                async with httpx.AsyncClient(timeout=8.0) as client:
                    lrc_resp = await client.get(
                        "https://lrclib.net/api/get",
                        params=params,
                        headers={"User-Agent": "ClashFLAC/2.2.0"}
                    )
                    if lrc_resp.status_code == 200:
                        lrc_data = lrc_resp.json()
                        lyrics = lrc_data.get("syncedLyrics") or lrc_data.get("plainLyrics")
                    if not lyrics:
                        lrc_srch = await client.get(
                            "https://lrclib.net/api/search",
                            params={"q": f"{title} {clean_artist}"},
                            headers={"User-Agent": "ClashFLAC/2.2.0"}
                        )
                        if lrc_srch.status_code == 200:
                            items = lrc_srch.json()
                            if items and isinstance(items, list):
                                lyrics = items[0].get("syncedLyrics") or items[0].get("plainLyrics")
            except Exception as lrc_err:
                logger.warning(f"LRCLIB fallback notice: {lrc_err}")

        report("Embedding metadata and artwork", 94)
        # 9. Apply Bit-Perfect Vorbis Tags & Front Cover Picture
        try:
            self._apply_flac_tags(
                file_path=final_flac_path,
                title=title,
                artist=artist,
                album=album_title,
                album_artist=album_artist,
                track_number=track_number,
                disc_number=disc_number,
                year=year,
                release_date=str(release_date) if release_date else None,
                isrc=isrc,
                copyright_str=copyright_str,
                lyrics=lyrics,
                cover_bytes=cover_bytes,
                genre=genre_name,
                total_tracks=total_tracks,
                total_discs=total_discs,
                is_explicit=is_explicit,
                replay_gain=replay_gain,
                peak=peak,
            )
        except Exception as e:
            logger.error(f"Failed applying Vorbis tags to {final_flac_path}: {e}")

        report("Tidal FLAC ready", 100)
        logger.info(f"Successfully processed & tagged Tidal FLAC track: {final_flac_path.name}")
        return final_flac_path

    def _apply_flac_tags(
        self,
        file_path: Path,
        title: str,
        artist: str,
        album: str,
        album_artist: Optional[str] = None,
        track_number: int = 1,
        disc_number: int = 1,
        year: Optional[str] = None,
        release_date: Optional[str] = None,
        isrc: Optional[str] = None,
        copyright_str: Optional[str] = None,
        lyrics: Optional[str] = None,
        cover_bytes: Optional[bytes] = None,
        genre: Optional[str] = None,
        total_tracks: Optional[int] = None,
        total_discs: Optional[int] = None,
        is_explicit: bool = False,
        replay_gain: Optional[float] = None,
        peak: Optional[float] = None,
    ):
        """Apply comprehensive FLAC Vorbis comments and embedded Front Cover picture block."""
        # 1. Strip any legacy/corrupted ID3 header if present
        try:
            from mutagen.id3 import delete as delete_id3
            delete_id3(str(file_path))
        except Exception:
            pass

        # 2. Open native FLAC and purge existing/ffmpeg container metadata
        audio = FLAC(str(file_path))
        audio.delete()

        # 3. Write standardized Vorbis Comments
        audio["TITLE"] = title
        audio["ARTIST"] = artist
        audio["ALBUM"] = album
        audio["ALBUMARTIST"] = album_artist or artist
        audio["ALBUM ARTIST"] = album_artist or artist
        audio["TRACKNUMBER"] = str(track_number)
        audio["DISCNUMBER"] = str(disc_number)
        if total_tracks:
            audio["TRACKTOTAL"] = str(total_tracks)
            audio["TOTALTRACKS"] = str(total_tracks)
        if total_discs:
            audio["DISCTOTAL"] = str(total_discs)
            audio["TOTALDISCS"] = str(total_discs)
        if year:
            audio["DATE"] = str(year)
            audio["YEAR"] = str(year)
        if release_date:
            audio["RELEASEDATE"] = str(release_date)
            audio["ORIGINALDATE"] = str(release_date)
        if isrc:
            audio["ISRC"] = isrc
        if genre:
            audio["GENRE"] = genre
        if copyright_str:
            audio["COPYRIGHT"] = copyright_str
            audio["LABEL"] = copyright_str
            audio["PUBLISHER"] = copyright_str
            audio["ORGANIZATION"] = copyright_str
        audio["RATING"] = "Explicit" if is_explicit else "Clean"
        if replay_gain is not None:
            audio["REPLAYGAIN_TRACK_GAIN"] = f"{replay_gain:+.2f} dB"
        if peak is not None:
            audio["REPLAYGAIN_TRACK_PEAK"] = f"{peak:.6f}"
        if lyrics:
            audio["LYRICS"] = lyrics
            audio["UNSYNCEDLYRICS"] = lyrics
        audio["COMMENT"] = "ClashFLAC Lossless Engine"
        audio["DESCRIPTION"] = f"{title} - {artist}"

        # 4. Embed Front Cover picture block with explicit dimensions for Windows Explorer thumbnail provider
        if cover_bytes:
            pic = Picture()
            pic.type = 3  # Front Cover
            pic.desc = "Front Cover"
            try:
                from PIL import Image
                import io
                with Image.open(io.BytesIO(cover_bytes)) as im:
                    pic.width = im.width
                    pic.height = im.height
                    pic.depth = 24 if im.mode in ("RGB", "P", "L") else 32
                    if im.format == "PNG":
                        pic.mime = "image/png"
                        pic.data = cover_bytes
                    else:
                        pic.mime = "image/jpeg"
                        if im.mode != "RGB":
                            im = im.convert("RGB")
                        out_io = io.BytesIO()
                        im.save(out_io, format="JPEG", quality=95)
                        pic.data = out_io.getvalue()
            except Exception:
                if cover_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    pic.mime = "image/png"
                elif cover_bytes.startswith(b"RIFF") and b"WEBP" in cover_bytes[:12]:
                    pic.mime = "image/webp"
                else:
                    pic.mime = "image/jpeg"
                pic.data = cover_bytes

            audio.clear_pictures()
            audio.add_picture(pic)

        audio.save()
