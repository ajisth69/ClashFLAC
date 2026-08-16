import os
import gc
import time
import logging
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Header, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from .config import TidalConfig
from .auth import TidalAuth
from .api import TidalAPI
from .downloader import TidalDownloader
from .models import (
    TidalSearchResultItem,
    TidalTrackResponse,
    TidalDownloadResponse,
    TidalResolveRequest,
    TidalDownloadRequest,
    DeviceAuthInitResponse,
    DeviceAuthCheckResponse,
    AuthStatusResponse,
    SetTokenRequest,
    TokenExportResponse,
)

logger = logging.getLogger("tidal.routes")

router = APIRouter(
    prefix="/api/tidal",
    tags=["Tidal Music API (Testing)"],
)

auth = TidalAuth()
api_client = TidalAPI(auth=auth)
downloader = TidalDownloader(api=api_client)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)

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
            logger.info(f"Cleaned up streamed Tidal audio file: {file_path.name}")
        
        if file_path and output_dir:
            parent = file_path.parent
            while parent != output_dir and parent.exists():
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break
    except Exception as e:
        logger.warning(f"Error during Tidal artifact cleanup: {e}")
    finally:
        free_memory()


@router.get("/search", response_model=List[TidalSearchResultItem])
async def search_endpoint(
    q: str = Query(..., description="Tidal catalog search query"),
    limit: int = Query(5, ge=1, le=20, description="Max search results")
):
    """
    Search Tidal catalog for tracks and metadata.
    Matches exact Amazon SearchResultItem contract.
    """
    try:
        query = q.strip()
        return await api_client.search(query, limit=limit)
    except Exception as e:
        logger.error(f"Tidal search failed for '{q}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resolve", response_model=TidalTrackResponse)
async def resolve_endpoint(req: TidalResolveRequest):
    """
    Resolve Tidal Track ID, URL, or Query directly.
    Matches exact Amazon TrackResponse contract.
    """
    try:
        return await api_client.resolve(req.input, quality=req.quality or "HD")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tidal resolve failed for '{req.input}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/resolve")
async def resolve_query_endpoint(q: str):
    """
    Resolve Tidal URLs to clean query / track ID.
    """
    try:
        track_id = api_client.extract_track_id(q)
        if track_id:
            track = await api_client.get_track(track_id)
            title = track.get("title") or ""
            artists = track.get("artists") or []
            artist_str = ", ".join([a.get("name") for a in artists if a.get("name")])
            return {"resolved": f"{title} {artist_str}".strip() or track_id}
        return {"resolved": q}
    except Exception as e:
        return {"resolved": q}


import httpx

TURNSTILE_SECRET_KEY = os.getenv("CLOUDFLARE_SECRET_KEY", "0x4AAAAAAEPBXVYtw61sAdYkpjCBYoyY4VI")
TURNSTILE_ENFORCE = os.getenv("TURNSTILE_ENFORCE", "false").lower() in ("true", "1")

async def verify_turnstile_download(
    x_turnstile_token: Optional[str] = Header(default=None, alias="X-Turnstile-Token"),
    request: Request = None
) -> bool:
    if not TURNSTILE_SECRET_KEY or not TURNSTILE_ENFORCE:
        return True
    if not x_turnstile_token:
        logger.info("Tidal download requested without Turnstile token; proceeding gracefully.")
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
                logger.warning(f"Tidal Turnstile verification noticed failure: {outcome}; allowing genuine request to proceed.")
            return True
    except Exception as e:
        logger.warning(f"Tidal Turnstile check skipped on exception: {e}")
        return True


