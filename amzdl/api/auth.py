"""Authentication for the Amazon Music downloader. Builds the single signed
`AmazonMusicMobileAPI` session every stage shares, performing interactive
browser OAuth on first run and persisting per-`customer_id` credentials to
`config/credentials.bin`."""

import os
import pickle
from pathlib import Path

from amazonmusic.models import (
    AmazonMusicMobileAPICredentials,
    AmazonRegion,
)
from amzdl.api.amzn_api import AmazonMusicMobileAPI
from amzdl.cli import cli, config, prompts


class _CompatUnpickler(pickle.Unpickler):

    def find_class(self, module, name):
        if module == "vendor.amazonmusic" or module.startswith("vendor.amazonmusic."):
            module = "amazonmusic" + module[len("vendor.amazonmusic"):]
        return super().find_class(module, name)

CREDENTIALS_FILE = Path(
    os.environ.get("AMZ_CREDENTIALS_FILE", str(config.CREDENTIALS_FILE))
)
_LEGACY_CREDENTIALS_FILE = Path("credentials.bin")


def _credentials_path() -> Path:
    if not CREDENTIALS_FILE.exists() and _LEGACY_CREDENTIALS_FILE.exists():
        return _LEGACY_CREDENTIALS_FILE
    return CREDENTIALS_FILE


def _account_key(credentials: AmazonMusicMobileAPICredentials) -> str:
    return credentials.customer_id or credentials.account_region.country


def _account_info(credentials: AmazonMusicMobileAPICredentials) -> dict:
    region = credentials.account_region
    return {
        "name": (credentials.customer_info or {}).get("name") or "Unknown",
        "country": region.country if region else None,
        "region": region.pretty_name if region else None,
    }


def _load_store() -> dict:
    path = _credentials_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            data = _CompatUnpickler(fh).load()
    except Exception as exc:
        cli.warn(f"could not read {path} ({exc}); ignoring.")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        _account_key(creds): creds
        for creds in data.values()
        if isinstance(creds, AmazonMusicMobileAPICredentials)
    }


def _save_store(store: dict) -> None:
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CREDENTIALS_FILE, "wb") as fh:
        pickle.dump(store, fh)


def _sync_accounts(store: dict) -> None:
    current = config.load_accounts()
    merged = dict(current)
    changed = False
    for account_id, creds in store.items():
        info = _account_info(creds)
        if current.get(account_id) != info:
            merged[account_id] = info
            changed = True
    if changed:
        config.save_accounts(merged)


def _save_account(credentials: AmazonMusicMobileAPICredentials) -> str:
    account_id = _account_key(credentials)
    store = _load_store()
    store[account_id] = credentials
    _save_store(store)
    _sync_accounts(store)
    return account_id

def _cli_oauth_callback(oauth_url: str, application_name: str, country: str) -> str:
    return prompts.prompt_oauth_url(f"{application_name} ({country})", oauth_url)


def login(country: str) -> AmazonMusicMobileAPI:
    country = (country or "").strip().upper()
    if len(country) != 2:
        raise ValueError(
            "Country must be a 2-letter ISO 3166-1 code (e.g. US, GB, JP)."
        )
    AmazonRegion.get_region_by_country(country)

    inst = AmazonMusicMobileAPI.login_via_mobile(
        email="",
        password="",
        country_code=country,
        oauth_flow_callback=lambda url, app: _cli_oauth_callback(url, app, country),
    )

    account_id = _save_account(inst.credentials)
    info = _account_info(inst.credentials)
    cli.note(f"Saved account '{info['name']}' ({info['region']}) [{account_id}].")
    return inst


def _prompt_country() -> str:
    return prompts.prompt_region().upper()


def _name_region(info: dict) -> tuple[str, str]:
    name = info.get("name") or "Unknown"
    region = info.get("region") or info.get("country") or "?"
    return name, region


def _resolve_account_id(store: dict, account: str | None) -> str | None:
    key = (account or "").strip()
    if not key:
        return None
    if key in store:
        return key
    accounts = config.load_accounts()
    for account_id, creds in store.items():
        info = accounts.get(account_id) or _account_info(creds)
        if ((info.get("name") or "").lower() == key.lower()
                or (info.get("country") or "").upper() == key.upper()):
            return account_id
    return None


def delete_account(account: str) -> dict:
    store = _load_store()
    account_id = _resolve_account_id(store, account)
    if account_id is None:
        raise KeyError(account)

    info = config.load_accounts().get(account_id) or _account_info(store[account_id])
    del store[account_id]
    _save_store(store)

    accounts = config.load_accounts()
    if account_id in accounts:
        del accounts[account_id]
        config.save_accounts(accounts)

    if (config.get_settings().get("default_account") or "") == account_id:
        config.save_setting("default_account", "")
    return info


def _account_label(account_id: str, store: dict, accounts: dict | None = None) -> str:
    accounts = config.load_accounts() if accounts is None else accounts
    info = accounts.get(account_id) or _account_info(store[account_id])
    name, region = _name_region(info)
    return f"{name} — {region} [{account_id}]"


def _account_for_country(store: dict, target: str):
    target = target.strip().upper()
    for creds in store.values():
        region = creds.account_region
        if region and region.country == target:
            return creds
    return None


def _select_credentials(
    store: dict, account: str | None, country: str | None,
    country_hint: str | None = None,
):
    if not store:
        return None

    if account:
        account_id = _resolve_account_id(store, account)
        if account_id is not None:
            return store[account_id]
        labels = ", ".join(_account_label(a, store) for a in store)
        raise RuntimeError(
            f"No stored account matching '{account}'. Stored accounts: {labels}. "
            f"Add one with `python src/main.py account --add`."
        )

    if country:
        return _account_for_country(store, country)

    if country_hint:
        creds = _account_for_country(store, country_hint)
        if creds is not None:
            return creds

    default = (config.get_settings().get("default_account") or "").strip()
    if default and default in store:
        return store[default]
    if len(store) == 1:
        return next(iter(store.values()))
    return None


def _prompt_account(store: dict) -> str | None:
    accounts = config.load_accounts()
    ids = list(store.keys())
    options = [
        _name_region(accounts.get(account_id) or _account_info(store[account_id]))
        for account_id in ids
    ]
    index = prompts.prompt_account(options)
    return None if index is None else ids[index]


def get_session(
    account: str | None = None,
    country: str | None = None,
    country_hint: str | None = None,
    interactive: bool = True,
) -> AmazonMusicMobileAPI:
    store = _load_store()
    _sync_accounts(store)

    credentials = _select_credentials(store, account, country, country_hint)

    if credentials is None:
        if not interactive:
            if store:
                labels = ", ".join(_account_label(a, store) for a in store)
                raise RuntimeError(
                    "Multiple Amazon Music accounts are stored and none was selected. "
                    f"Set `default_account` in config/config.json (one of: {labels})."
                )
            raise RuntimeError(
                "No saved Amazon Music login. Run `python src/main.py account --add` "
                "once to sign in."
            )
        if store and account is None and country is None:
            chosen = _prompt_account(store)
            if chosen is not None:
                credentials = store[chosen]
            else:
                return login(_prompt_country())
        else:
            if not store:
                cli.note("No saved Amazon Music login found.")
            return login(country or _prompt_country())

    return _ready_session(credentials)


def _ready_session(credentials) -> AmazonMusicMobileAPI:
    session = AmazonMusicMobileAPI(credentials=credentials)
    if session.credentials.access_token_expired:
        cli.note("Access token expired; refreshing...")
        session.refresh_access_token(force=True)
        _save_account(session.credentials)
    return session
