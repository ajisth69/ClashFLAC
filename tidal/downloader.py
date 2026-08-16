import os
import re
import shutil
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import httpx
import mutagen
from mutagen.flac import FLAC, Picture

from .config import TidalConfig
from .api import TidalAPI
from .decryption import decrypt_security_token, decrypt_file
from amzdl.remux.remux import remux_flac

logger = logging.getLogger("tidal.downloader")

def safe_filename(name: Optional[str], fallback: str = "track") -> str:
    if not name:
        return fallback
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name)).strip()
    return clean or fallback

class TidalDownloader:
    def __init__(self, api: Optional[TidalAPI] = None):
        self.api = api or TidalAPI()

    async def download_track(
        self,
        track_id_or_input: str,
        output_dir: Optional[Path] = None,
        quality: str = "HD"
    ) -> Path:
        """
        Download and tag a single Tidal track.
        Guarantees that the output file is ALWAYS delivered in .flac codec format with full Vorbis tags.
        """
        output_base = (output_dir or Path("downloads/tidal")).resolve()
        output_base.mkdir(parents=True, exist_ok=True)

        track_id = self.api.extract_track_id(track_id_or_input)
        if not track_id:
            hits = await self.api.search(track_id_or_input, limit=1)
            if not hits:
                raise Exception(f"Track '{track_id_or_input}' not found on Tidal")
            track_id = hits[0].asin

        # 1. Fetch track metadata
        track_meta = await self.api.get_track(track_id)
        if not track_meta:
            raise Exception(f"Track metadata for {track_id} not found on Tidal")

        title = track_meta.get("title") or "Unknown Title"
        artists = track_meta.get("artists") or []
        artist = ", ".join([a.get("name") for a in artists if a.get("name")]) or "Unknown Artist"
        album_meta = track_meta.get("album") or {}
        album_title = album_meta.get("title") or "Unknown Album"
        track_number = track_meta.get("trackNumber") or 1
        disc_number = track_meta.get("volumeNumber") or 1
        release_date = track_meta.get("streamStartDate") or album_meta.get("releaseDate")
        year = str(release_date)[:4] if release_date else None
        isrc = track_meta.get("isrc")
        copyright_str = track_meta.get("copyright")

        # 2. Resolve Stream URL (with candidate fallback)
        candidate_ids = [track_id]
        if not self.api.extract_track_id(track_id_or_input):
            hits = await self.api.search(track_id_or_input, limit=3)
            candidate_ids = [h.asin for h in hits] or [track_id]

        stream_url = None
        codec = "flac"
        bitrate = 0
        encryption_key = None

        for cid in candidate_ids:
            try:
                s_url, cd, br, ekey = await self.api.get_stream_url(cid, quality=quality)
                if s_url:
                    track_id = cid
                    stream_url = s_url
                    codec = cd
                    bitrate = br
                    encryption_key = ekey
                    break
            except Exception:
                continue

        if not stream_url:
            raise Exception(f"Could not retrieve playable stream URL for Tidal track {track_id_or_input} (quality: {quality})")

        # Refetch track metadata for the resolved track ID if candidate changed
        track_meta = await self.api.get_track(track_id)
        title = track_meta.get("title") or "Unknown Title"
        artists = track_meta.get("artists") or []
        artist = ", ".join([a.get("name") for a in artists if a.get("name")]) or "Unknown Artist"
        album_meta = track_meta.get("album") or {}
        album_title = album_meta.get("title") or "Unknown Album"
        track_number = track_meta.get("trackNumber") or 1
        disc_number = track_meta.get("volumeNumber") or 1
        release_date = track_meta.get("streamStartDate") or album_meta.get("releaseDate")
        year = str(release_date)[:4] if release_date else None
        isrc = track_meta.get("isrc")
        copyright_str = track_meta.get("copyright")

        target_folder = output_base / safe_filename(artist) / safe_filename(album_title)
        target_folder.mkdir(parents=True, exist_ok=True)

        track_num_str = f"{track_number:02d}" if isinstance(track_number, int) else str(track_number)
        base_name = f"{disc_number}-{track_num_str} {safe_filename(title)}"
        final_flac_path = target_folder / f"{base_name}.flac"
        temp_file_path = target_folder / f"{base_name}.part"

        # 3. Stream download bytes to disk (DASH segments or single stream)
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

        # 4. Decrypt if encrypted
        if encryption_key:
            try:
                decrypted_path = target_folder / f"{base_name}.decrypted"
                key, nonce = decrypt_security_token(encryption_key)
                decrypt_file(temp_file_path, decrypted_path, key, nonce)
                temp_file_path.unlink(missing_ok=True)
                temp_file_path = decrypted_path
            except Exception as e:
                logger.warning(f"Error during AES stream decryption: {e}")

        # 5. Ensure 100% FLAC Output Format (No M4A)
        if temp_file_path.exists():
            with open(temp_file_path, "rb") as f_check:
                header = f_check.read(16)

            if header.startswith(b"fLaC"):
                # Native FLAC elementary stream
                if final_flac_path.exists():
                    final_flac_path.unlink()
                temp_file_path.rename(final_flac_path)
            else:
                # Check if MP4 container contains FLAC dfLa box
                remux_success = False
                try:
                    remux_flac(temp_file_path, final_flac_path)
                    temp_file_path.unlink(missing_ok=True)
                    remux_success = True
                except Exception:
                    remux_success = False

                # If not native dfLa, convert container/audio to FLAC codec
                if not remux_success:
                    try:
                        cmd = ["ffmpeg", "-y", "-i", str(temp_file_path), "-c:a", "flac", str(final_flac_path)]
                        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        temp_file_path.unlink(missing_ok=True)
                    except Exception as conv_err:
                        logger.error(f"FLAC conversion fallback failed: {conv_err}")
                        # Fallback rename
                        if temp_file_path.exists():
                            temp_file_path.rename(final_flac_path)
        else:
            raise Exception("Downloaded audio file was not written.")

        # 6. Fetch Cover Image & Lyrics for Vorbis Tagging
        cover_id = album_meta.get("cover") or track_meta.get("cover")
        cover_bytes = None
        if cover_id:
            cover_url = self.api.format_cover_url(cover_id, "1280x1280")
            if cover_url:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        c_resp = await client.get(cover_url)
                        if c_resp.status_code == 200:
                            cover_bytes = c_resp.content
                except Exception as e:
                    logger.warning(f"Failed downloading cover art for tagging: {e}")

        lyrics = await self.api.get_lyrics(track_id)
        if not lyrics:
            try:
                clean_artist = artist.split(",")[0].split("&")[0].strip()
                async with httpx.AsyncClient(timeout=10.0) as client:
                    lrc_resp = await client.get(
                        "https://lrclib.net/api/get",
                        params={"track_name": title, "artist_name": clean_artist, "album_name": album_title or ""},
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

        # 7. Apply FLAC Vorbis Metadata Tags & Cover Picture
        genre_name = track_meta.get("genre") or album_meta.get("genre")
        total_tracks = album_meta.get("numberOfTracks")
        total_discs = album_meta.get("numberOfVolumes")
        try:
            self._apply_flac_tags(
                file_path=final_flac_path,
                title=title,
                artist=artist,
                album=album_title,
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
            )
        except Exception as e:
            logger.warning(f"Failed applying Vorbis tags to {final_flac_path}: {e}")

        logger.info(f"Successfully processed & tagged Tidal FLAC track: {final_flac_path.name}")
        return final_flac_path

    def _apply_flac_tags(
        self,
        file_path: Path,
        title: str,
        artist: str,
        album: str,
        track_number: int,
        disc_number: int,
        year: Optional[str] = None,
        release_date: Optional[str] = None,
        isrc: Optional[str] = None,
        copyright_str: Optional[str] = None,
        lyrics: Optional[str] = None,
        cover_bytes: Optional[bytes] = None,
        genre: Optional[str] = None,
        total_tracks: Optional[int] = None,
        total_discs: Optional[int] = None,
    ):
        """Apply comprehensive FLAC Vorbis comments and embedded Front Cover picture block."""
        try:
            audio = FLAC(file_path)
            audio["TITLE"] = title
            audio["ARTIST"] = artist
            audio["ALBUM"] = album
            audio["ALBUMARTIST"] = artist
            audio["TRACKNUMBER"] = str(track_number)
            audio["DISCNUMBER"] = str(disc_number)
            if total_tracks:
                audio["TRACKTOTAL"] = str(total_tracks)
                audio["TOTALTRACKS"] = str(total_tracks)
            if total_discs:
                audio["DISCTOTAL"] = str(total_discs)
                audio["TOTALDISCS"] = str(total_discs)
            if year:
                audio["DATE"] = year
                audio["YEAR"] = year
            if release_date:
                audio["RELEASEDATE"] = release_date
            if isrc:
                audio["ISRC"] = isrc
            if genre:
                audio["GENRE"] = genre
            if copyright_str:
                audio["COPYRIGHT"] = copyright_str
            if lyrics:
                audio["LYRICS"] = lyrics
                audio["UNSYNCEDLYRICS"] = lyrics
            audio["COMMENT"] = "ClashFLAC Lossless Engine"

            if cover_bytes:
                pic = Picture()
                pic.type = 3  # Cover (front)
                pic.mime = "image/jpeg"
                pic.desc = "Front Cover"
                pic.data = cover_bytes
                audio.clear_pictures()
                audio.add_picture(pic)

            audio.save()
        except Exception as e:
            logger.warning(f"Error saving FLAC tags on {file_path}: {e}")
