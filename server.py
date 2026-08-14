import os
import sys
import json
import re

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import xmltodict
import asyncio
import logging
import httpx
import shutil
import gc
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel
from dotenv import load_dotenv
from amzdl.utils import safe_filename, build_output_filename
from urllib.parse import quote

# Load environment variables from .env
load_dotenv()

import base64

# Restore credentials from AMZ_CREDENTIALS_BASE64 if running on cloud/Railway
creds_b64 = (os.getenv("AMZ_CREDENTIALS_BASE64") or "").strip()
if creds_b64:
    for target in [Path("credentials.bin"), Path("config/credentials.bin")]:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(creds_b64))
        except Exception as e:
            pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("server")

# Cloudflare Turnstile Secret Key configuration
TURNSTILE_SECRET_KEY = (os.getenv("CLOUDFLARE_SECRET_KEY") or os.getenv("TURNSTILE_SECRET_KEY") or "").strip()
CLOUDFLARE_SITE_KEY = (os.getenv("CLOUDFLARE_SITE_KEY") or os.getenv("TURNSTILE_SITE_KEY") or "").strip()

if TURNSTILE_SECRET_KEY:
    logger.info(f"Cloudflare Turnstile bot protection: ENABLED (Site Key: {CLOUDFLARE_SITE_KEY or 'configured'})")
else:
    logger.info("Cloudflare Turnstile bot protection: DISABLED (dev mode / bypass)")

