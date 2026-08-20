import re
import json
import base64
import logging
import asyncio
import xmltodict
from typing import Optional, List, Dict, Any, Tuple
import httpx

from .config import TidalConfig
from .auth import TidalAuth
from .models import (
    TidalSearchResultItem,
    TidalTrackResponse,
)

logger = logging.getLogger("tidal.api")

class TidalAPI:
    def __init__(self, auth: Optional[TidalAuth] = None):
        self.auth = auth or TidalAuth()

    @staticmethod
    def format_cover_url(cover_id: Optional[str], size: str = "1280x1280") -> Optional[str]:
        if not cover_id:
            return None
        # Format UUID: e.g. 29d49495-2bd0-424f-ba7d-3ef0ff3e5272 -> 29d49495/2bd0/424f/ba7d/3ef0ff3e5272
        clean_id = cover_id.replace("-", "/")
        return f"https://resources.tidal.com/images/{clean_id}/{size}.jpg"

    @staticmethod
    def extract_track_id(input_str: str) -> Optional[str]:
        input_str = input_str.strip()
        # Direct numeric ID
        if re.match(r"^\d{6,10}$", input_str):
            return input_str
        
        # URL match: /track/123456 or trackId=123456 or tidal:track:123456
        patterns = [
            r"tidal\.com/(?:browse/|intl-[a-z]+/)?track/(\d+)",
            r"listen\.tidal\.com/track/(\d+)",
            r"trackId=(\d+)",
            r"tidal:track:(\d+)",
        ]
        for p in patterns:
            m = re.search(p, input_str, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def extract_album_id(input_str: str) -> Optional[str]:
        input_str = input_str.strip()
        if re.match(r"^\d{6,10}$", input_str):
            return input_str
        patterns = [
            r"tidal\.com/(?:browse/|intl-[a-z]+/)?album/(\d+)",
            r"listen\.tidal\.com/album/(\d+)",
            r"albumId=(\d+)",
            r"tidal:album:(\d+)",
        ]
        for p in patterns:
            m = re.search(p, input_str, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    async def _request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute API request with automatic token header provisioning and countryCode.
        """
        url = f"{TidalConfig.API_URL}/{endpoint.lstrip('/')}"
        req_params = dict(params or {})
        if "countryCode" not in req_params:
            req_params["countryCode"] = self.auth.country_code

        req_headers = await self.auth.get_auth_headers()
        if headers:
            req_headers.update(headers)

        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.request(method, url, params=req_params, headers=req_headers, data=data)
            
            # If 401 Unauthorized and refresh token available, attempt refresh
            if resp.status_code == 401 and self.auth.user_refresh_token:
                logger.info("Tidal access token expired during request, attempting token refresh...")
                refreshed = await self.auth.refresh_access_token()
                if refreshed:
                    req_headers = await self.auth.get_auth_headers()
                    resp = await client.request(method, url, params=req_params, headers=req_headers, data=data)

            if resp.status_code != 200:
                logger.warning(f"Tidal API request failed ({resp.status_code}) on {url}: {resp.text}")
                try:
                    err_json = resp.json()
                    detail = err_json.get("userMessage") or err_json.get("message") or f"Tidal API Error ({resp.status_code})"
                except Exception:
                    detail = f"Tidal API Error ({resp.status_code}): {resp.text}"
                raise Exception(detail)

            return resp.json()

    async def search(self, query: str, limit: int = 20) -> List[TidalSearchResultItem]:
        """
        Search Tidal catalog for tracks and return formatted SearchResultItems.
        """
        query = query.strip()
        if not query:
            return []

        # Check if direct ID / URL
        direct_track_id = self.extract_track_id(query)
        if direct_track_id:
            try:
                track = await self.get_track(direct_track_id)
                if track:
                    return [self._format_search_item(track)]
            except Exception as e:
                logger.warning(f"Failed direct track lookup for ID {direct_track_id}: {e}")

        # Search tracks
        data = await self._request(
            "search/tracks",
            params={"query": query, "limit": limit, "offset": 0}
        )

        items = data.get("items") or []
        results: List[TidalSearchResultItem] = []
        for item in items:
            formatted = self._format_search_item(item)
            if formatted:
                results.append(formatted)

        return results

    def _format_search_item(self, doc: Dict[str, Any]) -> Optional[TidalSearchResultItem]:
        track_id = str(doc.get("id") or "")
        if not track_id:
            return None

        # Artist
        artists = doc.get("artists") or []
        artist_name = ", ".join([a.get("name") for a in artists if a.get("name")])
        if not artist_name:
            artist_doc = doc.get("artist") or {}
            artist_name = artist_doc.get("name") or "Unknown Artist"

        # Album
        album_doc = doc.get("album") or {}
        album_name = album_doc.get("title") or "Unknown Album"
        cover_id = album_doc.get("cover") or doc.get("cover")
        image_url = self.format_cover_url(cover_id)

        # Release date / Year
        release_date = doc.get("streamStartDate") or album_doc.get("releaseDate")
        year = None
        if release_date:
            year = str(release_date)[:4]

        # Audio modes / Quality
        audio_modes = doc.get("audioModes") or []
        audio_quality = doc.get("audioQuality") or "LOSSLESS"

        return TidalSearchResultItem(
            asin=track_id,
            title=doc.get("title") or "Unknown Title",
            artist=artist_name,
            album=album_name,
            duration_sec=int(doc.get("duration") or 0),
            thumbnail_url=image_url,
            url=f"https://tidal.com/browse/track/{track_id}",
            release_date=release_date,
            year=year,
            genre=doc.get("genre") or album_doc.get("genre"),
            explicit=bool(doc.get("explicit")),
            track_number=doc.get("trackNumber"),
            disc_number=doc.get("volumeNumber"),
            audio_quality=audio_quality,
            audio_modes=audio_modes,
            isrc=doc.get("isrc"),
        )

    async def get_track(self, track_id: str) -> Dict[str, Any]:
        """Fetch raw track metadata by track ID."""
        return await self._request(f"tracks/{track_id}")

    async def get_album(self, album_id: str) -> Dict[str, Any]:
        """Fetch album metadata by album ID."""
        return await self._request(f"albums/{album_id}")

    async def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        """Fetch all tracks for a given album."""
        data = await self._request(f"albums/{album_id}/tracks", params={"limit": 100})
        return data.get("items") or []

    async def get_lyrics(self, track_id: str) -> Optional[str]:
        """Fetch lyrics for a given track."""
        try:
            data = await self._request(f"tracks/{track_id}/lyrics")
            return data.get("lyrics") or data.get("subtitles")
        except Exception:
            return None

    async def get_stream_url(
        self,
        track_id: str,
        quality: str = "HD"
    ) -> Tuple[Optional[str], str, int, Optional[str]]:
        """Return the best stream compatible with the requested quality tier.

        UHD uses the best available Hi-Res stream. HD probes Hi-Res first, then
        chooses CD Lossless as the second tier when it exists.
        """
        requested = (quality or "HD").upper()
        quality_ladders = {
            "UHD": ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
            "HI_RES": ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
            "MASTER": ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
            "HD": ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
            "LOSSLESS": ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
            "HIGH": ["HIGH", "LOW"],
            "LOW": ["LOW"],
        }
        ladder = quality_ladders.get(requested, quality_ladders["HD"])

        hi_res_fallback = None
        for target_quality in ladder:
            stream_url, codec, bitrate, encryption_key = await self._get_stream_url_for_quality(
                track_id, target_quality
            )
            if not stream_url:
                continue

            # CD mode deliberately selects the second lossless tier. Still retain
            # Hi-Res so a track with no CD stream can use its best available copy.
            if requested in {"HD", "LOSSLESS"} and target_quality == "HI_RES_LOSSLESS":
                hi_res_fallback = (stream_url, codec, bitrate, encryption_key)
                continue

            if requested in {"HD", "LOSSLESS"} and hi_res_fallback and target_quality != "LOSSLESS":
                logger.info("Tidal track %s: CD lossless unavailable; using Hi-Res fallback", track_id)
                return hi_res_fallback

            if target_quality != ladder[0]:
                logger.info(
                    "Tidal track %s: requested %s unavailable; using %s fallback",
                    track_id,
                    requested,
                    target_quality,
                )
            return stream_url, codec, bitrate, encryption_key

        if hi_res_fallback:
            logger.info("Tidal track %s: CD lossless unavailable; using Hi-Res fallback", track_id)
            return hi_res_fallback

        return None, "flac", 0, None

    async def _get_stream_url_for_quality(
        self,
        track_id: str,
        target_quality: str,
    ) -> Tuple[Optional[str], str, int, Optional[str]]:
        """
        Resolve one exact Tidal quality tier from playback info or streamUrl.
        Returns: (stream_url, codec, bitrate, encryption_key)
        """
        # 1. Strategy A: playbackinfopostpaywall (Main Tidal streaming endpoint)
        try:
            pb_info = await self._request(
                f"tracks/{track_id}/playbackinfopostpaywall",
                params={
                    "audioquality": target_quality,
                    "playbackmode": "STREAM",
                    "assetpresentation": "FULL",
                }
            )

            manifest_mime = pb_info.get("manifestMimeType", "")
            manifest_raw = pb_info.get("manifest", "")

            # A1: application/vnd.tidal.bts (JSON inside Base64)
            if "vnd.tidal.bts" in manifest_mime and manifest_raw:
                try:
                    bts_json = json.loads(base64.b64decode(manifest_raw).decode("utf-8"))
                    urls = bts_json.get("urls") or []
                    codec = bts_json.get("codecs") or "flac"
                    encryption_type = bts_json.get("encryptionType", "NONE")
                    encryption_key = bts_json.get("securityToken") or bts_json.get("encryptionKey")
                    bitrate = int(bts_json.get("bitRate") or (1411000 if "flac" in codec else 320000))
                    
                    if urls:
                        return urls[0], codec, bitrate, encryption_key if encryption_type != "NONE" else None
                except Exception as e:
                    logger.warning(f"Error parsing Tidal BTS manifest: {e}")

            # A2: application/dash+xml (MPEG-DASH XML manifest inside Base64 or plain XML)
            elif ("dash+xml" in manifest_mime or "xml" in manifest_mime) and manifest_raw:
                try:
                    # Check if base64 encoded
                    xml_str = manifest_raw
                    if not manifest_raw.strip().startswith("<"):
                        try:
                            xml_str = base64.b64decode(manifest_raw).decode("utf-8")
                        except Exception:
                            pass

                    # Strategy 1: SegmentTemplate DASH (Tidal Hi-Res Master & Lossless 16/24-bit streams)
                    init_match = re.search(r'initialization="([^"]+)"', xml_str)
                    media_match = re.search(r'media="([^"]+)"', xml_str)
                    if init_match and media_match:
                        import html as py_html
                        init_url = py_html.unescape(init_match.group(1))
                        media_tmpl = py_html.unescape(media_match.group(1))
                        s_tags = re.findall(r'<S\s+([^>]+)/>', xml_str)
                        total_segments = 0
                        for s_attr in s_tags:
                            r_match = re.search(r'r="(\d+)"', s_attr)
                            if r_match:
                                total_segments += int(r_match.group(1)) + 1
                            else:
                                total_segments += 1
                        total_segments = max(total_segments, 1)
                        dash_info = {
                            "is_dash": True,
                            "init_url": init_url,
                            "media_template": media_tmpl,
                            "total_segments": total_segments,
                        }
                        return dash_info, "flac", 2400000, None

                    # Strategy 2: BaseURL DASH
                    parsed = xmltodict.parse(xml_str)
                    period = parsed.get("MPD", {}).get("Period", {})
                    adapt_sets = period.get("AdaptationSet", [])
                    if not isinstance(adapt_sets, list):
                        adapt_sets = [adapt_sets]

                    best_url = None
                    best_bitrate = 0
                    best_codec = "flac"

                    for aset in adapt_sets:
                        reps = aset.get("Representation", [])
                        if not isinstance(reps, list):
                            reps = [reps]
                        for rep in reps:
                            bw = int(rep.get("@bandwidth", 0))
                            cd = rep.get("@codecs", "flac").lower()
                            base_url_obj = rep.get("BaseURL")
                            curr_url = None
                            if isinstance(base_url_obj, str):
                                curr_url = base_url_obj
                            elif isinstance(base_url_obj, dict):
                                curr_url = base_url_obj.get("#text")

                            if curr_url and bw >= best_bitrate:
                                best_url = curr_url
                                best_bitrate = bw
                                best_codec = cd

                    if best_url:
                        return best_url, best_codec, best_bitrate, None
                except Exception as e:
                    logger.warning(f"Error parsing Tidal DASH manifest: {e}")

        except Exception as e:
            logger.warning(f"Playbackinfo endpoint resolution failed for track {track_id}: {e}")

        # 2. Strategy B: Legacy / direct streamUrl endpoint
        try:
            stream_data = await self._request(
                f"tracks/{track_id}/streamUrl",
                params={"soundQuality": target_quality}
            )
            url = stream_data.get("url")
            codec = stream_data.get("codec", "flac")
            encryption_key = stream_data.get("securityToken") or stream_data.get("encryptionKey")
            bitrate = 1411000 if codec == "flac" else 320000
            if url:
                return url, codec, bitrate, encryption_key
        except Exception as e:
            logger.warning(f"Legacy streamUrl resolution failed for track {track_id}: {e}")

        # 3. Strategy C: urlPostPaywall
        try:
            url_data = await self._request(
                f"tracks/{track_id}/urlPostPaywall",
                params={"audioquality": target_quality, "assetpresentation": "FULL"}
            )
            url = url_data.get("url")
            codec = url_data.get("codec", "flac")
            if url:
                return url, codec, 1411000 if codec == "flac" else 320000, None
        except Exception as e:
            logger.warning(f"urlPostPaywall resolution failed for track {track_id}: {e}")

        return None, "flac", 0, None

    async def resolve(self, input_str: str, quality: str = "HD") -> TidalTrackResponse:
        """
        Resolve ASIN/Track ID, Tidal URL, or plain search query into full TidalTrackResponse.
        """
        input_str = input_str.strip()
        track_id = self.extract_track_id(input_str)

        if not track_id:
            # Plain search query
            hits = await self.search(input_str, limit=1)
            if not hits:
                raise Exception(f"Track '{input_str}' not found on Tidal")
            track_id = hits[0].asin

        # Fetch metadata
        track_doc = await self.get_track(track_id)
        if not track_doc:
            raise Exception(f"Metadata for Tidal track {track_id} not found")

        # Resolve stream
        stream_url, codec, bitrate, _ = await self.get_stream_url(track_id, quality)

        artists = track_doc.get("artists") or []
        artist_name = ", ".join([a.get("name") for a in artists if a.get("name")])
        if not artist_name:
            artist_doc = track_doc.get("artist") or {}
            artist_name = artist_doc.get("name") or "Unknown Artist"

        album_doc = track_doc.get("album") or {}
        album_name = album_doc.get("title") or "Unknown Album"
        cover_id = album_doc.get("cover") or track_doc.get("cover")
        image_url = self.format_cover_url(cover_id)

        return TidalTrackResponse(
            source_type="tidal",
            asin=track_id,
            title=track_doc.get("title") or "Unknown Title",
            artist=artist_name,
            album=album_name,
            duration_sec=int(track_doc.get("duration") or 0),
            thumbnail_url=image_url,
            stream_url=stream_url,
            codec=codec,
            bitrate=bitrate,
        )
