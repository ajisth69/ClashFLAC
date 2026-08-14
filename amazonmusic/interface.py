import base64
from collections import OrderedDict
import functools
import logging
import os
import itertools
import pprint
import random
import re
import shutil
import subprocess
import sys
import typing
import dataclasses
import itertools
import httpx
from datetime import datetime, timedelta
import concurrent.futures
import threading
from urllib.parse import urlparse, parse_qs

import ffmpeg
import natsort
from pywidevine import PSSH, Cdm, Device
from pywidevine.license_protocol_pb2 import WidevinePsshData
from Crypto.Cipher import AES

from tqdm import tqdm
from xml.etree import ElementTree

from utils.models import *
from utils.utils import (
    create_temp_filename,
    download_file,
    get_clean_env,
    read_temporary_setting,
    resolve_mp4decrypt,
    resolve_shaka_packager,
    silentremove,
    sanitise_name,
)

from modules.amazonmusic.models import AmazonContinent
from .azapi import AmazonMusicMobileAPI, AmazonMusicMobileAPICredentials, AmazonMusicTier, AmazonRegion

LOGGER = logging.getLogger(__name__)

# Album/playlist search: probe a few tracks per row (not every track) for acceptable speed.
SEARCH_QUALITY_MAX_TRACK_PROBE = 3
# Search rows always probe up to these tiers (independent of the user's download quality).
SEARCH_PROBE_STEREO_TIER = QualityEnum.HIFI
SEARCH_PROBE_SPATIAL_TIER = QualityEnum.ATMOS

SHAKA_PACKAGER_HELP_URL = "https://github.com/shaka-project/shaka-packager/releases/latest"


class AmazonMusicConfigError(Exception):
    """Raised when Amazon Music module settings are missing or invalid."""


def _normalize_wvd_path(wvd_path) -> str:
    if wvd_path is None:
        return ""
    return str(wvd_path).strip()


def _validate_wvd_path(wvd_path) -> str:
    path = _normalize_wvd_path(wvd_path)
    if not path:
        raise AmazonMusicConfigError(
            "Amazon Music: A Widevine device file (.wvd) is required.\n"
            "Set wvd_path in settings.json (modules → amazonmusic) or in the GUI Amazon Music tab."
        )
    if path in (".", "./", ".\\"):
        raise AmazonMusicConfigError(
            "Amazon Music: wvd_path is set to the current directory, not a .wvd file.\n"
            "Browse to your .wvd file in settings and try again."
        )
    resolved = os.path.abspath(path)
    if not os.path.isfile(resolved):
        raise AmazonMusicConfigError(
            f"Amazon Music: Widevine device file not found: {path}\n"
            "Set wvd_path to a valid .wvd file (pywidevine create-device output)."
        )
    if not resolved.lower().endswith(".wvd"):
        raise AmazonMusicConfigError(
            f"Amazon Music: wvd_path must point to a .wvd file, got: {path}"
        )
    return resolved


@dataclasses.dataclass(slots=True)
class PSSHEntitlements:
    katana: typing.Optional[PSSH] = None
    """ Unlimited subscription tier (UHD, HD, SD, LD) """
    robin: typing.Optional[PSSH] = None
    """ Amazon Prime subscription tier """
    hawkfire: typing.Optional[PSSH] = None
    """ Free tier (SD) """
    nightwing: typing.Optional[PSSH] = None
    """ For AAC acquisition (all tiers have this) """
    sonic: typing.Optional[PSSH] = None
    """ Apart of Prime Music (SD)"""

    def to_dict(self):
        return dataclasses.asdict(self)
    
    def iterate(self):
        for name, item in (
            (field.name, getattr(self, field.name))
            for field in dataclasses.fields(self)
        ):
            if not isinstance(item, PSSH):
                continue
            yield (name, item)
    
    def _get_entitlements_group_id(self):
        # me when the method i want to use is literally a pain to implement:
        for n, entitlement in self.iterate():
            if not isinstance(entitlement, PSSH):
                continue
            pssh = WidevinePsshData()
            pssh.ParseFromString(entitlement.init_data)
            yield pssh.group_ids[0].decode()
    
    @property
    def music_territory(self):
        group_id = next(self._get_entitlements_group_id(), None)
        if not group_id:
            return
        
        return group_id[-2:]

    @property
    def entitlement_names(self):
        for group_id in self._get_entitlements_group_id():
            if not group_id:
                continue
            
            yield group_id.split(":", maxsplit=1)[0]

@dataclasses.dataclass(slots=True)
class AudioTrack:
    asin: str
    codec: CodecEnum
    bitrate: int
    """ As kilobits per second (kbps) """
    sample_rate: float
    url: str
    official_quality_name: str
    quality: str
    quality_ranking: int
    entitlements: PSSHEntitlements
    web_pssh: PSSH
    # pssh: typing.Any
    reference_loudness: Optional[str] = None
    """ e.g 7.2 LUFS, some manifests don't include it """
    bit_depth: Optional[int] = None

    def to_dict(self):
        return dataclasses.asdict(self)


# This is a Amazon Music module for OrpheusDL, requires an active account (optionally requires a active paid subscription)

module_information = ModuleInformation(  # Only service_name and module_supported_modes are mandatory
    service_name="Amazon Music",
    module_supported_modes=ModuleModes.download
    | ModuleModes.lyrics
    | ModuleModes.covers
    | ModuleModes.credits,
    # flags = ModuleFlags.hidden,
    # Flags:
    # startup_load: load module on startup
    # hidden: hides module from CLI help options
    # jwt_system_enable: handles bearer and refresh tokens automatically, though currently untested
    # private: override any public modules, only enabled with the -p/--private argument, currently broken
    global_settings={
        "wvd_path": "",
        "force_non_spatial": False,
        "prefer_spatial_mha1": False,
        "prefer_spatial_ac4": False,
        "prefer_aria2c": False,
        "prefer_removal_of_device_when_revoked": True,
        "prefer_account_continent": "NA", # or FE (Asia) or EU (Europe)
        "trim_track_by_sample_rate": True,
        "max_track_quality_to_use": "",
        "quality_format": "{sample_rate}kHz/{bit_depth}bit",
        "use_own_master_keys": False,
        "master_keys": {
            "TIER (KATANA/ROBIN):ISO 3166-1 Alpha-2 Country Code": "64 character key string in hex"
        },
        "force_login": False,
    },
    global_storage_variables=[],
    session_settings={
        "email": "",
        "password": "",
        "country": "",
    },
    session_storage_variables=["credentials"],
    netlocation_constant="amazon",
    test_url="https://music.amazon.com/albums/B08TZPYLJN",
    login_behaviour=ManualEnum.manual,  # setting to ManualEnum.manual disables Orpheus automatically calling login() when needed
    url_decoding=ManualEnum.manual,
)


