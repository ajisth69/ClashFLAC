import os
from pathlib import Path
from typing import Dict, Any

class TidalConfig:
    API_URL = "https://api.tidal.com/v1"
    AUTH_URL = "https://auth.tidal.com/v1/oauth2"
    
    # Public web search client token (works for catalog search & public metadata without login)
    WEB_CLIENT_TOKEN = os.getenv("TIDAL_WEB_TOKEN", "zU4XHVVkc2XDdaTX")
    
    # OAuth2 Client ID / Secret pairs from yaronzz/Tidal-Media-Downloader & community clients
    CLIENT_KEYS = [
        {
            "platform": "TIDAL HiFi / Lossless (Active API)",
            "clientId": "fX2JxdmntZWK0ixT",
            "clientSecret": "1Nn9AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg=",
            "scope": "r_usr+w_usr+w_sub",
        },
        {
            "platform": "Fire TV (Master/HiFi)",
            "clientId": "7m7Ap0JC9j1cOM3n",
            "clientSecret": "vRAdA108tlvkJpTsGZS8rGZ7xTlbJ0qaZ2K9saEzsgY=",
            "scope": "r_usr+w_usr+w_sub",
        },
        {
            "platform": "Android TV (HiFi)",
            "clientId": "Pzd0ExNVHkyZLiYN",
            "clientSecret": "W7X6UvBaho+XOi1MUeCX6ewv2zTdSOV3Y7qC3p3675I=",
            "scope": "r_usr+w_usr+w_sub",
        },
        {
            "platform": "Fire TV (Standard)",
            "clientId": "OmDtrzFgyVVL6uW56OnFA2COiabqm",
            "clientSecret": "zxen1r3pO0hgtOC7j6twMo9UAqngGrmRiWpV7QC1zJ8=",
            "scope": "r_usr+w_usr+w_sub",
        },
    ]

    # Token storage locations
    TOKEN_FILE = Path("config/tidal_tokens.json")

    # Quality mapping
    QUALITY_MAP = {
        "LOW": "LOW",              # 96 kbps AAC
        "HIGH": "HIGH",            # 320 kbps AAC
        "HD": "LOSSLESS",          # 16-bit 44.1kHz FLAC
        "UHD": "HI_RES_LOSSLESS",  # 24-bit up to 192kHz FLAC / Master
        "LOSSLESS": "LOSSLESS",
        "HI_RES": "HI_RES_LOSSLESS",
        "HI_RES_LOSSLESS": "HI_RES_LOSSLESS",
        "MASTER": "HI_RES_LOSSLESS",
    }

    DEFAULT_COUNTRY_CODE = os.getenv("TIDAL_COUNTRY_CODE", "US")

    @classmethod
    def get_primary_client_key(cls) -> Dict[str, str]:
        custom_client_id = os.getenv("TIDAL_CLIENT_ID")
        custom_client_secret = os.getenv("TIDAL_CLIENT_SECRET")
        if custom_client_id:
            return {
                "platform": "Custom",
                "clientId": custom_client_id,
                "clientSecret": custom_client_secret or "",
                "scope": "r_usr+w_usr+w_sub",
            }
        return cls.CLIENT_KEYS[0]
