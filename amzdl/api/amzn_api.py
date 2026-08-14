"""Local patches over the upstream Amazon Music API submodule. Subclasses the
read-only `AmazonMusicMobileAPI` to fix three touch points — forwarding the
OAuth callback through the JP Prime Video recursion, silencing the upstream
login banner that desyncs the UI redraw, and parsing track-credit role names
without the upstream `.title()` collapse so their camelCase word boundaries
survive for downstream UPPERCASE_SNAKE_CASE tagging."""

import contextvars
import typing

import amazonmusic.azapi as _az
from amazonmusic.azapi import AmazonMusicMobileAPI as _BaseAPI

_PROPER_CREDIT_NAMES = {
    "Performed By": "Performer",
    "Written By": "Lyricist",
    "Produced By": "Producer",
    "Music Publisher": "Publisher",
}

_oauth_callback: "contextvars.ContextVar[typing.Callable[[str, str], str] | None]" = (
    contextvars.ContextVar("amzn_oauth_flow_callback", default=None)
)

_LOGIN_BANNER_PREFIX = "Login confirmed for "


def _make_filtered_print(real_print):
    printer = real_print or print

    def _filtered_print(*args, **kwargs):
        if (
            args
            and isinstance(args[0], str)
            and args[0].startswith(_LOGIN_BANNER_PREFIX)
        ):
            return
        return printer(*args, **kwargs)

    return _filtered_print


class AmazonMusicMobileAPI(_BaseAPI):

    @classmethod
    def login_via_mobile(cls, *args, oauth_flow_callback=None, **kwargs):
        token = (
            _oauth_callback.set(oauth_flow_callback)
            if oauth_flow_callback is not None
            else None
        )
        real_print = _az.__dict__.get("print")
        _az.print = _make_filtered_print(real_print)
        try:
            return super().login_via_mobile(
                *args, oauth_flow_callback=oauth_flow_callback, **kwargs
            )
        finally:
            if real_print is None:
                _az.__dict__.pop("print", None)
            else:
                _az.print = real_print
            if token is not None:
                _oauth_callback.reset(token)

    @staticmethod
    def _exteral_login(oauth_url, application, oauth_flow_callback=None):
        if oauth_flow_callback is None:
            oauth_flow_callback = _oauth_callback.get()
        return _BaseAPI._exteral_login(oauth_url, application, oauth_flow_callback)

    @staticmethod
    def parse_credits_from_xray(response: dict):
        credits_mapping: dict[str, list[str]] = {}
        for method in response.get("methods", []):
            if not str(method.get("interface", "")).endswith(
                "CreateAndBindManagedContainerMethod"
            ):
                continue
            for page in method.get("template", {}).get("pages", []):
                if not str(page.get("interface", "")).endswith("ScrollableListElement"):
                    continue
                if str(page.get("label", {}).get("title")) != "CREDITS":
                    continue
                for page_element in page.get("elements", []):
                    if not str(page_element.get("interface", "")).endswith(
                        "VerticalContainerElement"
                    ):
                        continue
                    credit_name = ""
                    people_names: list[str] = []
                    for element in page_element.get("elements", []):
                        interface = str(element.get("interface", ""))
                        if interface.endswith("LabelElement"):
                            text = str(element.get("text", ""))
                            spaced = " ".join(w.title() for w in text.split())
                            credit_name = _PROPER_CREDIT_NAMES.get(spaced, text)
                        elif interface.endswith("ClickableTextElement"):
                            people_names.append(element["text"])
                    if not (credit_name and people_names):
                        continue
                    names = credits_mapping.get(credit_name, [])
                    names.extend(people_names)
                    credits_mapping[credit_name] = sorted(set(names), key=names.index)
        return credits_mapping
