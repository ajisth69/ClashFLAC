import os
import gc
import logging
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Header, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
import httpx

from .config import QobuzConfig
from .auth import QobuzAuth
from .api import QobuzAPI
from .downloader import QobuzDownloader
from download_progress import update as update_download_progress
from .models import (
    QobuzSearchResultItem,
    QobuzTrackResponse,
    QobuzDownloadResponse,
    QobuzResolveRequest,
    QobuzDownloadRequest,
    QobuzAuthLoginRequest,
    QobuzAuthStatusResponse,
    QobuzTokenExportResponse,
)

logger = logging.getLogger("qobuz.routes")

router = APIRouter(
    prefix="/api/qobuz",
    tags=["Qobuz Hi-Res Lossless API"],
)

auth = QobuzAuth()
api_client = QobuzAPI(auth=auth)
downloader = QobuzDownloader(api=api_client)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)

TURNSTILE_SECRET_KEY = os.getenv("CLOUDFLARE_SECRET_KEY", "0x4AAAAAAEPBXVYtw61sAdYkpjCBYoyY4VI")
TURNSTILE_ENFORCE = os.getenv("TURNSTILE_ENFORCE", "false").lower() in ("true", "1")

async def verify_turnstile_download(
    x_turnstile_token: Optional[str] = Header(default=None, alias="X-Turnstile-Token"),
    request: Request = None
) -> bool:
    if not TURNSTILE_SECRET_KEY or not TURNSTILE_ENFORCE:
        return True
    if not x_turnstile_token:
        logger.info("Qobuz download requested without Turnstile token; proceeding gracefully.")
        return True
    client_ip = request.client.host if request and request.client else None
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": TURNSTILE_SECRET_KEY,
                    "response": x_turnstile_token,
                    **({"remoteip": client_ip} if client_ip else {})
                }
            )
            outcome = resp.json()
            if not outcome.get("success"):
                logger.warning(f"Turnstile verification noticed failure: {outcome}; proceeding gracefully.")
            return True
    except Exception as e:
        logger.warning(f"Turnstile verification skipped on exception: {e}")
        return True

def free_memory():
    try:
        gc.collect()
    except Exception:
        pass
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

def cleanup_download_artifact(file_path: Path, output_dir: Path):
    try:
        if file_path and file_path.is_file():
            file_path.unlink(missing_ok=True)
            logger.info(f"Cleaned up streamed Qobuz audio file: {file_path.name}")
        
        if file_path and output_dir:
            parent = file_path.parent
            while parent != output_dir and parent.exists():
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break
    except Exception as e:
        logger.warning(f"Error during Qobuz artifact cleanup: {e}")
    finally:
        free_memory()

@router.get("/search", response_model=List[QobuzSearchResultItem])
async def search_endpoint(
    q: str = Query(..., description="Qobuz catalog search query"),
    limit: int = Query(20, ge=1, le=50, description="Max search results")
):
    """Search Qobuz catalog for tracks and Hi-Res metadata."""
    try:
        query = q.strip()
        return await api_client.search(query, limit=limit)
    except Exception as e:
        logger.error(f"Qobuz search failed for '{q}': {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/resolve", response_model=QobuzTrackResponse)