@router.post("/download")
async def download_endpoint(
    req: TidalDownloadRequest,
    x_turnstile_token: Optional[str] = Header(default=None, alias="X-Turnstile-Token"),
    request: Request = None
):
    """
    Download and tag Tidal track with strict concurrency control.
    """
    await verify_turnstile_download(x_turnstile_token, request)
    async with DOWNLOAD_SEMAPHORE:
        try:
            import uuid
            job_id = uuid.uuid4().hex[:12]
            output_dir = (Path("downloads/tidal") / f"job_{job_id}").resolve()
            output_dir.mkdir(parents=True, exist_ok=True)

            file_path = await downloader.download_track(
                track_id_or_input=req.input,
                output_dir=output_dir,
                quality=req.quality or "HD"
            )

            if file_path and file_path.exists():
                return FileResponse(
                    path=str(file_path),
                    media_type="audio/flac",
                    filename=file_path.name if file_path.suffix.lower() == ".flac" else f"{file_path.stem}.flac",
                    headers={
                        "Access-Control-Expose-Headers": "Content-Disposition",
                        "Content-Type": "audio/flac",
                    },
                    background=BackgroundTask(cleanup_download_artifact, file_path, output_dir)
                )
            else:
                raise HTTPException(status_code=500, detail="Downloaded Tidal audio file could not be located.")

        except HTTPException as he:
            raise he
        except Exception as e:
            logger.exception(f"Failed to download Tidal track for '{req.input}': {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            free_memory()


@router.get("/auth/status", response_model=AuthStatusResponse)
async def auth_status_endpoint():
    """
    Check if Tidal user account is authenticated or running on public Web Client token.
    """
    return AuthStatusResponse(
        authenticated=auth.is_authenticated(),
        user_id=auth.user_id,
        country_code=auth.country_code,
        token_expiry=int(auth.token_expiry) if auth.token_expiry else None,
        mode="authenticated_user" if auth.is_authenticated() else "guest_web_token",
    )


@router.post("/auth/device", response_model=DeviceAuthInitResponse)
async def device_auth_init_endpoint():
    """
    Initiate OAuth2 Device Code login flow for link.tidal.com.
    """
    try:
        data = await auth.init_device_authorization()
        return DeviceAuthInitResponse(**data)
    except Exception as e:
        logger.error(f"Failed initiating Tidal device authorization: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeviceCheckRequest(BaseModel):
    device_code: str

@router.post("/auth/check", response_model=DeviceAuthCheckResponse)
async def device_auth_check_endpoint(req: DeviceCheckRequest):
    """
    Poll status of device authorization after user visits link.tidal.com.
    """
    try:
        data = await auth.check_device_token(req.device_code)
        return DeviceAuthCheckResponse(**data)
    except Exception as e:
        logger.error(f"Failed checking Tidal device authorization: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auth/set_token", response_model=AuthStatusResponse)
async def set_token_endpoint(req: SetTokenRequest):
    """
    Directly set / import Tidal access token and refresh token via API.
    """
    try:
        token_record = {
            "access_token": req.access_token.strip(),
            "refresh_token": req.refresh_token.strip() if req.refresh_token else None,
            "user_id": req.user_id or "user",
            "country_code": req.country_code or "US",
            "expires_in": req.expires_in or 86400 * 30,
            "token_expiry": time.time() + (req.expires_in or 86400 * 30),
        }
        auth.save_tokens(token_record)
        return AuthStatusResponse(
            authenticated=True,
            user_id=auth.user_id,
            country_code=auth.country_code,
            token_expiry=int(auth.token_expiry) if auth.token_expiry else None,
            mode="authenticated_user",
        )
    except Exception as e:
        logger.error(f"Failed setting Tidal token: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/auth/export", response_model=TokenExportResponse)
async def export_auth_endpoint():
    """
    Export current Tidal credentials and TIDAL_CREDENTIALS_BASE64 string.
    """
    return TokenExportResponse(
        authenticated=auth.is_authenticated(),
        user_id=auth.user_id,
        country_code=auth.country_code,
        access_token=auth.user_access_token,
        refresh_token=auth.user_refresh_token,
        credentials_base64=auth.get_credentials_base64(),
    )


@router.post("/auth/logout")
async def logout_endpoint():
    """
    Clear stored Tidal credentials and revert to Client Credentials mode.
    """
    auth.clear_tokens()
    return {"status": "success", "message": "Tidal credentials cleared."}
