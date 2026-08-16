"""
Tidal Music Resolution & Downloader Engine
Integrated testing module inspired by yaronzz/Tidal-Media-Downloader
"""

from .config import TidalConfig
from .models import (
    TidalSearchResultItem,
    TidalTrackResponse,
    TidalDownloadResponse,
    TidalResolveRequest,
    TidalDownloadRequest,
    DeviceAuthInitResponse,
    DeviceAuthCheckResponse,
    AuthStatusResponse,
)
from .auth import TidalAuth
from .api import TidalAPI
from .downloader import TidalDownloader
from .routes import router as tidal_router

__all__ = [
    "TidalConfig",
    "TidalAuth",
    "TidalAPI",
    "TidalDownloader",
    "tidal_router",
    "TidalSearchResultItem",
    "TidalTrackResponse",
    "TidalDownloadResponse",
    "TidalResolveRequest",
    "TidalDownloadRequest",
    "DeviceAuthInitResponse",
    "DeviceAuthCheckResponse",
    "AuthStatusResponse",
]