async def verify_turnstile_token(
    x_turnstile_token: Optional[str] = Header(default=None, alias="X-Turnstile-Token"),
    request: Request = None
) -> bool:
    """
    Validate Cloudflare Turnstile token against Cloudflare's siteverify API.
    If TURNSTILE_SECRET_KEY is empty, verification passes automatically.
    """
    if not TURNSTILE_SECRET_KEY:
        return True

    if not x_turnstile_token:
        logger.warning("Request rejected: missing X-Turnstile-Token header.")
        raise HTTPException(
            status_code=403,
            detail="Cloudflare Turnstile bot challenge required."
        )

    try:
        client_ip = None
        if request:
            # Check X-Forwarded-For (Cloudflare CF-Connecting-IP)
            cf_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For")
            if cf_ip:
                client_ip = cf_ip.split(",")[0].strip()
            elif request.client:
                client_ip = request.client.host

        async with httpx.AsyncClient(timeout=8.0) as client:
            payload = {
                "secret": TURNSTILE_SECRET_KEY,
                "response": x_turnstile_token,
            }
            if client_ip:
                payload["remoteip"] = client_ip

            res = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=payload
            )
            data = res.json()
            if data.get("success"):
                return True
            else:
                error_codes = data.get("error-codes", [])
                logger.warning(f"Turnstile token verification failed: {error_codes}")
                raise HTTPException(
                    status_code=403,
                    detail=f"Turnstile bot verification failed ({', '.join(error_codes) if error_codes else 'invalid token'})."
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error communicating with Cloudflare Turnstile API: {e}")
        raise HTTPException(status_code=502, detail="Failed to verify bot challenge with Cloudflare.")

from amzdl.api.auth import _load_store
from amzdl.api.amzn_api import AmazonMusicMobileAPI
from amazonmusic.models import AmazonRegion
from amzdl.download.download import download as amzdl_download
from amzdl.metadata.metadata import fetch_metadata

app = FastAPI(
    title="Amazon Music Metadata & Resolution API",
    description="Strict Amazon Music API integration for catalog search and metadata resolution.",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


def cleanup_download_artifact(file_path: Path, output_dir: Path):
    """
    Safely delete streamed file and any empty directories from downloads/,
    followed by garbage collection to immediately free container memory.
    """
    try:
        if file_path and file_path.is_file():
            file_path.unlink(missing_ok=True)
            logger.info(f"Cleaned up streamed audio file: {file_path.name}")
        
        # Remove empty parent directories up to output_dir
        if file_path and output_dir:
            parent = file_path.parent
            while parent != output_dir and parent.exists():
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break
    except Exception as e:
        logger.warning(f"Error during artifact cleanup: {e}")
    
    # Force garbage collection to release cached buffers in Linux container
    try:
        gc.collect()
    except Exception:
        pass


@app.on_event("startup")
def purge_stale_downloads():
    """Purge stale files from downloads directory on startup."""
    try:
        dl_dir = Path("downloads").resolve()
        if dl_dir.exists():
            for item in dl_dir.iterdir():
                if item.name == ".gitkeep":
                    continue
                if item.is_file():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
            logger.info("Purged stale download artifacts from disk on startup.")
    except Exception as e:
        logger.warning(f"Startup purge failed: {e}")


class ResolveRequest(BaseModel):
    input: str  # ASIN, Amazon Music Link, or Search Query
    quality: Optional[str] = "HD"


class DownloadRequest(BaseModel):
    input: str  # ASIN, Amazon Music Link, or Search Query
    quality: Optional[str] = "HD"


class DownloadResponse(BaseModel):
    status: str
    message: str
    asin: str
    output_dir: str
    type: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    track_count: Optional[int] = None


class TrackResponse(BaseModel):
    source_type: str = "amazon"
    asin: str
    title: str
    artist: str
    album: Optional[str] = None
    duration_sec: int
    thumbnail_url: Optional[str] = None
    stream_url: Optional[str] = None
    codec: Optional[str] = "flac"
    bitrate: Optional[int] = 0


class SearchResultItem(BaseModel):
    asin: str
    title: str
    artist: Optional[str] = "Unknown Artist"
    album: Optional[str] = None
    duration_sec: int
    thumbnail_url: Optional[str] = None
    url: str
    release_date: Optional[str] = None
    year: Optional[str] = None
    genre: Optional[str] = None
    explicit: bool = False
    track_number: Optional[int] = None
    disc_number: Optional[int] = None


class SpotifyMetadataItem(BaseModel):
    spotify_id: Optional[str] = None
    title: str
    artist: str
    album: Optional[str] = None
    thumbnail_url: Optional[str] = None
    thumbnail_hq: Optional[str] = None
    year: Optional[str] = None
    release_date: Optional[str] = None


async def fetch_spotify_search(query: str, limit: int = 5) -> List[SpotifyMetadataItem]:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
        'Referer': 'https://open.spotify.com/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'
    }
    results = []
    query = query.strip()
    if not query:
        return results

    async with httpx.AsyncClient(timeout=7.0, follow_redirects=True, headers=headers) as client:
        # Method 1: Check if input is a Spotify track ID or link
        track_id_match = re.search(r'(?:spotify\.com/(?:intl-[a-zA-Z\-]+/)?track/|spotify:track:|^)([a-zA-Z0-9]{22})', query)
        if track_id_match:
            track_id = track_id_match.group(1)
            try:
                emb_res = await client.get(f'https://open.spotify.com/embed/track/{track_id}')
                if emb_res.status_code == 200:
                    m = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">([^<]+)</script>', emb_res.text)
                    if m:
                        data = json.loads(m.group(1))
                        entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                        title = entity.get("name") or "Unknown Title"
                        artists = ", ".join([a.get("name") for a in entity.get("artists", []) if a.get("name")]) or "Unknown Artist"
                        images = entity.get("visualIdentity", {}).get("image", [])
                        hq_img = images[0].get("url") if len(images) > 0 else ""
                        thumb_img = images[1].get("url") if len(images) > 1 else hq_img
                        release_date = entity.get("releaseDate", {}).get("isoString", "")
                        year = release_date[:4] if release_date else ""
                        album_name = entity.get("album", {}).get("name", "")
                        return [SpotifyMetadataItem(
                            spotify_id=track_id,
                            title=title,
                            artist=artists,
                            album=album_name,
                            thumbnail_url=thumb_img,
                            thumbnail_hq=hq_img,
                            year=year,
                            release_date=release_date
                        )]
            except Exception as e:
                logger.warning(f"Spotify embed metadata fetch failed: {e}")

            # Fallback for track link/ID: Official oEmbed
            try:
                oembed_res = await client.get(f'https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}')
                if oembed_res.status_code == 200:
                    odata = oembed_res.json()
                    title = odata.get("title") or "Unknown Title"
                    author = odata.get("author_name") or "Unknown Artist"
                    thumb = odata.get("thumbnail_url") or ""
                    return [SpotifyMetadataItem(
                        spotify_id=track_id,
                        title=title,
                        artist=author,
                        album="",
                        thumbnail_url=thumb,
                        thumbnail_hq=thumb,
                        year="",
                        release_date=""
                    )]
            except Exception as e:
                logger.warning(f"Spotify oEmbed fetch failed: {e}")

        # Method 2: Fetch via Spotify Web API get_access_token
        try:
            r = await client.get('https://open.spotify.com/get_access_token?reason=transport&productType=web_player')
            if r.status_code == 200:
                token = r.json().get("accessToken")
                if token:
                    sr = await client.get(
                        f'https://api.spotify.com/v1/search?q={quote(query)}&type=track&limit={limit}',
                        headers={'Authorization': f'Bearer {token}'}
                    )
                    if sr.status_code == 200:
                        tracks = sr.json().get('tracks', {}).get('items', [])
                        for t in tracks:
                            imgs = t.get('album', {}).get('images', [])
                            hq_img = imgs[0]['url'] if len(imgs) > 0 else ''
                            thumb_img = imgs[1]['url'] if len(imgs) > 1 else hq_img
                            artists = ", ".join([a.get('name') for a in t.get('artists', []) if a.get('name')])
                            rel_date = t.get('album', {}).get('release_date', '')
                            results.append(SpotifyMetadataItem(
                                spotify_id=t.get('id'),
                                title=t.get('name'),
                                artist=artists,
                                album=t.get('album', {}).get('name', ''),
                                thumbnail_url=thumb_img,
                                thumbnail_hq=hq_img,
                                year=rel_date[:4] if rel_date else '',
                                release_date=rel_date
                            ))
                        if results:
                            return results
        except Exception as e:
            logger.warning(f"Spotify API search failed: {e}")

    return results



class AmazonMusicResolver:
    def __init__(self):
        self._initialized = False
        self._session = None
        self._region = None
        self._creds = None

    def ensure_initialized(self):
        if self._initialized:
            return
        store = _load_store()
        if not store:
            raise HTTPException(
                status_code=401,
                detail="No registered Amazon Music credentials found. Please run device registration first."
            )
        
        self._creds = next(iter(store.values()))
        self._region = AmazonRegion.get_region_by_country(self._creds.account_region.country if self._creds.account_region else "US")
        self._creds.account_region = self._region

        self._session = AmazonMusicMobileAPI.__new__(AmazonMusicMobileAPI)
        self._session.credentials = self._creds
        self._session.session = self._session._create_httpx_session()
        self._initialized = True

    @property
    def session(self):
        self.ensure_initialized()
        return self._session

    @property
    def region(self):
        self.ensure_initialized()
        return self._region

    def search(self, query: str, limit: int = 5) -> List[SearchResultItem]:
        docs = self.session.search(query, search_types=('catalog_track',), limit=limit, region_to_use=self.region)
        results = []
        for doc in docs:
            asin = doc.get("asin") or doc.get("requestedAsin")
            if not asin:
                continue
            
            artist_data = doc.get("artist") or {}
            artist_name = (
                artist_data.get("name") if isinstance(artist_data, dict) else artist_data
            ) or doc.get("artistName") or doc.get("primaryArtistName") or "Unknown Artist"
            album_data = doc.get("album") or {}
            album_name = (
                album_data.get("title") if isinstance(album_data, dict) else album_data
            ) or doc.get("albumName") or "Unknown Album"
            image_url = album_data.get("image") if isinstance(album_data, dict) else None
            image_url = image_url or doc.get("image")
            art_original = doc.get("artOriginal") or {}
            if not image_url and isinstance(art_original, dict):
                image_url = art_original.get("URL") or art_original.get("artUrl")

            release_timestamp = doc.get("originalReleaseDate")
            release_date = None
            if release_timestamp:
                try:
                    release_date = datetime.fromtimestamp(float(release_timestamp), tz=timezone.utc).date().isoformat()
                except (TypeError, ValueError, OSError):
                    release_date = None
            parental_controls = doc.get("parentalControls") or {}

            results.append(SearchResultItem(
                asin=asin,
                title=doc.get("title") or "Unknown Title",
                artist=artist_name,
                album=album_name,
                duration_sec=int(doc.get("duration") or 0),
                thumbnail_url=image_url,
                url=f"https://music.amazon.com/albums/{doc.get('albumAsin', '')}?trackAsin={asin}",
                release_date=release_date,
                year=release_date[:4] if release_date else None,
                genre=doc.get("primaryGenre"),
                explicit=bool(parental_controls.get("hasExplicitLanguage")) if isinstance(parental_controls, dict) else False,
                track_number=doc.get("trackNum"),
                disc_number=doc.get("discNum"),
            ))
        return results

    def resolve(self, input_str: str, quality: str = "HD") -> TrackResponse:
        input_str = input_str.strip()

        # Extract ASIN from link or raw input
        asin = input_str
        match = re.search(r'(?:trackAsin=|albums/|tracks/)([A-Z0-9]{10})', input_str, re.IGNORECASE)
        if match:
            asin = match.group(1)
        elif not re.match(r'^[A-Z0-9]{10}$', input_str, re.IGNORECASE):
            # If plain text query, search Amazon catalog first
            search_hits = self.search(input_str, limit=1)
            if not search_hits:
                raise HTTPException(status_code=404, detail=f"Track '{input_str}' not found on Amazon Music")
            asin = search_hits[0].asin

        # 1. Fetch Track Metadata from Amazon Muse API
        meta_res = self.session.get_metadata(asin, region_to_use=self.region)
        tracks = meta_res.get("trackList") or meta_res.get("tracksList")
        if not tracks:
            raise HTTPException(status_code=404, detail=f"Metadata for ASIN {asin} not found on Amazon Music")
        
        track_doc = tracks[0]
        artist_data = track_doc.get("artist") or {}
        artist_name = (
            artist_data.get("name") if isinstance(artist_data, dict) else artist_data
        ) or track_doc.get("artistName") or track_doc.get("primaryArtistName") or "Unknown Artist"
        album_data = track_doc.get("album") or {}
        album_name = (
            album_data.get("title") if isinstance(album_data, dict) else album_data
        ) or track_doc.get("albumName") or "Unknown Album"
        image_url = album_data.get("image") if isinstance(album_data, dict) else None
        image_url = image_url or track_doc.get("image")
        art_original = track_doc.get("artOriginal") or {}
        if not image_url and isinstance(art_original, dict):
            image_url = art_original.get("URL") or art_original.get("artUrl")

        # 2. Fetch DASH Manifest from Amazon Digital Music Locator API
        force_3d = quality.startswith("SPATIAL")
        manifest_res = self.session._get_tracks_manifest((asin,), self.region, force_3d=force_3d)
        if not manifest_res or "manifest" not in manifest_res[0]:
            raise HTTPException(status_code=500, detail="Failed to retrieve Amazon DASH manifest")

        manifest_xml = manifest_res[0]["manifest"]
        parsed = xmltodict.parse(manifest_xml)
        period = parsed["MPD"]["Period"]
        adapt_sets = period["AdaptationSet"] if isinstance(period["AdaptationSet"], list) else [period["AdaptationSet"]]

        selected_rep = None
        selected_codec = "flac"
        selected_bitrate = 0

        for set_item in adapt_sets:
            reps = set_item.get("Representation")
            if not isinstance(reps, list):
                reps = [reps]
            
            for rep in reps:
                codec = rep.get("@codecs", "").lower()
                bitrate = int(rep.get("@bandwidth", "0"))

                if quality in ("HD", "UHD") and "flac" in codec:
                    if bitrate > selected_bitrate:
                        selected_rep = rep
                        selected_codec = "flac"
                        selected_bitrate = bitrate
                elif not selected_rep:
                    selected_rep = rep
                    selected_codec = codec
                    selected_bitrate = bitrate

        if not selected_rep:
            selected_rep = adapt_sets[0].get("Representation")[0]

        base_url = selected_rep.get("BaseURL")
        if isinstance(base_url, dict):
            base_url = base_url.get("#text")

        return TrackResponse(
            source_type="amazon",
            asin=asin,
            title=track_doc.get("title") or "Unknown Title",
            artist=artist_name,
            album=album_name,
            duration_sec=int(track_doc.get("duration") or 0),
            thumbnail_url=image_url,
            stream_url=base_url,
            codec=selected_codec,
            bitrate=selected_bitrate
        )


resolver = AmazonMusicResolver()

def clean_youtube_title(title: str) -> str:
    import html
    title = html.unescape(title)
    
    # Remove common brackets/parentheses noise
    noise_patterns = [
        r'[\(\[][^\]\)]*(?:official|video|audio|lyric|full|hd|4k|mp4|screen|clip|teaser|trailer|hq|exclusive|visualizer|shot|remix|slowed|reverb)[^\]\)]*[\)\]]',
        r'official\s+video', r'official\s+audio', r'official\s+music\s+video', r'lyrical\s+video', r'full\s+song',
        r'video\s+song', r'audio\s+song', r'lyric\s+song', r'[\(\[]\s*song\s*[\)\]]'
    ]
    for pattern in noise_patterns:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)
    
    # Handle '|' separators: Only take the first part
    if '|' in title:
        parts = [p.strip() for p in title.split('|') if p.strip()]
        title = parts[0]
            
    # Handle '-' separators
    if '-' in title or '–' in title or '—' in title:
        sep = '-' if '-' in title else ('–' if '–' in title else '—')
        parts = [p.strip() for p in title.split(sep) if p.strip()]
        if len(parts) >= 2:
            title = f"{parts[0]} {parts[1]}"
            
    # Clean up double spaces and punctuation at the ends safely
    title = re.sub(r'\s+', ' ', title)
    title = re.sub(r'^[\s\-_\|:\/\+\*\.]+|[\s\-_\|:\/\+\*\.]+$', '', title)
    
    return title.strip()

