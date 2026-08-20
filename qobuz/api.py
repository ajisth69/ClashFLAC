import asyncio
import time
import hashlib
import re
import logging
from typing import Optional, Dict, Any, List
import httpx

from .config import QobuzConfig
from .auth import QobuzAuth
from .models import QobuzSearchResultItem, QobuzTrackResponse

logger = logging.getLogger("qobuz.api")

class QobuzAPI:
    def __init__(self, auth: Optional[QobuzAuth] = None):
        self.auth = auth or QobuzAuth()
        self._valid_secret: Optional[str] = None

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "X-App-Id": str(self.auth.app_id or QobuzConfig.DEFAULT_APP_ID),
        }
        if self.auth.user_auth_token:
            headers["X-User-Auth-Token"] = self.auth.user_auth_token
        return headers

    def extract_track_id(self, input_str: str) -> Optional[str]:
        if not input_str:
            return None
        input_str = input_str.strip()
        if re.match(r'^\d+$', input_str):
            return input_str
        
        # open.qobuz.com/track/123456 or play.qobuz.com/track/123456
        track_match = re.search(r'(?:open|play)\.qobuz\.com/(?:[a-z]{2}-[a-z]{2}/)?track/(\d+)', input_str, re.IGNORECASE)
        if track_match:
            return track_match.group(1)

        # qobuz:track:123456
        uri_match = re.search(r'qobuz:track:(\d+)', input_str, re.IGNORECASE)
        if uri_match:
            return uri_match.group(1)

        return None

    def extract_album_id(self, input_str: str) -> Optional[str]:
        if not input_str:
            return None
        input_str = input_str.strip()
        album_match = re.search(r'(?:open|play)\.qobuz\.com/(?:[a-z]{2}-[a-z]{2}/)?album/([a-zA-Z0-9]+)', input_str, re.IGNORECASE)
        if album_match:
            return album_match.group(1)
        return None

    async def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{QobuzConfig.API_BASE}{endpoint}"
        req_params = dict(params or {})
        if "app_id" not in req_params:
            req_params["app_id"] = str(self.auth.app_id or QobuzConfig.DEFAULT_APP_ID)
        if self.auth.user_auth_token and "user_auth_token" not in req_params:
            req_params["user_auth_token"] = self.auth.user_auth_token
        
        headers = self._get_headers()
        async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
            resp = await client.get(url, params=req_params)
            if resp.status_code == 400 and "app_id" in resp.text.lower():
                # Try fallback app id
                for fallback_id in QobuzConfig.FALLBACK_APP_IDS:
                    if fallback_id != req_params["app_id"]:
                        req_params["app_id"] = fallback_id
                        headers["X-App-Id"] = fallback_id
                        resp2 = await client.get(url, params=req_params)
                        if resp2.status_code == 200:
                            self.auth.app_id = fallback_id
                            return resp2.json()
            resp.raise_for_status()
            return resp.json()

    async def search(self, query: str, limit: int = 20) -> List[QobuzSearchResultItem]:
        """Search Qobuz catalog for tracks matching query with concurrent multi-query expansion."""
        if not query or not query.strip():
            return []
        
        raw_query = query.strip()
        sub_queries = [raw_query]
        clean_q = re.sub(r'[\&\,\|\+\(\)\[\]]', ' ', raw_query)
        clean_q = re.sub(r'\s+', ' ', clean_q).strip()
        if clean_q and clean_q != raw_query and clean_q not in sub_queries:
            sub_queries.append(clean_q)

        words = clean_q.split()
        if len(words) > 2:
            sub_queries.append(f"{words[0]} {words[-1]}")
            if len(words) > 3:
                sub_queries.append(f"{words[0]} {' '.join(words[2:])}")

        # Query all sub-queries concurrently in parallel
        tasks = [self._request("track/search", {"query": q_try, "limit": max(limit, 12)}) for q_try in sub_queries[:3]]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        seen_ids = set()
        raw_candidates = []

        for resp in responses:
            if isinstance(resp, dict):
                tracks_obj = resp.get("tracks", {})
                items = tracks_obj.get("items", []) if isinstance(tracks_obj, dict) else (tracks_obj if isinstance(tracks_obj, list) else [])
                for item in items:
                    tid = str(item.get("id") or "")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        raw_candidates.append(item)

        if not raw_candidates:
            return []

        q_lower = raw_query.lower()
        def score_item(it):
            t = (it.get("title") or "").lower()
            a = ((it.get("performer", {}) or {}).get("name") if isinstance(it.get("performer"), dict) else (it.get("artist", {}).get("name") or "")).lower()
            sc = 0
            if any(bad in t for bad in ["karaoke", "parodia", "parody", "ringtone", "cover", "lofi", "instrumental", "tribute", "acoustic version"]):
                sc -= 100
            if "remix" in t and "remix" not in q_lower:
                sc -= 50
            clean_t = re.sub(r'[^a-zA-Z0-9]', '', t)
            if clean_t and clean_t in [re.sub(r'[^a-zA-Z0-9]', '', w) for w in words]:
                sc += 50
            for w in words:
                w_l = w.lower()
                if len(w_l) > 1:
                    if w_l in t:
                        sc += 10
                    if w_l in a:
                        sc += 25
            return sc

        ranked_candidates = sorted(raw_candidates, key=score_item, reverse=True)[:limit]

        results = []
        for item in ranked_candidates:
            track_id = str(item.get("id") or "")
            if not track_id:
                continue
            
            title = item.get("title") or "Unknown Title"
            if item.get("version"):
                title += f" ({item['version']})"

            performer = item.get("performer", {})
            artist = performer.get("name") if isinstance(performer, dict) else (item.get("artist", {}).get("name") or "Unknown Artist")

            album_obj = item.get("album", {})
            album_title = album_obj.get("title") if isinstance(album_obj, dict) else None
            
            image_url = None
            if isinstance(album_obj, dict) and album_obj.get("image"):
                img_dict = album_obj["image"]
                image_url = img_dict.get("large") or img_dict.get("extralarge") or img_dict.get("small") or img_dict.get("thumbnail")

            hires = bool(item.get("hires") or item.get("hires_streamable") or (isinstance(album_obj, dict) and album_obj.get("hires")))
            bit_depth = item.get("maximum_bit_depth") or (album_obj.get("maximum_bit_depth") if isinstance(album_obj, dict) else 16)
            sampling_rate = item.get("maximum_sampling_rate") or (album_obj.get("maximum_sampling_rate") if isinstance(album_obj, dict) else 44.1)

            audio_quality = "HI_RES" if hires or (bit_depth and bit_depth > 16) else "CD_LOSSLESS"

            release_date = item.get("release_date_original") or item.get("release_date") or (album_obj.get("release_date_original") if isinstance(album_obj, dict) else None)
            year = release_date[:4] if release_date and len(release_date) >= 4 else None

            results.append(QobuzSearchResultItem(
                asin=track_id,
                title=title,
                artist=artist,
                album=album_title,
                duration_sec=int(item.get("duration") or 0),
                thumbnail_url=image_url,
                url=f"https://open.qobuz.com/track/{track_id}",
                release_date=release_date,
                year=year,
                explicit=bool(item.get("parental_warning")),
                track_number=item.get("track_number"),
                disc_number=item.get("media_number"),
                audio_quality=audio_quality,
                hires=hires,
                bit_depth=int(bit_depth or 16),
                sampling_rate=float(sampling_rate or 44.1),
            ))

        return results

    async def get_track(self, track_id: str) -> Dict[str, Any]:
        """Fetch raw track metadata from track/get."""
        return await self._request("track/get", {"track_id": track_id})

    async def get_album(self, album_id: str) -> Dict[str, Any]:
        """Fetch raw album metadata from album/get."""
        return await self._request("album/get", {"album_id": album_id})

    async def get_file_url(self, track_id: str, format_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Request direct high-speed CDN audio streaming URL signed with MD5 secret.
        Automatically checks and selects the highest available quality format (27 -> 7 -> 6 -> 5).
        """
        candidate_formats = []
        if format_id:
            candidate_formats.append(format_id)
        for fid in [27, 7, 6, 5]:
            if fid not in candidate_formats:
                candidate_formats.append(fid)

        secrets = [s for s in [self._valid_secret, self.auth.app_secret, *QobuzConfig.get_secrets()] if s]
        last_err = None

        for fmt in candidate_formats:
            for secret in secrets:
                ts = int(time.time())
                raw_sig = f"trackgetFileUrlformat_id{fmt}intentstreamtrack_id{track_id}{ts}{secret}"
                request_sig = hashlib.md5(raw_sig.encode("utf-8")).hexdigest()
                
                params = {
                    "request_ts": ts,
                    "request_sig": request_sig,
                    "track_id": track_id,
                    "format_id": fmt,
                    "intent": "stream",
                    "app_id": str(self.auth.app_id or QobuzConfig.DEFAULT_APP_ID),
                }
                if self.auth.user_auth_token:
                    params["user_auth_token"] = self.auth.user_auth_token

                headers = self._get_headers()
                async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                    resp = await client.get(f"{QobuzConfig.API_BASE}track/getFileUrl", params=params)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("url") and "sample" not in data:
                            self._valid_secret = secret
                            self.auth.app_secret = secret
                            data["selected_format_id"] = fmt
                            return data
                        elif data.get("url"):
                            data["selected_format_id"] = fmt
                            return data
                    elif resp.status_code == 400:
                        last_err = resp.text
                        continue
                    else:
                        last_err = f"HTTP {resp.status_code}: {resp.text}"

        raise ValueError(f"Could not retrieve Qobuz stream URL for track {track_id}: {last_err or 'Invalid signature or restricted track'}")

    async def resolve(self, input_val: str, quality: str = "UHD") -> QobuzTrackResponse:
        """Resolve a track ID, URL, or Query to complete Qobuz track details."""
        track_id = self.extract_track_id(input_val)
        if not track_id:
            album_id = self.extract_album_id(input_val)
            if album_id:
                album = await self.get_album(album_id)
                tracks = album.get("tracks", {}).get("items", [])
                if tracks:
                    track_id = str(tracks[0].get("id"))
        
        if not track_id:
            results = await self.search(input_val, limit=1)
            if results:
                track_id = results[0].asin

        if not track_id:
            raise ValueError(f"Could not resolve Qobuz track from input: {input_val}")

        track_data = await self.get_track(track_id)
        title = track_data.get("title") or "Unknown Title"
        if track_data.get("version"):
            title += f" ({track_data['version']})"

        performer = track_data.get("performer", {})
        artist = performer.get("name") if isinstance(performer, dict) else (track_data.get("artist", {}).get("name") or "Unknown Artist")
        
        album_obj = track_data.get("album", {})
        album_title = album_obj.get("title") if isinstance(album_obj, dict) else None
        
        image_url = None
        if isinstance(album_obj, dict) and album_obj.get("image"):
            img_dict = album_obj["image"]
            image_url = img_dict.get("large") or img_dict.get("extralarge") or img_dict.get("small")

        hires = bool(track_data.get("hires") or track_data.get("hires_streamable") or (isinstance(album_obj, dict) and album_obj.get("hires")))
        bit_depth = track_data.get("maximum_bit_depth") or (album_obj.get("maximum_bit_depth") if isinstance(album_obj, dict) else 16)
        sampling_rate = track_data.get("maximum_sampling_rate") or (album_obj.get("maximum_sampling_rate") if isinstance(album_obj, dict) else 44.1)

        return QobuzTrackResponse(
            asin=track_id,
            title=title,
            artist=artist,
            album=album_title,
            duration_sec=int(track_data.get("duration") or 0),
            thumbnail_url=image_url,
            codec="flac",
            bit_depth=int(bit_depth or 16),
            sample_rate=int(float(sampling_rate or 44.1) * 1000),
            hires=hires,
        )
