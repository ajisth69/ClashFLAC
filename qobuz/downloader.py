import os
import re
import shutil
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
import httpx
import mutagen
from mutagen.flac import FLAC, Picture

from .config import QobuzConfig
from .api import QobuzAPI

logger = logging.getLogger("qobuz.downloader")

def safe_filename(name: Optional[str], fallback: str = "track") -> str:
    if not name:
        return fallback
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name)).strip()
    return clean or fallback

def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
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

    if getattr(candidate, "hires", False) or (isinstance(candidate, dict) and candidate.get("hires")):
        score += 15

    return score

class QobuzDownloader:
    def __init__(self, api: Optional[QobuzAPI] = None):
        self.api = api or QobuzAPI()

    async def download_track(
        self,
        track_id_or_input: str,
        output_dir: Optional[Path] = None,
        quality: str = "UHD",
        track_hint: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[str, int], None]] = None,
    ) -> Path:
        """
        Download and tag a single Qobuz track with bit-perfect lossless quality and complete FLAC tags.
        """
        output_base = (output_dir or Path("downloads/qobuz")).resolve()
        output_base.mkdir(parents=True, exist_ok=True)
        track_hint = track_hint or {}

        def report(stage: str, progress: int) -> None:
            if progress_callback:
                progress_callback(stage, progress)

        target_title = track_hint.get("title") or ""
        target_artist = track_hint.get("artist") or ""
        target_duration = int(track_hint.get("duration") or track_hint.get("duration_sec") or 0)

        # 1. Resolve Track ID
        track_id = self.api.extract_track_id(track_id_or_input)
        candidate_ids = []

        if track_id:
            candidate_ids.append(track_id)
        else:
            clean_artist = re.sub(r'[,&/]|feat\..*$', ' ', target_artist).strip()
            clean_title = re.sub(r'[\(\[\{].*?[\)\]\}]', '', target_title).strip()
            queries = [
                f"{clean_title} {clean_artist}".strip(),
                clean_title,
                track_id_or_input.strip(),
            ]

            found_items = []
            for q in queries:
                if not q:
                    continue
                results = await self.api.search(q, limit=10)
                if results:
                    found_items.extend(results)
                    break

            if found_items:
                ranked = sorted(found_items, key=lambda c: score_candidate(c, target_title, target_artist, target_duration), reverse=True)
                for item in ranked:
                    if item.asin not in candidate_ids:
                        candidate_ids.append(item.asin)

        if not candidate_ids:
            candidate_ids = [track_id_or_input]

        report("Resolving Qobuz stream...", 10)

        # 2. Get file URL and metadata
        format_id = QobuzConfig.get_format_id(quality)
        file_info = None
        track_meta = None
        selected_id = None

        for cand_id in candidate_ids:
            try:
                meta = await self.api.get_track(cand_id)
                stream_dict = await self.api.get_file_url(cand_id, format_id=format_id)
                if stream_dict.get("url"):
                    file_info = stream_dict
                    track_meta = meta
                    selected_id = cand_id
                    break
            except Exception as e:
                logger.debug(f"Candidate {cand_id} failed: {e}")
                continue

        if not file_info or not file_info.get("url"):
            raise ValueError(f"Could not retrieve active Qobuz audio stream for: {track_id_or_input}")

        stream_url = file_info["url"]
        
        # 3. Extract metadata fields
        title = track_meta.get("title") or target_title or "track"
        if track_meta.get("version"):
            title += f" ({track_meta['version']})"

        performer = track_meta.get("performer", {})
        artist = performer.get("name") if isinstance(performer, dict) else (track_meta.get("artist", {}).get("name") or target_artist or "Unknown Artist")
        
        album_obj = track_meta.get("album", {})
        album = album_obj.get("title") if isinstance(album_obj, dict) else (track_hint.get("album") or "Unknown Album")
        
        album_artist = None
        if isinstance(album_obj, dict) and album_obj.get("artist"):
            album_artist = album_obj["artist"].get("name")
        album_artist = album_artist or artist

        track_num = track_meta.get("track_number") or 1
        disc_num = track_meta.get("media_number") or 1
        total_tracks = album_obj.get("tracks_count") if isinstance(album_obj, dict) else 1
        total_discs = album_obj.get("media_count") if isinstance(album_obj, dict) else 1

        release_date = track_meta.get("release_date_original") or track_meta.get("release_date") or (album_obj.get("release_date_original") if isinstance(album_obj, dict) else None)
        year = release_date[:4] if release_date and len(release_date) >= 4 else ""

        isrc = track_meta.get("isrc") or ""
        copyright_str = track_meta.get("copyright") or (album_obj.get("copyright") if isinstance(album_obj, dict) else "")

        cover_url = None
        if isinstance(album_obj, dict) and album_obj.get("image"):
            cover_url = album_obj["image"].get("large") or album_obj["image"].get("extralarge") or album_obj["image"].get("small")

        # 4. Stream audio content
        safe_t = safe_filename(title, "track")
        safe_a = safe_filename(artist, "artist")
        out_filename = f"{safe_a} - {safe_t}.flac"
        final_path = output_base / out_filename
        temp_path = output_base / f".tmp_{selected_id}_{int(asyncio.get_event_loop().time())}.flac"

        report("Downloading FLAC stream...", 20)

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", stream_url) as resp:
                resp.raise_for_status()
                total_bytes = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                
                with open(temp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_bytes > 0:
                                pct = 20 + int((downloaded / total_bytes) * 60)
                                report(f"Downloading FLAC... ({pct}%)", min(80, pct))

        report("Applying FLAC metadata...", 85)

        # 5. Fetch Cover Art
        cover_bytes = b""
        cover_mime = "image/jpeg"
        if cover_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    c_resp = await client.get(cover_url)
                    if c_resp.status_code == 200:
                        cover_bytes = c_resp.content
                        if "png" in cover_url.lower():
                            cover_mime = "image/png"
            except Exception as e:
                logger.warning(f"Could not download cover art from {cover_url}: {e}")

        # 6. Apply Vorbis Tags with Mutagen
        try:
            audio = FLAC(temp_path)
            audio["TITLE"] = title
            audio["ARTIST"] = artist
            audio["ALBUM"] = album
            audio["ALBUMARTIST"] = album_artist
            if year:
                audio["DATE"] = year
                audio["YEAR"] = year
            if release_date:
                audio["RELEASEDATE"] = release_date
            audio["TRACKNUMBER"] = str(track_num)
            audio["TRACKTOTAL"] = str(total_tracks)
            audio["DISCNUMBER"] = str(disc_num)
            audio["DISCTOTAL"] = str(total_discs)
            if isrc:
                audio["ISRC"] = isrc
            if copyright_str:
                audio["COPYRIGHT"] = copyright_str
            audio["COMMENT"] = "ClashFLAC Lossless Engine"

            if cover_bytes:
                pic = Picture()
                pic.type = 3  # Front Cover
                pic.mime = cover_mime
                pic.desc = "Front Cover"
                pic.data = cover_bytes
                audio.add_picture(pic)

            audio.save()
        except Exception as e:
            logger.warning(f"Error tagging FLAC audio: {e}")

        # Move to final location
        if final_path.exists():
            final_path.unlink()
        shutil.move(str(temp_path), str(final_path))
        
        report("Download complete", 100)
        logger.info(f"Successfully saved Qobuz FLAC: {final_path}")
        return final_path
