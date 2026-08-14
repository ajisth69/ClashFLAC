import dataclasses
from datetime import datetime, timedelta
import typing
import enum
import rsa
import functools
import itertools

@dataclasses.dataclass(frozen=False, slots=True)
class AmazonMusicDevice:
    device_name: str
    """ The final name recorded on Amazon's backend"""
    device_serial_number: str
    """ The attributed generated serial number """
    device_type: str
    """ Always `A1DL2DVDQVK3Q` """

@dataclasses.dataclass(frozen=False, slots=True)
class AmazonMusicMobileAPICredentials:
    adp_token: str
    device_private_key: rsa.PrivateKey
    access_token: str
    refresh_token: str
    expires: datetime
    website_cookies: dict
    store_authentication_cookie: dict
    customer_info: dict
    device_info: AmazonMusicDevice
    customer_id: typing.Optional[str] = None
    tier: "AmazonMusicTier" = None
    account_region: "AmazonRegion" = None

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, creds_dict: dict):
        # deprecrate some configs
        creds_dict.pop("web_client_config", None)
        creds_dict.pop("tld", None)

        if account_region_dict := creds_dict.get("account_region"):
            account_region = AmazonRegion(**account_region_dict)
            creds_dict.update({"account_region": account_region})
        if device_dict := creds_dict.get("device_info"):
            device_dict = AmazonMusicDevice(**device_dict)
            creds_dict.update({"device_info": device_dict})

        inst = cls(**creds_dict)

        return inst

    @staticmethod
    def is_dict_of_instance(creds_to_test: dict):
        return all(name in creds_to_test for name in AmazonMusicMobileAPICredentials.__dataclass_fields__.keys())

    @property
    def access_token_expires(self) -> timedelta:
        return self.expires - datetime.now()

    @property
    def access_token_expired(self) -> bool:
        return self.expires < datetime.now()

class AmazonMusicTier(enum.Flag):
    FREE = enum.auto()
    PRIME = enum.auto()
    UNLIMITED = enum.auto()

    @property
    def internal_content_tiers(self):
        return {
            AmazonMusicTier.FREE: ["NIGHTWING_CONTENT", "OWNED_CONTENT"],
            AmazonMusicTier.PRIME: ["ROBIN_CONTENT", "SONIC_CONTENT", "NIGHTWING_CONTENT", "OWNED_CONTENT"],
            AmazonMusicTier.UNLIMITED: ["KATANA_CONTENT", "HAWKFIRE_CONTENT", "NIGHTWING_CONTENT", "OWNED_CONTENT"]
        }[self]

class AmazonContinent(enum.Enum):
    NA = enum.auto()
    EU = enum.auto()
    FE = enum.auto()