async def resolve_external_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url

    # 1. YouTube Link
    if "youtube.com/" in url or "youtu.be/" in url:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            try:
                resp = await client.get(f"https://noembed.com/embed?url={quote(url)}")
                if resp.status_code == 200:
                    data = resp.json()
                    title = data.get("title")
                    if title:
                        cleaned = clean_youtube_title(title)
                        logger.info(f"Resolved YouTube URL to search query: '{cleaned}'")
                        return cleaned
            except Exception as e:
                logger.error(f"Failed to resolve YouTube URL via noembed: {e}")
        raise HTTPException(status_code=400, detail="Could not resolve YouTube video title. Please search by name directly.")

    # 2. Spotify Link
    elif "spotify.com" in url or "spotify.link" in url or "spoti.fi" in url or url.startswith("spotify:"):
        async with httpx.AsyncClient(timeout=7.0, follow_redirects=True) as client:
            # Handle redirect short links (spotify.link / spoti.fi)
            final_url = url
            if "spotify.link" in url or "spoti.fi" in url:
                try:
                    head_resp = await client.get(url)
                    final_url = str(head_resp.url)
                except Exception as e:
                    logger.warning(f"Spotify redirect resolution failed: {e}")

            track_match = re.search(r'(?:spotify\.com/(?:intl-[a-zA-Z\-]+/)?track/|spotify:track:)([a-zA-Z0-9]{22})', final_url)
            album_match = re.search(r'(?:spotify\.com/(?:intl-[a-zA-Z\-]+/)?album/|spotify:album:)([a-zA-Z0-9]{22})', final_url)
            target_id = track_match.group(1) if track_match else (album_match.group(1) if album_match else None)
            is_album = bool(album_match and not track_match)

            # Strategy 1: Spotify Embed Page
            if target_id:
                embed_url = f"https://open.spotify.com/embed/{'album' if is_album else 'track'}/{target_id}"
                try:
                    emb_res = await client.get(embed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                    if emb_res.status_code == 200:
                        m = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">([^<]+)</script>', emb_res.text)
                        if m:
                            data = json.loads(m.group(1))
                            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                            title = entity.get("name")
                            artists = [a.get("name") for a in entity.get("artists", []) if a.get("name")]
                            artist_str = ", ".join(artists) if artists else ""
                            if title:
                                query = f"{title} {artist_str}".strip()
                                logger.info(f"Resolved Spotify link via embed: '{query}'")
                                return query
                except Exception as e:
                    logger.warning(f"Spotify embed resolve failed: {e}")

            # Strategy 2: Official oEmbed API
            try:
                oembed_url = f"https://open.spotify.com/oembed?url={quote(final_url)}"
                oembed_res = await client.get(oembed_url)
                if oembed_res.status_code == 200:
                    odata = oembed_res.json()
                    title = odata.get("title")
                    author = odata.get("author_name") or ""
                    if title:
                        query = f"{title} {author}".strip()
                        logger.info(f"Resolved Spotify link via oEmbed: '{query}'")
                        return query
            except Exception as e:
                logger.warning(f"Spotify oEmbed resolve failed: {e}")

            # Strategy 3: Spotify Web Access Token
            if target_id and not is_album:
                try:
                    token_resp = await client.get(
                        'https://open.spotify.com/get_access_token?reason=transport&productType=web_player',
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                    )
                    if token_resp.status_code == 200:
                        token = token_resp.json().get("accessToken")
                        if token:
                            track_resp = await client.get(
                                f'https://api.spotify.com/v1/tracks/{target_id}',
                                headers={'Authorization': f'Bearer {token}'}
                            )
                            if track_resp.status_code == 200:
                                track_data = track_resp.json()
                                title = track_data.get("name")
                                artists = [a.get("name") for a in track_data.get("artists", []) if a.get("name")]
                                artist_str = ", ".join(artists) if artists else ""
                                query = f"{title} {artist_str}".strip()
                                logger.info(f"Resolved Spotify link via Web API: '{query}'")
                                return query
                except Exception as e:
                    logger.warning(f"Spotify Web API resolve failed: {e}")

        raise HTTPException(status_code=400, detail="Could not resolve Spotify track metadata. Please search by name directly.")

    return url


@app.post("/api/resolve", response_model=TrackResponse)
async def resolve_endpoint(
    req: ResolveRequest,
    x_turnstile_token: Optional[str] = Header(default=None, alias="X-Turnstile-Token"),
    request: Request = None
):
    """
    Strict Amazon Music Resolver: Resolves ASIN, Link, or Search Query directly via Amazon Music Mobile API.
    """
    await verify_turnstile_token(x_turnstile_token, request)
    try:
        resolved_input = await resolve_external_url(req.input)
        return resolver.resolve(resolved_input, quality=req.quality or "HD")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search", response_model=List[SearchResultItem])
async def search_endpoint(
    q: str = Query(..., description="Amazon Music catalog search query"),
    limit: int = Query(5, ge=1, le=20, description="Max search results")
):
    """
    Amazon Music Catalog Search Endpoint with automatic external URL resolution.
    """
    try:
        query = q.strip()
        if "http://" in query or "https://" in query or query.startswith("spotify:"):
            try:
                resolved = await resolve_external_url(query)
                if resolved:
                    query = resolved
            except Exception as e:
                logger.warning(f"Failed to resolve URL query in search_endpoint: {e}")
        return resolver.search(query, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/spotify/search", response_model=List[SpotifyMetadataItem])
async def spotify_search_endpoint(
    q: str = Query(..., description="Search query or Spotify track URL/ID"),
    limit: int = Query(5, ge=1, le=20, description="Max search results")
):
    """
    Spotify Metadata Search Endpoint: Returns high-resolution album artwork, thumbnails, and metadata from Spotify.
    """
    try:
        return await fetch_spotify_search(q, limit=limit)
    except Exception as e:
        logger.error(f"Spotify metadata search failed for '{q}': {e}")
        return []



@app.get("/api/resolve")
async def resolve_endpoint(q: str):
    """
    Resolve YouTube/Spotify links to a clean text search query.
    """
    try:
        resolved = await resolve_external_url(q)
        return {"resolved": resolved}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/download")
async def download_endpoint(
    req: DownloadRequest,
    x_turnstile_token: Optional[str] = Header(default=None, alias="X-Turnstile-Token"),
    request: Request = None
):
    """
    Download, decrypt, and save Amazon Music tracks locally.
    """
    await verify_turnstile_token(x_turnstile_token, request)
    try:
        resolved_input = await resolve_external_url(req.input)
        input_str = resolved_input.strip()

        # Extract ASIN from link or raw input
        asin = input_str
        match = re.search(r'(?:trackAsin=|albums/|tracks/)([A-Z0-9]{10})', input_str, re.IGNORECASE)
        if match:
            asin = match.group(1)
        elif not re.match(r'^[A-Z0-9]{10}$', input_str, re.IGNORECASE):
            # If plain text query, search Amazon catalog first
            search_hits = resolver.search(input_str, limit=1)
            if not search_hits:
                raise HTTPException(status_code=404, detail=f"Track '{input_str}' not found on Amazon Music")
            asin = search_hits[0].asin

        # Fetch metadata using fetch_metadata
        kind, meta = await asyncio.to_thread(fetch_metadata, resolver.session, asin)

        title = None
        artist = None
        album = None
        track_count = None

        if kind == "track":
            title = getattr(meta, "title", None)
            artist = getattr(meta, "artist", None)
            album = getattr(meta, "album_name", None)
        elif kind == "album":
            title = getattr(meta, "album_name", None)
            artist = getattr(meta, "artist", None)
            album = getattr(meta, "album_name", None)
            track_count = len(getattr(meta, "tracks", []))
        elif kind == "playlist":
            title = getattr(meta, "name", None)
            artist = "Various Artists"
            track_count = len(getattr(meta, "track_asins", []))
        elif kind == "artist":
            title = getattr(meta, "name", None)
            artist = getattr(meta, "name", None)
            track_count = len(getattr(meta, "album_asins", []))

        # Set output directory to "downloads"
        output_dir = Path("downloads").resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        quality_target = (req.quality or "UHD").upper()
        if quality_target not in {"HD", "UHD"}:
            raise HTTPException(status_code=422, detail="Quality must be either HD or UHD")

        await amzdl_download(
            session=resolver.session,
            asin=asin,
            output_dir=output_dir,
            quality=quality_target,
            plain=True
        )

        # For single track downloads, return the file directly to the browser
        if kind == "track":
            extension = ".flac"
            
            safe_artist = safe_filename(getattr(meta, "album_artist", None) or getattr(meta, "artist", None), False)
            safe_album = safe_filename(getattr(meta, "album_name", None), False)
            out_name = build_output_filename(str(getattr(meta, "disc", "1")), getattr(meta, "track_number", 1), getattr(meta, "title", "track"))
            
            file_path = output_dir / safe_artist / safe_album / (out_name + extension)
            
            # Robust fallback: find newest matching audio file created in output_dir
            if not file_path.exists():
                audio_candidates = sorted(
                    [p for p in output_dir.rglob("*") if p.is_file() and p.suffix.lower() in (".flac", ".opus", ".mp4", ".m4a")],
                    key=lambda x: x.stat().st_mtime,
                    reverse=True
                )
                if audio_candidates:
                    file_path = audio_candidates[0]
            
            if file_path.exists():
                return FileResponse(
                    path=str(file_path),
                    media_type="audio/flac",
                    filename=file_path.name,
                    headers={"Access-Control-Expose-Headers": "Content-Disposition"},
                    background=BackgroundTask(cleanup_download_artifact, file_path, output_dir)
                )
            else:
                raise HTTPException(status_code=500, detail="Downloaded audio file could not be located on the server.")

        display_name = title or asin
        if artist:
            message = f"Successfully downloaded '{display_name}' by {artist}"
        else:
            message = f"Successfully downloaded {kind} '{display_name}'"

        return DownloadResponse(
            status="success",
            message=message,
            asin=asin,
            output_dir=str(output_dir),
            type=kind,
            title=title,
            artist=artist,
            album=album,
            track_count=track_count
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception("Failed to download track/album")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Amazon Music API Resolver"}


@app.get("/api/config")
def get_public_config():
    return {
        "turnstile_site_key": CLOUDFLARE_SITE_KEY,
        "turnstile_enabled": bool(TURNSTILE_SECRET_KEY)
    }


# Serve Frontend Web App (Single Page Application)
frontend_dir = Path("frontend/dist").resolve() if (Path("frontend/dist").resolve() / "index.html").exists() else Path("frontend").resolve()

@app.get("/")
async def serve_spa_index():
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"status": "online", "service": "ClashFLAC Lossless API"}

@app.get("/{full_path:path}")
async def serve_spa_static(full_path: str):
    # Pass through API and system routes
    if full_path.startswith("api/") or full_path in ("health", "docs", "openapi.json", "redoc"):
        raise HTTPException(status_code=404, detail="Not Found")
    
    # 1. Serve specific static asset if it exists
    candidate = frontend_dir / full_path
    if candidate.is_file():
        return FileResponse(str(candidate))
    
    # 2. Check in root or frontend
    alt_candidate = Path("frontend").resolve() / full_path
    if alt_candidate.is_file():
        return FileResponse(str(alt_candidate))

    # 3. Fallback to index.html for SPA view routing
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    raise HTTPException(status_code=404, detail="Not Found")




