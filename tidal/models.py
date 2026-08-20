from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

class TidalSearchResultItem(BaseModel):
    asin: str = Field(description="Tidal track ID or ISRC identifier")
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
    audio_quality: Optional[str] = None
    audio_modes: Optional[List[str]] = None
    isrc: Optional[str] = None

class TidalTrackResponse(BaseModel):
    source_type: str = "tidal"
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
    isrc: Optional[str] = None

class TidalResolveRequest(BaseModel):
    input: str
    quality: Optional[str] = "HD"

class TidalDownloadRequest(BaseModel):
    input: str
    track: Optional[Dict[str, Any]] = None
    quality: Optional[str] = "HD"

class TidalDownloadResponse(BaseModel):
    status: str
    message: str
    asin: str
    output_dir: str
    type: Optional[str] = "track"
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    track_count: Optional[int] = None

class DeviceAuthInitResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int

class DeviceAuthCheckResponse(BaseModel):
    status: str
    message: str
    authenticated: bool
    user_id: Optional[str] = None
    country_code: Optional[str] = None

class AuthStatusResponse(BaseModel):
    authenticated: bool
    user_id: Optional[str] = None
    country_code: Optional[str] = "US"
    token_expiry: Optional[int] = None
    mode: str = "guest_web_token" # "guest_web_token" or "authenticated_user"

class SetTokenRequest(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    user_id: Optional[str] = None
    country_code: Optional[str] = "US"
    expires_in: Optional[int] = 86400 * 30

class TokenExportResponse(BaseModel):
    authenticated: bool
    user_id: Optional[str] = None
    country_code: Optional[str] = "US"
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    credentials_base64: Optional[str] = None
