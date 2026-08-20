import os
import json
import time
import base64
import re
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Dict, Any, List
import httpx

from .config import QobuzConfig

logger = logging.getLogger("qobuz.auth")

_SEED_TIMEZONE_REGEX = re.compile(
    r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)'
)
_INFO_EXTRAS_REGEX = r'name:"\w+/(?P<timezone>{timezones})",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
_APP_ID_REGEX = re.compile(
    r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"\w{32}"'
)
_BUNDLE_URL_REGEX = re.compile(
    r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>'
)

class QobuzAuth:
    def __init__(self):
        self.app_id: str = QobuzConfig.DEFAULT_APP_ID
        self.app_secret: Optional[str] = None
        self.user_auth_token: Optional[str] = os.getenv("QOBUZ_USER_AUTH_TOKEN")
        self.user_id: Optional[str] = os.getenv("QOBUZ_USER_ID")
        self.user_email: Optional[str] = os.getenv("QOBUZ_EMAIL")
        self.membership_label: Optional[str] = None
        self.tested_valid_secret: Optional[str] = None
        
        self.load_tokens()

    def load_tokens(self):
        # 1. Try config/qobuz_tokens.json
        for path in [QobuzConfig.TOKEN_FILE, QobuzConfig.ROOT_TOKEN_FILE]:
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._apply_token_dict(data)
                    logger.info(f"Loaded Qobuz credentials from {path}")
                    return
                except Exception as e:
                    logger.warning(f"Failed loading Qobuz tokens from {path}: {e}")

        # 2. Try env vars
        b64_creds = (os.getenv("QOBUZ_CREDENTIALS_BASE64") or "").strip()
        if b64_creds:
            try:
                data = json.loads(base64.b64decode(b64_creds).decode("utf-8"))
                self._apply_token_dict(data)
                logger.info("Loaded Qobuz credentials from QOBUZ_CREDENTIALS_BASE64")
                return
            except Exception as e:
                logger.warning(f"Failed loading Qobuz credentials from env: {e}")

        if os.getenv("QOBUZ_APP_ID"):
            self.app_id = os.getenv("QOBUZ_APP_ID")
        if os.getenv("QOBUZ_APP_SECRET"):
            self.app_secret = os.getenv("QOBUZ_APP_SECRET")

    def _apply_token_dict(self, data: Dict[str, Any]):
        if data.get("app_id"):
            self.app_id = str(data["app_id"])
        if data.get("app_secret"):
            self.app_secret = str(data["app_secret"])
        if data.get("user_auth_token"):
            self.user_auth_token = str(data["user_auth_token"])
        if data.get("user_id"):
            self.user_id = str(data["user_id"])
        if data.get("user_email"):
            self.user_email = str(data["user_email"])
        if data.get("membership"):
            self.membership_label = str(data["membership"])

    def save_tokens(self, data: Dict[str, Any]):
        """Persist tokens to config/qobuz_tokens.json and .env."""
        QobuzConfig.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        QobuzConfig.TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            QobuzConfig.ROOT_TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

        self._apply_token_dict(data)

        # Update .env if present
        env_path = Path(".env")
        if env_path.exists():
            try:
                env_text = env_path.read_text(encoding="utf-8")
                b64_creds = self.get_credentials_base64() or ""

                def update_key(content, key, value):
                    pattern = re.compile(rf'^{key}=.*$', re.MULTILINE)
                    if pattern.search(content):
                        return pattern.sub(f'{key}="{value}"', content)
                    return content + f'\n{key}="{value}"'

                if b64_creds:
                    env_text = update_key(env_text, "QOBUZ_CREDENTIALS_BASE64", b64_creds)
                if self.app_id:
                    env_text = update_key(env_text, "QOBUZ_APP_ID", self.app_id)
                if self.app_secret:
                    env_text = update_key(env_text, "QOBUZ_APP_SECRET", self.app_secret)
                if self.user_auth_token:
                    env_text = update_key(env_text, "QOBUZ_USER_AUTH_TOKEN", self.user_auth_token)
                if self.user_id:
                    env_text = update_key(env_text, "QOBUZ_USER_ID", self.user_id)

                env_path.write_text(env_text, encoding="utf-8")
                logger.info("Updated .env with Qobuz credentials.")
            except Exception as e:
                logger.warning(f"Failed updating .env with Qobuz credentials: {e}")

    def get_credentials_base64(self) -> Optional[str]:
        creds = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
            "user_auth_token": self.user_auth_token,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "membership": self.membership_label,
        }
        try:
            return base64.b64encode(json.dumps(creds).encode("utf-8")).decode("utf-8")
        except Exception:
            return None

    async def fetch_bundle_tokens(self) -> Dict[str, Any]:
        """Dynamically scrape App ID and App Secrets from play.qobuz.com bundle."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
            resp = await client.get(f"{QobuzConfig.WEB_PLAYER_URL}/login")
            resp.raise_for_status()
            
            bundle_match = _BUNDLE_URL_REGEX.search(resp.text)
            if not bundle_match:
                logger.warning("Could not locate bundle.js on Qobuz web login page")
                return {}

            bundle_url = bundle_match.group(1)
            bundle_resp = await client.get(f"{QobuzConfig.WEB_PLAYER_URL}{bundle_url}")
            bundle_resp.raise_for_status()
            bundle_text = bundle_resp.text

            app_id = None
            id_match = _APP_ID_REGEX.search(bundle_text)
            if id_match:
                app_id = id_match.group("app_id")

            seed_matches = _SEED_TIMEZONE_REGEX.finditer(bundle_text)
            secrets = OrderedDict()
            for match in seed_matches:
                seed, timezone = match.group("seed", "timezone")
                secrets[timezone] = [seed]

            keypairs = list(secrets.items())
            if len(keypairs) > 1:
                secrets.move_to_end(keypairs[1][0], last=False)

            info_extras_regex = _INFO_EXTRAS_REGEX.format(
                timezones="|".join([timezone.capitalize() for timezone in secrets])
            )
            info_extras_matches = re.finditer(info_extras_regex, bundle_text)
            for match in info_extras_matches:
                timezone, info, extras = match.group("timezone", "info", "extras")
                secrets[timezone.lower()] += [info, extras]

            decoded_secrets = []
            for secret_pair in secrets:
                try:
                    dec = base64.standard_b64decode("".join(secrets[secret_pair])[:-44]).decode("utf-8")
                    if dec:
                        decoded_secrets.append(dec)
                except Exception:
                    pass

            return {
                "app_id": app_id,
                "secrets": decoded_secrets,
            }

    async def login(self, email: str, pwd: str, app_id: Optional[str] = None) -> Dict[str, Any]:
        """Authenticate user with Qobuz user/login endpoint."""
        target_app_id = app_id or self.app_id or QobuzConfig.DEFAULT_APP_ID
        params = {
            "email": email,
            "password": pwd,
            "app_id": target_app_id,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "X-App-Id": target_app_id,
        }
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(f"{QobuzConfig.API_BASE}user/login", params=params)
            if resp.status_code == 401:
                raise ValueError("Invalid Qobuz email or password.")
            elif resp.status_code == 400:
                raise ValueError("Invalid Qobuz App ID.")
            resp.raise_for_status()
            data = resp.json()

            user_auth_token = data.get("user_auth_token")
            user_info = data.get("user", {})
            user_id = str(user_info.get("id") or "")
            params_cred = user_info.get("credential", {}).get("parameters", {})
            membership = params_cred.get("short_label") or params_cred.get("label") or "Lossless"

            save_dict = {
                "app_id": target_app_id,
                "app_secret": self.app_secret or QobuzConfig.KNOWN_SECRETS[0],
                "user_auth_token": user_auth_token,
                "user_id": user_id,
                "user_email": email,
                "membership": membership,
            }
            self.save_tokens(save_dict)
            return save_dict

    def is_authenticated(self) -> bool:
        return bool(self.user_auth_token)
