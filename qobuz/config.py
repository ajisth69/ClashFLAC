import os
from pathlib import Path
from typing import List, Dict, Any

class QobuzConfig:
    API_BASE = "https://www.qobuz.com/api.json/0.2/"
    WEB_PLAYER_URL = "https://play.qobuz.com"
    
    # App ID from environment or token store
    DEFAULT_APP_ID = os.getenv("QOBUZ_APP_ID", "")
    
    # Token storage paths
    TOKEN_FILE = Path("config/qobuz_tokens.json")
    ROOT_TOKEN_FILE = Path("qobuz_tokens.json")

    # Quality formats:
    # 5: MP3 320kbps
    # 6: FLAC 16-bit / 44.1kHz (CD Lossless)
    # 7: FLAC 24-bit <= 96kHz (Hi-Res Lossless)
    # 27: FLAC 24-bit > 96kHz (Hi-Res Lossless up to 192kHz)
    QUALITY_FORMAT_MAP = {
        "LOW": 5,
        "MP3": 5,
        "HIGH": 5,
        "HD": 6,
        "CD": 6,
        "LOSSLESS": 6,
        "UHD": 27,
        "HI_RES": 27,
        "HI_RES_LOSSLESS": 27,
        "MASTER": 27,
    }

    @classmethod
    def get_format_id(cls, quality_name: str = "UHD") -> int:
        return cls.QUALITY_FORMAT_MAP.get((quality_name or "UHD").upper(), 27)

    @classmethod
    def get_secrets(cls) -> List[str]:
        sec = os.getenv("QOBUZ_APP_SECRET", "").strip()
        return [sec] if sec else []