async def resolve_endpoint(req: QobuzResolveRequest):
    """Resolve Qobuz Track ID, URL, or Query directly."""
    try:
        return await api_client.resolve(req.input, quality=req.quality or "UHD")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Qobuz resolve failed for '{req.input}': {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/resolve")
async def resolve_query_endpoint(q: str):
    """Resolve Qobuz URLs to clean query / track ID."""
    try:
        track_id = api_client.extract_track_id(q)
        if track_id:
            track = await api_client.get_track(track_id)
            title = track.get("title") or ""
            artist = track.get("performer", {}).get("name") or track.get("artist", {}).get("name") or ""
            return {"resolved": f"{title} {artist}".strip() or track_id}
        return {"resolved": q}
    except Exception:
        return {"resolved": q}

@router.get("/stream/{track_id}")
async def stream_endpoint(track_id: str):
    """Get high-speed browser-compatible audio stream URL (MP3 320kbps) from Qobuz."""
    try:
        data = await api_client.get_file_url(track_id, format_id=5)
        url = data.get("url")
        if not url:
            raise ValueError("No stream URL returned by Qobuz.")
        return {
            "stream_url": url,
            "format": "mp3",
            "bitrate": 320,
            "track_id": track_id,
            "mime_type": data.get("mime_type") or "audio/mpeg"
        }
    except Exception as e:
        logger.warning(f"Qobuz stream notice for track {track_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/download")
async def download_endpoint(
    req: QobuzDownloadRequest,
    is_human: bool = Depends(verify_turnstile_download),
    x_download_job_id: Optional[str] = Header(default=None, alias="X-Download-Job-ID"),
):
    """Download Hi-Res Lossless FLAC track from Qobuz."""
    track_input = (req.input or "").strip()
    if not track_input and req.track:
        track_input = str(req.track.get("asin") or req.track.get("id") or req.track.get("title") or "")

    if not track_input:
        raise HTTPException(status_code=400, detail="Missing required track identifier or metadata.")

    # Unique job ID for progress reporting
    job_id = x_download_job_id or f"qobuz_{track_input}_{req.quality or 'UHD'}"
    output_dir = Path("downloads/qobuz").resolve()

    def progress_hook(stage: str, pct: int):
        update_download_progress(job_id, stage, pct)

    update_download_progress(job_id, "Queueing Qobuz engine...", 5)

    async with DOWNLOAD_SEMAPHORE:
        try:
            file_path = await downloader.download_track(
                track_id_or_input=track_input,
                output_dir=output_dir,
                quality=req.quality or "UHD",
                track_hint=req.track,
                progress_callback=progress_hook,
            )

            if not file_path.exists() or file_path.stat().st_size == 0:
                raise ValueError("Downloaded Qobuz FLAC file is empty or missing.")

            filename = file_path.name
            encoded_filename = quote(filename.encode("utf-8"))

            update_download_progress(job_id, "Ready", 100)

            background = BackgroundTask(cleanup_download_artifact, file_path, output_dir)
            return FileResponse(
                path=str(file_path),
                media_type="audio/flac",
                filename=filename,
                headers={
                    "Content-Disposition": f"attachment; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}",
                    "Access-Control-Expose-Headers": "Content-Disposition",
                },
                background=background,
            )
        except Exception as e:
            logger.error(f"Qobuz download failed for '{track_input}': {e}", exc_info=True)
            update_download_progress(job_id, str(e), 0, error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

@router.post("/auth/login")
async def auth_login_endpoint(req: QobuzAuthLoginRequest):
    """Authenticate Qobuz account via email & password."""
    try:
        result = await auth.login(email=req.email, pwd=req.password, app_id=req.app_id)
        return {"status": "success", "message": "Qobuz authentication successful.", "user_id": result.get("user_id"), "membership": result.get("membership")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/auth/status", response_model=QobuzAuthStatusResponse)
async def auth_status_endpoint():
    """Get current Qobuz credentials status."""
    return QobuzAuthStatusResponse(
        authenticated=auth.is_authenticated(),
        user_id=auth.user_id,
        user_email=auth.user_email,
        membership=auth.membership_label or "Lossless",
        app_id=auth.app_id,
        has_secret=bool(auth.app_secret or QobuzConfig.KNOWN_SECRETS[0]),
        mode="authenticated_user" if auth.is_authenticated() else "public_web_client",
    )

@router.get("/auth/export", response_model=QobuzTokenExportResponse)
async def auth_export_endpoint():
    """Export Qobuz credentials base64 for deployment."""
    return QobuzTokenExportResponse(
        authenticated=auth.is_authenticated(),
        user_id=auth.user_id,
        user_auth_token=auth.user_auth_token,
        app_id=auth.app_id,
        app_secret=auth.app_secret,
        credentials_base64=auth.get_credentials_base64(),
    )

@router.get("/config")
async def config_endpoint():
    return {
        "engine": "Qobuz Hi-Res Lossless FLAC Engine",
        "app_id": auth.app_id,
        "max_quality": "24-bit / 192 kHz FLAC",
    }
