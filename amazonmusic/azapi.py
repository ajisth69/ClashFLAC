import base64
import dataclasses
import functools
import json
import logging
import logging.handlers
import math
import os
import re
import secrets
import time
import typing
import uuid
import concurrent.futures
import pprint
from datetime import datetime, timedelta
from enum import Enum, auto
from urllib.parse import parse_qs, urlencode
from xml.etree import ElementTree

import httpx
import rsa
import rsa.pkcs1
import xmltodict

# from audible import Authenticator, Client, localization
# from audible.auth import sign_request
from audible.login import (
    build_device_serial,
    check_for_approval_alert,
    check_for_captcha,
    check_for_choice_mfa,
    check_for_cvf,
    check_for_mfa,
    create_code_verifier,
    create_s256_code_challenge,
    default_approval_alert_callback,
    default_captcha_callback,
    default_cvf_callback,
    default_login_url_callback,
    default_otp_callback,
    extract_captcha_url,
    extract_code_from_url,
    get_inputs_from_soup,
    get_next_action_from_soup,
    get_soup,
)
from audible.metadata import encrypt_metadata
from bs4 import BeautifulSoup
from Crypto.PublicKey import RSA

from .models import AmazonMusicDevice, AmazonMusicMobileAPICredentials, AmazonMusicTier, AmazonRegion, AmazonContinent

LOGGER = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, type, msg, payload):
        self.type = type
        self.msg = msg
        self.payload = payload

    def __str__(self):
        return ", ".join((self.type, self.msg, str(self.payload)))


class AmazonMobileApplication(Enum):
    MUSIC = auto()
    PRIME_VIDEO = auto()
    SHOPPING = auto()

    @property
    def device_type(self):
        return {
            self.MUSIC: "A1DL2DVDQVK3Q",
            self.PRIME_VIDEO: "A43PXU4ZN2AL1",
            self.SHOPPING: "A1MPSLFC7L5AFK",
        }[self]

    @property
    def assoc_handle(self):
        return {
            self.MUSIC: "amzn_tiburon_na",
            self.PRIME_VIDEO: "amzn_piv_android_v2_us",
        }[self]

    @property
    def official_name(self):
        return {
            self.MUSIC: "Amazon Music",
            self.PRIME_VIDEO: "Amazon Prime Video",
            self.SHOPPING: "Amazon Shopping",
        }[self]


