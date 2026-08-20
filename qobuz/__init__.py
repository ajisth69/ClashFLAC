"""
Qobuz Hi-Res Lossless Audio Resolution & Downloader Engine
Integrated testing module inspired by vitiko98/qobuz-dl
"""

from .config import QobuzConfig
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
from .auth import QobuzAuth
from .api import QobuzAPI
from .downloader import QobuzDownloader
from .routes import router as qobuz_router

__all__ = [
    "QobuzConfig",
    "QobuzAuth",
    "QobuzAPI",
    "QobuzDownloader",
    "qobuz_router",
    "QobuzSearchResultItem",
    "QobuzTrackResponse",
    "QobuzDownloadResponse",
    "QobuzResolveRequest",
    "QobuzDownloadRequest",
    "QobuzAuthLoginRequest",
    "QobuzAuthStatusResponse",
    "QobuzTokenExportResponse",
]