@dataclasses.dataclass(frozen=True, slots=True)
class AmazonRegion:
    """ A representation of a Amazon Music supported country """
    region: AmazonContinent
    """ either NA, FE or EU """
    country: str
    marketplace_id: str
    pretty_name: str
    locale: str
    domain_tld: str
    
    def to_dict(self):
        return dataclasses.asdict(self)
    
    @classmethod
    @functools.lru_cache
    def get_known_regions(cls):
        # NOTE: country > instance
        
        init_data: dict[str, list[tuple[str, str, str, str, str]]] = {
            "NA": [
                ("US", "ATVPDKIKX0DER", "United States of America", "en_US", "com"),
                ("CA", "A2EUQ1WTGCTBG2", "Canada", "en_CA", "ca"),
                ("MX", "A1AM78C64UM0Y8", "Mexico", "es_MX", "com.mx"),
                ("BR", "A2Q3Y263D00KWC", "Brazil", "es_BR", "com.br"),
                ("AR", "ATVPDKIKX0DER", "Argentina", "es_AR", "com"),
                ("BO", "ATVPDKIKX0DER", "Bolivia", "es_BO", "com"),
                ("CL", "ATVPDKIKX0DER", "Chile", "es_CL", "com"),
                ("CO", "ATVPDKIKX0DER", "Colombia", "es_CO", "com"),
                ("CR", "ATVPDKIKX0DER", "Costa Rica", "CR", "com"),
                ("DO", "ATVPDKIKX0DER", "Dominican Republic", "es_DO", "com"),
                ("EC", "ATVPDKIKX0DER", "Ecuador", "es_EC", "com"),
                ("SV", "ATVPDKIKX0DER", "El Salvador", "es_SV", "com"),
                ("GT", "ATVPDKIKX0DER", "Guatemala", "es_GT", "com"),
                ("HN", "ATVPDKIKX0DER", "Honduras", "es_HN", "com"),
                ("NI", "ATVPDKIKX0DER", "Nicaragua", "es_NI", "com"),
                ("PA", "ATVPDKIKX0DER", "Panama", "es_PA", "com"),
                ("PY", "ATVPDKIKX0DER", "Paraguay", "es_PY", "com"),
                ("UY", "ATVPDKIKX0DER", "Uruguay", "es_UY", "com"),
            ],
            "EU": [
                ("NL", "A1805IZSGTT6HS", "Netherlands", "nl_NL", "com"),
                ("IN", "A21TJRUUN4KGV", "India", "hi_IN", "in"), # special in terms of the marketplace id
                ("GB", "A1F83G8C2ARO7P", "United Kingdom", "en_GB", "co.uk"),
                ("ES", "A1RKKUPIHCS9HS", "Spain", "es_ES", "es"),
                ("FR", "A13V1IB3VIYZZH", "France", "fr_FR", "fr"),
                ("IT", "APJ6JRA9NG5V4", "Italy", "it_IT", "it"),
                ("DE", "A1PA6795UKMFR9", "Germany", "de_DE", "de"),
                ("AT", "A1PA6795UKMFR9", "Austria", "de_AT", "de"),

                ("BE", "ATVPDKIKX0DER", "Belgium", "fr_BE", "com"),
                ("BG", "ATVPDKIKX0DER", "Bulgaria", "bg_BG", "com"),
                ("CY", "ATVPDKIKX0DER", "Cyprus", "el_CY", "com"),
                ("CZ", "ATVPDKIKX0DER", "Czech Republic", "cs_CZ", "com"),
                ("EE", "ATVPDKIKX0DER", "Estonia", "et_EE", "com"),
                ("FI", "ATVPDKIKX0DER", "Finland", "fi_FI", "com"),
                ("GR", "ATVPDKIKX0DER", "Greece", "el_GR", "com"),
                ("HU", "ATVPDKIKX0DER", "Hungary", "hu_HU", "com"),
                ("IS", "ATVPDKIKX0DER", "Iceland", "is_IS", "com"),
                ("IE", "ATVPDKIKX0DER", "Ireland", "ga_IE", "com"),
                ("LV", "ATVPDKIKX0DER", "Latvia", "lv_LV", "com"),
                ("LI", "ATVPDKIKX0DER", "Liechtenstein", "de_LI", "com"),
                ("LT", "ATVPDKIKX0DER", "Lithuania", "lt_LT", "com"),
                ("LU", "ATVPDKIKX0DER", "Luxembourg", "fr_LU", "com"),
                ("MT", "ATVPDKIKX0DER", "Malta", "mt_MT", "com"),
                ("PL", "A1C3SOZRARQ6R3", "Poland", "pl_PL", "com"),
                ("PT", "ATVPDKIKX0DER", "Portugal", "pt_PT", "com"),
                ("SK", "ATVPDKIKX0DER", "Slovakia", "sk_SK", "com"),
                ("SE", "A2NODRKZP88ZB9", "Sweden", "sv_SE", "com"),
            ],
            "FE": [
                ("JP", "A1VC38T7YXB528", "Japan", "ja_JP", "co.jp"),
                ("AU", "A39IBJ37TRP1C6", "Australia", "en_AU", "com.au"),
                ("NZ", "A39IBJ37TRP1C6", "New Zealand", "en_NZ", "com.au"),
            ]
        }
        # Member must satisfy enum value set: [
            # US, ATVPDKIKX0DER,
            # GB, A1F83G8C2ARO7P,
            # DE, A1PA6795UKMFR9,
            # ES, A1RKKUPIHCS9HS,
            # JP, A1VC38T7YXB528,
            # FR, A13V1IB3VIYZZH,
            # IT, APJ6JRA9NG5V4
        # ]
        
        data = {
            country: cls(AmazonContinent[region_name], country, marketplace_id, pretty_name, locale, domain_tld)
            for region_name, regions in init_data.items()
            for (country, marketplace_id, pretty_name, locale, domain_tld) in regions
        }

        return data
    
    @classmethod
    def get_region_by_country(cls, country: str):
        selected_region = cls.get_known_regions().get(country)
        if not selected_region:
            raise TypeError(f"Requested country is not known! {country}")
        return selected_region
    
    @classmethod
    def get_available_regions_by_continent(cls, continent: str):
        def key_for_grouping_by_continent(inst: AmazonRegion):
            return inst.region
        
        for group_continent, items in itertools.groupby(cls.get_known_regions().values(), key=key_for_grouping_by_continent):
            if group_continent.name != continent:
                continue
            data = list(items)
            return data
        raise RuntimeError("No regions loaded?")