class AmazonMusicMobileAPI:
    """Amazon Music API"""

    application_version = "22.15.12"
    harley_version = "3.12.3.86"

    HARLEY_USER_AGENT = f"Harley/{harley_version} {AmazonMobileApplication.MUSIC.device_type}/{application_version}"
    """ Used for accessing playing DRM protected content """
    APP_USER_AGENT = f"MusicAndroid/{application_version}"
    """ Used for API requests """

    USER_AGENT = "Mozilla/5.0 (Linux; Android 11; Pixel 5 Build/RD2A.211001.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/108.0.5359.128 Mobile Safari/537.36"
    """ Used for Amazon login & other general requests """

    credentials: AmazonMusicMobileAPICredentials

    def __init__(
        self,
        credentials: AmazonMusicMobileAPICredentials,
    ) -> None:
        self.credentials = credentials
        self.session = self._create_httpx_session()
        self.session.cookies.update(credentials.website_cookies)
        # if not self.credentials.web_client_config:
        #     self.credentials.web_client_config = self._get_web_client_configuration(
        #         self.credentials.account_region.domain_tld,
        #         self.parse_for_app_config(self.get_root(self.credentials.account_region.domain_tld)),
        #     )
        
        if not self.credentials.account_region:
            self.credentials.account_region = AmazonRegion.get_region_by_country(
                dict(self.get_account_status()).get("customerAccount", {}).get("accountInfo", {}).get("musicTerritory", "")
            )
        # if self.credentials.account_region and self.credentials.web_client_config.region != self.credentials.account_region.region.name:
        #     self.credentials.web_client_config.region = self.credentials.account_region.region.name
        
        # Always update the tier on instance creation
        self.credentials.tier = self.get_account_subscription_tier()

        return

    @classmethod
    def login_via_mobile(
        cls,
        email: str,
        password: str,
        country_code: str = "US",
        serial: typing.Optional[str] = None,
        load_credentials: typing.Optional[bool] = True,
        application: typing.Optional[AmazonMobileApplication] = None,
        oauth_flow_callback: typing.Optional[typing.Callable[[str, str], str]] = None,
    ):
        if len(country_code) != 2:
            raise ValueError(
                f"Country code must be a ISO 3166-1 alpha-2 value!, got: {country_code}"
            )
        selected_region = AmazonRegion.get_region_by_country(country_code)
        application = application or AmazonMobileApplication.MUSIC
        
        session = cls._create_httpx_session()

        if country_code == "JP" and application is not AmazonMobileApplication.PRIME_VIDEO:
            # Login to Prime Video first, because amazon.
            session = cls.login_via_mobile(
                email=email,
                password=password,
                load_credentials=False,
                application=AmazonMobileApplication.PRIME_VIDEO,
                country_code=country_code,
            )


        base_url = f"https://amazon.{selected_region.domain_tld}"
        init_cookies = cls._build_init_cookies()

        session.base_url = base_url
        session.cookies.update(init_cookies)

        code_verifier = create_code_verifier()
        
        oauth_url, serial = cls._build_oauth_url(
            domain="com",
            code_verifier=code_verifier,
            application=application,
            serial=serial,
            selected_region=selected_region
        )

        # authorization_code = cls._internal_login(session, oauth_url, email, password)
        authorization_code = cls._exteral_login(
            oauth_url, application, oauth_flow_callback=oauth_flow_callback
        )

        items = {
            "authorization_code": authorization_code,
            "code_verifier": code_verifier,
            "serial": serial,
        }

        if not load_credentials:
            return session

        inst = cls.register(
            application=application,
            selected_region=selected_region,
            **items
        )
        print(
            f"Login confirmed for {inst.credentials.customer_info.get('name', 'Unknown user')} in {selected_region.pretty_name} on {application.official_name}"
        )

        # Authorize device for usage on Amazon Music
        auth_device_resp = dict(inst.authorize_device(device_serial=serial).json())

        inst.credentials.customer_id = auth_device_resp["device"]["customerId"]

        # confirm the device has been successfully authorized

        # device_resp = self.session.post(url=base_post, data={
        #     "customerInfo": {
        #         "customerId": "", #the value is not set, but it is required
        #         "deviceId": serial,
        #         "deviceType": self.credentials.device_info.device_type,
        #     },
        #     "deviceId": serial,
        #     "deviceType": self.credentials.device_info.device_type,
        #     "targetDeviceId": serial,
        #     "targetDeviceType": self.credentials.device_info.device_type,
        # }, headers={
        #     'x-amz-target': 'com.amazon.stratus.StratusServiceExternal.retrieveDevice',
        #     'x-amzn-RequestId': str(uuid.uuid4()),
        # })
        # LOGGER.debug(f"{device_resp.status_code} {device_resp.text}")

        # TODO add a check if too many devices are registered, and if so, notify the user and add a way to remove devices via a prompt
        # get devices
        inst._list_devices()

        if not inst.credentials:
            raise Exception("Login failed. Please check the log.")
        return inst

    @staticmethod
    @functools.lru_cache()
    def _wait_for_response(session: httpx.Client, request: httpx.Request):
        # Sometimes we get a DNS resolve error (too many requests for manifest?), this attempts to retry 5 times
        attempt = 0
        resp = None
        last_http_exc = None
        while attempt <= 5:
            attempt += 1
            try:
                LOGGER.debug("Handling request: %s", request)
                # print(f"Handling request: {request}")
                # print(f"Handling request: {vars(request)} at {time.perf_counter()}")
                resp = session.send(request)
                resp.raise_for_status()
                LOGGER.debug(
                    "OK with request with status code %s for request %s",
                    resp.status_code,
                    request.url,
                )
                # print(vars(resp))
            except httpx.HTTPError as ce:
                if resp and resp.status_code == 400:
                    # this is usually an error with the user, than the server itself.
                    return resp
                LOGGER.error(ce)
                if resp:
                    LOGGER.error(str(resp.content))
                LOGGER.debug(ce, exc_info=True)
                last_http_exc = ce
                time.sleep(2)
                continue
            else:
                # return the response when successful
                return resp
        else:
            if resp:
                LOGGER.error("%s, %s", resp.text, resp.content)
            raise last_http_exc or RuntimeError()

    def post(
        self,
        url: str,
        data: dict | None,
        headers: typing.Optional[dict] = None,
        add_default_stratus_headers: typing.Optional[bool] = True,
        sign: typing.Optional[bool] = True,
    ) -> httpx.Response:
        # these headers assume that the url is https://music.amazon.com/NA/api/stratus/
        # TODO have a enum representing the the api endpoints for the different headers
        if add_default_stratus_headers:
            headers = {
                "User-Agent": self.APP_USER_AGENT,
                "android-app-version": self.application_version,
                "content-encoding": "amz-1.0",
                "accept": "application/json",
                "accept-encoding": "gzip",
                "accept-charset": "utf-8",
                "content-type": "application/json; charset=UTF-8",
            } | (headers or {})
        request = httpx.Request(
            "POST",
            url,
            cookies=self.credentials.website_cookies
            if hasattr(self, "credentials")
            else None,
            headers=headers,
            json=data,
        )
        if sign:
            self._apply_signing_auth_flow(request)
        self._apply_cookies_auth_flow(request)
        # LOGGER.debug(vars(request))
        resp = self._wait_for_response(self.session, request)
        return resp

    def get(self, url: str, headers: typing.Optional[dict] = None) -> httpx.Response:
        if not headers:
            headers = {}

        d_headers = {
            "User-Agent": self.APP_USER_AGENT,
            "X-Amz-RequestId": str(uuid.uuid4()),
        }
        request = httpx.Request(
            "GET",
            url,
            cookies=self.credentials.website_cookies,
            headers=d_headers | headers,
        )
        # self._apply_signing_auth_flow(request)
        self._apply_cookies_auth_flow(request)
        return self._wait_for_response(self.session, request)

    def get_root(
        self, tld: typing.Optional[str] = None, credentials: typing.Optional[str] = None
    ):
        """
        Get the response of the root URL of Amazon Music.

        Useful for parsing the web app configuration.
        """
        return self.get(
            url=f"https://music.amazon.{tld or credentials.tld}/",
            headers={"User-Agent": self.USER_AGENT},
        ).text

    @functools.lru_cache()
    def get_metadata(
        self,
        asins: str | typing.Sequence[str],
        use_alternative_naming: typing.Optional[bool] = None,
        region_to_use: typing.Optional[AmazonRegion] = None
    ) -> dict[str, list[dict[str, typing.Any]]]:
        """
        Get metadata for a track, album, playlist or artist.


        Track ASIN -> `response.json()['tracksList'][0]`

        Album ASIN -> `response.json()['albumsList'][0]`

        Artist ASIN -> `response.json()['artistList'][0]`

        ## List of avaliable features:
        fullAlbumDetails, playlistLibraryAvailability, disableSubstitution, childParentOwnership, migratedLikeAvailability, trackLibraryAvailability,
        hasLyrics, ownership, expandTracklist, includeVideo, requestAudioVideo, popularity, albumArtist, collectionLibraryAvailability, includePurchaseDetails
        """
        # Valid keywords to Amazon JP (for playlist metadata, diff endpoint) 
        # objectId,fileName,fileExtension,fileSize,creationDate,lastUpdatedDate,orderId,asin,purchaseDate,localFilePath,md5,status,purchased,uploaded,title,sortTitle,rating,marketplace,physicalOrderId,assetType,artistName,artistAsin,contributors,trackNum,discNum,primaryGenre,duration,bitrate,composer,songWriter,performer,lyricist,publisher,errorCode,instantImport,primeStatus,isMusicSubscription,albumName,albumAsin,albumArtistName,albumArtistAsin,albumContributors,albumRating,albumPrimaryGenre,albumReleaseDate,sortArtistName,sortAlbumName,sortAlbumArtistName,audioUpgradeDate,parentalControls,assetEligibility,eligibility,internalTags
        if not asins:
            raise ValueError(asins)
        if not region_to_use:
            region_to_use = self.credentials.account_region

        asins = [asins] if isinstance(asins, str) else list(asins)
        response = self.post(
            url=f"https://music.amazon.{region_to_use.domain_tld}/{region_to_use.region.name}/api/muse/",
            headers={
                "User-Agent": self.APP_USER_AGENT,
                "x-amz-target": "com.amazon.musicensembleservice.MusicEnsembleService.lookup",
                "X-Amz-Requestid": str(uuid.uuid4()),
            },
            data={
                "allowedParentalControls": {"hasExplicitLanguage": True},
                "asins": asins,
                "currencyOfPreference": None,
                "customerIP": None,
                "customerId": None,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
                "features": [
                    "ownership",
                    "expandTracklist",
                    "hasLyrics",
                    "includeVideo",
                    "requestAudioVideo",
                    "popularity",
                    "expandTracklist",
                    "fullAlbumDetails",
                    "includePurchaseDetails",
                    "trackLibraryAvailability",
                    "collectionLibraryAvailability",
                    "migratedLikeAvailability",
                    "playlistLibraryAvailability",
                ],
                "filters": None,
                "lang": region_to_use.locale,  # the lang locale of the phone/mobile app, en_US
                "marketplaceId": None, # Member must satisfy enum value set: [ATVPDKIKX0DER, A1F83G8C2ARO7P, A1PA6795UKMFR9, A1RKKUPIHCS9HS, A1VC38T7YXB528, A13V1IB3VIYZZH, APJ6JRA9NG5V4]"}
                "debug": True,
                "metadataLang": "en"
                if use_alternative_naming
                else None,  # null for locale based on IP, setting to a random string value returns it romanized
                "musicRequestIdentityContextToken": None,
                "musicTerritory": region_to_use.country,
                "requestedContent": "FULL_CATALOG",  # ALL_STREAMABLE (for current account only), FULL_CATALOG is valid too
                "sessionId": None,
                "stub": None,
                # "debug": True
            },
        )
        if response.status_code != 200:
            raise Exception(
                f"Failed to get metadata: {response.status_code} {response.text}"
            )
        resp_json = response.json()

        LOGGER.debug(json.dumps(resp_json, indent=2))
        return resp_json

    def get_page(
        self,
        uri: str,
        count: typing.Optional[int] = None,
        next_token: typing.Optional[str] = None,
        offset: typing.Optional[int] = None,
        region_to_use: typing.Optional[AmazonRegion] = None
    ):
        """
        Get a page of a Amazon Music URI.

        Args:
            uri: str: A valid Amazon Music URI.
            count: int: How many related albums you want to obtain?
            I have no idea what the `count` paramter means.

        Example usage:

        `self.mobile_session.get_page("album/B0CDJC65LH", count=0, locale="en_US")`
        """
        if not count:
            count = 5
        if not region_to_use:
            region_to_use = self.credentials.account_region

        # Content features can be any of the following:
        # 'contentFeatures' failed to satisfy constraint: Member must satisfy constraint: [Member must satisfy enum value set: [requestFeaturedPlayV4Sub1, podcastSonicRush, pinPodcastsInFacetedNavigation, requestFeaturedPlayV6NoPodcasts, includeFacetedNavigation, personalizedPlaylist, includeVideoStory, includePodcastCuratedContent, podcast, includePodcastExploreBites, populateRecentlyPlayed, includeLiveEvent, includeVideoStoryOnArtistHighlights, allowDeepLinkURLInWidget, includeAlbumDetailUpsellWidgets, requestFeaturedPlayV2, requestFeaturedPlayV3, bundesliga, artistTasteCollection, requestFeaturedPlayV4, includePodcastBitesVisualShoveler, requestFeaturedPlayV5, includeVideoShow, includeCommentary, requestFeaturedPlayV6, requestFeaturedPlay, includePodcastUserContent, includeVideo, audioShow, includeLiveStream, includeFollowArtistsWidget, includeMerch, includeStationFromAnything, editorialAssociations, includeUpsellWidgets, includeAmpShows, recentlyPlayed, allowVerticalItemBarkers, includeCommentaryFlag, includePodcastEpisodeDescriptiveShoveler]]

        resp = self.post(
            url=f"https://music.amazon.{region_to_use.domain_tld}/{region_to_use.region.name}/api/musepage/",
            headers={
                "x-amz-target": "com.amazon.musicensembleservice.MusicEnsembleService.page",
                "User-Agent": self.APP_USER_AGENT,
                "X-Amz-Requestid": str(uuid.uuid4()).lower(),
            },
            data={
                "allowedParentalControls": {"hasExplicitLanguage": True},
                "allowedParentalControlsString": None,
                "artistVideoStoryEntityAsin": None,
                "browseId": None,
                "campaignsXml": None,
                "contentFeatures": [
                    "includeVideo",
                    "includeVideoStory",
                    "allowDeepLinkURLInWidget",
                    "podcast",
                    "includePodcastCuratedContent",
                    "includePodcastUserContent",
                    "includePodcastEpisodeDescriptiveShoveler",
                    "podcastSonicRush",
                    "includeLiveStream",
                ],
                "count": count,
                "countOfEntitiesPerWidget": None,
                "customerIP": None,
                "customerId": None,
                "debug": None,  # set to True for.. a new errors attribute.
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
                "ipAddress": None,
                "languagesOfPerformance": None,
                "locale": region_to_use.locale,  # "ja_JP"
                "marketplaceId": None,
                "musicRequestIdentityContextToken": None,
                "musicTerritory": region_to_use.country,
                "nextToken": next_token,
                "offset": offset,
                "requestedContent": "KATANA",
                "sessionId": None,
                "stub": False,
                "testTraffic": None,
                "upsellContent": None,
                "uri": uri,  # e.g "album/B0CDJC65LH"
                "validationPayload": None,
            },
        )
        return dict(resp.json())

    @typing.overload
    def search(
        self,
        query: str,
        asins: tuple[str, ...],
        search_types: typing.Optional[tuple[str, ...]] = None,
        limit: typing.Optional[int] = 50,
        region_to_use: typing.Optional[AmazonRegion] = None,
    ) -> dict[typing.Any, typing.Any]:
        ...

    @typing.overload
    def search(
        self,
        query: str,
        asins: typing.Optional[tuple[str, ...]] = None,
        search_types: typing.Optional[tuple[str, ...]] = None,
        limit: typing.Optional[int] = 50,
        region_to_use: typing.Optional[AmazonRegion] = None,
    ) -> typing.Generator[dict[typing.Any, typing.Any], None, None]:
        ...

    def search(self, *args, **kwargs):
        # mfw https://github.com/microsoft/pyright/issues/2414
        # its annoying, so we do this as a workaround
        """
        Search for a item using a query.

        Args:
            asins: A tuple of str (Optional): Return the document which matched with the nth index of ASINs.
            search_types: Iterable (tuple) (Optional): Search for a specific catalog type.

            Valid types are:
            `catalog_album, catalog_artist, catalog_playlist, catalog_station,
            catalog_track, livesports_program, catalog_video, catalog_video_playlist,
            catalog_podcast_show, catalog_podcast_episode, live_event`

        """
        return self._search(*args, **kwargs)

    def _search(
        self,
        query: str,
        asins: typing.Optional[tuple[str]] = None,
        search_types: typing.Optional[tuple[str, ...]] = None,
        limit: typing.Optional[int] = 50,
        region_to_use: typing.Optional[AmazonRegion] = None,
    ):
        url = f"https://music.amazon.{region_to_use.domain_tld}/{region_to_use.region.name}/api/textsearch/search/v1_1/"
        headers = {
            "x-amz-target": "com.amazon.tenzing.textsearch.v1_1.TenzingTextSearchServiceExternalV1_1.search",
            "User-Agent": self.APP_USER_AGENT,
            "X-Amz-Requestid": str(uuid.uuid4()).lower(),
        }
        if search_types is None:
            search_types = ("catalog_album",)
        if not region_to_use:
            region_to_use = self.credentials.account_region

        requested_limit = int(limit) if isinstance(limit, int) and limit > 0 else 50
        page_size = max(1, min(requested_limit, 100))
        max_pages = 100
        page_tokens: dict[str, typing.Optional[str]] = {
            label_type: None for label_type in search_types
        }
        seen_asins: set[str] = set()
        collected_docs: list[dict[str, typing.Any]] = []

        def _next_token_for_category(category: dict) -> typing.Optional[str]:
            for token_key in ("nextPageToken", "pageToken", "nextToken"):
                token = category.get(token_key)
                if token:
                    return str(token)
            # Some responses nest tokens under pagination/meta objects.
            stack = [category]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    for k, v in cur.items():
                        lk = str(k).lower()
                        if "token" in lk and "next" in lk and v:
                            return str(v)
                        if isinstance(v, (dict, list, tuple)):
                            stack.append(v)
                elif isinstance(cur, (list, tuple)):
                    for item in cur:
                        if isinstance(item, (dict, list, tuple)):
                            stack.append(item)
            return None

        for _ in range(max_pages):
            result_specs = [
                {
                    "contentRestrictions": {
                        "allowedParentalControls": {"hasExplicitLanguage": True},
                        "assetQuality": {"quality": []},
                        "contentTier": "UNLIMITED" if region_to_use.country != "IN" else "PRIME",
                        "eligibility": None,
                    },
                    "documentSpecs": [
                        {
                            "fields": [
                                "__default",
                                "parentalControls.hasExplicitLanguage",
                                "contentTier",
                                "artOriginal",
                                "contentEncoding",
                            ],
                            "filters": None,
                            "type": label_type,
                        }
                    ],
                    "label": label_type,
                    "maxResults": page_size,
                    "pageToken": page_tokens.get(label_type),
                    "topHitSpec": None,
                }
                for label_type in search_types
            ]

            data = {
                "customerIdentity": {
                    "customerId": self.credentials.customer_id,
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceType": AmazonMobileApplication.MUSIC.device_type,
                    "musicRequestIdentityContextToken": None,
                    "sessionId": "123-1234567-5555555",  # this is legit what the app uses :skull:
                },
                "explain": None,
                "features": {
                    "spellCorrection": {
                        "accepted": None,
                        "allowCorrection": True,
                        "rejected": None,
                    },
                    "spiritual": None,  # a boolean, unknown purpose
                    "upsell": {"allowUpsellForCatalogContent": False},
                },
                "locale": region_to_use.locale,
                "musicTerritory": region_to_use.country,
                "query": query,
                "queryMetadata": None,
                "resultSpecs": result_specs,
            }

            response = self.post(url=url, headers=headers, data=data)
            resp_json = response.json()
            LOGGER.debug(resp_json)

            results = resp_json.get("results", {})
            if not results:
                break

            next_page_tokens = {}
            docs_added_this_page = 0
            for category in results:
                if not isinstance(category, dict):
                    continue
                label = str(category.get("label") or "")
                if label:
                    next_page_tokens[label] = _next_token_for_category(category)

                if int(category.get("totalHitCount", 0)) == 0:
                    continue
                for hit in category.get("hits", []):
                    document = dict(hit.get("document") or {})
                    if not document:
                        continue
                    dedupe_key = str(
                        document.get("asin")
                        or document.get("seriesAsin")
                        or document.get("artistAsin")
                        or document.get("albumAsin")
                        or ""
                    )
                    if dedupe_key and dedupe_key in seen_asins:
                        continue
                    if dedupe_key:
                        seen_asins.add(dedupe_key)
                    collected_docs.append(document)
                    docs_added_this_page += 1

            if asins:
                for asin in asins:
                    result = next(
                        (
                            doc
                            for doc in collected_docs
                            if str(asin)
                            in {
                                str(doc.get(item))
                                for item in ("albumAsin", "artistAsin", "asin", "seriesAsin")
                                if doc.get(item)
                            }
                        ),
                        None,
                    )
                    if result:
                        return result
            else:
                if len(collected_docs) >= requested_limit:
                    return tuple(collected_docs[:requested_limit])

            page_tokens = {
                label_type: next_page_tokens.get(label_type)
                for label_type in search_types
            }
            has_more_pages = any(page_tokens.values())
            if not has_more_pages or docs_added_this_page == 0:
                break

        if asins:
            return {}
        return tuple(collected_docs[:requested_limit]) if collected_docs else {}

    def find_item_by_asin_in_search_results(self, results: dict, asin: str):
        """
        Comedically long function name
        """
        for document in self.get_documents_from_search_results(results):
            avaliable_asins = [
                str(document.get(item))
                for item in ("albumAsin", "artistAsin", "asin", "seriesAsin")
                if document.get(item)
            ]
            if asin not in avaliable_asins:
                continue
            return document
        return

    @staticmethod
    def get_documents_from_search_results(results: dict):
        for category in results:
            if int(category["totalHitCount"]) == 0:
                continue
            for hit in category["hits"]:
                yield dict(hit["document"])

    def get_catalog_playlist(self, asin: str, region_to_use: typing.Optional[AmazonRegion] = None):
        """
        Get a playlist and its tracks.

        Args:
            asin: A valid ASIN.
            region_to_use: (Optional) A valid AmazonRegion instance.
        """
        if not region_to_use:
            region_to_use = self.credentials.account_region

        resp = self.post(
            url=f"https://music.amazon.{region_to_use.domain_tld}/{region_to_use.region.name}/api/playlists/",
            headers={
                "x-amz-target": "com.amazon.musicplaylist.model.MusicPlaylistService.getCatalogPlaylistByAsin",
                "User-Agent": self.APP_USER_AGENT,
                "x-amzn-requestid": str(uuid.uuid4()).lower(),
            },
            data={
                "asin": asin,
                "contentEncoding": True,
                "customerInfo": {
                    "customerId": "",
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceType": AmazonMobileApplication.MUSIC.device_type,
                },
                "musicTerritory": region_to_use.country,
            },
        )
        return dict(resp.json())

    def get_user_playlist(self, playlist_uuid: str):
        """
        Get a playlist and its tracks.

        Args:
            asin: A valid ASIN.
        """

        resp = self.post(
            url=f"https://music.amazon.{self.credentials.account_region.domain_tld}/{self.credentials.account_region.region.name}/api/playlists/",
            headers={
                "x-amz-target": "com.amazon.musicplaylist.model.MusicPlaylistService.getPlaylistsByIdV2",
                "User-Agent": self.APP_USER_AGENT,
                "x-amzn-requestid": str(uuid.uuid4()).lower(),
            },
            data={
                "contentEncoding": True,
                "customerInfo": {
                    "customerId": "",
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceType": AmazonMobileApplication.MUSIC.device_type,
                },
                "featureSet": ["SUPPORT_MIXED_ID_TYPES", "INCLUDE_FOLLOWER_COUNT"],
                "playlistIds": [playlist_uuid],
                "requestedMetadata": [
                    "albumArtistAsin",
                    "albumArtistName",
                    "albumAsin",
                    "albumContributors",
                    "albumCoverImageFull",
                    "albumCoverImageLarge",
                    "albumCoverImageMedium",
                    "albumCoverImageSmall",
                    "albumCoverImageTiny",
                    "albumCoverImageXL",
                    "albumName",
                    "albumPrimaryGenre",
                    "albumRating",
                    "albumReleaseDate",
                    "artistAsin",
                    "artistName",
                    "asin",
                    "assetType",
                    "assetEligibility",
                    "audioUpgradeDate",
                    "bitrate",
                    "composer",
                    "contributors",
                    "creationDate",
                    "customMeta",
                    "discNum",
                    "dmid",
                    "duration",
                    "eligibility",
                    "fileExtension",
                    "fullAlbumPurchased",
                    "gracenoteId",
                    "instantImport",
                    "isMusicSubscription",
                    "internalTags",
                    "lastUpdatedDate",
                    "localFilePath",
                    "lyricist",
                    "marketplace",
                    "matchType",
                    "matchVersion",
                    "md5",
                    "fileName",
                    "objectId",
                    "orderId",
                    "parentalControls",
                    "performer",
                    "physicalOrderId",
                    "primaryGenre",
                    "primeStatus",
                    "publisher",
                    "purchased",
                    "purchaseDate",
                    "rating",
                    "rogueBackfillDate",
                    "fileSize",
                    "songWriter",
                    "sortAlbumArtistName",
                    "sortAlbumName",
                    "sortArtistName",
                    "sortTitle",
                    "status",
                    "storageLocation",
                    "title",
                    "trackNum",
                    "errorCode",
                    "uploaded",
                ],
            },
        )
        return dict(resp.json())

    def get_recent_tracks(self):
        """
        Get the logged in user's recent tracks.
        """
        url = f"https://music.amazon.{self.credentials.account_region.domain_tld}/api/nimbly/"
        headers = {
            "x-amz-target": "com.amazon.nimblymusicservice.NimblyMusicService.GetRecentTrackActivity",
            "User-Agent": self.APP_USER_AGENT,
            "X-Amz-Requestid": str(uuid.uuid4()).lower(),
        }
        data = {
            # "activityTypeFilters": ["PLAYED"],
            "allowedParentalControls": None,
            "customerId": None,
            "deviceId": self.credentials.device_info.device_serial_number,
            "deviceType": AmazonMobileApplication.MUSIC.device_type,
            "features": ["HIGHQUALITY"],
            "languageLocale": None,
            "marketplaceId": None,
            "musicRequestIdentityContext": None,
            "musicRequestIdentityContextToken": None,
            "musicTerritory": self.credentials.account_region.country,
            "pageToken": "",
        }
        resp = self.post(url=url, headers=headers, data=data, sign=True)

        # print(json.dumps(resp.json(), indent=3))
        return resp.json()

    def get_track_lyrics(self, track_asin: str, region_to_use: typing.Optional[AmazonRegion] = None) -> dict[str, typing.Any]:
        """
        Get the lyrics for a track.

        Response format:

        A dict with the following keys:

        `lrcSource`: Unknown representation. Usually 'AMAZON_INTERNAL'.

        `lyrics`: A dictionary with the following keys:
            `explicitLyricsStatus`: A string with the value 'unfilteredLyrics'. (Other values unknown)

            `lines`: A list of dictionaries with the following keys:
                `endTime`: The end time of the lyric in milliseconds.
                `startTime`: The start time of the lyric in milliseconds.
                `text`: The lyric text.

            `writers`: A list of strings with the lyric writers.

        `lyricsResponseCode`: A string with the value '1002' if the lyrics were found, '2001' if not.

        `lyricsSource`: The source of the lyrics. One version is 'MUSIX_MATCH'.

        `trackAsinAndMarketplace`: A dictionary with the following keys:
            `asin`: The track asin.
            `marketplaceId`: The ID of the marketplace.
        """

        if not region_to_use:
            region_to_use = self.credentials.account_region

        if region_to_use.region.name == "FE":
            tld = "co.jp"
        elif region_to_use.region.name == "NA":
            tld = "com"
        elif region_to_use.region.name == "EU":
            tld = "eu"
        else:
            print(
                "Warning! This type of TLD is not recognized, \n"
                "You are LIKELY to encounter an error. \n"
                f"URL: https://music-xray-service.amazon.{tld}/"
            )
        

        response = self.post(
            url=f"https://music-xray-service.amazon.{tld}/",
            headers={
                "User-Agent": self.APP_USER_AGENT,
                "x-amz-target": "com.amazon.musicxray.MusicXrayService.getLyricsByTrackAsinBatch",
                "X-Amz-Requestid": str(uuid.uuid4()),
            },
            data={
                "trackAsinsAndMarketplaceList": [
                    {
                        "asin": track_asin,
                        "musicTerritory": region_to_use.country,
                    }
                ]
            },
        )

        if response.status_code == 200:
            return dict(response.json().get("lyricsResponseList", [{}])[0])
        return {}

    def get_tracks_manifest(
        self, asins: typing.Iterable[str], force_3d: typing.Optional[bool] = None, region_to_use: typing.Optional[AmazonRegion] = None
    ):
        """
        Get the playback manifest of tracks (MPD)

        Args:
            asins: An iterable of str. They all must be a valid ASIN.
            force_3d: typing.Optional[bool]: Sometimes 3D audio isn't attributed to the ASIN.
            Setting this to true allows Amazon to subtitute the ASIN provided for another ASIN
            which has 3D audio (different ASIN, same metadata). A downside for enabling this option results in UHD not being provided.

        Returns:
        A generator which yields a tuple of the corresponding track ASIN and
        the Amazon Music Dash Manifest as a `xml.etree.ElementTree`

        TRACK_PSSH + SIREN_KATANA = All audio format (Lossless and 360).
        TRACK_PSSH + SIREN_KATANA_NO_CLEAR_LEAD = No issues, only up to lossless
        """
        if not region_to_use:
            region_to_use = self.credentials.account_region

        # Amazon only allows a specific amount of ASINs to be requested at once (10 asins)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(self._get_tracks_manifest, tuple(item), region_to_use, force_3d)
                for item in divide_sequence(list(asins), size=10)
            ]
            executor.shutdown(wait=True)
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if not result:
                    continue
                yield from self.parse_from_content_responses(result)

    def _get_tracks_manifest(
        self, asins: tuple[str], region_to_use: AmazonRegion, force_3d: typing.Optional[bool] = None
    ):
        """Internal function of get_tracks_manifest"""
        # for dash_version in ("SIREN", "SIREN_KATANA"):
        content_id_list = [
            {
                "identifier": asin,
                "identifierType": "ASIN",
            }
            for asin in asins
        ]
        music_agent = f"Harley/{self.harley_version} Harley/{self.application_version} ( {str(uuid.uuid4())} {asins[0]} )"
        response = self.post(
            url=f"https://music.amazon.{region_to_use.domain_tld}/{region_to_use.region.name}/api/dmls/getDashManifestsV2",
            headers={
                "User-Agent": self.HARLEY_USER_AGENT,
                "X-Amz-Requestid": str(uuid.uuid4()),
                "X-Amz-Target": "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getDashManifestsV2",
                "Accept": "application/json, text/javascript, */*",
            },
            data={
                "appInfo": {"musicAgent": music_agent},
                "contentIdList": content_id_list,
                "contentProtectionList": [
                    "GROUP_PSSH", # for entitlement key, mobile uses this
                    "TRACK_PSSH",  # used for web playback
                ],
                "customerInfo": {
                    "entitlementList": [
                        "NIGHTWING",
                        "SONIC_RUSH",
                        "HAWKFIRE", # used in app
                        "ROBIN",
                        "KATANA", # used in app
                        "MERCURY"
                    ],
                    "marketplaceId": region_to_use.marketplace_id,
                    "territoryId": region_to_use.country,
                },
                "customerId": self.credentials.customer_id,
                "deviceToken": {
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceTypeId": AmazonMobileApplication.MUSIC.device_type,
                },
                "musicDashVersionList": [
                    # dash_version,
                    # "SIREN", # only HAWKFIRE, incompatible with SIREN_KATANA parsing. like V2 parsing.
                    "SIREN_KATANA",  # with 360 audio
                    # "SIREN_KATANA_NO_CLEAR_LEAD", #this and no entitlement, is what is used by Amazon Music Web
                    # "V2", # for obtaining legacy AAC audio
                    # "V1", # not working
                ],
                # only if musicDashVersionList is "V2"
                # "bitrateTypeList": [
                #     "HIGH",
                #     "MEDIUM",
                #     "LOW",
                # ],
                # Sometimes having tryAsinSubstitution set to true
                # but no try3dAsinSubstitution
                # fails to get 360RA audio (3-6) for these albums:
                # https://music.amazon.co.jp/albums/B08P6QMJ9D?trackAsin=B08P6S83PK
                # https://music.amazon.ca/albums/B08P688B62
                # Having both Asin and 3dAsin substitution
                # has 360RA spatial audio, but no UHD
                "try3dAsinSubstitution": True if force_3d else False,
                "tryAsinSubstitution": True,
            },
        )
        resp_dict = response.json()

        if (
            response.status_code != 200
            or resp_dict["contentResponseList"][0]["contentResponseStatusCode"]
            != "SUCCESS"
        ):
            raise Exception(
                f"Failed to get track manifest: {response.status_code} {response.text}"
            )
            # continue

        # return xmltodict.parse(resp_dict["contentResponseList"][0]["manifest"])
        # yield from self.parse_from_content_responses(resp_dict["contentResponseList"])
        result: list[dict] = resp_dict.get("contentResponseList", [])
        return result
        # raise RuntimeError(f"Failed to get track manifest for {asins}")

    def get_license_response(self, asin: str, challenge: str, drm_type: typing.Optional[str] = "WIDEVINE") -> str:
        """
        Retrieve a License Response with a License Challenge.

        Args:
            asin: The ASIN of the item.
            challenge: A base64 encoded Widevine challenge.

        Returns:
            The response from the license server.

        Valid DRM types:

        `WIDEVINE_ENTITLEMENT`, `PLAYREADY`, `FAIRPLAY`, `WIDEVINE`

        Entitlement is not possible without the proper widevine device, 9480
        """
        response = self.post(
            url=f"https://music.amazon.{self.credentials.account_region.domain_tld}/{self.credentials.account_region.region.name}/api/dmls/getLicenseForPlaybackV2",
            data={
                "DrmType": str(drm_type),
                "appInfo": {
                    "musicAgent": f"Harley/{self.harley_version} Harley/{self.application_version} ( {str(uuid.uuid4())} {asin} )"
                },
                "deviceToken": {
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceTypeId": AmazonMobileApplication.MUSIC.device_type,
                },
                "licenseChallenge": challenge,
                "persistent": False,
            },
            headers={
                "User-Agent": self.USER_AGENT,
                "X-Amz-requestid": str(uuid.uuid4()),
                "X-Amz-Target": "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getLicenseForPlaybackV2",
                "Origin": f"https://music.amazon.{self.credentials.account_region.domain_tld}",
                "Referer": f"https://music.amazon.{self.credentials.account_region.domain_tld}/",
            },
        )

        if response.status_code != 200:
            raise ValueError(
                f"Failed to get license: {response.status_code} {response.text}"
            )
        resp = response.json()
        return resp["license"]

    # Shortcuts

    def get_track_manifest(
        self, track_asin: str, *args, **kwargs
    ):
        return next(
            self.get_tracks_manifest((track_asin,), *args, **kwargs),
            (None, None),
        )

    def get_track_info(self, track_asin: str, *args, **kwargs):
        resp = self.get_metadata(track_asin, *args, **kwargs)["trackList"]
        if len(resp) > 1 or not resp:
            raise ValueError(f"Track metadata is {'not available' if not resp else 'invalid'}: {resp}")
        return resp[0]

    def get_album_info(self, album_asin: str, *args, **kwargs):
        resp = self.get_metadata(album_asin, *args, **kwargs)["albumList"]
        if len(resp) > 1 or not resp:
            raise ValueError(f"Album metadata is {'not available' if not resp else 'invalid'}: {resp}")

        return resp[0]

    def get_artist_info(self, artist_asin: str, *args, **kwargs):
        resp = self.get_metadata(artist_asin, *args, **kwargs)["artistList"]
        if len(resp) > 1 or not resp:
            raise ValueError(f"Artist metadata is {'not available' if not resp else 'invalid'}: {resp}")
        return resp[0]

    def get_track_xray(self, asin: str, region_to_use: AmazonRegion, parse_credits: typing.Optional[bool] = False):
        response = self.post(
            url=f"https://{str(self.credentials.account_region.region.name).lower()}.mobilemesk.skill.music.a2z.com/api/showXray/{asin}",
            add_default_stratus_headers=False,
            headers={
                "x-amzn-device-id": self.credentials.device_info.device_serial_number,
                "x-amzn-device-family": "MobileAndroid",
                "x-amzn-device-manufacturer": "Google",
                "x-amzn-device-model": "Pixel 5",
                "x-amzn-device-language": region_to_use.locale,
                "x-amzn-device-height": "2560",
                "x-amzn-device-width": "1440",
                "x-amzn-device-scale": "3.5",
                "x-amzn-application-version": self.application_version,
                "x-amzn-os-version": "11",
                "x-amzn-device-time-zone": "America/Toronto",
                "x-amzn-timestamp": f"{time.time_ns() // 1_000_000}",
                "x-amzn-user-agent": self.APP_USER_AGENT,
                "x-amzn-device-type-id": AmazonMobileApplication.MUSIC.device_type,
                "x-amzn-request-id": str(uuid.uuid4()).lower(),
                "x-amzn-authentication": json.dumps(
                    {
                        "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
                        "accessToken": f"{self.credentials.access_token}",
                    }
                ),
                "x-amzn-session-id": self.credentials.website_cookies["session-id"],
                # "x-amzn-feature-flags": "includeArtistRefinements",
                "content-type": "application/json; charset=utf-8",
                "accept-encoding": "gzip",
                "user-agent": "okhttp/4.10.0",
            },
            data={
                # "id": asin,
                "assetType": "AUDIO",
                "swipeablePageConfig": json.dumps(
                    {
                        "interface": "Touch.SwipeablePagesTemplateInterface.v1_0.SwipeablePagesClientInformation",
                        "isChartsV3Enabled": True,
                        "isStageEnabled": False,
                    }
                ),
            },
        )

        # LOGGER.debug(json.dumps(response.json(), indent=3))

        resp_dict = dict(response.json())

        if parse_credits:
            return self.parse_credits_from_xray(resp_dict)

        return resp_dict

    @staticmethod
    def proper_credits_names():
        """Some credit names are not formatted correctly, this can be used to fix them."""
        return {
            "Performed By": "Performer",
            "Written By": "Lyricist",
            "Produced By": "Producer",
            "Music Publisher": "Publisher"
        }

    @staticmethod
    def parse_credits_from_xray(response: dict):
        credits_mapping: dict[str, list[str]] = {}
        for method in response.get("methods", []):
            if not str(method.get("interface", "")).endswith(
                "CreateAndBindManagedContainerMethod"
            ):
                # print("not CreateAndBindManagedContainerMethod")
                continue
            for page in method.get("template", {}).get("pages", []):
                if not str(page.get("interface", "")).endswith("ScrollableListElement"):
                    # print("not ScrollableListElement")
                    continue
                if str(page.get("label", {}).get("title")) != "CREDITS":
                    # print("label title not CREDITS")
                    continue

                for page_element in page.get("elements", []):
                    if not str(page_element.get("interface", "")).endswith(
                        "VerticalContainerElement"
                    ):
                        continue
                    credit_name: str = ""
                    people_names: list[str] = []

                    for container_element in page_element.get("elements", []):
                        if str(container_element.get("interface", "")).endswith(
                            "LabelElement"
                        ):
                            raw_credit_name = str(
                                "".join(
                                    re.findall(r"[A-Z][^A-Z]*", container_element["text"])
                                )
                            ).title()
                            credit_name = (
                                AmazonMusicMobileAPI.proper_credits_names().get(
                                    raw_credit_name, raw_credit_name
                                )
                            )

                        if str(container_element.get("interface", "")).endswith(
                            "ClickableTextElement"
                        ):
                            people_names.append(container_element["text"])

                    if not (credit_name and people_names):
                        continue

                    names = credits_mapping.get(credit_name, [])
                    names.extend(people_names)
                    # Remove duplicate names
                    names = sorted(
                        set(names),
                        key=names.index
                    )

                    credits_mapping.update({credit_name: names})

        return credits_mapping

    @staticmethod
    def parse_from_content_responses(content_responses: list[dict[str, typing.Any]]):
        for content_response in content_responses:
            content_identifier = content_response.get("contentIdentifier", {})
            if not (content_identifier or isinstance(content_identifier, dict)):
                raise ValueError(type(content_identifier))

            if content_identifier.get("identifierType") != "ASIN":
                raise ValueError(
                    f"{content_identifier.get('identifierType')} is not an ASIN!"
                )
            asin = str(content_identifier.get("identifier", ""))

            manifest = None
            if content_response.get("contentResponseStatusCode") == "SUCCESS":
                manifest = ElementTree.fromstring(
                    re.sub(
                        r'xmlns="[^"]+"',
                        "",
                        content_response.get("manifest", ""),
                        count=1,
                    )
                )
            # import pprint
            # pprint.pprint(xmltodict.parse(content_response.get("manifest", "")))

            yield asin, manifest
        return

    @staticmethod
    def _create_httpx_session():
        default_headers = {
            "User-Agent": AmazonMusicMobileAPI.USER_AGENT,
            "Accept-Language": "en-US",
            "Accept-Encoding": "gzip",
            "x-requested-with": "com.amazon.mp3",
        }

        session = httpx.Client(
            headers=default_headers,
            follow_redirects=True,
        )
        return session

    @classmethod
    def register(
        cls,
        application: AmazonMobileApplication,
        selected_region: AmazonRegion,
        authorization_code: str,
        code_verifier: bytes,
        serial: str,
    ):
        """Registers a dummy Amazon device for Amazon Music.

        Args:
            authorization_code: The code given after a successful authorization
            code_verifier: The verifier code from authorization
            domain: The top level domain of the requested Amazon server (e.g. com).
            serial: The device serial

        Returns:
            An instance of AmazonMusicMobileAPI, with the credentials attacted to the instance.

        """

        device_name = f"ripperino {os.urandom(16).hex()} Android Device (MP3)"
        LOGGER.debug(f"Registering device {device_name} with serial {serial}")

        body = {
            "requested_token_type": [
                "bearer",
                "mac_dms",
                "website_cookies",
                "store_authentication_cookie",
            ],
            "cookies": {"website_cookies": [], "domain": f".amazon.{selected_region.domain_tld}"},
            "registration_data": {
                "domain": "Device",
                "app_version": cls.application_version,
                "device_serial": serial,
                "device_type": application.device_type,
                "device_name": device_name,
                "os_version": "11",
                "software_version": "523160014",
                "device_model": "Pixel 5",
                "app_name": application.official_name,
            },
            "auth_data": {
                "client_id": cls._build_client_id(serial, application),
                "authorization_code": authorization_code,
                "code_verifier": code_verifier.decode(),
                "code_algorithm": "SHA-256",
                "client_domain": "DeviceLegacy",
                # "client_domain": "Device",
            },
            "requested_extensions": ["device_info", "customer_info"],
        }

        resp = httpx.post(f"https://api.amazon.{selected_region.domain_tld}/auth/register", json=body)

        LOGGER.debug(json.dumps(resp.json(), indent=4))
        resp_json = resp.json()
        if resp.status_code != 200:
            raise ValueError(resp_json)
        # pprint.pprint(vars(resp))

        success_response = resp_json["response"]["success"]

        tokens = dict(success_response["tokens"])
        adp_token = tokens["mac_dms"]["adp_token"]
        device_private_key = tokens["mac_dms"]["device_private_key"]
        pem_prefix = "-----BEGIN RSA PRIVATE KEY-----\n"
        pem_suffix = "\n-----END RSA PRIVATE KEY-----"
        if not str(device_private_key).startswith(
            pem_prefix
        ) and not str(device_private_key).endswith(pem_suffix):
            key = RSA.import_key(base64.b64decode(str(device_private_key)))
            device_private_key = rsa.PrivateKey.load_pkcs1(key.export_key("PEM"))
        else:
            key = rsa.PrivateKey.load_pkcs1(device_private_key)

        store_authentication_cookie = tokens["store_authentication_cookie"]
        access_token = tokens["bearer"]["access_token"]
        refresh_token = tokens["bearer"]["refresh_token"]
        expires_s = int(tokens["bearer"]["expires_in"])
        expires = datetime.utcnow() + timedelta(seconds=expires_s)

        extensions = success_response["extensions"]
        device_info = AmazonMusicDevice(**dict(extensions["device_info"]))
        customer_info = dict(extensions["customer_info"])

        website_cookies = {
            cookie["Name"]: str(cookie["Value"]).replace(r'"', r"")
            for cookie in tokens.get("website_cookies", [{}])
        }

        credentials = AmazonMusicMobileAPICredentials(
            adp_token=adp_token,
            device_private_key=device_private_key,
            access_token=access_token,
            refresh_token=refresh_token,
            expires=expires,
            website_cookies=website_cookies,
            store_authentication_cookie=store_authentication_cookie,
            device_info=device_info,
            customer_info=customer_info,
        )

        return cls(credentials)

    @staticmethod
    def _build_client_id(
        serial: str, app: typing.Optional[AmazonMobileApplication] = None
    ) -> str:
        if app is not None:
            device_type = app.device_type
        else:
            device_type = AmazonMobileApplication.MUSIC
        client_id = serial.encode() + f"#{device_type}".encode("utf-8")
        return client_id.hex()

    @staticmethod
    def _build_init_cookies() -> dict[str, str]:
        """Build initial cookies to prevent captcha in most cases."""

        frc = secrets.token_bytes(313)
        frc = base64.b64encode(frc).decode("ascii").rstrip("=")
        amzn_app_id = "MAPAndroidLib-1.3.4028.0"

        map_md = {
            "device_registration_data": {"software_version": "130050002"},
            "app_identifier": {
                "package": "com.amazon.mp3",
                "SHA-256": [
                    "2f19adeb284eb36f7f07786152b9a1d14b21653203ad0b04ebbf9c73ab6d7625"
                ],
                # https://www.apkmirror.com/apk/amazon-mobile-llc/amazon-music-discover-songs/amazon-music-discover-songs-22-15-12-release/amazon-music-songs-podcasts-22-15-12-4-android-apk-download/
                "app_version": "523160014",
                "app_version_name": AmazonMusicMobileAPI.application_version,
                "app_sms_hash": "QGCBba+brC5",
                "map_version": amzn_app_id,
            },
            "app_info": {
                "auto_pv": 0,
                "auto_pv_with_smsretriever": 0,
                "smartlock_supported": 0,
                "permission_runtime_grant": 2,
            },
            "device_user_dictionary": [],  # maybe adding the email would help bypass captcha
        }

        map_md = json.dumps(map_md)
        map_md = base64.b64encode(map_md.encode()).decode().rstrip("=")

        return {"frc": frc, "map-md": map_md, "amzn-app-id": amzn_app_id}

    @staticmethod
    def _build_oauth_url(
        domain: str,
        code_verifier: bytes,
        application: AmazonMobileApplication,
        selected_region: AmazonRegion,
        serial: typing.Optional[str] = None,
    ) -> tuple[str, str]:
        """Builds the url to login to Amazon Music."""

        serial = (
            serial or "PIXEL5" + build_device_serial()
        )  # requires some random model name at the start
        client_id = AmazonMusicMobileAPI._build_client_id(serial, application)
        code_challenge = create_s256_code_challenge(code_verifier)

        LOGGER.debug("device serial: %s", serial)
        LOGGER.debug("client id: %s", client_id)

        base_url = f"https://www.amazon.{domain}/ap/signin"
        return_to = f"https://www.amazon.{domain}/ap/maplanding"

        oauth_params = {
            "openid.pape.max_auth_age": "0",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "accountStatusPolicy": "P1",
            "language": selected_region.locale,
            "openid.return_to": return_to,
            "openid.assoc_handle": application.assoc_handle,
            "openid.oa2.response_type": "code",
            "openid.mode": "checkid_setup",
            "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
            "openid.oa2.code_challenge_method": "S256",
            "openid.ns.oa2": f"http://www.amazon.{domain}/ap/ext/oauth/2",
            "openid.oa2.code_challenge": code_challenge,
            "openid.oa2.scope": "device_auth_access",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.oa2.client_id": f"device:{client_id}",
            "disableLoginPrepopulate": "0",
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "forceMobileLayout": "true",  # custom, unsure if required by azm or is useless
        }
        if (
            selected_region.region is not AmazonContinent.NA 
            and selected_region.country not in ("AU")
        ) :
            # TODO, find which countries that require to login into prime video first
            # NOTE: amz music australia hates the marketplace id in the oauth url (404)
            oauth_params.update({"marketPlaceId": selected_region.marketplace_id})

        return f"{base_url}?{urlencode(oauth_params)}", serial

    @staticmethod
    def _now_to_unix_ms() -> int:
        return math.floor(datetime.now().timestamp() * 1000)

    @classmethod
    def _get_app_metadata(cls) -> str:
        """
        Returns json-formatted metadata to simulate sign-in from an Android Amazon Music app.
        """

        meta_dict = {
            "metrics": {
                "el": 0,
                "script": 0,
                "h": 1,
                "batt": 0,
                "perf": 0,
                "auto": 0,
                "tz": 0,
                "fp2": 0,
                "lsubid": 0,
                "browser": 0,
                "capabilities": 1,
                "gpu": 0,
                "dnt": 0,
                "math": 0,
                "tts": 0,
                "input": 1,
                "canvas": 0,
                "captchainput": 0,
                "pow": 0,
            },
            "start": cls._now_to_unix_ms(),
            "interaction": {
                "clicks": 1,
                "touches": 1,
                "keyPresses": 33,
                "cuts": 0,
                "copies": 0,
                "pastes": 0,
                "keyPressTimeIntervals": [168, 343, 131, 1118, 92, 192, 205, 98, 144],
                "mouseClickPositions": ["74,294"],
                "keyCycles": [16, 10, 8, 7, 8, 13, 11, 12, 17, 12],
                "mouseCycles": [16],
                "touchCycles": [],
            },
            "scripts": {
                "dynamicUrls": [
                    "https://images-na.ssl-images-amazon.com/images/I/31YXrY93hfL.js",
                    "https://images-na.ssl-images-amazon.com/images/I/61NeHXhGwSL._RC|11Y+5x+kkTL.js,01qkmZhGmAL.js,71-8cBvmf4L.js_.js?AUIClients/MusicBlackAndBlueAndroidSkin&amp;KK9dlo3A#mobile.412402-T1.412405-T1",
                    "https://images-na.ssl-images-amazon.com/images/I/21ZMwVh4T0L._RC|21OJDARBhQL.js,218GJg15I8L.js,31lucpmF4CL.js,2119M3Ks9rL.js,51X7BnRF64L.js_.js?AUIClients/AuthenticationPortalAssets&amp;QmmAyoMU#mobile.194821-T1",
                    "https://images-na.ssl-images-amazon.com/images/I/01wGDSlxwdL.js?AUIClients/AuthenticationPortalInlineAssets",
                    "https://images-na.ssl-images-amazon.com/images/I/41XHAz6BnWL.js?AUIClients/CVFAssets#mobile",
                    "https://images-na.ssl-images-amazon.com/images/I/818jIy8T6BL.js?AUIClients/SiegeClientSideEncryptionAUI",
                    "https://images-na.ssl-images-amazon.com/images/I/31IwoCo8XiL.js?AUIClients/AmazonUIFormControlsJS#mobile",
                    "https://images-na.ssl-images-amazon.com/images/I/819PzLyzJVL.js?AUIClients/FWCIMAssets",
                    "https://images-na.ssl-images-amazon.com/images/I/7195RJQQs1L.js?AUIClients/ACICAssets",
                    "https://static.siege-amazon.com/prod/profiles/AuthenticationPortalSigninNA.js",
                ],
                "inlineHashes": [
                    -1746719145,
                    776692753,
                    -1106742843,
                    -314038750,
                    172381973,
                    1292021430,
                    452512068,
                    928554431,
                    318224283,
                    -24495950,
                    1506353394,
                    700743993,
                    4606827,
                    -1611905557,
                    1800521327,
                    2118020403,
                    1532181211,
                    1502018687,
                    841624991,
                    -1677151674,
                ],
                "elapsed": 28,
                "dynamicUrlCount": 10,
                "inlineHashesCount": 20,
            },
            "history": {"length": 2},
            "battery": {},
            "performance": {
                "timing": {
                    "navigationStart": cls._now_to_unix_ms(),
                    "unloadEventStart": 0,
                    "unloadEventEnd": 0,
                    "redirectStart": 0,
                    "redirectEnd": 0,
                    "fetchStart": cls._now_to_unix_ms(),
                    "domainLookupStart": cls._now_to_unix_ms(),
                    "domainLookupEnd": cls._now_to_unix_ms(),
                    "connectStart": cls._now_to_unix_ms(),
                    "connectEnd": cls._now_to_unix_ms(),
                    "secureConnectionStart": cls._now_to_unix_ms(),
                    "requestStart": cls._now_to_unix_ms(),
                    "responseStart": cls._now_to_unix_ms(),
                    "responseEnd": cls._now_to_unix_ms(),
                    "domLoading": cls._now_to_unix_ms(),
                    "domInteractive": cls._now_to_unix_ms(),
                    "domContentLoadedEventStart": cls._now_to_unix_ms(),
                    "domContentLoadedEventEnd": cls._now_to_unix_ms(),
                    "domComplete": cls._now_to_unix_ms(),
                    "loadEventStart": cls._now_to_unix_ms(),
                    "loadEventEnd": cls._now_to_unix_ms(),
                }
            },
            "automation": {
                "wd": {"properties": {"document": [], "window": [], "navigator": []}},
                "phantom": {"properties": {"window": []}},
            },
            "end": cls._now_to_unix_ms() + 29151, # add some delay
            "timeZone": -5,
            "flashVersion": None,
            "plugins": "unknown||412-732-732-24-*-*-*",
            "dupedPlugins": "unknown||412-732-732-24-*-*-*",
            "screenInfo": "412-732-732-24-*-*-*",
            "userAgent": AmazonMusicMobileAPI.USER_AGENT,
            "webDriver": False,
            "capabilities": {
                "css": {
                    "textShadow": 1,
                    "WebkitTextStroke": 1,
                    "boxShadow": 1,
                    "borderRadius": 1,
                    "borderImage": 1,
                    "opacity": 1,
                    "transform": 1,
                    "transition": 1,
                },
                "js": {
                    "audio": True,
                    "geolocation": True,
                    "localStorage": "supported",
                    "touch": True,
                    "video": True,
                    "webWorker": True,
                },
                "elapsed": 2,
            },
            "gpu": {
                "vendor": "ARM",
                "model": "Mali-T880",
                "extensions": [
                    "ANGLE_instanced_arrays",
                    "EXT_blend_minmax",
                    "EXT_float_blend",
                    "EXT_sRGB",
                    "OES_element_index_uint",
                    "OES_fbo_render_mipmap",
                    "OES_standard_derivatives",
                    "OES_vertex_array_object",
                    "WEBGL_compressed_texture_astc",
                    "WEBGL_compressed_texture_etc",
                    "WEBGL_compressed_texture_etc1",
                    "WEBGL_debug_renderer_info",
                    "WEBGL_debug_shaders",
                    "WEBGL_depth_texture",
                    "WEBGL_lose_context",
                    "WEBGL_multi_draw",
                ],
            },
            "dnt": None,
            "math": {
                "tan": "-1.4214488238747245",
                "sin": "0.8178819121159085",
                "cos": "-0.5753861119575491",
            },
            "form": {
                "ap-credential-autofill-hint": {
                    "clicks": 0,
                    "touches": 0,
                    "keyPresses": 0,
                    "cuts": 0,
                    "copies": 0,
                    "pastes": 0,
                    "keyPressTimeIntervals": [],
                    "mouseClickPositions": [],
                    "keyCycles": [],
                    "mouseCycles": [],
                    "touchCycles": [],
                    "width": 0,
                    "height": 0,
                    "totalFocusTime": 0,
                    "prefilled": False,
                },
                "password": {
                    "clicks": 1,
                    "touches": 1,
                    "keyPresses": 69,
                    "cuts": 0,
                    "copies": 0,
                    "pastes": 0,
                    "keyPressTimeIntervals": [
                        168,
                        344,
                        131,
                        1117,
                        92,
                        193,
                        203,
                        100,
                        143,
                    ],
                    "mouseClickPositions": ["41,23.053558349609375"],
                    "keyCycles": [17, 11, 8, 8, 9, 14, 11, 14, 17, 13],
                    "mouseCycles": [16],
                    "touchCycles": [],
                    "width": 346.0000305175781,
                    "height": 43.000003814697266,
                    "totalFocusTime": 0,
                    "prefilled": False,
                },
            },
            "canvas": 0,
            "token": {"isCompatible": True, "pageHasCaptcha": 0},
            "auth": {"form": {"method": "post"}},
            "errors": [],
            "version": "4.0.0",
        }
        return json.dumps(meta_dict, separators=(",", ":"))

    def refresh_access_token(self, force: bool = False) -> None:
        """
        Refresh the access token

        """
        if force or self.credentials.access_token_expired:
            if self.credentials.refresh_token is None:
                message = "No refresh token found. Can't refresh access token."
                LOGGER.critical(message)
                raise Exception(message)

            body = {
                "app_name": "Amazon Music",
                "app_version": self.application_version,
                "source_token": self.credentials.refresh_token,
                "requested_token_type": "access_token",
                "source_token_type": "refresh_token",
            }

            resp = self.post(
                f"https://api.amazon.{self.credentials.account_region.domain_tld}/auth/token",
                data=body,
                sign=False,
            )
            resp_dict = resp.json()
            resp.raise_for_status()

            expires = datetime.utcnow() + timedelta(
                seconds=int(resp_dict["expires_in"])
            )

            self.credentials.access_token = resp_dict["access_token"]
            self.credentials.expires = expires

        else:
            LOGGER.info(
                "Access Token not expired. No refresh necessary. "
                "To force refresh please use force=True"
            )

    def _apply_signing_auth_flow(self, request: httpx.Request) -> None:
        date = datetime.utcnow().isoformat("T") + "Z"
        body = request.content.decode("utf-8")

        data = f"{request.method}\n{request.url.raw_path.decode()}\n{date}\n{body}\n{self.credentials.adp_token}"

        cipher = rsa.pkcs1.sign(data.encode(), self.credentials.device_private_key, "SHA-256")
        signed_encoded = base64.b64encode(cipher)

        signature = f"{signed_encoded.decode()}:{date}"

        headers = {
            "x-adp-token": self.credentials.adp_token,
            "x-adp-alg": "SHA256withRSA:1.0",
            "x-adp-signature": signature,
        }

        # LOGGER.debug(headers)

        request.headers.update(headers)
        LOGGER.debug("Signing auth flow applied to request")

    def _apply_cookies_auth_flow(self, request: httpx.Request) -> None:
        if not self.credentials:
            raise ValueError("You must login first!")

        httpx.Cookies(self.credentials.website_cookies).set_cookie_header(request)
        LOGGER.debug("Cookies auth flow applied to request")

    def _list_devices(self):
        devices_resp = self.post(
            url=f"https://music.amazon.{self.credentials.account_region.domain_tld}/{self.credentials.account_region.region.name}/api/stratus/",
            data={
                "customerId": None,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": self.credentials.device_info.device_type,
            },
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.listDevicesByCustomerId",
                "x-amzn-requestid": str(uuid.uuid4()),
            },
        )
        LOGGER.debug(
            f"{devices_resp.status_code} {json.dumps(devices_resp.json(), indent=4)}"
        )
        return devices_resp

    def authorize_device(
        self,
        device_serial: typing.Optional[str] = None,
        home_region: typing.Optional[str] = None,
        domain: typing.Optional[str] = None,
    ):
        device_type = AmazonMobileApplication.MUSIC.device_type

        if not device_serial:
            device_serial = self.credentials.device_info.device_serial_number

        if not home_region:
            home_region = self.credentials.account_region.region.name

        if not domain:
            domain = self.credentials.account_region.domain_tld

        auth_device_resp = self.post(
            url=f"https://music.amazon.{domain}/{home_region}/api/stratus/",
            data={
                "capabilities": [
                    "RETRIEVE_OWNED_CONTENT",
                    "RETRIEVE_ROBIN_CONTENT",
                    # "RETRIEVE_MERCURY_CONTENT",
                    # "RETRIEVE_NIGHTWING_CONTENT",
                ],
                "customerInfo": {
                    "customerId": "",  # it is not set, but it is required
                    "deviceId": device_serial,
                    "deviceType": device_type,
                },
                "deviceId": device_serial,
                "deviceType": device_type,
                "targetDeviceId": device_serial,
                "targetDeviceType": device_type,
            },
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.authorizeDevice",
                "x-amzn-RequestId": str(uuid.uuid4()),
            },
        )
        LOGGER.debug(auth_device_resp.content)
        auth_device_resp_json = auth_device_resp.json()
        LOGGER.debug(
            f"{auth_device_resp.status_code} {json.dumps(auth_device_resp_json, indent=4)}"
        )
        return auth_device_resp

    def retrieve_capability(self):
        response = self.post(
            url=f"https://music.amazon.{self.credentials.account_region.domain_tld}/{self.credentials.account_region.region.name}/api/stratus/",
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.retrieveCapability",
                "x-amzn-requestid": str(uuid.uuid4()),
            },
            data={
                "capabilityTypes": [
                    "RETRIEVE_ROBIN_CONTENT",
                    # "RETRIEVE_OWNED_CONTENT",
                ],
                "customerId": self.credentials.customer_id, # None,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
            },
        )
        return dict(response.json())

    def retrieve_customer_home(self):
        resp = self.post(
            url=f"https://music.amazon.{self.credentials.account_region.domain_tld}/{self.credentials.account_region.region.name}/api/stratus/",
            data={
                "customerId": self.credentials.customer_id,  # it is not set, but it is required
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": self.credentials.device_info.device_type,
                "ipAddress": None,
                "sessionId": None,
            },
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.retrieveCustomerHome",
                "x-amzn-RequestId": str(uuid.uuid4()),
            },
        )
        return dict(resp.json())

    @functools.lru_cache()
    def get_account_status(self):
        response = self.post(
            url=f"https://music.amazon.com/{self.credentials.customer_info['home_region']}/api/stratus/",
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.isAccountValid",
                "x-amzn-requestid": str(uuid.uuid4()),
            },
            data={
                "customerId": self.credentials.customer_id,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
                "ipAddress": None,
                "verbose": True,
            },
        )
        return dict(response.json())
    
    def get_account_subscription_tier(self, resp: typing.Optional[dict] = None):
        if not resp:
            resp = self.get_account_status()

        customer_benefits = resp.get("customerAccount", {}).get("customerBenefits", {})
        if customer_benefits.get("HAWKFIRE_KATANA_ACCESS") == "true" and customer_benefits.get("HAWKFIRE_PLAYBACK_ACCESS") == "true":
            return AmazonMusicTier.UNLIMITED
        elif customer_benefits.get("PRIME_MUSIC_BROWSE") == "true" and customer_benefits.get("PRIME_MUSIC_CONTENT_ACCESS") == "true":
            return AmazonMusicTier.PRIME
        return AmazonMusicTier.FREE
        

    def _deauthorize_device(self, device_serial: typing.Optional[str]):
        # remove device from authorized devices in amazon music
        return

    @staticmethod
    def _exteral_login(
        oauth_url: str,
        application: AmazonMobileApplication,
        oauth_flow_callback: typing.Optional[typing.Callable[[str, str], str]] = None,
    ):
        if oauth_flow_callback:
            callback_url = oauth_flow_callback(oauth_url, application.official_name)
        else:
            print(
                "\n"
                "=== Amazon Music login (browser) ===\n"
                "\n"
                "1. Open this URL in your browser (Ctrl+click if your terminal supports it):\n"
                f"\n{oauth_url}\n"
                "\n"
                "2. Sign in with your Amazon account.\n"
                "   You may need to enter your password twice and complete a CAPTCHA.\n"
                "\n"
                "3. After login, the browser will show a \"not found\" / error page — that is normal.\n"
                "\n"
                "4. Copy the full URL from the address bar and paste it below.\n"
                f"\n"
                f"   (Logging into {application.official_name} as required by the module.)\n"
            )
            callback_url = input("\nPaste the URL from your browser after login:\n").strip()

        if not callback_url or not str(callback_url).strip():
            raise ValueError("Amazon Music login cancelled: no callback URL provided.")

        response_url = httpx.URL(str(callback_url).strip())
        parsed_url = parse_qs(response_url.query.decode())

        if "openid.oa2.authorization_code" not in parsed_url:
            raise ValueError(
                "Amazon Music login failed: pasted URL does not contain an authorization code.\n"
                "Copy the full address bar URL from the page shown right after login (maplanding)."
            )

        authorization_code = parsed_url["openid.oa2.authorization_code"][0]
        return authorization_code

    @classmethod
    def _internal_login(cls, session: httpx.Client, oauth_url: str, email: str, password: str):
        oauth_resp = session.get(oauth_url)
        LOGGER.debug(oauth_resp)
        oauth_soup = get_soup(oauth_resp)

        login_inputs = get_inputs_from_soup(oauth_soup)
        login_inputs["email"] = email
        login_inputs["password"] = password
        metadata = cls._get_app_metadata(
            # user_agent=cls.USER_AGENT, oauth_url=oauth_url
        )
        login_inputs["metadata1"] = encrypt_metadata(metadata)
        method, url = get_next_action_from_soup(oauth_soup, {"name": "signIn"})

        login_resp = session.request(method, url, data=login_inputs)
        login_soup = get_soup(login_resp)

        # check for captcha
        def check_for_captcha(soup: BeautifulSoup) -> bool:
            """Checks a Amazon login page for a captcha form."""

            captcha = soup.find("img", alt=lambda x: x and ("CAPTCHA" in x or "captcha" in x))
            return True if captcha else False
        
        def extract_captcha_url(soup: BeautifulSoup) -> str | None:
            """Returns the captcha url from a Amazon login page."""

            captcha = soup.find("img", alt=lambda x: x and ("CAPTCHA" in x or "captcha" in x))
            return captcha["src"] if captcha else None

        while check_for_captcha(login_soup):
            captcha_url = extract_captcha_url(login_soup)
            if not captcha_url:
                continue
            guess = default_captcha_callback(captcha_url)

            inputs = get_inputs_from_soup(login_soup)
            inputs["guess"] = guess
            inputs["use_image_captcha"] = "true"
            inputs["use_audio_captcha"] = "false"
            inputs["showPasswordChecked"] = "false"
            inputs["email"] = email
            inputs["password"] = password

            method, url = get_next_action_from_soup(login_soup, {"name": "signIn"})

            login_resp = session.request(method, url, data=inputs, timeout=20000)
            print(vars(login_resp))
            login_soup = get_soup(login_resp)

        # check for choice mfa
        # https://www.amazon.de/ap/mfa/new-otp
        while check_for_choice_mfa(login_soup):
            inputs = get_inputs_from_soup(login_soup)
            for node in login_soup.select("div[data-a-input-name=otpDeviceContext]"):
                # auth-TOTP, auth-SMS, auth-VOICE
                if "auth-TOTP" in node["class"]:
                    inp_node = node.find("input")
                    inputs[inp_node["name"]] = inp_node["value"]

            method, url = get_next_action_from_soup(login_soup)

            login_resp = session.request(method, url, data=inputs)
            print(vars(login_resp))
            login_soup = get_soup(login_resp)

        # check for mfa (otp_code)
        while check_for_mfa(login_soup):
            otp_code = default_otp_callback()

            inputs = get_inputs_from_soup(login_soup)
            inputs["otpCode"] = otp_code
            inputs["mfaSubmit"] = "Submit"
            inputs["rememberDevice"] = "false"

            method, url = get_next_action_from_soup(login_soup)

            login_resp = session.request(method, url, data=inputs)
            print(vars(login_resp))
            login_soup = get_soup(login_resp)

        # check for cvf
        while check_for_cvf(login_soup):
            print(
                "Check your email or SMS for a code from Amazon and enter it in the below prompt."
            )
            print(login_soup.find(name="span", attrs={"class": "transaction-approval-word-break"}))
            cvf_code = default_cvf_callback()

            inputs = get_inputs_from_soup(login_soup)

            method, url = get_next_action_from_soup(login_soup)

            login_resp = session.request(method, url, data=inputs)
            LOGGER.debug("cvf resp: %s, %s", login_resp, login_resp.text)
            login_soup = get_soup(login_resp)

            inputs = get_inputs_from_soup(login_soup)
            inputs["action"] = "code"
            inputs["code"] = cvf_code

            method, url = get_next_action_from_soup(login_soup)

            login_resp = session.request(method, url, data=inputs)
            login_soup = get_soup(login_resp)

        # check for approval alert
        while check_for_approval_alert(login_soup):
            default_approval_alert_callback()

            # url = login_soup.find(id="resend-approval-link")["href"]
            url = login_resp.url

            login_resp = session.get(url)
            login_soup = get_soup(login_resp)

            while login_soup.find(
                "span", {"class": "transaction-approval-word-break"}
            ):  # a-size-base-plus transaction-approval-word-break a-text-bold
                login_resp = session.get(url)
                login_soup = get_soup(login_resp)
                LOGGER.info("still waiting for redirect")

        # print(login_resp.url)
        if b"openid.oa2.authorization_code" not in login_resp.url.query:
            raise Exception("Login failed. Please check the log.")

        authorization_code = extract_code_from_url(login_resp.url)
        LOGGER.debug(parse_qs(login_resp.url.query.decode()))
        return authorization_code

    @staticmethod
    def parse_for_app_config(response_text: str):
        return dict(
            json.loads(
                re.search(r"appConfig: ({.*}),", response_text, re.DOTALL).group(1)
            )
        )

# bruh

T = typing.TypeVar("T")


def divide_sequence(
    seq: typing.Sequence[T], size: typing.Optional[int] = None
) -> typing.Generator[typing.Sequence[T], None, None]:
    """Divide a sequence into chunks of size `size`"""
    if size is None:
        size = 5

    for index in range(0, len(seq), size):
        yield seq[index : index + size]
