import os
import json
import time
import base64
import logging
from pathlib import Path
from typing import Optional, Dict, Any
import httpx
from .config import TidalConfig

logger = logging.getLogger("tidal.auth")

class TidalAuth:
    def __init__(self):
        self.user_access_token: Optional[str] = None
        self.user_refresh_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.user_country_code: str = TidalConfig.DEFAULT_COUNTRY_CODE
        self.user_token_expiry: float = 0
        
        # Client credentials cache
        self.client_token: Optional[str] = None
        self.client_token_expiry: float = 0
        
        self.load_tokens()

    def load_tokens(self) -> bool:
        """
        Load tokens from .env, TIDAL_CREDENTIALS_BASE64, or config/tidal_tokens.json
        """
        # 1. Check JSON token file (most up-to-date from device OAuth)
        token_path = TidalConfig.TOKEN_FILE
        if token_path.exists():
            try:
                data = json.loads(token_path.read_text(encoding="utf-8"))
                self._apply_token_dict(data)
                logger.info(f"Loaded Tidal credentials from {token_path}.")
                return True
            except Exception as e:
                logger.warning(f"Failed loading {token_path}: {e}")

        # 2. Check base64 credentials in env (Railway / Production deployment)
        creds_b64 = (os.getenv("TIDAL_CREDENTIALS_BASE64") or "").strip()
        if creds_b64:
            try:
                data = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
                self._apply_token_dict(data)
                logger.info("Loaded Tidal credentials from TIDAL_CREDENTIALS_BASE64.")
                return True
            except Exception as e:
                logger.warning(f"Failed decoding TIDAL_CREDENTIALS_BASE64: {e}")

        # 3. Check direct environment variables
        env_access = os.getenv("TIDAL_ACCESS_TOKEN")
        env_refresh = os.getenv("TIDAL_REFRESH_TOKEN")
        if env_access:
            self.user_access_token = env_access.strip()
            self.user_refresh_token = env_refresh.strip() if env_refresh else None
            self.user_id = os.getenv("TIDAL_USER_ID", "user")
            self.user_country_code = os.getenv("TIDAL_COUNTRY_CODE", TidalConfig.DEFAULT_COUNTRY_CODE)
            self.user_token_expiry = time.time() + 86400 * 30  # Default 30 days
            logger.info("Loaded Tidal credentials from environment variables.")
            return True

        logger.info("No saved Tidal user credentials found. Using Client Credentials mode.")
        return False

    def _apply_token_dict(self, data: Dict[str, Any]):
        if data.get("client_id") or data.get("clientId"):
            os.environ["TIDAL_CLIENT_ID"] = data.get("client_id") or data.get("clientId")
        if data.get("client_secret") or data.get("clientSecret"):
            os.environ["TIDAL_CLIENT_SECRET"] = data.get("client_secret") or data.get("clientSecret")
        if data.get("web_token") or data.get("webToken"):
            val = data.get("web_token") or data.get("webToken")
            os.environ["TIDAL_WEB_TOKEN"] = val
            TidalConfig.WEB_CLIENT_TOKEN = val

        self.user_access_token = data.get("access_token")
        self.user_refresh_token = data.get("refresh_token")
        self.user_id = str(data.get("user_id") or data.get("userId") or "")
        self.user_country_code = data.get("country_code") or data.get("countryCode") or TidalConfig.DEFAULT_COUNTRY_CODE
        self.user_token_expiry = float(data.get("token_expiry") or data.get("expires_at") or (time.time() + data.get("expires_in", 86400)))

    def save_tokens(self, data: Dict[str, Any]):
        """Persist tokens to config/tidal_tokens.json."""
        token_path = TidalConfig.TOKEN_FILE
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._apply_token_dict(data)

    def clear_tokens(self):
        """Clear user tokens from memory and disk."""
        self.user_access_token = None
        self.user_refresh_token = None
        self.user_id = None
        self.user_token_expiry = 0
        token_path = TidalConfig.TOKEN_FILE
        if token_path.exists():
            try:
                token_path.unlink(missing_ok=True)
            except Exception:
                pass

    def get_credentials_base64(self) -> Optional[str]:
        """Export credentials dict as a base64 encoded string."""
        if not self.user_access_token:
            return None
        payload = {
            "access_token": self.user_access_token,
            "refresh_token": self.user_refresh_token,
            "user_id": self.user_id,
            "country_code": self.user_country_code,
            "token_expiry": self.user_token_expiry,
        }
        return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

    @property
    def country_code(self) -> str:
        return self.user_country_code or TidalConfig.DEFAULT_COUNTRY_CODE

    @property
    def is_user_authenticated(self) -> bool:
        return bool(self.user_access_token)

    def is_authenticated(self) -> bool:
        return self.is_user_authenticated

    @property
    def token_expiry(self) -> float:
        return self.user_token_expiry if self.is_user_authenticated else self.client_token_expiry

    async def get_client_credentials_token(self) -> str:
        """
        Obtain or return cached Client Credentials Bearer token for catalog access.
        """
        now = time.time()
        if self.client_token and self.client_token_expiry > (now + 60):
            return self.client_token

        client_key = TidalConfig.get_primary_client_key()
        cid = client_key["clientId"]
        csec = client_key.get("clientSecret")
        auth = (cid, csec) if csec else None

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TidalConfig.AUTH_URL}/token",
                data={"grant_type": "client_credentials"},
                auth=auth
            )
            if resp.status_code != 200:
                raise Exception(f"Client credentials token request failed ({resp.status_code}): {resp.text}")
            
            data = resp.json()
            self.client_token = data.get("access_token")
            expires_in = data.get("expires_in", 14400)
            self.client_token_expiry = now + expires_in
            logger.info("Successfully fetched Tidal client_credentials token.")
            return self.client_token

    async def get_valid_bearer_token(self) -> str:
        """
        Returns valid Bearer token (user token if logged in, or client credentials token).
        """
        now = time.time()
        # 1. If user is logged in
        if self.user_access_token:
            if self.user_token_expiry < (now + 60) and self.user_refresh_token:
                await self.refresh_access_token()
            return self.user_access_token
        
        # 2. Fallback to client credentials token
        return await self.get_client_credentials_token()

    async def get_auth_headers(self) -> Dict[str, str]:
        """
        Returns standard Authorization headers for Tidal API calls.
        """
        token = await self.get_valid_bearer_token()
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": "TIDAL_ANDROID/1039 okhttp/3.14.9",
        }

    async def init_device_authorization(self) -> Dict[str, Any]:
        """
        Request OAuth2 device code from auth.tidal.com for link.tidal.com verification.
        """
        client_key = TidalConfig.get_primary_client_key()
        payload = {
            "client_id": client_key["clientId"],
            "scope": client_key.get("scope", "r_usr+w_usr+w_sub"),
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{TidalConfig.AUTH_URL}/device_authorization", data=payload)
            if resp.status_code != 200:
                raise Exception(f"Tidal device authorization failed: {resp.text}")
            
            data = resp.json()
            user_code = data.get("userCode")
            verification_uri = data.get("verificationUri", "link.tidal.com")
            complete_uri = data.get("verificationUriComplete") or f"https://{verification_uri}/{user_code}"
            
            return {
                "device_code": data["deviceCode"],
                "user_code": user_code,
                "verification_uri": verification_uri,
                "verification_uri_complete": complete_uri,
                "expires_in": data.get("expiresIn", 300),
                "interval": data.get("interval", 5),
            }

    async def check_device_token(self, device_code: str) -> Dict[str, Any]:
        """
        Check if the user approved the device login request on link.tidal.com.
        """
        client_key = TidalConfig.get_primary_client_key()
        payload = {
            "client_id": client_key["clientId"],
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "scope": client_key.get("scope", "r_usr+w_usr+w_sub"),
        }
        auth = None
        if client_key.get("clientSecret"):
            auth = (client_key["clientId"], client_key["clientSecret"])

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{TidalConfig.AUTH_URL}/token", data=payload, auth=auth)
            
            if resp.status_code == 200:
                data = resp.json()
                user_obj = data.get("user") or {}
                token_record = {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token"),
                    "expires_in": data.get("expires_in", 86400),
                    "token_expiry": time.time() + data.get("expires_in", 86400),
                    "user_id": str(user_obj.get("userId") or ""),
                    "country_code": user_obj.get("countryCode") or TidalConfig.DEFAULT_COUNTRY_CODE,
                }
                self.save_tokens(token_record)
                return {
                    "status": "success",
                    "authenticated": True,
                    "message": "Authorization successful! Tidal tokens saved.",
                    "user_id": self.user_id,
                    "country_code": self.country_code,
                }
            elif resp.status_code in (400, 401):
                err = resp.json()
                error_code = err.get("error", "")
                if error_code == "authorization_pending":
                    return {
                        "status": "pending",
                        "authenticated": False,
                        "message": "Waiting for user authorization on link.tidal.com.",
                    }
                elif error_code == "expired_token":
                    return {
                        "status": "expired",
                        "authenticated": False,
                        "message": "Device authorization expired. Please generate a new code.",
                    }
                else:
                    return {
                        "status": "error",
                        "authenticated": False,
                        "message": err.get("error_description") or error_code or "Authorization error.",
                    }
            else:
                return {
                    "status": "error",
                    "authenticated": False,
                    "message": f"Unexpected response from auth server: {resp.text}",
                }

    async def refresh_access_token(self) -> bool:
        """
        Refresh user access token using refresh_token grant.
        """
        if not self.user_refresh_token:
            return False

        client_key = TidalConfig.get_primary_client_key()
        payload = {
            "client_id": client_key["clientId"],
            "refresh_token": self.user_refresh_token,
            "grant_type": "refresh_token",
            "scope": client_key.get("scope", "r_usr+w_usr+w_sub"),
        }
        auth = None
        if client_key.get("clientSecret"):
            auth = (client_key["clientId"], client_key["clientSecret"])

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{TidalConfig.AUTH_URL}/token", data=payload, auth=auth)
                if resp.status_code == 200:
                    data = resp.json()
                    user_obj = data.get("user") or {}
                    token_record = {
                        "access_token": data.get("access_token"),
                        "refresh_token": data.get("refresh_token") or self.user_refresh_token,
                        "expires_in": data.get("expires_in", 86400),
                        "token_expiry": time.time() + data.get("expires_in", 86400),
                        "user_id": str(user_obj.get("userId") or self.user_id),
                        "country_code": user_obj.get("countryCode") or self.country_code,
                    }
                    self.save_tokens(token_record)
                    logger.info("Successfully refreshed Tidal access token.")
                    return True
                else:
                    logger.warning(f"Failed to refresh Tidal token: {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Error during Tidal token refresh: {e}")
            return False
