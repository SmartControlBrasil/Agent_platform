import ipaddress
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ValidationError

from prospecting.interfaces.website import WebsiteFetchResult

SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _blocked_ip(address):
    ip = ipaddress.ip_address(address)
    return any([
        ip.is_loopback,
        ip.is_private,
        ip.is_link_local,
        ip.is_multicast,
        ip.is_reserved,
        ip.is_unspecified,
    ])


def validate_public_http_url(url):
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("Website URL must use http or https.")
    if not parsed.hostname:
        raise ValidationError("Website URL must include a host.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValidationError("Website host could not be resolved safely.") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or any(_blocked_ip(address) for address in addresses):
        raise ValidationError("Website URL resolves to a blocked network address.")
    return parsed.geturl()


class WebsiteHttpFetcher:
    def __init__(self, *, opener=None):
        self.opener = opener or build_opener(NoRedirectHandler())
        self.connect_timeout = settings.WEBSITE_ENRICHMENT_CONNECT_TIMEOUT
        self.read_timeout = settings.WEBSITE_ENRICHMENT_READ_TIMEOUT
        self.max_redirects = settings.WEBSITE_ENRICHMENT_MAX_REDIRECTS
        self.max_body_bytes = settings.WEBSITE_ENRICHMENT_MAX_BODY_BYTES
        self.user_agent = settings.WEBSITE_ENRICHMENT_USER_AGENT

    def fetch(self, url: str) -> WebsiteFetchResult:
        current_url = validate_public_http_url(url)
        redirects = 0
        while True:
            request = Request(current_url, headers={"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"})
            try:
                response = self.opener.open(request, timeout=max(self.connect_timeout, self.read_timeout))
            except HTTPError as exc:
                if 300 <= exc.code < 400 and exc.headers.get("Location"):
                    redirects += 1
                    if redirects > self.max_redirects:
                        raise ValidationError("Website redirected too many times.") from exc
                    current_url = validate_public_http_url(urljoin(current_url, exc.headers["Location"]))
                    continue
                raise ValidationError(f"Website fetch failed with HTTP {exc.code}.") from exc
            except (URLError, TimeoutError) as exc:
                raise ValidationError("Website fetch failed.") from exc
            final_url = validate_public_http_url(response.geturl())
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in SUPPORTED_CONTENT_TYPES:
                raise ValidationError("Website content type is not supported.")
            raw = response.read(self.max_body_bytes + 1)
            if len(raw) > self.max_body_bytes:
                raise ValidationError("Website response is too large.")
            charset = response.headers.get_content_charset() or "utf-8"
            return WebsiteFetchResult(url=final_url, status_code=getattr(response, "status", 200), content_type=content_type, body=raw.decode(charset, errors="replace"))