def validate_amazonmusic_setup(settings: dict, session_storage_path: str, *, gui_mode: bool = False) -> None:
    """Raise AmazonMusicConfigError when required setup is missing (before module init)."""
    if not isinstance(settings, dict):
        settings = {}

    missing: list[str] = []
    wvd_path = _normalize_wvd_path(settings.get("wvd_path"))
    if not wvd_path:
        missing.append("wvd_path — Widevine device file (.wvd)")
    else:
        _validate_wvd_path(wvd_path)

    logged_in = ModuleInterface.has_cached_credentials(session_storage_path)
    country = str(settings.get("country") or "").strip()
    if not logged_in and not country:
        missing.append("country — two-letter Amazon store country (e.g. US, DE, NL)")

    if missing:
        where = "Settings > Amazon Music" if gui_mode else "settings.json (modules.amazonmusic)"
        lines = "\n  • ".join(missing)
        extra = ""
        if not logged_in:
            extra = (
                "\n  • Sign in — complete the browser login in the Amazon Music settings tab"
                if gui_mode
                else "\n  • Sign in — complete browser login in the GUI or CLI"
            )
        raise AmazonMusicConfigError(
            "Amazon Music is not set up yet. Before searching or downloading, configure:\n"
            f"  • {lines}{extra}\n\n"
            f"Configure these in {where}."
        )


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        self.settings = module_controller.module_settings
        self.options = module_controller.orpheus_options
        self.module_controller = module_controller
        self.print = module_controller.printer_controller.oprint
        self.tsc = module_controller.temporary_settings_controller
        self._quality_notice_seen: set[str] = set()
        self._quality_notice_lock = threading.Lock()

        # Items highest are iterated first
        self.quality_parse = {
            QualityEnum.MINIMUM: [
                "LD",
            ],
            QualityEnum.LOW: [
                "SD_LOW",
            ],
            QualityEnum.MEDIUM: [
                "SD_MEDIUM",
            ],
            QualityEnum.HIGH: [
                "SD_HIGH",
            ],
            QualityEnum.LOSSLESS: [
                "HD",
            ],
            QualityEnum.HIFI: ["UHD"],
            # Spatial tiers are only used when download quality is Atmos (or max_track_quality is spatial).
            QualityEnum.ATMOS: [
                "SPATIAL_RA360",
                "SPATIAL_ATMOS",
            ],
        }
        
        self.tier_parse = {
            AmazonMusicTier.FREE: QualityEnum.MINIMUM,
            AmazonMusicTier.PRIME: QualityEnum.HIGH,
            AmazonMusicTier.UNLIMITED: QualityEnum.HIFI,
        }

        # if not module_controller.orpheus_options.disable_subscription_check and (
        #     self.quality_parse[module_controller.orpheus_options.quality_tier]
        #     > self.mobile_session.get_user_tier()
        # ):
        #     print(
        #         "Example: quality set in the settings is not accessible by the current subscription"
        #     )

        wvd_path = _validate_wvd_path(self.settings.get("wvd_path"))
        self.settings["wvd_path"] = wvd_path
        self.cdm = Cdm.from_device(Device.load(wvd_path))

        if self.settings.get("force_login", False) or not self.load_cached_mobile_session(check_only=True):
            mobile_session = self.login_onto_mobile(
                self.settings.get("email", ""),
                self.settings.get("password", ""),
            )

    def login_onto_mobile(
        self, email: str, password: str
    ):  # Called automatically by Orpheus when standard_login is flagged, otherwise optional
        country = str(self.settings.get("country") or "").strip()
        if not country:
            raise AmazonMusicConfigError(
                "Amazon Music: Country is required before login.\n"
                "Set a two-letter ISO country code (e.g. US, BE, NL) in settings.json or the GUI Amazon Music tab."
            )
        self.print(
            f"\n{module_information.service_name}: Logging in using "
            f"{AmazonRegion.get_region_by_country(self.settings['country']).pretty_name} as the region.\n"
        )
        oauth_flow = self.module_controller.get_gui_handler("amazonmusic_oauth_flow")
        mobile_session = AmazonMusicMobileAPI.login_via_mobile(
            email,
            password,
            self.settings["country"],
            oauth_flow_callback=oauth_flow,
        )
        if not isinstance(mobile_session, AmazonMusicMobileAPI):
            # die
            raise TypeError("Login failed")

        LOGGER.debug(mobile_session.retrieve_capability())
        self.update_cached_credentials(mobile_session.credentials)

        return mobile_session
    
    def update_cached_credentials(self, credentials: AmazonMusicMobileAPICredentials, remove_arg_credentials: typing.Optional[bool] = False):
        cached_creds = self.tsc.read("credentials") or {}
        # Add support for previous versions of this module
        if isinstance(cached_creds, dict) and AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds):
            cached_creds = {
                cached_creds["web_client_config"]["music_territory"]: cached_creds
            }
        if remove_arg_credentials:
            if list(cached_creds.keys()).count(credentials.account_region.country) > 1:
                self.print(
                    f"{module_information.service_name}: "
                    f"There are multiple cached account credentials for {credentials.account_region.pretty_name}, "
                    "deleting the most recent one."
                )
            cached_creds.pop(credentials.account_region.country, None)
            final_creds = cached_creds
        else:
            final_creds = cached_creds | {
                credentials.account_region.country: 
                credentials.to_dict()
            }
        return self._save_credentials_to_tsc(final_creds)
    
    def _save_credentials_to_tsc(self, credentials: dict):
        # Save credentials to loginstorage.bin
        try: 
            self.tsc.set(
                "credentials",
                credentials
            )
        except Exception:
            return False
        return True

    @staticmethod
    def has_cached_credentials(session_storage_path: str) -> bool:
        """True when loginstorage.bin has a parseable Amazon Music session (logged in)."""
        cached_creds = (
            read_temporary_setting(
                session_storage_path, "amazonmusic", "custom_data", "credentials"
            )
            or {}
        )
        if not isinstance(cached_creds, dict) or not cached_creds:
            return False
        try:
            if AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds):
                creds_dict = cached_creds
            else:
                country_keys = [
                    k
                    for k, v in cached_creds.items()
                    if isinstance(v, dict)
                    and AmazonMusicMobileAPICredentials.is_dict_of_instance(v)
                ]
                if not country_keys:
                    return False
                creds_dict = cached_creds[country_keys[0]]
            if not str(creds_dict.get("refresh_token") or "").strip():
                return False
            AmazonMusicMobileAPICredentials.from_dict(creds_dict)
            return True
        except Exception:
            return False

    def load_cached_mobile_session(
        self,
        selected_region: typing.Optional[AmazonRegion] = None,
        use_exact_region: typing.Optional[bool] = False,
        check_only: typing.Optional[bool] = False
    ):
        cached_creds = self.tsc.read("credentials") or {}
        if not cached_creds:
            return
        # for debug
        # pprint.pprint(cached_creds)

        if selected_region and selected_region.country not in cached_creds and use_exact_region:
            return
        if not selected_region or (
            selected_region.country not in cached_creds
            and not AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds)
        ):
            selected_region = AmazonRegion.get_region_by_country(list(cached_creds.keys())[0])
        if not selected_region:
            return

        credentials = (
            AmazonMusicMobileAPICredentials.from_dict(
                cached_creds[selected_region.country]
                if not AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds)
                and selected_region.country in cached_creds
                else cached_creds
            ) if cached_creds else None
        )
        if not credentials:
            return
        if check_only is True:
            return True

        mobile_session = AmazonMusicMobileAPI(credentials=credentials)
        
        if mobile_session.credentials.access_token_expired:
            try:
                mobile_session.refresh_access_token()
            except httpx.HTTPError as he:
                LOGGER.error(he)
                self.print(
                    f"{module_information.service_name}: "
                    f"This account (name: {mobile_session.credentials.customer_info.get('name', 'Unknown user')}, "
                    f"country: {mobile_session.credentials.account_region.pretty_name}) "
                    f"device session/account may be deleted.")
                
                if self.settings["prefer_removal_of_device_when_revoked"] is True:
                    self.update_cached_credentials(credentials, remove_arg_credentials=True)
                    self.print(f"{module_information.service_name}: Deleted from cache as per your settings.")
                    return
                while True:
                    user_input = input(
                        f"{module_information.service_name}: "
                        "Would you like to remove the current device for the account region "
                        f"{mobile_session.credentials.account_region.pretty_name!r}? (Y/N): "
                    )
                    if "Y" in user_input.upper():
                        self.print(f"{module_information.service_name}: Deleting from cache..")
                        self.update_cached_credentials(credentials, remove_arg_credentials=True)
                        return
                    elif "N" in user_input.upper():
                        self.print(f"{module_information.service_name}: Skipped.")
                        return
                    else:
                        self.print(f"{module_information.service_name}: Invalid input, please try again.")
                        continue

            else:
                self.update_cached_credentials(mobile_session.credentials)

        LOGGER.debug(mobile_session.credentials)
        LOGGER.debug(mobile_session.get_account_subscription_tier())
        # print(pprint.pformat(mobile_session.retrieve_customer_home()))
        # print(pprint.pformat(mobile_session.get_account_status()))

        return mobile_session
    
    def select_usable_region_from_regions(self, regions: typing.Iterable[AmazonRegion]):
        if not regions:
            return
        cached_creds = self.tsc.read("credentials") or {}
        if not cached_creds:
            return
        for region in regions:
            if region.country not in cached_creds:
                continue
            return region
        return
    
    @functools.lru_cache
    def select_session(self, selected_region: AmazonRegion):
        # select by country
        session = self.load_cached_mobile_session(selected_region, use_exact_region=True)
        
        if not session:
            # select by continent (NA, EU, FE)
            continents = [selected_region.region] + [item for item in AmazonContinent if selected_region.region != item]
            for continent in continents:
                new_region = self.select_usable_region_from_regions(AmazonRegion.get_available_regions_by_continent(continent.name))
                if not new_region:
                    continue
                session = self.load_cached_mobile_session(
                    new_region,
                    use_exact_region=True
                )
                if not session:
                    continue
                self.print(
                    f"\n{module_information.service_name}: Using a {new_region.pretty_name!r} account "
                    f"for the region {selected_region.pretty_name!r} "
                    f"(you are not logged into that region directly).\n"
                )
                
                selected_region = new_region
                break
        if not isinstance(session, AmazonMusicMobileAPI):
            raise ValueError("No accounts logged in!")
        
        entitlement_usage = (
            self.settings['use_own_master_keys']
            and any(
                str(item).endswith(selected_region.country)
                for item in self.settings['master_keys']
            )
        )
        self.print(
            f"{module_information.service_name}: Selected account region: {selected_region.pretty_name!r}\n"
            f"  Subscription: {session.credentials.tier.name.title()!r}\n"
            f"  Entitlement master keys: {'enabled' if entitlement_usage else 'disabled'}\n"
        )

        return session, selected_region

    def custom_url_parse(self, link: str) -> MediaIdentification:
        url = urlparse(link)
        queries = parse_qs(url.query)
        
        music_territory_name = next(iter(queries.get("musicTerritory", [])), None)
        music_territory = None
        if music_territory_name:
            music_territory = AmazonRegion.get_region_by_country(music_territory_name)
        if not music_territory:
            music_territory = AmazonRegion.get_available_regions_by_continent(self.settings["prefer_account_continent"])[0]
        mobile_session, _ = self.select_session(music_territory)

        # if not (url.netloc.endswith(mobile_session.credentials.tld)) and not self.settings["use_own_master_keys"]:
        #     raise ValueError(
        #         f"You must provide a URL that is within the same region as the account!"
        #     )
        
        if track_item := queries.get("trackAsin"):
            return MediaIdentification(
                DownloadTypeEnum.track,
                media_id=track_item[0],
                extra_kwargs={
                    "media_region": music_territory
                }
            )

        components = [item.strip("\\") for item in url.path.split("/") if item]

        # use the same logic as orpheusdl cuz lazy
        url_constants = {
            "tracks": DownloadTypeEnum.track,
            "albums": DownloadTypeEnum.album,
            "playlists": DownloadTypeEnum.playlist,
            "user-playlists": DownloadTypeEnum.playlist,
            "artists": DownloadTypeEnum.artist,
        }

        type_matches = [
            media_type
            for url_check, media_type in url_constants.items()
            if url_check in components
        ]

        if len(components) > 2:
            return MediaIdentification(
                media_type=type_matches[-1],
                media_id=components[1]
                if type_matches[-1] is DownloadTypeEnum.artist
                else components[2],  # my/playlists/uuid
                extra_kwargs={
                    "media_region": music_territory
                }
            )
        else:
            return MediaIdentification(
                media_type=type_matches[-1],
                media_id=components[-1],
                extra_kwargs={
                    "media_region": music_territory
                }
            )

    def get_track_info(
        self,
        track_id: str,
        quality_tier: QualityEnum,
        codec_options: CodecOptions,
        media_region: AmazonRegion,
        data: dict = {},
    ) -> TrackInfo:  # Mandatory
        mobile_session, _ = self.select_session(media_region)

        if (
            codec_options.spatial_codecs is False
            and not self.settings["force_non_spatial"]
        ):
            self.print(
                f"{module_information.service_name}: Warning: force_non_spatial is not set to True in settings.json"
            )
            self.print(f"Spatial codecs will be downloaded unless this is changed!")
        try:
            mapped_audio_tracks = (
                data[f"{track_id}_quality_mapping"]
                if data and f"{track_id}_quality_mapping" in data
                else None
            )

            if not mapped_audio_tracks:
                asin, mpd = mobile_session.get_track_manifest(
                    track_asin=track_id,
                    force_3d=any(
                        not self.settings["force_non_spatial"] or self.settings[key]
                        for key in ("prefer_spatial_mha1", "prefer_spatial_ac4")
                    ),
                    region_to_use=media_region
                )
                if not (asin and mpd):
                    raise RuntimeError(
                        f"Failed to obtain track manifest for {track_id}"
                    )
                mapped_audio_tracks = self.mpd_to_quality_map(
                    mpd,
                    asin,
                    mobile_session.credentials.tier,
                    media_region,
                    mobile_session.credentials.account_region
                )

            effective_tier = (
                QualityEnum(min(quality_tier.value, self.tier_parse[mobile_session.credentials.tier].value))
                if not self.options.disable_subscription_check and not self.settings["use_own_master_keys"]
                else quality_tier
            )
            track_to_use = self._get_usable_audio_track_of_mapped_quailty(
                mapped_audio_tracks=mapped_audio_tracks,
                quality_tier=effective_tier,
                codec_options=codec_options,
                to_print=True,
            )

            track_data = (
                data[track_id]
                if data and track_id in data
                else mobile_session.get_track_info(track_id, region_to_use=media_region) # music_territory="CA"
            )
            album_id = str(track_data["album"]["asin"])
            album_data = (
                data[album_id]
                if data and album_id in data
                else mobile_session.get_album_info(album_id, region_to_use=media_region) # music_territory="NZ"
            )
            album_id = str(album_data["asin"])
            
            # if playlist_track_metadata := data.get(f"{track_id}_playlist"):
            #     import pprint
            #     pprint.pprint(playlist_track_metadata)

            # NOTE: I *could* use catalog_track AND catalog album
            # but I prefer to limit the amount of API Requests per track
            search_data = (
                data[f"{album_id}_search"]
                if data and f"{album_id}_search" in data
                else mobile_session.search(
                    query='"{}", "{}"'.format(
                        album_data["artist"]["name"],
                        album_data["title"],
                        # track_data["title"],
                    ),
                    asins=(album_id, track_id, str(track_data["globalAsin"])),
                    search_types=("catalog_album",),
                    region_to_use=media_region
                )
                or {}
            )
            # page_entity_data = (
            #     dict(data[f"{album_id}_page_entity_data"])
            #     if data and f"{album_id}_page_entity_data" in data
            #     else mobile_session.get_page(f"album/{album_id}", count=0, locale="en_US").get("entity", {})
            # )

            release_datetime = self._get_date_from_metadata(album_data)

            artists: list[str] = []
            contributor_asins = tuple(track_data["artist"]["contributorAsins"])
            # Most of the time there are more than one version of the main artist
            # e.g the main artist with all the albums is track_data["artist"]["asin"]
            # meanwhile there exists a alternative version of the same artist inside (no albums)
            # track_data["artist"]["contributorAsins"] (usually only one item)
            # e.g https://music.amazon.co.jp/albums/B08P688B62
            if len(contributor_asins) > 1:
                artists.extend(
                    str(item.get("name"))
                    for item in mobile_session.get_metadata(contributor_asins, region_to_use=media_region)[
                        "artistList"
                    ]
                    if item.get("name")
                )
            else:
                # Fallback, include the formatted artist name (might include contributors)
                artists.append(track_data["artist"]["name"])

            # Assumes the primaryArtistName is one single individual name, not concanated
            primary_artist_name = album_data.get("primaryArtistName", "")
            # Attempt to seperate each contributor if they're concatenated
            for artist in artists.copy():
                if sep_artists := self.parse_credit_names_from_name(artist):
                    if artist in artists:
                        artists.remove(artist)
                    for new_art in sep_artists:
                        if not primary_artist_name:
                            continue
                        if (
                            (
                                new_art in primary_artist_name
                                or
                                primary_artist_name in new_art
                            ) and
                            new_art != primary_artist_name
                        ):
                            # Fails here when an artist thats split
                            # isn't supposed to be split
                            continue
                        artists.append(new_art)

            if primary_artist_name in artists:
                artists.remove(primary_artist_name)

            # Remove duplicates
            artists = natsort.natsorted(set(artists))

            # Prefer to have the primary artist name at the start
            artists.insert(0, primary_artist_name)

            # Calculate the total disc avaliable by iterating each track and using the highest value
            disc_total = max(int(t["discNum"]) for t in album_data.get("tracks", [{}]))
            # Sanitize writers
            writers = [str(item).strip() for item in track_data.get("songWriters", []) if item]

            for name in writers.copy():
                if names := self.parse_credit_names_from_name(name):
                    if name in writers:
                        writers.remove(name)
                    writers.extend(names)

            composers = natsort.natsorted(set(writers))

            composers = "; ".join(composers)

            url = f"https://music.amazon.{media_region.domain_tld}/albums/{album_id}?trackAsin={track_id}&musicTerritory={media_region.country}"
            extra_tags = {
                "Composer": composers,  # force set the composer tag, because orpheus doesn't handle it
                "WWW": url,
            }

            if merchant_name := album_data.get("productDetails", {}).get(
                "merchantName"
            ):
                extra_tags.update(
                    {
                        "Merchant": " ".join(
                            str(
                                merchant_name
                            ).split()  # Remove whitespaces, if any (e.g Amazon Music USA)
                        )
                    }
                )
            if track_to_use.reference_loudness:
                extra_tags.update(
                    {
                        "REPLAYGAIN_REFERENCE_LOUDNESS": track_to_use.reference_loudness  # ref. https://wiki.hydrogenaud.io/index.php?title=ReplayGain_2.0_specification
                    }
                )

            product_details = album_data.get("productDetails") or {}

            # sanitize and format the genre name from search data
            # this genre tends to be more specific however,
            # it may not be always avaliable
            # e.g https://music.amazon.co.jp/albums/B09JNRPVHN?trackAsin=B09JNRQQ3P
            genres = sorted(
                list(
                    item
                    for item in set(
                        self.parse_genres_from_genre(search_data.get("primaryGenre", ""))
                        | self.parse_genres_from_genre(
                            product_details.get("primaryGenreName", "")
                        )
                    )
                    if item
                ),
                key=len
            )
            tags = Tags(
                album_artist=primary_artist_name,
                composer=composers,
                copyright=product_details.get("copyright"),
                isrc=track_data.get("isrc"),
                # upc="",
                disc_number=int(track_data["discNum"] or 1),
                total_discs=disc_total,
                track_number=int(track_data["trackNum"] or 1),  # None/0/1 if no discs
                total_tracks=int(album_data["trackCount"] or 1),  # None/0/1 if no discs
                # replay_gain=0.0,
                # replay_peak=0.0,
                genres=genres,
                label=product_details.get("label"),
                release_date=release_datetime.strftime(
                    "%Y-%m-%d"
                ),  # Format: YYYY-MM-DD
                # comment=comment,
                extra_tags=extra_tags,
            )

            valid_album_asins = [album_id]
            
            if asin := album_data.get("requestedAsin"):
                valid_album_asins.append(asin)
            if asin := album_data.get("globalAsin"):
                valid_album_asins.append(asin)

            cover_url, search_data = self.get_hi_res_cover(
                asins=valid_album_asins,
                query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
                search_data=search_data,
                mobile_session=mobile_session,
                media_region=media_region
            )
            if not cover_url:
                # Default 600 x 600 cover art
                cover_url = str(album_data.get("image", ""))
            else:
                cover_url = self.format_cover_url(
                    cover_url, self.options.default_cover_options
                )
            
            return TrackInfo(
                name=self.sanitize_parental_status_name(track_data["title"]),
                album_id=f"{album_id}_{media_region.country}",
                album=self.sanitize_parental_status_name(track_data["album"]["title"]),
                artists=artists,
                tags=tags,
                codec=track_to_use.codec,
                cover_url=cover_url,  # make sure to check module_controller.orpheus_options.default_cover_options
                release_year=int(release_datetime.strftime("%Y")),
                explicit=track_data["parentalControls"]["hasExplicitLanguage"],
                artist_id=track_data.get("artist", {}).get("asin", ""),  # optional
                id=str(track_id),
                duration=track_data["duration"],
                # animated_cover_url="",  # optional
                # description="",  # optional
                bit_depth=track_to_use.bit_depth,  # optional
                sample_rate=track_to_use.sample_rate,  # optional
                bitrate=track_to_use.bitrate,  # optional
                download_extra_kwargs={
                    "audio_track": track_to_use,
                    "media_region": media_region,
                },  # optional only if download_type isn't DownloadEnum.TEMP_FILE_PATH, whatever you want
                cover_extra_kwargs={
                    "media_region": media_region
                    # "data": {track_id: ""}
                },  # optional, whatever you want, but be very careful
                credits_extra_kwargs={
                    "media_region": media_region
                    # "data": {track_id: ""}
                },  # optional, whatever you want, but be very careful
                lyrics_extra_kwargs={
                    "media_region": media_region
                    # "data": {track_id: ""}
                },  # optional, whatever you want, but be very careful
                error="",  # only use if there is an error
            )
        except Exception as e:
            LOGGER.error(e, exc_info=True)
            raise e

    def get_track_download(self, audio_track: AudioTrack, media_region: AmazonRegion, **kwargs):
        mobile_session, _ = self.select_session(media_region)
        # print(audio_track)
        session_id = None
        try:
            os.makedirs("temp/", exist_ok=True)
            encrypted_track_location = f"{create_temp_filename()}.mp4"

            self.download(
                audio_track.url,
                encrypted_track_location,
                use_aria2c=self.settings["prefer_aria2c"],
            )

            decrypted_track_location = f"{create_temp_filename()}.mp4"  # {codec_data[audio_track.codec].container.name}
            if audio_track.entitlements.music_territory and audio_track.entitlements.music_territory != media_region.country:
                self.print(
                    f'{module_information.service_name}: Entitlements are '
                    f'for {audio_track.entitlements.music_territory}, '
                    f'instead of {media_region.country}'
            )
            if (
                self.settings["use_own_master_keys"]
                and any(str(item).endswith(media_region.country) for item in self.settings["master_keys"])
            ):
                while True:
                    selected_pssh = None
                    main_key: bytes = b""
                    for name, entitle_pssh in audio_track.entitlements.to_dict().items():
                        if not isinstance(entitle_pssh, PSSH):
                            continue
                        key_name = f"{name.upper()}:{audio_track.entitlements.music_territory}"
                        if key_name not in self.settings["master_keys"]:
                            continue

                        main_key = bytes.fromhex(self.settings["master_keys"][key_name])
                        if main_key:
                            selected_pssh = entitle_pssh
                        break
                    
                    if not (main_key and selected_pssh):
                        break
                    
                    key_id, key = self.get_decrypted_key(
                        enc_key=main_key,
                        pssh=selected_pssh,
                        enc_key_type="CONTENT",
                    )
                    # print(f"using own entitlements {key_id=} {key=}")
                    self.call_shaka_packager(
                        encrypted_file=encrypted_track_location,
                        destination_file=decrypted_track_location,
                        key_id=key_id,
                        key=key,
                        label=audio_track.official_quality_name
                    )
                    break
                    
            # Interact with the license server
            if not os.path.exists(decrypted_track_location) and mobile_session.credentials.tier is not AmazonMusicTier.FREE:
                session_id = self.cdm.open()
                # print(f"{audio_track.entitlements=} {audio_track.web_pssh=}")
                
                used_entitlement: dict[str, PSSH] | None = {}
                for name, pssh_to_test in audio_track.entitlements.to_dict().items():
                    if not self.cdm.system_id == 9780:
                        continue
                    if not pssh_to_test:
                        continue
                    if f"{name.upper()}_CONTENT" not in mobile_session.credentials.tier.internal_content_tiers and not self.options.disable_subscription_check:
                        continue
                    
                    # maybe?
                    # if audio_track.entitlements.music_territory != mobile_session.credentials.account_region.country:
                    #     continue

                    license_challenge = base64.b64encode(
                        self.cdm.get_license_challenge(
                            session_id, pssh_to_test, privacy_mode=False
                        )
                    ).decode("utf-8")

                    license_response = None
                    try:
                        license_response = mobile_session.get_license_response(
                            asin=audio_track.asin, challenge=license_challenge, drm_type="WIDEVINE_ENTITLEMENT"
                        )
                    except (ValueError, httpx.HTTPStatusError) as e:
                        self.print(f"{module_information.service_name}: Failed entitlement master key acquisition.")
                        continue
                    else:
                        if not license_response:
                            # license_response = input("License retrieval failed, enter response here (for testing): ")
                            continue
                        # print(f"used entitle acq: {name}")
                        used_entitlement.update({
                            name: pssh_to_test
                        })
                        self.cdm.parse_license(session_id, license_response)
                        break
                else:
                    # print("reg")
                    license_challenge = base64.b64encode(
                        self.cdm.get_license_challenge(
                            session_id, audio_track.web_pssh, privacy_mode=False
                        )
                    ).decode("utf-8")

                    license_response = mobile_session.get_license_response(
                        asin=audio_track.asin, challenge=license_challenge, drm_type="WIDEVINE"
                    )
                    if not license_response:
                        raise ValueError("Failed to communicate with the license server")

                    self.cdm.parse_license(session_id, license_response)

                # print(used_entitlement)
                for key in self.cdm.get_keys(session_id):
                    if key.type == "ENTITLEMENT":
                        if not used_entitlement:
                            continue

                        name, used_pssh = used_entitlement.popitem()
                        # name , used_pssh = used_entitlement[next(iter(used_entitlement))]
                        key_id, dec_key = self.get_decrypted_key(
                            key.key,
                            used_pssh,
                            "CONTENT"
                        )
                        key_name = f"{name.upper()}:{audio_track.entitlements.music_territory}"
                        # if not self.settings["master_keys"].get("key_name"):
                        self.print(
                            f'{module_information.service_name}: New entitlement key '
                            f'found for {audio_track.entitlements.music_territory}! '
                            f'"{key_name}": "{key.key.hex()}"'
                        )

                    elif key.type == "CONTENT":
                        key_id = key.kid.hex
                        dec_key = key.key.hex()
                    else:
                        continue
                    # print(f"{key_id=} {dec_key=}")
                    # continue
                        
                    self.call_shaka_packager(
                        encrypted_file=encrypted_track_location,
                        destination_file=decrypted_track_location,
                        key_id=key_id,
                        key=dec_key,
                        label=audio_track.quality.upper()
                    )

            if not os.path.exists(decrypted_track_location):
                raise FileNotFoundError("Unable to decrypt the downloaded media file")

            LOGGER.debug("Ok with decryption")

            silentremove(encrypted_track_location)

            selected_codec_data = codec_data[audio_track.codec]

            if selected_codec_data.spatial:
                return TrackDownloadInfo(
                    download_type=DownloadEnum.TEMP_FILE_PATH,
                    temp_file_path=decrypted_track_location,
                    different_codec=audio_track.codec,
                )

            LOGGER.debug(
                f"Using {selected_codec_data.container.name} as the container for {audio_track.codec.name}"
            )
            final_decrypted_track_location = (
                f"{create_temp_filename()}.{selected_codec_data.container.name}"
            )
            # return
            ffcmd: ffmpeg = ffmpeg.input(decrypted_track_location)  # type: ignore
            ffcmd_out_kwargs = {
                "filename": final_decrypted_track_location,
                "loglevel": "warning",
                "audio_bitrate": f"{audio_track.bitrate}k",
            }
            if self.settings["trim_track_by_sample_rate"]:
                # self.print(f"Trimming out the first {int(int(audio_track.sample_rate) * 6.5)} samples")
                ffcmd_out_kwargs.update(
                    {"af": f"atrim=start_sample={int(int(audio_track.sample_rate) * 6.5)}"}
                )

            ffcmd = ffcmd.output(**ffcmd_out_kwargs)
            stdout, stderr = ffcmd.run()
            if stderr:
                raise RuntimeError(f"ffmpeg: {stderr}")
            silentremove(decrypted_track_location)


        finally:
            if session_id:
                self.cdm.close(session_id)

        return TrackDownloadInfo(
            download_type=DownloadEnum.TEMP_FILE_PATH,
            temp_file_path=final_decrypted_track_location,
        )

    def get_preview_audio_path(
        self,
        track_id: str,
        media_region: typing.Optional[AmazonRegion] = None,
        media_region_country: typing.Optional[str] = None,
    ) -> typing.Optional[str]:
        """
        Decrypt the full track at low quality for in-app search preview playback.
        Returns a local temp file path (not an HTTP URL). Results are cached per session.
        """
        if not track_id or not str(track_id).strip():
            return None

        if media_region is None and media_region_country:
            try:
                media_region = AmazonRegion.get_region_by_country(str(media_region_country).strip().upper())
            except Exception:
                media_region = None

        if media_region is None:
            continents = AmazonRegion.get_available_regions_by_continent(
                self.settings["prefer_account_continent"]
            )
            media_region = (
                self.select_usable_region_from_regions(continents)
                or (continents[0] if continents else None)
            )
        if media_region is None:
            return None

        cache_key = f"{track_id}:{media_region.country}"
        cache = getattr(self, "_preview_audio_cache", None)
        if cache is None:
            cache = {}
            self._preview_audio_cache = cache
        cached = cache.get(cache_key)
        if cached and os.path.isfile(cached):
            return cached

        mobile_session, media_region = self.select_session(media_region)
        asin, mpd = mobile_session.get_track_manifest(
            track_asin=track_id,
            force_3d=False,
            region_to_use=media_region,
        )
        if not (asin and mpd):
            return None

        mapped_audio_tracks = self.mpd_to_quality_map(
            mpd,
            asin,
            mobile_session.credentials.tier,
            media_region,
            mobile_session.credentials.account_region,
        )
        preview_tier = QualityEnum.MINIMUM
        audio_track = self._get_usable_audio_track_of_mapped_quailty(
            mapped_audio_tracks=mapped_audio_tracks,
            quality_tier=QualityEnum(
                min(
                    preview_tier.value,
                    self.tier_parse[mobile_session.credentials.tier].value,
                )
            )
            if not self.options.disable_subscription_check
            else preview_tier,
            to_print=False,
        )
        if not isinstance(audio_track, AudioTrack):
            return None

        download_info = self.get_track_download(audio_track, media_region)
        if (
            download_info
            and download_info.download_type is DownloadEnum.TEMP_FILE_PATH
            and download_info.temp_file_path
            and os.path.isfile(download_info.temp_file_path)
        ):
            cache[cache_key] = download_info.temp_file_path
            return download_info.temp_file_path
        return None

    @staticmethod
    def _metadata_artist_name(entity: dict) -> str:
        """Artist display name from album/track metadata or text-search hits."""
        if not entity:
            return ""
        artist = entity.get("artist")
        if isinstance(artist, dict) and artist.get("name"):
            return str(artist["name"])
        for key in ("primaryArtistName", "artistName", "albumArtistName"):
            if entity.get(key):
                return str(entity[key])
        return ""

    @staticmethod
    def _format_sample_rate_khz(sample_rate: typing.Any) -> str:
        try:
            sr = float(sample_rate)
        except (TypeError, ValueError):
            return ""
        if sr <= 0:
            return ""
        if sr == int(sr):
            return str(int(sr))
        return f"{sr:g}"

    @staticmethod
    def _is_spatial_quality_display(codec_name: str, official_name: str) -> bool:
        codec_lower = (codec_name or "").lower()
        official_upper = (official_name or "").strip().upper()
        if official_upper in ("3D", "ATMOS"):
            return True
        return any(
            hint in codec_lower
            for hint in (
                "e-ac-3",
                "ac-4",
                "mpeg-h 3d",
                "mha1",
                "mhm1",
                "dolby atmos",
                "360",
            )
        )

    @staticmethod
    def _spatial_quality_label(quality_tags: dict) -> str:
        official = str(quality_tags.get("official_quality_name") or "").strip()
        codec = str(quality_tags.get("codec_pretty_name") or "").strip()
        parts: list[str] = []
        if official:
            parts.append(official)
        if codec and codec not in parts:
            parts.append(codec)
        return " ".join(parts)

    @staticmethod
    def _amazon_spatial_display_label(codec_name: str = "", official_name: str = "") -> str:
        """Canonical spatial labels for search UI (small-caps via gui._to_small_caps)."""
        combined = f"{official_name} {codec_name}".lower()
        if re.search(r"mpeg-h|mha1|mhm1|\bra360\b", combined) or re.search(
            r"\b360\b", combined
        ):
            return "3D MPEG-H Audio"
        if re.search(r"e-ac-3|ac-4|\batmos\b|joc|dolby", combined):
            return "◗◖ ATMOS"
        if (official_name or "").strip().upper() in ("3D", "ATMOS"):
            return "◗◖ ATMOS"
        raw = ModuleInterface._spatial_quality_label(
            {
                "official_quality_name": official_name,
                "codec_pretty_name": codec_name,
            }
        )
        return raw or "◗◖ ATMOS"

    def _quality_label_from_mapped(
        self,
        mapped_audio_tracks: dict,
        quality_tier: typing.Optional[QualityEnum] = None,
    ) -> str:
        tier = quality_tier or getattr(self.options, "quality_tier", None) or QualityEnum.HIGH
        best = self._get_usable_audio_track_of_mapped_quailty(mapped_audio_tracks, tier)
        return self._format_quality_display(
            {
                "official_quality_name": best.official_quality_name,
                "codec_pretty_name": codec_data[best.codec].pretty_name,
                "bit_depth": best.bit_depth,
                "sample_rate": best.sample_rate,
            },
            str(self.settings.get("quality_format", "")),
        )

    @staticmethod
    def _quality_display_sort_key(label: str) -> tuple[int, float, int]:
        """Sort key for picking the single best album-level quality label."""
        text = str(label or "").strip()
        low = text.lower()
        if re.search(r"opus\s+only", low):
            return (1, 0.0, 0)
        m = re.search(r"(\d+(?:\.\d+)?)\s*kHz\s*/\s*(\d+)\s*bit", text, re.I)
        if m:
            return (3, float(m.group(1)), int(m.group(2)))
        if ModuleInterface._is_spatial_quality_display(text, text):
            return (2, 48.0, 0)
        return (0, 0.0, 0)

    @staticmethod
    def _sample_track_asins_for_quality_probe(
        track_asins: list[str], max_probe: int = SEARCH_QUALITY_MAX_TRACK_PROBE
    ) -> tuple[str, ...]:
        """First / middle / last track — enough to detect the album's highest tier."""
        if not track_asins:
            return ()
        if len(track_asins) <= max_probe:
            return tuple(track_asins)
        indices = sorted({0, len(track_asins) // 2, len(track_asins) - 1})
        return tuple(track_asins[i] for i in indices[:max_probe])

    @staticmethod
    def _best_quality_display(displays: typing.Iterable[str]) -> str:
        """Highest available quality only (e.g. 44.1kHz/24bit, not 16bit + OPUS)."""
        labels = [str(d).strip() for d in displays if d and str(d).strip()]
        if not labels:
            return ""
        return max(labels, key=ModuleInterface._quality_display_sort_key)

    def _collect_manifest_quality_labels(
        self,
        mapped_audio_tracks: dict[QualityEnum, dict[str, list[AudioTrack]]],
    ) -> tuple[set[str], set[str]]:
        """All distinct spatial and stereo tiers present in one MPD (for search badges)."""
        spatial_labels: set[str] = set()
        stereo_labels: set[str] = set()
        quality_format = str(self.settings.get("quality_format", ""))
        seen_spatial: set[str] = set()
        seen_stereo: set[str] = set()

        for _qe, _qn, tracks in self._iter_over_tracks_to_quality_map(mapped_audio_tracks):
            if not tracks:
                continue
            best = tracks[0]
            tier_key = str(best.quality or best.official_quality_name or "")
            is_spatial = self._is_spatial_audio_track(best)
            if is_spatial:
                if tier_key in seen_spatial:
                    continue
                seen_spatial.add(tier_key)
            else:
                if tier_key in seen_stereo:
                    continue
                seen_stereo.add(tier_key)
            if is_spatial or self._is_spatial_audio_track(best):
                label = self._spatial_manifest_badge(best)
                if not label:
                    continue
                spatial_labels.add(label)
            else:
                label = self._format_quality_display(
                    {
                        "official_quality_name": best.official_quality_name,
                        "codec_pretty_name": codec_data[best.codec].pretty_name,
                        "bit_depth": best.bit_depth,
                        "sample_rate": best.sample_rate,
                    },
                    quality_format,
                )
                if not label:
                    continue
                stereo_labels.add(label)
        return spatial_labels, stereo_labels

    def _amazon_force_3d_for_manifest(self) -> bool:
        """Match get_album_info / expand manifest requests (Atmos + UHD on same ASIN)."""
        return any(
            not self.settings.get("force_non_spatial", False)
            or self.settings.get(key)
            for key in ("prefer_spatial_mha1", "prefer_spatial_ac4")
        )

    def _probe_manifest_quality_label_sets(
        self,
        mobile_session: AmazonMusicMobileAPI,
        track_asins: tuple[str, ...],
        media_region: AmazonRegion,
        *,
        force_3d: bool,
        per_track_stereo_label: bool = False,
    ) -> tuple[set[str], set[str]]:
        """
        Discover tiers in the manifest for the given force_3d mode.

        Amazon's try3dAsinSubstitution (force_3d=True) exposes Atmos but often drops UHD;
        force_3d=False is required for 192 kHz / stereo tiers (see azapi.get_tracks_manifest).
        """
        spatial_labels: set[str] = set()
        stereo_labels: set[str] = set()
        if not track_asins:
            return spatial_labels, stereo_labels
        for track_asin, manifest in mobile_session.get_tracks_manifest(
            track_asins,
            force_3d=force_3d,
            region_to_use=media_region,
        ):
            if manifest is None:
                continue
            try:
                mapped = self.mpd_to_quality_map(
                    manifest,
                    track_asin,
                    mobile_session.credentials.tier,
                    media_region,
                    mobile_session.credentials.account_region,
                )
                spatial, stereo = self._collect_manifest_quality_labels(mapped)
                spatial_labels |= spatial
                stereo_labels |= stereo
                if per_track_stereo_label and not force_3d:
                    per_track = self._quality_label_from_mapped(
                        mapped, SEARCH_PROBE_STEREO_TIER
                    )
                    if per_track and not self._is_spatial_quality_display(
                        per_track, per_track
                    ):
                        stereo_labels.add(per_track)
            except Exception as ex:
                LOGGER.debug(
                    "Manifest quality collect failed for %s: %s", track_asin, ex
                )
        return spatial_labels, stereo_labels

    def _probe_quality_labels(
        self,
        mobile_session: AmazonMusicMobileAPI,
        track_asins: tuple[str, ...],
        media_region: AmazonRegion,
        force_3d: bool,
        quality_tier: QualityEnum,
    ) -> set[str]:
        labels: set[str] = set()
        if not track_asins:
            return labels
        for track_asin, manifest in mobile_session.get_tracks_manifest(
            track_asins,
            force_3d=force_3d,
            region_to_use=media_region,
        ):
            if manifest is None:
                continue
            mapped = self.mpd_to_quality_map(
                manifest,
                track_asin,
                mobile_session.credentials.tier,
                media_region,
                mobile_session.credentials.account_region,
            )
            label = self._quality_label_from_mapped(mapped, quality_tier)
            if label:
                labels.add(label)
        return labels

    @staticmethod
    def _pick_spatial_display_label(labels: typing.Iterable[str]) -> typing.Optional[str]:
        for label in labels:
            text = str(label)
            if ModuleInterface._is_spatial_quality_display(text, text):
                return text
        return None

    @staticmethod
    def _spatial_label_is_atmos(text: str) -> bool:
        combined = str(text or "").lower()
        return bool(
            re.search(r"e-ac-3|ac-4|\batmos\b|joc|dolby|◗", combined)
            and not re.search(r"mpeg-h|mha1|mhm1|\bra360\b", combined)
        )

    @staticmethod
    def _spatial_label_is_ra360(text: str) -> bool:
        combined = str(text or "").lower()
        if "3d mpeg-h" in combined or "spatial_ra360" in combined:
            return True
        return bool(
            re.search(r"mpeg-h|mha1|mhm1", combined)
            and not re.search(r"e-ac-3|ac-4|\batmos\b|joc|dolby|◗", combined)
        )

    @staticmethod
    def _spatial_display_tokens(spatial_labels: typing.Iterable[str]) -> list[str]:
        """Distinct spatial badges (Atmos and Sony 360RA are separate tiers)."""
        has_atmos = False
        has_ra360 = False
        for label in spatial_labels or []:
            text = str(label).strip()
            if not text:
                continue
            display = ModuleInterface._amazon_manifest_label_for_display(text)
            if ModuleInterface._spatial_label_is_atmos(text) or display == "◗◖ ATMOS":
                has_atmos = True
            if ModuleInterface._spatial_label_is_ra360(text) or display == "3D MPEG-H Audio":
                has_ra360 = True
        tokens: list[str] = []
        if has_atmos:
            tokens.append("◗◖ ATMOS")
        if has_ra360:
            tokens.append("3D MPEG-H Audio")
        return tokens

    def _merge_manifest_quality_labels(
        self,
        spatial_labels: typing.Iterable[str],
        stereo_labels: typing.Iterable[str],
    ) -> list[str]:
        """Build search Additional tokens: spatial first, then best non-spatial tier."""
        result: list[str] = []
        spatial_set = {str(label).strip() for label in spatial_labels if label and str(label).strip()}
        stereo_set = {
            str(label).strip()
            for label in stereo_labels
            if label
            and str(label).strip()
            and not self._is_spatial_quality_display(str(label), str(label))
        }
        if spatial_set:
            for spatial_display in self._spatial_display_tokens(spatial_set):
                if spatial_display and spatial_display not in result:
                    result.append(spatial_display)
        if stereo_set:
            best_stereo = self._best_quality_display(stereo_set)
            if best_stereo:
                stereo_display = self._amazon_manifest_label_for_display(best_stereo)
                if stereo_display and stereo_display not in result:
                    result.append(stereo_display)
        return result

    @staticmethod
    def _strip_legacy_bit_khz_suffix(text: str) -> str:
        """Remove template artifacts like 'bit-48kHz' left when bit_depth is empty."""
        return re.sub(
            r"\s*bit-?\s*\d+(?:\.\d+)?\s*khz\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    @staticmethod
    def _format_quality_display(quality_tags: dict, quality_format: str) -> str:
        """Human-readable quality (Qobuz-style kHz/bit); Opus SD has no bit depth."""
        codec_name = str(quality_tags.get("codec_pretty_name") or "").strip().lower()
        official_name = str(quality_tags.get("official_quality_name") or "").strip()
        bit_depth = quality_tags.get("bit_depth")
        sample_rate = quality_tags.get("sample_rate")
        if codec_name == "opus" and bit_depth is None:
            return "OPUS only"
        if ModuleInterface._is_spatial_quality_display(codec_name, official_name):
            return ModuleInterface._amazon_spatial_display_label(codec_name, official_name)
        if bit_depth is not None and sample_rate is not None:
            try:
                bd = int(bit_depth)
                sr_text = ModuleInterface._format_sample_rate_khz(sample_rate)
                if bd > 0 and sr_text:
                    return f"{sr_text}kHz/{bd}bit"
            except (TypeError, ValueError):
                pass
        fmt = {
            "official_quality_name": official_name,
            "codec_pretty_name": quality_tags.get("codec_pretty_name") or "",
            "bit_depth": bit_depth if bit_depth is not None else "",
            "sample_rate": ModuleInterface._format_sample_rate_khz(sample_rate),
        }
        try:
            out = str(quality_format or "").format(**fmt)
        except (KeyError, ValueError, TypeError):
            out = f"{official_name} {fmt['codec_pretty_name']}".strip()
        if re.search(r"\bopus\b", out, re.I) and re.search(r"none\s*bit|nonebit", out, re.I):
            return "OPUS only"
        return ModuleInterface._strip_legacy_bit_khz_suffix(out)

    @staticmethod
    def _quality_labels_from_entity(entity: dict) -> list[str]:
        """HD / UHD / Atmos from catalog contentEncoding (may not match actual MPD codecs)."""
        if not isinstance(entity, dict):
            return []
        encoding_labels = {
            "hdAvailable": "HD",
            "hdavailable": "HD",
            "HD": "HD",
            "hd": "HD",
            "uhdAvailable": "UHD",
            "uhdavailable": "UHD",
            "UHD": "UHD",
            "uhd": "UHD",
            "ULTRA_HD": "Ultra HD",
            "ULTRAHD": "Ultra HD",
            "ultraHdAvailable": "Ultra HD",
            "atmosAvailable": "Dolby Atmos",
            "atmosavailable": "Dolby Atmos",
            "ra360Available": "3D MPEG-H Audio",
            "ra360available": "3D MPEG-H Audio",
        }
        tags: list[str] = []

        def _add(label: str):
            if label and label not in tags:
                tags.append(label)

        def _label_for_key(key: str) -> typing.Optional[str]:
            if key in encoding_labels:
                return encoding_labels[key]
            kl = key.lower()
            if "uhd" in kl or "ultra" in kl:
                return "UHD"
            if kl == "hd" or kl.endswith("hd") or "highdefinition" in kl:
                return "HD"
            if "atmos" in kl:
                return "Dolby Atmos"
            if "360" in kl or "ra360" in kl:
                return "3D MPEG-H Audio"
            return None

        enc = entity.get("contentEncoding")
        if isinstance(enc, dict):
            for key, val in enc.items():
                if val in (False, 0, None, "", "false", "False"):
                    continue
                _add(_label_for_key(str(key)) or str(key))
        elif isinstance(enc, list):
            for item in enc:
                if isinstance(item, dict):
                    key = str(item.get("type") or item.get("name") or item.get("encoding") or "")
                    if item.get("value") in (False, 0, None, "", "false", "False"):
                        continue
                    _add(_label_for_key(key) or str(key))
                elif isinstance(item, str):
                    try:
                        _add(encoding_labels[item])
                    except KeyError:
                        _add(_label_for_key(item) or item)

        for flag_key, label in (
            ("uhdAvailable", "UHD"),
            ("hdAvailable", "HD"),
            ("atmosAvailable", "Dolby Atmos"),
            ("ra360Available", "3D MPEG-H Audio"),
        ):
            if entity.get(flag_key) in (True, "true", "True", 1):
                _add(label)

        audio_quality = entity.get("audioQuality") or entity.get("maximumAudioQuality")
        if isinstance(audio_quality, str):
            _add(_label_for_key(audio_quality) or audio_quality)

        return natsort.natsorted(tags, key=len)

    @staticmethod
    def _parse_khz_bit_display_label(label: str) -> typing.Optional[tuple[float, int]]:
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*kHz\s*/\s*(\d+)\s*bit",
            str(label or ""),
            re.IGNORECASE,
        )
        if not m:
            return None
        try:
            return float(m.group(1)), int(m.group(2))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_hi_res_lossless(sample_rate_khz: float, bit_depth: int) -> bool:
        """Same rule as Apple Music / Qobuz search: hi-res only above 48 kHz at 24-bit+."""
        return float(sample_rate_khz) > 48.0 and int(bit_depth) >= 24

    @staticmethod
    def _is_amazon_uhd_lossless(sample_rate_khz: float, bit_depth: int) -> bool:
        """Amazon Ultra HD is commonly 48 kHz / 24-bit (catalog + MPD UHD tier)."""
        return float(sample_rate_khz) >= 48.0 and int(bit_depth) >= 24

    @staticmethod
    def _amazon_manifest_label_for_display(label: str) -> str:
        """MPD-derived label → ATMOS / kHz·bit / HI-RES / OPUS only (never catalog UHD flags)."""
        text = str(label or "").strip()
        if not text:
            return ""
        if ModuleInterface._is_spatial_quality_display(text, text):
            return ModuleInterface._amazon_spatial_display_label(text, "")
        if re.search(r"\bopus\s+only\b", text, re.IGNORECASE):
            return "OPUS only"
        if re.search(r"\b(?:uhd|ultra\s*hd)\b", text, re.IGNORECASE):
            return "🅷 HI-RES"
        parsed = ModuleInterface._parse_khz_bit_display_label(text)
        if parsed:
            sr_khz, bit_depth = parsed
            if (
                ModuleInterface._is_hi_res_lossless(sr_khz, bit_depth)
                or ModuleInterface._is_amazon_uhd_lossless(sr_khz, bit_depth)
            ):
                return "🅷 HI-RES"
            return text
        return text

    @staticmethod
    def _amazon_catalog_quality_display_labels(tags: typing.Iterable[str]) -> list[str]:
        """Fallback when manifest probe fails: never treat catalog UHD as HI-RES."""
        lower = [str(t).strip().lower() for t in tags if t]
        if not lower:
            return []
        out: list[str] = []
        if any("atmos" in tl or tl == "dolby atmos" for tl in lower):
            out.append("◗◖ ATMOS")
        if any(
            "ra360" in tl or tl in ("360 reality audio",) or "mpeg-h" in tl
            for tl in lower
        ):
            if "3D MPEG-H Audio" not in out:
                out.append("3D MPEG-H Audio")
        has_uhd = any(
            tl in ("uhd", "ultra hd", "ultra_hd", "ultrahd") or "ultra" in tl for tl in lower
        )
        has_hd = any(tl == "hd" for tl in lower)
        if has_uhd:
            if "🅷 HI-RES" not in out:
                out.append("🅷 HI-RES")
        elif has_hd:
            if "FLAC" not in out:
                out.append("FLAC")
        if out:
            return out
        return [str(t) for t in tags if t]

    @staticmethod
    def _quality_labels_include_atmos(labels: typing.Iterable[str]) -> bool:
        for label in labels or []:
            text = str(label)
            low = text.lower()
            if "atmos" in low or "◗" in text or "immersive" in low:
                return True
        return False

    @staticmethod
    def _manifest_labels_include_stereo(labels: typing.Iterable[str]) -> bool:
        for label in labels or []:
            text = str(label)
            low = text.lower()
            if "hi-res" in low or "🅷" in text:
                return True
            if re.search(r"\d+(?:\.\d+)?\s*kHz", text, re.I):
                return True
            if text.strip().upper() == "FLAC" or re.search(r"\bopus\s+only\b", low):
                return True
        return False

    @staticmethod
    def _catalog_quality_tags_from_entity_and_tracks(entity: dict) -> list[str]:
        """Album/playlist encoding flags from the entity and each track row."""
        tags: list[str] = []
        seen: set[str] = set()

        def _add_from(source: dict):
            for tag in ModuleInterface._quality_labels_from_entity(source):
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)

        if not isinstance(entity, dict):
            return tags
        _add_from(entity)
        meta = entity.get("metadata")
        if isinstance(meta, dict):
            _add_from(meta)
        for track in entity.get("tracks") or []:
            if isinstance(track, dict):
                _add_from(track)
                # Playlist tracks carry contentEncoding under a nested metadata dict.
                track_meta = track.get("metadata")
                if isinstance(track_meta, dict):
                    _add_from(track_meta)
        return tags

    def _supplement_stereo_quality_from_catalog(
        self,
        manifest_labels: list[str],
        item: dict,
        catalog: typing.Optional[dict] = None,
    ) -> list[str]:
        """When manifest probe found Atmos only, add catalog UHD/HD if present."""
        if not manifest_labels or not self._quality_labels_include_atmos(manifest_labels):
            return manifest_labels
        if self._manifest_labels_include_stereo(manifest_labels):
            return manifest_labels
        merged = list(manifest_labels)
        for entity in (catalog, item):
            if not isinstance(entity, dict):
                continue
            tags = self._catalog_quality_tags_from_entity_and_tracks(entity)
            for label in self._amazon_catalog_quality_display_labels(tags):
                text = str(label)
                if self._is_spatial_quality_display(text, text) or "◗" in text:
                    continue
                if text not in merged:
                    merged.append(text)
        return merged

    @staticmethod
    def _catalog_atmos_display_label(entity: dict) -> typing.Optional[str]:
        """Atmos badge from catalog contentEncoding when manifest probe missed spatial tiers."""
        if not isinstance(entity, dict):
            return None
        tags = ModuleInterface._quality_labels_from_entity(entity)
        if not any("atmos" in str(t).lower() for t in tags):
            return None
        for label in ModuleInterface._amazon_catalog_quality_display_labels(tags):
            if "atmos" in str(label).lower() or "◗" in str(label):
                return label
        return None

    def _album_quality_from_catalog(
        self,
        album_catalog: dict,
        search_hit: typing.Optional[dict] = None,
    ) -> str:
        """Album-level quality from catalog flags (matches search/discography Additional column)."""
        if not isinstance(album_catalog, dict):
            return ""
        catalog_tags = self._catalog_quality_tags_from_entity_and_tracks(album_catalog)
        if isinstance(search_hit, dict):
            for tag in self._catalog_quality_tags_from_entity_and_tracks(search_hit):
                if tag not in catalog_tags:
                    catalog_tags.append(tag)
        if not catalog_tags:
            return ""
        labels = self._amazon_catalog_quality_display_labels(catalog_tags)
        return " / ".join(labels) if labels else ""

    def _amazon_search_track_quality_labels(
        self,
        mobile_session: AmazonMusicMobileAPI,
        track_asin: str,
        media_region: AmazonRegion,
    ) -> list[str]:
        """One-track manifest probe for accurate track search Additional column."""
        if not track_asin:
            return []
        try:
            spatial_labels, _ = self._probe_manifest_quality_label_sets(
                mobile_session,
                (str(track_asin),),
                media_region,
                force_3d=True,
            )
            _, stereo_labels = self._probe_manifest_quality_label_sets(
                mobile_session,
                (str(track_asin),),
                media_region,
                force_3d=False,
                per_track_stereo_label=True,
            )
            return self._merge_manifest_quality_labels(spatial_labels, stereo_labels)
        except Exception as ex:
            LOGGER.debug("Track search manifest quality probe failed for %s: %s", track_asin, ex)
            return []

    @staticmethod
    def _playlist_payload_is_complete(p_data: dict) -> bool:
        """True when payload is a full playlist API response, not a text-search hit."""
        if not p_data or not isinstance(p_data, dict):
            return False
        meta = p_data.get("metadata")
        if not isinstance(meta, dict) or not meta.get("title"):
            return False
        tracks = p_data.get("tracks")
        if not tracks or not isinstance(tracks, list):
            return True
        first = tracks[0]
        if not isinstance(first, dict):
            return False
        return bool(first.get("metadata") or first.get("asin"))

    @staticmethod
    def _playlist_track_asin(track: dict) -> typing.Optional[str]:
        if not isinstance(track, dict):
            return None
        meta = track.get("metadata")
        if isinstance(meta, dict) and meta.get("asin"):
            return str(meta["asin"])
        if track.get("asin"):
            return str(track["asin"])
        return None

    @staticmethod
    def _playlist_track_metadata(track: dict) -> dict:
        if not isinstance(track, dict):
            return {}
        meta = track.get("metadata")
        return meta if isinstance(meta, dict) else track

    def get_album_info(self, album_id: str, media_region: typing.Optional[AmazonRegion] = None, data: dict = {}) -> Optional[AlbumInfo]:
        LOGGER.debug("Getting album info")
        if not media_region:
            split_album_id = album_id.split("_", maxsplit=1)
            if len(split_album_id[-1]) == 2:
                media_region = AmazonRegion.get_region_by_country(split_album_id[-1])
                album_id = split_album_id[0]
                
        if not media_region:
            raise TypeError(f"Invalid selected media region for {album_id}")
        
        mobile_session, _ = self.select_session(media_region)

        cached = data.get(album_id) if data and album_id in data else None
        # Text-search hits are not full albums (no tracks / nested artist); always fetch catalog album.
        if cached and isinstance(cached.get("tracks"), list) and cached["tracks"]:
            album_data = dict(cached)
        else:
            album_data = dict(
                mobile_session.get_album_info(
                    album_id,
                    use_alternative_naming=False,
                    region_to_use=media_region,
                )
            )
        # Force use the ASIN the API returns with
        album_id = album_data["asin"]
        
        valid_asins = [album_id]
        
        if asin := album_data.get("requestedAsin"):
            valid_asins.append(asin)
        if asin := album_data.get("globalAsin"):
            valid_asins.append(asin)
        
        valid_asins.extend((track["asin"] for track in album_data.get("tracks", [])))

        artist_name = self._metadata_artist_name(album_data)
        album_title = str(album_data.get("title") or "Unknown")
        cover_url, search_data = self.get_hi_res_cover(
            asins=valid_asins,
            query=f'"{artist_name}" - "{album_title}"',
            search_data=data.get(f"{album_id}_search") if data else None,
            mobile_session=mobile_session,
            media_region=media_region
        )
        if not cover_url:
            # Default 600 x 600 cover art
            cover_url = str(album_data.get("image", ""))

        tracks_cover_art = self.format_cover_url(
            cover_url, self.options.default_cover_options
        ) if cover_url else ""

        # album_data = self.mobile_session.get_album_info(album_id, use_alternative_naming=True)
        # pprint.pprint(mobile_session.get_page(f"album/{album_id}", region_to_use=media_region))

        mapped_tracks = {
            f"{asin}_quality_mapping": self.mpd_to_quality_map(mpd, asin, mobile_session.credentials.tier, media_region, mobile_session.credentials.account_region)
            for asin, mpd in mobile_session.get_tracks_manifest(
                (track["asin"] for track in album_data.get("tracks", [])),
                force_3d=any(
                    not self.settings["force_non_spatial"] or self.settings[key]
                    for key in ("prefer_spatial_mha1", "prefer_spatial_ac4")
                ),
                region_to_use=media_region
            )
            if mpd
        }

        per_track_audio = [
            self._get_usable_audio_track_of_mapped_quailty(
                mapped_audio_tracks, self.options.quality_tier
            )
            for mapped_audio_tracks in mapped_tracks.values()
        ]
        best_audio_track = max(per_track_audio, key=lambda i: i.bitrate, default=None)
        if not best_audio_track:
            raise TypeError("No available tracks.")
        track_quality_labels = {
            self._quality_label_from_mapped(mapped_audio_tracks)
            for mapped_audio_tracks in mapped_tracks.values()
        }
        search_hit = data.get(f"{album_id}_search") if data else None
        catalog_quality = self._album_quality_from_catalog(album_data, search_hit)
        album_quality_summary = catalog_quality or self._best_quality_display(track_quality_labels)
        track_extra_kwargs = (
            {track["asin"]: track for track in album_data.get("tracks", [])}
            | {album_id: album_data}
            | mapped_tracks
            # | {f"{album_id}_page_entity_data": mobile_session.get_page(f"album/{album_id}", count=0, locale="en_US").get("entity", {})}
        )

        if search_data:
            track_extra_kwargs.update({f"{album_id}_search": search_data})
        else:
            self.print(
                (
                    f"Couldn't retrieve the search results for {album_data['title']}; {album_id}."
                    f" Is this ASIN in the same region as the account?"
                )
            )
        explicit = any(
            (track.get("parentalControls") or {}).get("hasExplicitLanguage")
            for track in album_data.get("tracks", [])
        )
        
        quality_tags = {
            "official_quality_name": best_audio_track.official_quality_name,
            "codec_pretty_name": codec_data[best_audio_track.codec].pretty_name,
            "bit_depth": best_audio_track.bit_depth,
            "sample_rate": best_audio_track.sample_rate,
        }

        return AlbumInfo(
            name=self.sanitize_parental_status_name(album_data.get("title", "Unknown")),
            artist=album_data.get("primaryArtistName", ""),
            tracks=[track["asin"] for track in album_data.get("tracks", [])],
            release_year=int(self._get_date_from_metadata(album_data).strftime("%Y")),
            explicit=explicit,
            duration=album_data.get("duration"),
            artist_id=album_data.get("artist", {}).get("asin"),  # optional
            # booklet_url="",  # optional
            cover_url=tracks_cover_art or cover_url,  # optional
            cover_type=ImageFileTypeEnum.jpg,  # optional
            all_track_cover_jpg_url=tracks_cover_art,  # technically optional, but HIGHLY recommended
            animated_cover_url="",  # optional
            description="",  # optional
            quality=album_quality_summary
            or self._format_quality_display(
                quality_tags, str(self.settings.get("quality_format", ""))
            ),
            expected_track_count=int(album_data["trackCount"]) if album_data.get("trackCount") is not None else None,
            track_extra_kwargs={
                "data": track_extra_kwargs,
                "media_region": media_region,
            },  # optional, whatever you want
        )

    def get_playlist_info(
        self, playlist_id: str, media_region: AmazonRegion, data={}
    ) -> (
        PlaylistInfo
    ):  # Mandatory if either ModuleModes.download or ModuleModes.playlist
        mobile_session, _ = self.select_session(media_region)

        cached = data.get(playlist_id) if data and playlist_id in data else None
        if cached and self._playlist_payload_is_complete(cached):
            p_data = dict(cached)
        else:
            p_data = {}

        if not p_data:
            if len(playlist_id) == 10:
                # An ASIN (catalog playlist, e.g. Top 100)
                catalog = mobile_session.get_catalog_playlist(playlist_id, region_to_use=media_region)
                p_data = dict(catalog.get("playlist") or catalog)
            else:
                # A user playlist (either from the shared link, or the address bar)
                user_resp = mobile_session.get_user_playlist(playlist_id)
                playlists = user_resp.get("playlists") or []
                if not playlists:
                    raise TypeError(f"Playlist not found: {playlist_id}")
                p_data = dict(playlists[0])

        meta = p_data.get("metadata") if isinstance(p_data.get("metadata"), dict) else {}
        if not meta.get("title"):
            meta = {
                "title": p_data.get("title") or p_data.get("name") or "Unknown Playlist",
                "curatedBy": p_data.get("curatedBy") or p_data.get("artistName") or "",
                "durationSeconds": p_data.get("duration") or p_data.get("durationSeconds") or 0,
                "parentalControls": p_data.get("parentalControls") or {},
                "profileId": p_data.get("profileId"),
                "fourSquareArt": p_data.get("fourSquareArt") or p_data.get("artOriginal") or {},
                "description": p_data.get("description"),
            }
        cover_art = meta.get("fourSquareArt") or {}
        cover_url = ""
        if isinstance(cover_art, dict):
            cover_url = str(cover_art.get("url") or "")
        if not cover_url:
            art = p_data.get("artOriginal") or {}
            if isinstance(art, dict) and art.get("artUrl"):
                cover_url = str(art["artUrl"])

        track_asins = []
        track_data = {}
        for track in p_data.get("tracks", []) or []:
            asin = self._playlist_track_asin(track)
            if not asin:
                continue
            track_asins.append(asin)
            track_data[f"{asin}_playlist"] = self._playlist_track_metadata(track)

        playlist_year = self._entity_release_year(
            p_data, p_data, DownloadTypeEnum.playlist
        )

        return PlaylistInfo(
            name=str(meta.get("title") or "Unknown Playlist"),
            creator=str(meta.get("curatedBy") or ""),
            tracks=track_asins,
            release_year=int(playlist_year) if playlist_year else 0,
            duration=int(meta.get("durationSeconds") or 0),
            explicit=bool(
                (meta.get("parentalControls") or {}).get("hasExplicitLanguage", False)
            ),
            creator_id=meta.get("profileId"),
            cover_url=cover_url or None,
            cover_type=ImageFileTypeEnum.jpg,
            description=meta.get("description"),
            track_extra_kwargs={
                "media_region": media_region,
                "data": track_data,
            },
        )

    def get_artist_info(
        self, artist_id: str, get_credited_albums: bool, media_region: AmazonRegion, data: dict = {}
    ) -> ArtistInfo:  # Mandatory if ModuleModes.download
        mobile_session, _ = self.select_session(media_region)

        # get_credited_albums means stuff like remix compilations the artist was part of
        try:
            artist_data = mobile_session.get_artist_info(artist_id, region_to_use=media_region)
        except Exception as e:
            LOGGER.error(e, exc_info=True)
            raise e
        else:
            artist_id = str(artist_data["asin"])
            artist_name = str(artist_data["name"])

        albums: list[dict[str, typing.Any]] = []
        albums_metadata: list[dict[str, typing.Any]] = []

        self.print(
            f"{module_information.service_name}: Loading albums for {artist_data['name']}, this may take a while.."
        )

        # Candidate albums from the artist catalog search.
        search_albums = [
            album
            for album in mobile_session.search(
                query=f'"{artist_name}"',
                search_types=("catalog_album",),
                # Fetch a large pool so artist expand can show the full discography.
                limit=1000,
                region_to_use=media_region,
            )
            if artist_name in album.get("artistName", "")
        ]

        # Some regions appear to cap a single textsearch window near 100 albums.
        # When we detect that plateau, fan out album queries with simple suffixes
        # and merge newly discovered ASINs.
        seen_album_asins = {
            str(a.get("asin"))
            for a in search_albums
            if isinstance(a, dict) and a.get("asin")
        }
        if 95 <= len(seen_album_asins) <= 130:
            self.print(
                f"{module_information.service_name}: Expanding artist albums beyond initial search window..."
            )
            suffixes = list("abcdefghijklmnopqrstuvwxyz0123456789")
            no_new_streak = 0
            for suffix in suffixes:
                try:
                    supplemental = list(
                        mobile_session.search(
                            query=f'"{artist_name}" {suffix}',
                            search_types=("catalog_album",),
                            limit=1000,
                            region_to_use=media_region,
                        )
                    )
                except Exception as ex:
                    LOGGER.debug(
                        "Artist supplemental album search failed (%s %s): %s",
                        artist_name,
                        suffix,
                        ex,
                    )
                    no_new_streak += 1
                    if no_new_streak >= 8:
                        break
                    continue

                added_now = 0
                for album in supplemental:
                    if not isinstance(album, dict):
                        continue
                    if artist_name not in str(album.get("artistName", "")):
                        continue
                    asin = str(album.get("asin") or "")
                    if not asin or asin in seen_album_asins:
                        continue
                    seen_album_asins.add(asin)
                    search_albums.append(album)
                    added_now += 1

                if added_now == 0:
                    no_new_streak += 1
                else:
                    no_new_streak = 0
                if no_new_streak >= 8:
                    break

        # Textsearch for catalog_album can plateau around 100 in some regions.
        # Add a secondary source: catalog_track hits -> album ASINs.
        track_album_hits: list[dict] = []
        try:
            track_hits = list(
                mobile_session.search(
                    query=f'"{artist_name}"',
                    search_types=("catalog_track",),
                    limit=1000,
                    region_to_use=media_region,
                )
            )
            for hit in track_hits:
                if not isinstance(hit, dict):
                    continue
                hit_artist = str(hit.get("artistName") or "")
                if artist_name not in hit_artist:
                    continue
                meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
                album_asin = (
                    hit.get("albumAsin")
                    or meta.get("albumAsin")
                    or meta.get("albumGlobalAsin")
                )
                if not album_asin:
                    continue
                track_album_hits.append(
                    {
                        "asin": str(album_asin),
                        "artistName": hit_artist or artist_name,
                        "title": str(meta.get("albumTitle") or ""),
                        "artOriginal": (
                            {"artUrl": str((meta.get("albumArt") or {}).get("url") or "")}
                            if isinstance(meta.get("albumArt"), dict)
                            else {}
                        ),
                        "metadata": meta,
                    }
                )
        except Exception as ex:
            LOGGER.debug("Artist track-album harvest failed (%s): %s", artist_id, ex)

        # Some territories/accounts return only ~20 albums from textsearch.
        # Use artist muse pages as an additional source and merge both sets.
        def _extract_album_docs(obj):
            docs = []
            seen_doc_asins: set[str] = set()
            stack = [obj]

            def _maybe_add_doc(candidate: dict, asin_value: str):
                asin_norm = str(asin_value or "").strip().upper()
                if not re.fullmatch(r"[A-Z0-9]{10}", asin_norm):
                    return
                if asin_norm in seen_doc_asins:
                    return
                seen_doc_asins.add(asin_norm)
                materialized = dict(candidate)
                materialized["asin"] = asin_norm
                docs.append(materialized)

            def _extract_asin_from_string(text: str) -> typing.Optional[str]:
                s = str(text or "")
                patterns = (
                    r"(?:/albums?/|album/|asin=)([A-Z0-9]{10})",
                    r"\b([A-Z0-9]{10})\b",
                )
                for pat in patterns:
                    m = re.search(pat, s, re.IGNORECASE)
                    if m:
                        return str(m.group(1)).upper()
                return None

            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    direct_asins = []
                    for key, value in current.items():
                        lk = str(key).lower()
                        if "asin" in lk:
                            if isinstance(value, str):
                                direct_asins.append(value)
                            elif isinstance(value, (list, tuple)):
                                for item in value:
                                    if isinstance(item, str):
                                        direct_asins.append(item)
                    for asin_value in direct_asins:
                        _maybe_add_doc(current, str(asin_value))

                    # Artist pages frequently store album IDs in links/URIs/actions.
                    for value in current.values():
                        if isinstance(value, str):
                            asin_from_link = _extract_asin_from_string(value)
                            if asin_from_link:
                                _maybe_add_doc(current, asin_from_link)
                        elif isinstance(value, (dict, list, tuple)):
                            stack.append(value)

                elif isinstance(current, (list, tuple)):
                    for item in current:
                        if isinstance(item, (dict, list, tuple)):
                            stack.append(item)
                        elif isinstance(item, str):
                            asin_from_link = _extract_asin_from_string(item)
                            if asin_from_link:
                                docs.append({"asin": asin_from_link})

            # Deduplicate fallback dict-only rows generated from string-only matches.
            deduped = {}
            for d in docs:
                asin = str(d.get("asin") or "").upper()
                if asin and asin not in deduped:
                    deduped[asin] = d
            return list(deduped.values())

        page_albums = []
        seen_page_asins: set[str] = set()
        def _extract_next_token(obj):
            if not isinstance(obj, (dict, list, tuple)):
                return None
            stack = [obj]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    for key, value in cur.items():
                        lk = str(key).lower()
                        if "token" in lk and "next" in lk and value:
                            return str(value)
                        if isinstance(value, (dict, list, tuple)):
                            stack.append(value)
                elif isinstance(cur, (list, tuple)):
                    for item in cur:
                        if isinstance(item, (dict, list, tuple)):
                            stack.append(item)
            return None

        next_token = None
        page_count = 200
        offset = 0
        for _ in range(40):  # hard stop guard
            try:
                artist_page = mobile_session.get_page(
                    f"artist/{artist_id}",
                    count=page_count,
                    next_token=next_token,
                    offset=offset if not next_token else None,
                    region_to_use=media_region,
                )
            except Exception as ex:
                LOGGER.debug("Artist page fetch failed (%s): %s", artist_id, ex)
                break

            docs = _extract_album_docs(artist_page)
            added = 0
            for doc in docs:
                asin = str(doc.get("asin") or "")
                if not asin or asin in seen_page_asins:
                    continue
                seen_page_asins.add(asin)
                page_albums.append(doc)
                added += 1

            if getattr(self.options, "debug_mode", False):
                self.print(
                    f"{module_information.service_name}: artist page debug -> "
                    f"added_per_page={added}, offset={offset}, next_token={'yes' if next_token else 'no'}"
                )

            extracted_token = _extract_next_token(artist_page)
            if extracted_token:
                next_token = extracted_token
                continue

            # Fallback for regions where artist pages paginate by offset without exposing next tokens.
            next_token = None
            if added == 0:
                break
            offset += page_count

        if page_albums:
            seen_search_asins = {str(a.get("asin")) for a in search_albums if isinstance(a, dict) and a.get("asin")}
            for album in page_albums:
                asin = str(album.get("asin") or "")
                if asin and asin not in seen_search_asins:
                    search_albums.append(album)
                    seen_search_asins.add(asin)

        if track_album_hits:
            seen_search_asins = {str(a.get("asin")) for a in search_albums if isinstance(a, dict) and a.get("asin")}
            for album in track_album_hits:
                asin = str(album.get("asin") or "")
                if asin and asin not in seen_search_asins:
                    search_albums.append(album)
                    seen_search_asins.add(asin)

        if getattr(self.options, "debug_mode", False):
            self.print(
                f"{module_information.service_name}: artist merge debug -> "
                f"search_albums={len(search_by_asin) if 'search_by_asin' in locals() else len(search_albums)}, "
                f"page_albums={len(page_albums)}, "
                f"track_album_hits={len(track_album_hits)}, merged_candidates={len(search_albums)}"
            )

        # Map requested ASIN -> search hit so we can pair batched metadata back to it.
        search_by_asin: dict[str, dict] = {}
        for album in search_albums:
            asin = album.get("asin")
            if asin:
                search_by_asin.setdefault(str(asin), album)

        def _match_search_hit(album_metadata: dict) -> typing.Optional[dict]:
            for key in ("requestedAsin", "asin", "globalAsin"):
                asin = album_metadata.get(key)
                if asin and str(asin) in search_by_asin:
                    return search_by_asin[str(asin)]
            return None

        # The muse lookup endpoint accepts up to 10 ASINs per request, so batch
        # metadata fetches (94 single calls -> ~10 batched calls) and run them
        # concurrently. Full album details (tracks, year, quality flags) are
        # preserved because each entry is the same payload as get_album_info.
        all_asins = list(search_by_asin.keys())
        asin_batches = [all_asins[i : i + 10] for i in range(0, len(all_asins), 10)]

        def _fetch_batch(asin_batch: list[str]) -> list[dict]:
            try:
                resp = mobile_session.get_metadata(
                    tuple(asin_batch), region_to_use=media_region
                )
                return list(resp.get("albumList") or [])
            except Exception as ex:
                LOGGER.debug("Album metadata batch failed (%s): %s", asin_batch, ex)
                return []

        fetched_metadata: list[dict] = []
        matched_asins: set[str] = set()
        if asin_batches:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(asin_batches))
            ) as executor:
                batch_futures = [
                    executor.submit(_fetch_batch, batch) for batch in asin_batches
                ]
                executor.shutdown(wait=True)
                for future in concurrent.futures.as_completed(batch_futures):
                    for album_metadata in future.result():
                        if not album_metadata:
                            continue
                        fetched_metadata.append(album_metadata)
                        hit = _match_search_hit(album_metadata)
                        if hit is not None and hit.get("asin"):
                            matched_asins.add(str(hit["asin"]))

        # Fallback: fetch any ASINs the batched lookup silently dropped.
        missing_asins = [a for a in all_asins if a not in matched_asins]
        if missing_asins:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(missing_asins))
            ) as executor:
                single_futures = {
                    executor.submit(
                        mobile_session.get_album_info,
                        asin,
                        region_to_use=media_region,
                    ): asin
                    for asin in missing_asins
                }
                executor.shutdown(wait=True)
                for future in concurrent.futures.as_completed(single_futures):
                    try:
                        album_metadata = future.result()
                    except Exception as ex:
                        LOGGER.debug(
                            "Album metadata fallback failed (%s): %s",
                            single_futures[future],
                            ex,
                        )
                        continue
                    if album_metadata:
                        fetched_metadata.append(album_metadata)

        def _norm_text(value: typing.Any) -> str:
            return re.sub(r"\s+", " ", str(value or "").strip().lower())

        def _is_various_artists(name: str) -> bool:
            n = _norm_text(name)
            return n in {"various artists", "various artist", "various"}

        artist_name_norm = _norm_text(artist_name)
        artist_id_str = str(artist_id)

        added_asins: set[str] = set()
        for album_metadata in fetched_metadata:
            if not album_metadata:
                continue
            album = _match_search_hit(album_metadata)
            if album is None:
                continue
            album_asin = str(album.get("asin") or "")
            if album_asin in added_asins:
                continue

            artist_meta = album_metadata.get("artist") if isinstance(album_metadata.get("artist"), dict) else {}
            album_artist_name = (
                album_metadata.get("primaryArtistName")
                or artist_meta.get("name")
                or album.get("artistName")
                or ""
            )
            album_artist_norm = _norm_text(album_artist_name)
            title_norm = _norm_text(album_metadata.get("title") or album.get("title"))
            primary_artist_match = str(artist_meta.get("asin") or "") == artist_id_str
            contributor_asins = {
                str(v)
                for v in (artist_meta.get("contributorAsins") or [])
                if v is not None
            }
            contributor_match = artist_id_str in contributor_asins
            exact_artist_name_match = bool(
                artist_name_norm and album_artist_norm == artist_name_norm
            )

            # Drop noisy compilation rows unless they are truly primary for this artist.
            if _is_various_artists(album_artist_name) and not primary_artist_match:
                continue

            # Keep only strong ownership links to avoid false positives for name collisions
            # (e.g. "The Blessed Madonna", "Luigi Madonna").
            if not primary_artist_match and not contributor_match and not exact_artist_name_match:
                continue

            added_asins.add(album_asin)
            albums_metadata.append(album_metadata)
            albums.append(album)

        if not albums:
            self.print(f"No albums found for {artist_name} with ID: {artist_id}.")
            return ArtistInfo(artist_name)

        return ArtistInfo(
            name=artist_name,
            albums=[album["asin"] for album in albums],
            album_extra_kwargs={
                "media_region": media_region,
                "data": {
                    f"{album_data['asin']}_search": album_data for album_data in albums
                }
                | {
                    str(album_meta["asin"]): album_meta
                    for album_meta in albums_metadata
                }
            },
            # tracks=[],
            # track_extra_kwargs={"data": ""},  # optional, whatever you want
        )

    def get_track_credits(
        self, track_id: str, media_region: AmazonRegion, data={}, **kwargs
    ):  # Mandatory if ModuleModes.credits
        mobile_session, _ = self.select_session(media_region)
        track_credits = mobile_session.get_track_xray(track_id, region_to_use=media_region, parse_credits=True)
        return [
            CreditsInfo(
                sanitise_name(k),
                list(
                    itertools.chain.from_iterable(
                        [self.parse_credit_names_from_name(name) for name in v]
                    )
                ),
            )
            for k, v in track_credits.items()
        ]

    def get_track_cover(
        self, track_id: str, cover_options: CoverOptions, media_region: AmazonRegion, data={}, **kwargs
    ) -> CoverInfo:  # Mandatory if ModuleModes.covers
        mobile_session, _ = self.select_session(media_region)
        
        track_data = (
            data[track_id]
            if track_id in data
            else mobile_session.get_track_info(track_id, region_to_use=media_region) #music_territory="CY"
        )
        album_id = str(track_data["album"]["asin"])
        album_data = (
            data[track_id]
            if track_id in data
            else mobile_session.get_album_info(album_id, region_to_use=media_region) #music_territory="CY"
        )

        valid_album_asins = [album_id]
        
        if asin := album_data.get("requestedAsin"):
            valid_album_asins.append(asin)
        if asin := album_data.get("globalAsin"):
            valid_album_asins.append(asin)

        cover_url, _ = self.get_hi_res_cover(
            asins=valid_album_asins,
            query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
            search_data=data.get(f"{album_id}_search"),
            mobile_session=mobile_session,
            media_region=media_region
        )
        if not cover_url:
            # Default 600 x 600 cover art
            cover_url = str(album_data.get("image", ""))
        else:
            cover_url = self.format_cover_url(
                cover_url, cover_options
            )

        return CoverInfo(
            url=cover_url,
            file_type=cover_options.file_type,
        )

    def get_track_lyrics(
        self, track_id: str, media_region: AmazonRegion, data={}, **kwargs
    ) -> LyricsInfo:  # Mandatory if ModuleModes.lyrics
        mobile_session, _ = self.select_session(media_region)
        track_lyrics_resp = mobile_session.get_track_lyrics(track_id, region_to_use=media_region) # music_territory="CY"

        embedded_lyrics = ""
        synced_lyrics = ""
        lyrics_payload = track_lyrics_resp.get("lyrics", {}) if isinstance(track_lyrics_resp, dict) else {}
        lines = lyrics_payload.get("lines", [])

        if isinstance(lines, dict):
            lines = list(lines.values())

        parsed_lines = []
        for line in lines if isinstance(lines, list) else []:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            start_time_raw = line.get("startTime")
            if start_time_raw is None:
                parsed_lines.append((None, text))
                continue
            try:
                start_time = int(start_time_raw)
            except (TypeError, ValueError):
                parsed_lines.append((None, text))
                continue
            parsed_lines.append((start_time, text))

        if parsed_lines:
            for start_time, text in parsed_lines:
                if start_time is None:
                    embedded_lyrics += f"{text}\n"
                    continue
                start_time_str = self.milliseconds_to_lrc_time(start_time)
                embedded_lyrics += f"[{start_time_str}] {text}\n"
                synced_lyrics += f"[{start_time_str}]{text}\n"
        else:
            # Some regions/tracks can return unsynced-only payloads without `lines`.
            plain_text = (
                lyrics_payload.get("text")
                or lyrics_payload.get("plainText")
                or lyrics_payload.get("displayText")
                or track_lyrics_resp.get("lyricsText")
            ) if isinstance(track_lyrics_resp, dict) else None
            if plain_text:
                embedded_lyrics = str(plain_text).strip()

        return LyricsInfo(
            embedded=embedded_lyrics, synced=synced_lyrics
        )  # both optional if not found

    def _search_additional_quality_labels(
        self,
        mobile_session: AmazonMusicMobileAPI,
        catalog: dict,
        media_region: AmazonRegion,
        query_type: DownloadTypeEnum,
        max_track_probe: int = SEARCH_QUALITY_MAX_TRACK_PROBE,
    ) -> typing.Optional[list[str]]:
        """
        Quality from track MPD manifests (same source as expand/download).
        Search/artist rows sample a few tracks per album for speed; expand still uses all tracks.
        Returns None to fall back to contentEncoding flags when the probe fails.
        """
        track_asins: list[str] = []
        if query_type == DownloadTypeEnum.album:
            for track in catalog.get("tracks") or []:
                if isinstance(track, dict) and track.get("asin"):
                    track_asins.append(str(track["asin"]))
        elif query_type == DownloadTypeEnum.playlist:
            for track in catalog.get("tracks") or []:
                asin = self._playlist_track_asin(track)
                if asin:
                    track_asins.append(str(asin))
        if not track_asins:
            return None
        # Search rows only sample a few tracks; expand/download still inspects every track.
        probe_asins = self._sample_track_asins_for_quality_probe(
            track_asins, max_probe=max_track_probe
        )
        try:
            return self._album_search_quality_labels(
                mobile_session,
                probe_asins,
                media_region,
            )
        except Exception as ex:
            LOGGER.debug("Search manifest quality probe failed: %s", ex)
            return None

    def _stereo_only_quality_labels(self, labels: typing.Iterable[str]) -> set[str]:
        return {
            str(label).strip()
            for label in labels
            if label
            and str(label).strip()
            and not self._is_spatial_quality_display(str(label), str(label))
        }

    def _album_search_quality_labels(
        self,
        mobile_session: AmazonMusicMobileAPI,
        track_asins: tuple[str, ...],
        media_region: AmazonRegion,
    ) -> typing.Optional[list[str]]:
        """
        Same manifest + per-track label pass as get_album_info / expand (force_3d from settings).
        Optional second pass with force_3d=False when Amazon omits UHD on the 3D substitution ASIN.
        """
        if not track_asins:
            return None

        spatial_labels: set[str] = set()
        per_track_labels: set[str] = set()
        collected_stereo: set[str] = set()
        has_ra360_manifest: bool = False
        force_3d = self._amazon_force_3d_for_manifest()

        def _ingest_manifests(force_3d_flag: bool) -> None:
            nonlocal has_ra360_manifest
            for track_asin, manifest in mobile_session.get_tracks_manifest(
                track_asins,
                force_3d=force_3d_flag,
                region_to_use=media_region,
            ):
                if manifest is None:
                    continue
                try:
                    mapped = self.mpd_to_quality_map(
                        manifest,
                        track_asin,
                        mobile_session.credentials.tier,
                        media_region,
                        mobile_session.credentials.account_region,
                    )
                    if self._mapped_has_spatial_prefix(mapped, "SPATIAL_RA360"):
                        has_ra360_manifest = True
                    spatial, stereo = self._collect_manifest_quality_labels(mapped)
                    spatial_labels |= spatial
                    collected_stereo |= stereo
                    label = self._quality_label_from_mapped(
                        mapped, SEARCH_PROBE_STEREO_TIER
                    )
                    if label:
                        per_track_labels.add(label)
                except Exception as ex:
                    LOGGER.debug(
                        "Album search manifest ingest failed for %s: %s",
                        track_asin,
                        ex,
                    )

        _ingest_manifests(force_3d)

        stereo_candidates = self._stereo_only_quality_labels(
            per_track_labels | collected_stereo
        )
        if not self._manifest_labels_include_stereo(stereo_candidates):
            _ingest_manifests(False)
            stereo_candidates = self._stereo_only_quality_labels(
                per_track_labels | collected_stereo
            )

        if not spatial_labels and track_asins:
            spatial_only, _ = self._probe_manifest_quality_label_sets(
                mobile_session,
                (track_asins[0],),
                media_region,
                force_3d=True,
            )
            spatial_labels |= spatial_only

        merged = self._merge_manifest_quality_labels(spatial_labels, stereo_candidates)
        if merged and "3D MPEG-H Audio" in merged and not has_ra360_manifest:
            merged = [label for label in merged if label != "3D MPEG-H Audio"]
        return merged or None

    def artist_album_display_meta(
        self,
        album_catalog: dict,
        media_region: AmazonRegion,
        search_hit: typing.Optional[dict] = None,
    ) -> dict[str, typing.Any]:
        """Year, track count, and best quality for artist discography rows (matches album search).

        Quality is read from local catalog contentEncoding flags only (no per-album
        manifest probe), so expanding a large discography stays fast. The exact tier
        is still refined from the MPD manifest on expand/download.
        """
        year = ""
        try:
            if album_catalog.get("originalReleaseDate") or album_catalog.get("merchantReleaseDate"):
                year = self._get_date_from_metadata(
                    {
                        "originalReleaseDate": album_catalog.get("originalReleaseDate"),
                        "merchantReleaseDate": album_catalog.get("merchantReleaseDate"),
                    }
                ).strftime("%Y")
        except (OSError, OverflowError, ValueError, TypeError):
            pass

        additional_parts: list[str] = []
        track_count = album_catalog.get("trackCount")
        if track_count is None and isinstance(album_catalog.get("tracks"), list):
            track_count = len(album_catalog["tracks"])
        if track_count is not None:
            try:
                tc = int(track_count)
                if tc > 0:
                    additional_parts.append("1 track" if tc == 1 else f"{tc} tracks")
            except (TypeError, ValueError):
                pass

        # Gather catalog quality flags from the full metadata (and its tracks) plus the
        # original search hit, which reliably carries contentEncoding flags.
        catalog_tags = self._catalog_quality_tags_from_entity_and_tracks(album_catalog)
        if isinstance(search_hit, dict):
            for tag in self._catalog_quality_tags_from_entity_and_tracks(search_hit):
                if tag not in catalog_tags:
                    catalog_tags.append(tag)
        if catalog_tags:
            quality_labels = self._amazon_catalog_quality_display_labels(catalog_tags)
            if quality_labels:
                additional_parts.extend(quality_labels)

        title = str(album_catalog.get("title") or "")
        explicit = any(
            (track.get("parentalControls") or {}).get("hasExplicitLanguage")
            for track in (album_catalog.get("tracks") or [])
            if isinstance(track, dict)
        ) or "[explicit]" in title.lower()

        return {
            "year": year,
            "additional": " / ".join(additional_parts),
            "explicit": explicit,
        }

    def search(
        self,
        query_type: DownloadTypeEnum,
        query: str,
        track_info: typing.Optional[TrackInfo] = None,
        limit: int = 10,
    ):  # Mandatory
        preferred_region = (
            self.select_usable_region_from_regions(
                AmazonRegion.get_available_regions_by_continent(
                    self.settings["prefer_account_continent"]
                )
            )
            or AmazonRegion.get_available_regions_by_continent(
                self.settings["prefer_account_continent"]
            )[0]
        )
        mobile_session, media_region = self.select_session(preferred_region)
        self.print(f"{module_information.service_name}: Using {media_region.country} for search query ({query})")

        results = []
        search_type = f"catalog_{query_type.name}"
        # if track_info and track_info.tags.isrc:
        #     results = list(
        #         mobile_session.search(
        #             query=track_info.tags.isrc, search_types=(search_type,), limit=limit
        #         )
        #     )
        if not results:
            results = list(
                mobile_session.search(
                    query=query,
                    search_types=(search_type,),
                    limit=limit,
                    region_to_use=media_region
                )
            )

        def _cover_from_item(item: dict) -> typing.Optional[str]:
            art = item.get("artOriginal") or {}
            url = art.get("artUrl") if isinstance(art, dict) else None
            if url:
                return str(url)
            album_art = (item.get("metadata") or {}).get("albumArt") or {}
            if isinstance(album_art, dict) and album_art.get("url"):
                return str(album_art["url"])
            return None

        def _track_album_asin(item: dict) -> typing.Optional[str]:
            """Best-effort album ASIN for a track search hit (used to backfill year)."""
            sources = [item]
            meta = item.get("metadata")
            if isinstance(meta, dict):
                sources.append(meta)
            for src in sources:
                if not isinstance(src, dict):
                    continue
                for key in ("albumAsin", "albumGlobalAsin"):
                    val = src.get(key)
                    if val:
                        return str(val)
                album = src.get("album")
                if isinstance(album, dict) and album.get("asin"):
                    return str(album["asin"])
            return None

        def _fetch_catalog_entity_for_search(item: dict) -> typing.Optional[dict]:
            """Full album/playlist metadata (text search hits omit year, duration, quality)."""
            asin = item.get("asin") or item.get("seriesAsin")
            if not asin or query_type not in (DownloadTypeEnum.album, DownloadTypeEnum.playlist):
                return None
            try:
                if query_type == DownloadTypeEnum.playlist:
                    if len(str(asin)) == 10:
                        resp = mobile_session.get_catalog_playlist(
                            str(asin), region_to_use=media_region
                        )
                        return dict(resp.get("playlist") or resp)
                    user_resp = mobile_session.get_user_playlist(str(asin))
                    playlists = user_resp.get("playlists") or []
                    if playlists:
                        return dict(playlists[0])
                elif query_type == DownloadTypeEnum.album:
                    return dict(
                        mobile_session.get_album_info(str(asin), region_to_use=media_region)
                    )
            except Exception as ex:
                LOGGER.debug("Catalog metadata fetch failed for %s: %s", asin, ex)
            return None

        def _search_release_year(
            item: dict, catalog: typing.Optional[dict] = None
        ) -> typing.Optional[str]:
            return self._entity_release_year(item, catalog, query_type)

        def _search_duration_seconds(
            item: dict, catalog: typing.Optional[dict] = None
        ) -> typing.Optional[int]:
            for src in (catalog, item):
                if not isinstance(src, dict):
                    continue
                raw = src.get("duration")
                if raw is None and isinstance(src.get("metadata"), dict):
                    raw = src["metadata"].get("durationSeconds")
                if raw is None:
                    continue
                try:
                    sec = int(raw)
                    if sec > 86400:
                        sec = sec // 1000
                    return sec if sec >= 0 else None
                except (TypeError, ValueError):
                    continue
            return None

        def _quality_for_search_item(item: dict, catalog: typing.Optional[dict] = None) -> list[str]:
            catalog_tags: list[str] = []
            if isinstance(item, dict):
                catalog_tags.extend(
                    self._catalog_quality_tags_from_entity_and_tracks(item)
                )
            if isinstance(catalog, dict):
                for tag in self._catalog_quality_tags_from_entity_and_tracks(catalog):
                    if tag not in catalog_tags:
                        catalog_tags.append(tag)

            if catalog and query_type in (
                DownloadTypeEnum.album,
                DownloadTypeEnum.playlist,
            ):
                # Prefer catalog HD/UHD/Atmos flags (no MPD round-trips) when Amazon already sent them.
                if catalog_tags:
                    catalog_labels = self._amazon_catalog_quality_display_labels(
                        catalog_tags
                    )
                    if catalog_labels:
                        if not self._quality_labels_include_atmos(catalog_labels):
                            atmos = self._catalog_atmos_display_label(
                                catalog
                            ) or self._catalog_atmos_display_label(item)
                            if atmos:
                                return [atmos]
                        return self._supplement_stereo_quality_from_catalog(
                            catalog_labels, item, catalog
                        )

                manifest_labels = self._search_additional_quality_labels(
                    mobile_session, catalog, media_region, query_type
                )
                if manifest_labels is not None:
                    if not self._quality_labels_include_atmos(manifest_labels):
                        atmos = self._catalog_atmos_display_label(catalog) or self._catalog_atmos_display_label(item)
                        if atmos:
                            return [atmos]
                    return self._supplement_stereo_quality_from_catalog(
                        manifest_labels, item, catalog
                    )
            if query_type == DownloadTypeEnum.track:
                track_asin = item.get("asin")
                if track_asin:
                    probed = self._amazon_search_track_quality_labels(
                        mobile_session, str(track_asin), media_region
                    )
                    if probed:
                        if not self._quality_labels_include_atmos(probed):
                            atmos = self._catalog_atmos_display_label(item) or (
                                self._catalog_atmos_display_label(catalog) if catalog else None
                            )
                            if atmos:
                                return [atmos]
                        return self._supplement_stereo_quality_from_catalog(
                            probed, item, catalog
                        )
            if catalog_tags:
                return self._amazon_catalog_quality_display_labels(catalog_tags)
            return []

        # Filled in below for track searches whose lightweight hits omit a release
        # date; maps album ASIN -> year via a few batched album-metadata lookups.
        album_year_backfill: dict[str, str] = {}

        def _resolve_year(item: dict, catalog: typing.Optional[dict]) -> typing.Optional[str]:
            if query_type not in (
                DownloadTypeEnum.track,
                DownloadTypeEnum.album,
                DownloadTypeEnum.playlist,
            ):
                return None
            year = _search_release_year(item, catalog)
            if year:
                return year
            if query_type == DownloadTypeEnum.track:
                album_asin = _track_album_asin(item)
                if album_asin:
                    return album_year_backfill.get(album_asin)
            return None

        def _build_search_result(item: dict) -> SearchResult:
            asin = item.get("asin") or item.get("seriesAsin")
            catalog = _fetch_catalog_entity_for_search(item) if asin else None
            data_payload = {f"{asin}_search": item} if asin else {}
            if catalog and asin:
                data_payload[str(asin)] = catalog
            if query_type == DownloadTypeEnum.playlist:
                artists: typing.Optional[list[str]] = ["Amazon Music"]
            else:
                artists = [item["artistName"]] if item.get("artistName") else None
            return SearchResult(
                result_id=asin,
                name=item.get("title") or item.get("name"),
                artists=artists,
                year=_resolve_year(item, catalog),
                duration=_search_duration_seconds(item, catalog),
                image_url=_cover_from_item(item),
                explicit=item["parentalControls"]["hasExplicitLanguage"]
                if item.get("parentalControls")
                else None,
                additional=_quality_for_search_item(item, catalog),
                extra_kwargs={
                    "media_region": media_region,
                    "data": data_payload,
                },
            )

        if query_type == DownloadTypeEnum.track and results:
            # Track search hits frequently omit a release date. Backfill the year
            # from the owning album's metadata using batched lookups (up to 10
            # ASINs per request) instead of one slow call per track.
            needed_album_asins: list[str] = []
            seen_album_asins: set[str] = set()
            for item in results:
                if not isinstance(item, dict) or _search_release_year(item, None):
                    continue
                album_asin = _track_album_asin(item)
                if album_asin and album_asin not in seen_album_asins:
                    seen_album_asins.add(album_asin)
                    needed_album_asins.append(album_asin)

            if needed_album_asins:
                year_batches = [
                    needed_album_asins[i : i + 10]
                    for i in range(0, len(needed_album_asins), 10)
                ]

                def _fetch_year_batch(batch: list[str]) -> list[dict]:
                    try:
                        resp = mobile_session.get_metadata(
                            tuple(batch), region_to_use=media_region
                        )
                        return list(resp.get("albumList") or [])
                    except Exception as ex:
                        LOGGER.debug(
                            "Track-year album metadata batch failed (%s): %s", batch, ex
                        )
                        return []

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, len(year_batches))
                ) as executor:
                    year_futures = [
                        executor.submit(_fetch_year_batch, batch)
                        for batch in year_batches
                    ]
                    executor.shutdown(wait=True)
                    for future in concurrent.futures.as_completed(year_futures):
                        for album_meta in future.result():
                            if not isinstance(album_meta, dict):
                                continue
                            year = self._year_from_date_fields(
                                album_meta, self._ALBUM_YEAR_KEYS
                            )
                            if not year:
                                continue
                            for key in ("requestedAsin", "asin", "globalAsin"):
                                asin_val = album_meta.get(key)
                                if asin_val:
                                    album_year_backfill[str(asin_val)] = year

        if query_type in (DownloadTypeEnum.album, DownloadTypeEnum.playlist) and len(results) > 1:
            # Album rows: sequential catalog fetch + optional 3-track MPD sample (session not thread-safe).
            if query_type == DownloadTypeEnum.album:
                return [_build_search_result(item) for item in results]
            search_output: list[SearchResult] = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, len(results))
            ) as executor:
                for result in executor.map(_build_search_result, results):
                    search_output.append(result)
            return search_output

        return [_build_search_result(item) for item in results]

    # helpers

    def get_hi_res_cover(
        self,
        asins: typing.Iterable[str],
        query: str,
        mobile_session: AmazonMusicMobileAPI,
        media_region: AmazonRegion,
        search_data: typing.Optional[dict] = {},
    ):
        search_data = {}
        cover_url = None

        # Typically this works
        if not search_data:
            search_data = mobile_session.search(
                query=query,
                asins=tuple(asins),
                search_types=("catalog_album", "catalog_track"),
                limit=100,
                region_to_use=media_region
            )

        cover_url = str(
            search_data.get("artOriginal", {}).get(
                "artUrl",
                search_data.get("metadata", {}).get("albumArt", {}).get("url", ""),
            )
        )

        if not cover_url:
            # These types of album/tracks are unique as they only appear in Playlists (curated by Amazon Music)
            # e.g tracks with the label "Amazon Content Service"
            # We have to trust that Amazon Search API returns the correct playlist that has the album/track inside of it
            for p_search_data in mobile_session.search(
                # query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
                query=query,
                search_types=("catalog_playlist",),
                limit=100,
                region_to_use=media_region
            ):
                p_data = mobile_session.get_catalog_playlist(
                    p_search_data["seriesAsin"], region_to_use=media_region
                )
                for track in p_data.get("playlist", {}).get("tracks", []):
                    if track.get("metadata", {}).get("albumAsin") not in asins:
                        continue
                    search_data = track
                    cover_url = str(
                        track.get("metadata", {}).get("albumArt", {}).get("url", "")
                    )
                    break
                else:
                    continue
                break

        return cover_url, search_data

    _ALBUM_YEAR_KEYS = (
        "originalReleaseDate",
        "merchantReleaseDate",
        "releaseDate",
        "albumReleaseDate",
    )
    _PLAYLIST_YEAR_KEYS = (
        "lastUpdatedDate",
        "creationDate",
        "lastModifiedDate",
        "publishedDate",
        "publishDate",
        "updatedDate",
        "createdDate",
        "uploadDate",
        "playlistReleaseDate",
        "releaseYear",
        "releaseDate",
        "originalReleaseDate",
        "merchantReleaseDate",
    )

    @classmethod
    def _parse_amazon_year(cls, raw: typing.Any) -> typing.Optional[str]:
        """Parse Amazon ms/second timestamps, year ints, or date strings to YYYY."""
        if raw is None:
            return None
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return None
            if s.isdigit():
                raw = int(s)
            elif len(s) >= 4 and s[:4].isdigit():
                return s[:4]
            else:
                match = re.search(r"\b(19|20)\d{2}\b", s)
                return match.group(0) if match else None
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None
        if 1000 <= val <= 9999:
            return str(val)
        try:
            ms = val if val > 1_000_000_000_000 else val * 1000
            return cls._get_date_from_metadata({"originalReleaseDate": ms}).strftime("%Y")
        except (OSError, OverflowError, ValueError, TypeError):
            return None

    @classmethod
    def _year_from_date_fields(
        cls, entity: typing.Optional[dict], date_keys: tuple[str, ...]
    ) -> typing.Optional[str]:
        if not isinstance(entity, dict):
            return None
        for key in date_keys:
            year = cls._parse_amazon_year(entity.get(key))
            if year:
                return year
        meta = entity.get("metadata")
        if isinstance(meta, dict) and meta is not entity:
            return cls._year_from_date_fields(meta, date_keys)
        return None

    @classmethod
    def _playlist_year_from_track_metadata(
        cls, catalog: dict, max_tracks: int = 25
    ) -> typing.Optional[str]:
        """User/catalog playlists: lastUpdatedDate or creationDate on track rows."""
        years: list[int] = []
        for track in (catalog.get("tracks") or [])[:max_tracks]:
            if not isinstance(track, dict):
                continue
            meta = track.get("metadata")
            if not isinstance(meta, dict):
                meta = track
            for key in ("lastUpdatedDate", "creationDate"):
                year = cls._parse_amazon_year(meta.get(key))
                if year:
                    try:
                        years.append(int(year))
                    except ValueError:
                        pass
        return str(max(years)) if years else None

    def _entity_release_year(
        self,
        item: dict,
        catalog: typing.Optional[dict],
        query_type: DownloadTypeEnum,
    ) -> typing.Optional[str]:
        keys = (
            self._PLAYLIST_YEAR_KEYS
            if query_type == DownloadTypeEnum.playlist
            else self._ALBUM_YEAR_KEYS
        )
        for src in (catalog, item):
            year = self._year_from_date_fields(src, keys)
            if year:
                return year
        if query_type == DownloadTypeEnum.playlist and catalog:
            return self._playlist_year_from_track_metadata(catalog)
        return None

    @staticmethod
    def _get_date_from_metadata(album_data: dict[str, typing.Any]):
        proper_ts = (
            int(
                str(
                    album_data.get("originalReleaseDate")
                    or album_data.get("merchantReleaseDate")
                )
            )
            / 1000
        )
        if os.name == "nt" and proper_ts < 0:
            # Windows doesn't fully support any dates
            # before 1970 (negative dates), so this is a workaround.
            return datetime(1970, 1, 1) - timedelta(seconds=abs(proper_ts))

        return datetime.fromtimestamp(proper_ts)

    @staticmethod
    def milliseconds_to_lrc_time(milliseconds: int):
        # Convert milliseconds to the proper LRC time format [mm:ss.xxx]
        return f"{milliseconds // 60000:02}:{(milliseconds // 1000) % 60:02}.{milliseconds % 1000:03}"

    @staticmethod
    def sanitize_parental_status_name(name: str):
        sanitized = re.split(r"(\s\[Explicit\]|\s\[Clean\])$", name, maxsplit=1)
        if len(sanitized) >= 2:
            return sanitized[0]
        # No matches found
        return name

    @staticmethod
    def parse_credit_names_from_name(name: str):
        return list(
            {str(item) for item in re.split(r" & |, | - | / | feat. ", name) if item}
        )

    @staticmethod
    def parse_genres_from_genre(name: str):
        return {
            str(item).title()
            for item in re.split(r", | & | - | / |/", name)
            if item != "General"
        }

    @staticmethod
    def format_cover_url(url: str, options: CoverOptions):
        frag_url, extension = url.rsplit(".", 1)
        file_format = (
            f"_FM{options.file_type.name}"
            if options.file_type != ImageFileTypeEnum.jpg
            else ""
        )

        new_url = f"{frag_url}.SX{options.resolution}{file_format}.{extension}"

        return new_url

    def download(self, url: str, location: str, use_aria2c: typing.Optional[bool]):
        # OrpheusDL default downloader only (aria2c/aria2p not supported in this distribution)
        indent = getattr(self.module_controller.printer_controller, "indent_number", 0)
        download_file(
            url,
            location,
            enable_progress_bar=bool(self.module_controller.progress_bar_enabled),
            indent_level=indent,
        )

    def _print_quality_notice_once(self, key: str, message: str) -> None:
        """Avoid per-track spam when max_track_quality_to_use applies to a whole album."""
        with self._quality_notice_lock:
            if key in self._quality_notice_seen:
                return
            self._quality_notice_seen.add(key)
        self.print(message)

    @staticmethod
    def _is_spatial_audio_track(track: AudioTrack) -> bool:
        return bool(codec_data[track.codec].spatial)

    @staticmethod
    def _is_ra360_audio_track(track: AudioTrack) -> bool:
        quality = str(track.quality or "").upper()
        if quality.startswith("SPATIAL_RA360"):
            return True
        return track.codec in (CodecEnum.MHA1, CodecEnum.MHM1)

    @staticmethod
    def _is_atmos_audio_track(track: AudioTrack) -> bool:
        quality = str(track.quality or "").upper()
        if quality.startswith("SPATIAL_ATMOS"):
            return True
        return track.codec in (CodecEnum.EAC3, CodecEnum.AC4)

    @staticmethod
    def _spatial_manifest_badge(track: AudioTrack) -> str:
        """Search/download badge from MPD track type + codec (not catalog 'immersive')."""
        if ModuleInterface._is_ra360_audio_track(track):
            return "3D MPEG-H Audio"
        if ModuleInterface._is_atmos_audio_track(track):
            return "◗◖ ATMOS"
        if ModuleInterface._is_spatial_audio_track(track):
            codec_name = codec_data[track.codec].pretty_name
            official = str(track.official_quality_name or "")
            return ModuleInterface._amazon_spatial_display_label(codec_name, official)
        return ""

    @staticmethod
    def _mapped_has_spatial_prefix(
        mapped_audio_tracks: dict[QualityEnum, dict[str, list[AudioTrack]]],
        prefix: str,
    ) -> bool:
        want = str(prefix or "").upper()
        for _qe, _qn, tracks in ModuleInterface._iter_over_tracks_to_quality_map(
            mapped_audio_tracks
        ):
            for track in tracks:
                if str(track.quality or "").upper().startswith(want):
                    return True
        return False

    @staticmethod
    def _quality_prefix_is_spatial(quality_prefix: str) -> bool:
        return str(quality_prefix or "").upper().startswith("SPATIAL")

    def _select_track_for_quality_prefix(
        self,
        mapped_audio_tracks: dict[QualityEnum, dict[str, list[AudioTrack]]],
        quality_enum: QualityEnum,
        quality_prefix: str,
        *,
        spatial_only: bool,
        stereo_only: bool,
    ) -> AudioTrack | None:
        tier_tracks = mapped_audio_tracks.get(quality_enum) or {}
        for _group_name, tracks in tier_tracks.items():
            if not tracks:
                continue
            for track in tracks:
                if not track.quality.startswith(quality_prefix):
                    continue
                is_spatial = self._is_spatial_audio_track(track)
                if stereo_only and is_spatial:
                    continue
                if spatial_only and not is_spatial:
                    continue
                return track
        return None

    def _select_audio_track_for_tier(
        self,
        mapped_audio_tracks: dict[QualityEnum, dict[str, list[AudioTrack]]],
        quality_tier: QualityEnum,
        codec_options: typing.Optional[CodecOptions] = None,
        *,
        to_print: bool = False,
    ) -> AudioTrack | None:
        allow_spatial = True
        if codec_options is not None and not codec_options.spatial_codecs:
            allow_spatial = False
        if self.settings.get("force_non_spatial", False):
            allow_spatial = False

        want_spatial = (
            allow_spatial
            and quality_tier.value >= QualityEnum.ATMOS.value
        )

        tier_order: list[tuple[QualityEnum, list[str]]] = []
        for quality_enum in reversed(QualityEnum):
            if quality_enum.value > quality_tier.value:
                continue
            prefixes = self.quality_parse.get(quality_enum)
            if not prefixes:
                continue
            tier_order.append((quality_enum, prefixes))

        if want_spatial:
            for quality_enum, prefixes in tier_order:
                for quality_prefix in prefixes:
                    if not self._quality_prefix_is_spatial(quality_prefix):
                        continue
                    track = self._select_track_for_quality_prefix(
                        mapped_audio_tracks,
                        quality_enum,
                        quality_prefix,
                        spatial_only=True,
                        stereo_only=False,
                    )
                    if track:
                        if to_print:
                            self._print_quality_notice_once(
                                f"spatial:{track.quality}",
                                f"{module_information.service_name}: Downloading spatial audio "
                                f"({track.quality}).",
                            )
                        return track

        for quality_enum, prefixes in tier_order:
            for quality_prefix in prefixes:
                if self._quality_prefix_is_spatial(quality_prefix):
                    continue
                track = self._select_track_for_quality_prefix(
                    mapped_audio_tracks,
                    quality_enum,
                    quality_prefix,
                    spatial_only=False,
                    stereo_only=True,
                )
                if track:
                    return track

        return None

    def _get_usable_audio_track_of_mapped_quailty(
        self,
        mapped_audio_tracks: dict[QualityEnum, dict[str, list[AudioTrack]]],
        quality_tier: QualityEnum,
        codec_options: typing.Optional[CodecOptions] = None,
        to_print: typing.Optional[bool] = False,
    ):
        track_to_use: AudioTrack | None = None
        mapped_qual_tracks = list(
            self._iter_over_tracks_to_quality_map(mapped_audio_tracks)
        )

        if max_track_quality_to_use := self.settings["max_track_quality_to_use"]:
            max_track_quality_to_use = str(max_track_quality_to_use).upper()
            for _quality_enum, _quality_name, tracks in mapped_qual_tracks:
                if not tracks:
                    continue
                for track in tracks:
                    if not track.quality.startswith(max_track_quality_to_use):
                        continue
                    track_to_use = track
                    break
                if track_to_use:
                    if to_print:
                        self._print_quality_notice_once(
                            f"matched:{max_track_quality_to_use}",
                            f"{module_information.service_name}: Using {max_track_quality_to_use} "
                            "quality where available.",
                        )
                    break

            if not track_to_use and to_print:
                self._print_quality_notice_once(
                    f"fallback:{max_track_quality_to_use}",
                    f"{module_information.service_name}: {max_track_quality_to_use} is not on every "
                    "track; using the highest available quality where needed.",
                )
            # If user explicitly chose UHD/HD, prefer a lossless fallback before dropping to lossy.
            if not track_to_use and max_track_quality_to_use in ("UHD", "HD"):
                track_to_use = self._select_audio_track_for_tier(
                    mapped_audio_tracks,
                    QualityEnum.LOSSLESS,
                    codec_options,
                    to_print=False,
                )

        if not track_to_use:
            track_to_use = self._select_audio_track_for_tier(
                mapped_audio_tracks,
                quality_tier,
                codec_options,
                to_print=bool(to_print),
            )

        if not isinstance(track_to_use, AudioTrack):
            raise TypeError(f"{track_to_use=}, type: {type(track_to_use)}")

        LOGGER.debug(f"Using AudioTrack: {track_to_use}")
        return track_to_use

    def mpd_to_quality_map(
        self,
        mpd: ElementTree.Element,
        track_asin: str,
        subscription_tier: AmazonMusicTier,
        media_region: AmazonRegion,
        account_region: AmazonRegion
    ):
        """
        A helper function to retrieve a mapping of a QualityEnum
        to a dictionary of a Quality and a list of AudioTracks via a MPD.

        """
        # LOGGER.debug(json.dumps(mpd, indent=3))

        avaliable_tracks = self.get_audios_from_mpd(mpd, track_asin)

        # Sorting by bitrate helps retrieve the highest resolution avaliable
        LOGGER.debug(avaliable_tracks)

        preferred_codecs = [CodecEnum.OPUS]
        # TS: 1703471476
        # I fucking wrote the entitlement and KATANA exclusion condition in the wrong place
        # I'll write it later, but TODO it's supposed to be its own independant function after mapped tracks

        # Know what entitlements are avaliable
        available_entitlements = set(
            i
            for track in avaliable_tracks
            for i in track.entitlements.entitlement_names
        )
        has_entitlements = bool(
            self.settings["use_own_master_keys"]
            # Make sure the key required exists
            and any(
                f"KATANA:{media_region.country}" in str(item)
                for item in self.settings["master_keys"]
            )
            # Make sure the entitlement PSSH exists
            and any(
                local_name in av_entitlement_name
                for local_name, _ in (
                    item.split(":", maxsplit=1)
                    for item in self.settings["master_keys"]
                )
                for av_entitlement_name in available_entitlements
            )
        )
        has_katana_tier = (
            not self.settings["use_own_master_keys"]
            and (
                self.options.disable_subscription_check
                or subscription_tier is AmazonMusicTier.UNLIMITED
            )
            # Needed as license acquitsion, might be blocked due to cross region calling
            and account_region.region == media_region.region
        )

        # print(f"{has_entitlements=}, {has_katana_tier=}, {available_entitlements=}")
        if (
            has_entitlements or has_katana_tier
        ):
            # print("flac")
            preferred_codecs.append(CodecEnum.FLAC)

            if not self.settings["force_non_spatial"]:
                if self.settings["prefer_spatial_mha1"]:
                    preferred_codecs.append(CodecEnum.MHA1)
                else:
                    preferred_codecs.append(CodecEnum.MHM1)

                if self.settings["prefer_spatial_ac4"]:
                    preferred_codecs.append(CodecEnum.AC4)
                else:
                    preferred_codecs.append(CodecEnum.EAC3)
        
        mapped_audio_tracks = self.tracks_to_quality_map(
            avaliable_tracks, preferred_codecs, entitlements_only=has_entitlements, allow_web_pssh=has_katana_tier
        )
        LOGGER.debug(mapped_audio_tracks)
        return mapped_audio_tracks
    
    def get_decrypted_key(self, enc_key: bytes, pssh: PSSH, enc_key_type: str):
        pssh_data = WidevinePsshData()
        pssh_data.ParseFromString(pssh.init_data)
        entitled_key = pssh_data.entitled_keys[0]

        LOGGER.debug(f"Using {enc_key.hex()} as the master key.")
        LOGGER.debug(f"Raw encrypted key ID and key: {entitled_key.key_id.hex()}:{entitled_key.key.hex()}")
        key_id = entitled_key.key_id.hex()
        
        cipher = AES.new(enc_key, AES.MODE_CBC, entitled_key.iv)
        raw_final_key = cipher.decrypt(entitled_key.key)
        
        if enc_key_type == "ENTITLEMENT":
            final_key = raw_final_key.hex()
        elif enc_key_type == "CONTENT":
            final_key = raw_final_key[:16].hex()
        else:
            raise ValueError(f"enc_key_type must be ENTITLEMENT or CONTENT, not {enc_key_type}")
        
        LOGGER.debug(f"Raw key ({enc_key_type}): {raw_final_key.hex()}, IV: {entitled_key.iv.hex()}")

        return key_id, final_key
        

    def get_audios_from_mpd(self, manifest: ElementTree.Element, track_asin: str):
        """
        Retrieve each avaliable audio quality/format as a AudioTrack.

        The manifest must be retrieved from manifest API endpoint with the SIREN / SIREN_KATANA DASH version.

        The manifest must follow the `urn:mpeg:dash:profile:isoff-on-demand:2011` XML profile.
        """
        avaliable_tracks: list[AudioTrack] = []
        ns = {
            "mpd": "urn:mpeg:dash:schema:mpd:2011",
            "drm": "urn:mpeg:cenc:2013",
            "amz": "urn:amazon:music:drm:2019",
        }

        for adaptation_set in manifest.findall(".//AdaptationSet", ns):
            entitlement_psshs: dict[str, PSSH] = {}
            web_pssh: PSSH | None = None
            for content_protection in adaptation_set.findall(
                "ContentProtection", ns
            ):
                if (
                    content_protection.get("schemeIdUri")
                    == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
                    and content_protection.get("value") == "AmzMusic-2019"
                ):
                    entitlement_psshs.update(
                        {
                            content_protection.find("amz:groupId", ns).text.split("_")[0].lower(): PSSH(content_protection.find("drm:pssh", ns).text, strict=True)
                        }
                    )
                if content_protection.get(
                    "schemeIdUri"
                ) == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" and not content_protection.get(
                    "value"
                ):
                    web_pssh = PSSH(content_protection.find("drm:pssh", ns).text, strict=True)
                    
            if not web_pssh:
                LOGGER.warning("Failed to find the PSSH for web playback. License acquisition may fail.")
            
            official_quality_name = ""
            audio_ref_loudness = ""

            for prop in adaptation_set.findall("SupplementalProperty"):
                # Get track type property (LD, SD, HD, SD)
                if prop.get("schemeIdUri") == "amz-music:trackType":
                    official_quality_name = prop.get("value", "Unknown")
                    LOGGER.debug(
                        f"Official name for track: {official_quality_name}"
                    )
                    continue

                # Get track loudness property
                if prop.get("schemeIdUri") == "urn:mpeg:mpegB:cicp:ProgramLoudness":
                    audio_ref_loudness = prop.get("value", "Unknown")
                    continue

            for representation in adaptation_set.findall("Representation", ns):
                media_url_elem = representation.find("BaseURL", ns)
                if not (media_url_elem is not None and media_url_elem.text):
                    raise IndexError(f"Failed to find Media URL for {track_asin}")
                media_url = media_url_elem.text

                media_url_query = parse_qs(urlparse(media_url).query)
                quality = str(media_url_query.get("ql", [""])[0])

                if quality.startswith(
                    "UHD"
                ) and not official_quality_name.startswith("UHD"):
                    # amz-music:trackType only returns the following:
                    # LD, SD, HD, and 3D
                    official_quality_name = "UHD"
                elif not official_quality_name:
                    official_quality_name = quality

                codec = str(representation.get("codecs")).upper()
                # 360 Audio
                if codec.startswith("MHA1"):
                    codec = "MHA1"
                elif codec.startswith("MHM1"):
                    codec = "MHM1"
                # Dolby Atmos
                elif codec.startswith("EC-3"):
                    codec = "EAC3"
                elif codec.startswith("AC-4"):
                    codec = "AC4"
                # V2 musicDashVersionList
                elif codec.startswith("MP4A"):
                    codec = "AAC"

                codec = CodecEnum[codec]

                bit_depth = None
                sp = representation.find("SupplementalProperty")
                if sp is not None and not codec_data[codec].spatial:
                    bit_depth = int(sp.get("value", 0))

                avaliable_tracks.append(
                    AudioTrack(
                        asin=track_asin,
                        codec=codec,
                        bit_depth=bit_depth,
                        bitrate=int(
                            int(representation.get("bandwidth") or 0) // 1000
                        ),
                        sample_rate=float(
                            int(representation.get("audioSamplingRate") or 0) / 1000
                        ),
                        url=media_url,
                        official_quality_name=official_quality_name,
                        quality_ranking=int(
                            representation.get("qualityRanking", 0)
                        ),
                        quality=quality,
                        entitlements=PSSHEntitlements(**entitlement_psshs),
                        web_pssh=web_pssh,
                        reference_loudness=audio_ref_loudness,
                    )
                )
                
        if not avaliable_tracks:
            raise ValueError("No tracks found!")

        avaliable_tracks = natsort.natsorted(
            avaliable_tracks, key=lambda x: x.quality, reverse=False
        )
        # import pprint
        # pprint.pprint(avaliable_tracks)
        return avaliable_tracks

    @staticmethod
    def _iter_over_tracks_to_quality_map(
        mapping: dict[QualityEnum, OrderedDict[str, list[AudioTrack]]]
    ):
        for quality_enum in reversed(QualityEnum):
            quality_tracks = mapping.get(quality_enum)
            if not quality_tracks:
                continue
            for quality_name, tracks in quality_tracks.items():
                # print(f"{quality_enum=}, {quality_name=}, {tracks=}")
                yield quality_enum, quality_name, tracks

        return

    def tracks_to_quality_map(
        self,
        tracks: typing.Iterable[AudioTrack],
        preferred_codecs: typing.Iterable[CodecEnum],
        entitlements_only: typing.Optional[bool] = None,
        allow_web_pssh: typing.Optional[bool] = None,
    ) -> dict[QualityEnum, dict[str, list[AudioTrack]]]:
        """
        Example output:

        {
            "hifi": {
                "SPATIAL_ATMOS_HIGH": [
                    AudioTrack(),
                ],
                "SPATIAL_ATMOS_MEDIUM": [
                    AudioTrack()
                ]
            },
        }

        """
        quality_to_track_mapping = {}

        for quality_enum, qualities in reversed(self.quality_parse.items()):
            def key_for_sorting_avaliable_tracks(track: AudioTrack):
                return track.bitrate

            def key_for_filtering_audiotracks(track: AudioTrack):
                if allow_web_pssh and not track.web_pssh:
                    return
                elif entitlements_only and not track.entitlements.music_territory:
                    return
                for quality in qualities:
                    if not (
                        track.quality.startswith(quality)
                        and track.codec in preferred_codecs
                    ):
                        continue
                    return quality
                return

            def key_for_grouping_audiotracks(track: AudioTrack):
                return track.official_quality_name

            # AudioTracks are sorted best quality to worse
            
            grouped_tracks = OrderedDict([
                (key, natsort.natsorted(
                    group, key=key_for_sorting_avaliable_tracks, reverse=True
                ))
                for key, group in itertools.groupby(
                    [item for item in tracks if key_for_filtering_audiotracks(item)],
                    key=key_for_grouping_audiotracks,
                )
            ])

            quality_to_track_mapping.update({quality_enum: grouped_tracks})

        # import pprint
        # pprint.pprint(quality_to_track_mapping)

        return quality_to_track_mapping

    @staticmethod
    def _try_mp4decrypt(encrypted_file: str, destination_file: str, key_id: str, key: str) -> bool:
        """Fallback decrypt via Bento4 mp4decrypt when Shaka Packager crashes (e.g. Tiny10 VM)."""
        executable = resolve_mp4decrypt()
        if not executable:
            return False

        enc_path = os.path.abspath(encrypted_file)
        out_path = os.path.abspath(destination_file)
        env = get_clean_env()
        tool_dir = str(executable.parent)
        env["PATH"] = tool_dir + os.pathsep + env.get("PATH", "")

        # KID:key (CENC), then track index fallbacks per Bento4 docs.
        key_specs = (
            f"{key_id}:{key}",
            f"1:{key}",
            f"0:{key}",
        )
        for key_spec in key_specs:
            if os.path.isfile(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            run_kwargs = {
                "args": [str(executable), "--key", key_spec, enc_path, out_path],
                "env": env,
                "cwd": os.path.dirname(enc_path) or os.getcwd(),
                "capture_output": True,
                "text": True,
            }
            if sys.platform == "win32":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(**run_kwargs)
            if result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                LOGGER.info(
                    "mp4decrypt decrypted %s bytes (key spec %s)",
                    os.path.getsize(out_path),
                    key_spec.split(":")[0],
                )
                return True
            LOGGER.debug(
                "mp4decrypt failed (exit %s, key %s): %s",
                result.returncode,
                key_spec,
                ((result.stderr or "") + (result.stdout or "")).strip()[:300],
            )
        return False

    @staticmethod
    def call_shaka_packager(encrypted_file: str, destination_file: str, key_id: str, key: str, label: str):
        executable = resolve_shaka_packager()
        if not executable:
            raise EnvironmentError(
                "Shaka Packager executable not found but is required.\n"
                f"Download it at: {SHAKA_PACKAGER_HELP_URL}\n"
                "Place packager-win-x64.exe next to OrpheusDL_GUI.exe (or orpheus.py when running from source)."
            )

        enc_path = os.path.abspath(encrypted_file)
        out_path = os.path.abspath(destination_file)
        if not os.path.isfile(enc_path):
            raise FileNotFoundError(f"Encrypted media file not found: {enc_path}")
        enc_size = os.path.getsize(enc_path)
        if enc_size < 512:
            raise ValueError(
                f"Encrypted media file is too small ({enc_size} bytes). "
                "The download may have failed or been blocked."
            )

        drm_label = (label or "0").strip()
        keys_arg = f"--keys=label={drm_label}:key_id={key_id}:key={key}"
        # stream=0 matches the original module (works with v3.7.2 on dev); stream=audio is a fallback.
        stream_variants = [
            f"input={enc_path},stream=0,output={out_path}",
            f"input={enc_path},stream=audio,output={out_path},drm_label={drm_label}",
        ]

        env = get_clean_env()
        packager_dir = str(executable.parent)
        env["PATH"] = packager_dir + os.pathsep + env.get("PATH", "")
        last_detail = ""
        last_returncode = 1
        packager_args: list[str] = []

        for stream_descriptor in stream_variants:
            if os.path.isfile(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass

            packager_args = [
                stream_descriptor,
                "--enable_raw_key_decryption",
                keys_arg,
            ]
            run_kwargs = {
                "args": [str(executable), *packager_args],
                "env": env,
                "cwd": os.path.dirname(enc_path) or os.getcwd(),
                "capture_output": True,
                "text": True,
            }
            if sys.platform == "win32":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(**run_kwargs)
            last_returncode = result.returncode
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            last_detail = stderr or stdout or f"exit code {result.returncode}"

            if result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                LOGGER.debug(
                    "Shaka Packager OK (%s bytes decrypted)",
                    os.path.getsize(out_path),
                )
                return

            if result.returncode == 0:
                last_detail = (
                    f"Packager exited 0 but output missing or empty: {out_path} "
                    f"(encrypted input {enc_size} bytes)"
                )
            LOGGER.warning(
                "Shaka Packager attempt failed (exit %s): %s — %s",
                result.returncode,
                stream_descriptor,
                last_detail[:500],
            )

        if ModuleInterface._try_mp4decrypt(enc_path, out_path, key_id, key):
            return

        detail_lower = (last_detail or "").lower()
        # macOS: a too-new Shaka Packager build links libc++ symbols missing on older
        # macOS, so it SIGABRTs (exit -6) with a dyld "Symbol not found" message.
        macos_abi_crash = sys.platform == "darwin" and (
            last_returncode == -6
            or "symbol not found" in detail_lower
            or "dyld" in detail_lower
        )
        windows_crash = last_returncode in (3221225477, -1073741819)
        has_mp4decrypt = bool(resolve_mp4decrypt())

        if macos_abi_crash:
            last_detail = (
                f"{last_detail}\n"
                "The bundled Shaka Packager is incompatible with this macOS version "
                "(it was built for a newer macOS and aborts on this one). "
                "Fix: install the Bento4 mp4decrypt fallback — `brew install bento4` — "
                "or download mp4decrypt from https://www.bento4.com/downloads/ and place it "
                "inside the app at Contents/Frameworks/ (next to packager-osx-x64). "
                "Then restart the app."
            )
        elif windows_crash:
            last_detail = (
                f"{last_detail}\n"
                "Shaka Packager crashed on this system (exit 3221225477). "
                "Place mp4decrypt.exe (Bento4) next to packager-win-x64.exe as a fallback — "
                "https://www.bento4.com/downloads/ → Windows binaries zip → mp4decrypt.exe. "
                "Or use a full Windows 10 VM instead of Tiny10."
            )
        elif not has_mp4decrypt:
            if sys.platform == "darwin":
                hint = (
                    "Optional: install the Bento4 mp4decrypt fallback (`brew install bento4`, "
                    "or place mp4decrypt in the app's Contents/Frameworks/ folder) for when "
                    "Shaka Packager fails."
                )
            elif sys.platform.startswith("linux"):
                hint = (
                    "Optional: install the Bento4 mp4decrypt fallback (e.g. your package "
                    "manager or https://www.bento4.com/downloads/) for when Shaka Packager fails."
                )
            else:
                hint = (
                    "Optional: add mp4decrypt.exe from Bento4 next to the app folder "
                    "(https://www.bento4.com/downloads/) when Shaka Packager fails."
                )
            last_detail = f"{last_detail}\n{hint}"
        raise subprocess.CalledProcessError(
            last_returncode,
            [str(executable), *packager_args],
            last_detail.strip(),
        )
