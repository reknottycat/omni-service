"""Opt-in first-party portal enrollment; no session persistence or auto retries."""

from html.parser import HTMLParser
from http.cookiejar import CookieJar
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler, Request

from .models import ChannelError
from .telegram_tdlib import _validate_application


ORIGIN = "https://my.telegram.org"
APP_TITLE = "OmniService"
SHORT_NAME = "omniservicelogistics"
_FORM = {"app_title", "app_shortname", "app_platform", "app_desc"}


class _SameOrigin(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        pair = urlsplit(newurl)
        if req.get_method() != "GET" or (pair.scheme, pair.netloc) != ("https", "my.telegram.org"):
            raise ChannelError("developer_unexpected_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request_session():
    # Exactly one in-memory signing context from request to application readback.
    opener = build_opener(_SameOrigin(), HTTPCookieProcessor(CookieJar()))

    def fetch(path, payload=None):
        opts = {"Referer": ORIGIN + ("/apps" if path.startswith("/apps") else "/auth?to=apps")}
        body = None
        if payload is not None:
            opts.update({"Content-Type": "application/x-www-form-urlencoded",
                         "X-Requested-With": "fetch", "Origin": ORIGIN})
            body = urlencode(payload).encode()
        with opener.open(Request(ORIGIN + path, headers=opts, data=body), timeout=18) as reply:
            url = urlsplit(reply.geturl())
            if (url.scheme, url.netloc) != ("https", "my.telegram.org"):
                raise ChannelError("developer_unexpected_redirect")
            data = reply.read(262145)
            if len(data) > 262144:
                raise ChannelError("developer_portal_response_too_large")
            return url.path, data.decode("utf-8", "strict")

    return fetch


class _AppPage(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.has_create = False
        self.hidden = {}
        self.fields = set()
        self.desktop = False
        self.words = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        v = dict(attrs)
        if tag == "form":
            self.active = v.get("id") == "app_create_form"
            self.has_create |= self.active
        if self.active and tag in ("input", "select", "textarea") and v.get("name"):
            name = v["name"]
            self.fields.add(name)
            if v.get("type") == "hidden" and v.get("value"):
                self.hidden[name] = v["value"]
            self.desktop |= name == "app_platform" and v.get("value") == "desktop"

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False

    def handle_data(self, value):
        if value.strip():
            self.words.append(value.strip())

    def credentials(self):
        def field(label, pattern):
            hits = []
            for i, word in enumerate(self.words):
                if word.rstrip(":").casefold() == label:
                    if i + 1 < len(self.words) and re.fullmatch(pattern, self.words[i + 1]):
                        hits.append(self.words[i + 1])
            return hits[0] if len(hits) == 1 else None
        value, digest = field("app api_id", r"[0-9]{1,10}"), field("app api_hash", r"[a-fA-F0-9]{32}")
        if value is None or digest is None:
            return None
        return _validate_application(int(value), digest)


def enroll(phone, obtain_code, *, create=False, fetch=None, on_stage=lambda _: None):
    """Return validated app credentials only on receipt; code stays in memory."""
    if type(phone) is not str or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise ChannelError("developer_invalid_phone")
    fetch = _request_session() if fetch is None else fetch
    _, front = fetch("/auth?to=apps")
    if 'id="my_send_form"' not in front:
        raise ChannelError("developer_login_form_unverified")
    import json
    _, raw = fetch("/auth/send_password", {"phone": phone})
    try:
        state = json.loads(raw)
        challenge = state.get("random_hash") if type(state) is dict else None
    except ValueError:
        challenge = None
    if type(challenge) is not str or not re.fullmatch(r"[A-Za-z0-9_=+/.-]{8,512}", challenge):
        raise ChannelError("developer_send_not_confirmed")
    on_stage("code_requested")
    code = obtain_code()
    if type(code) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{5,20}", code):
        raise ChannelError("developer_invalid_code")
    fetch("/auth/login", {"phone": phone, "random_hash": challenge, "password": code})
    del code, challenge
    path, page = fetch("/apps")
    if path != "/apps" or 'id="my_login_form"' in page:
        raise ChannelError("developer_session_not_retained")
    parsed = _AppPage(page)
    pair = parsed.credentials()
    if pair is not None:
        on_stage("credentials_read")
        return pair
    if not parsed.has_create or not _FORM <= parsed.fields or not parsed.desktop or not parsed.hidden:
        raise ChannelError("developer_application_form_unverified")
    on_stage("create_form_verified")
    if not create:
        raise ChannelError("developer_registration_not_enabled")
    payload = dict(parsed.hidden, app_title=APP_TITLE, app_shortname=SHORT_NAME,
                   app_platform="desktop", app_desc="Private OmniService Telegram connector.")
    if "app_url" in parsed.fields:
        payload["app_url"] = ""
    # Never automatically repeat application creation or assume success from its HTTP result.
    try:
        fetch("/apps/create", payload)
    except (ChannelError, ValueError, OSError):
        on_stage("create_outcome_unconfirmed")
    path, page = fetch("/apps")
    if path != "/apps" or 'id="my_login_form"' in page:
        raise ChannelError("developer_create_receipt_unknown")
    pair = _AppPage(page).credentials()
    if pair is None:
        raise ChannelError("developer_create_receipt_unknown")
    on_stage("credentials_read")
    return pair
