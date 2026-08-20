from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

class QobuzSearchResultItem(BaseModel):
    asin: str = Field(description="Qobuz track ID or identifier")
    title: str
    artist: Optional[str] = "Unknown Artist"
    album: Optional[str] = None
    duration_sec: int = 0
    thumbnail_url: Optional[str] = None
    url: str
    release_date: Optional[str] = None
    year: Optional[str] = None
    genre: Optional[str] = None
    explicit: bool = False
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    audio_quality: Optional[str] = "HI_RES"
    hires: bool = True
    bit_depth: Optional[int] = 24
    sampling_rate: Optional[float] = 96.0
    isrc: Optional[str] = None

class QobuzTrackResponse(BaseModel):
    source_type: str = "qobuz"
    asin: str
    title: str
    artist: str
    album: Optional[str] = None
    duration_sec: int = 0
    thumbnail_url: Optional[str] = None
    stream_url: Optional[str] = None
    codec: Optional[str] = "flac"
    bitrate: Optional[int] = 0
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    hires: bool = True
    isrc: Optional[str] = None

class QobuzResolveRequest(BaseModel):
    input: str
    quality: Optional[str] = "UHD"

class QobuzDownloadRequest(BaseModel):
    input: str
    track: Optional[Dict[str, Any]] = None
    quality: Optional[str] = "UHD"

class QobuzDownloadResponse(BaseModel):
    status: str
    message: str
    asin: str
    output_dir: str
    type: Optional[str] = "track"
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    bit_depth: Optional[int] = None
    sampling_rate: Optional[float] = None

class QobuzAuthLoginRequest(BaseModel):
    email: str
    password: str
    app_id: Optional[str] = None

class QobuzAuthStatusResponse(BaseModel):
    authenticated: bool
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    membership: Optional[str] = None
    app_id: Optional[str] = None
    has_secret: bool = False
    mode: str = "public_web_client"  # "authenticated_user" or "public_web_client"

class QobuzTokenExportResponse(BaseModel):
    authenticated: bool
    user_id: Optional[str] = None
    user_auth_token: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    credentials_base64: Optional[str] = None
