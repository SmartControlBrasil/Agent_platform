import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator

from prospecting.domain.enrichment import normalize_domain, normalize_email, normalize_phone, normalize_website

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.+-])", re.I)
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
IGNORED_EMAILS = {"example@example.com", "nome@dominio.com", "teste@teste.com", "test@test.com"}
INVALID_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
CONTACT_KEYWORDS = (
    "contato",
    "contatos",
    "contact",
    "contact-us",
    "fale-conosco",
    "faleconosco",
    "atendimento",
    "sobre",
    "empresa",
    "institucional",
)
CONTACT_PATH_MARKERS = (
    "/contato",
    "/contatos",
    "/contact",
    "/contact-us",
    "/fale-conosco",
    "/atendimento",
    "/sobre",
    "/institucional",
)
JSON_LD_TYPES = {
    "organization",
    "localbusiness",
    "hospital",
    "medicalorganization",
    "medicalclinic",
    "physician",
}
MIN_ADDRESS_LENGTH = 12
MAX_ADDRESS_LENGTH = 280

_email_validator = EmailValidator()


@dataclass(frozen=True)
class PageCandidate:
    url: str
    label: str = ""


class ContactHTMLParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []
        self.mailtos = []
        self.tels = []
        self.text_parts = []
        self.address_blocks = []
        self._link_href = None
        self._link_text = []
        self._in_address = False
        self._address_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get("href", "")
        if tag == "address":
            self._in_address = True
            self._address_parts = []
        if tag == "a" and href:
            lower = href.lower()
            if lower.startswith("mailto:"):
                self.mailtos.append(href.split(":", 1)[1].split("?", 1)[0])
            elif lower.startswith("tel:"):
                self.tels.append(href.split(":", 1)[1].split("?", 1)[0])
            else:
                self._link_href = href
                self._link_text = []

    def handle_endtag(self, tag):
        if tag == "address" and self._in_address:
            block = " ".join(part for part in self._address_parts if part.strip())
            if block.strip():
                self.address_blocks.append(block.strip())
            self._in_address = False
            self._address_parts = []
        if tag == "a" and self._link_href:
            self.links.append(PageCandidate(url=urljoin(self.base_url, self._link_href), label=" ".join(self._link_text)))
            self._link_href = None
            self._link_text = []

    def handle_data(self, data):
        if data.strip():
            self.text_parts.append(data)
            if self._in_address:
                self._address_parts.append(data.strip())
            if self._link_href:
                self._link_text.append(data)

    @property
    def text(self):
        return " ".join(self.text_parts)


def canonical_url(url):
    parsed = urlsplit(normalize_website(url))
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") or "", parsed.query, ""))


def same_site(url, base_url):
    return normalize_domain(url) == normalize_domain(base_url)


def is_acceptable_email(raw):
    email = normalize_email(raw)
    if not email or email in IGNORED_EMAILS or email.endswith("@example.com"):
        return False
    if any(email.endswith(suffix) for suffix in INVALID_EMAIL_SUFFIXES):
        return False
    try:
        _email_validator(email)
    except ValidationError:
        return False
    return True


def normalize_display_phone(raw):
    normalized = normalize_phone(raw)
    if 8 <= len(normalized) <= 15:
        return " ".join(str(raw or "").split())
    return ""


def is_acceptable_address(value):
    text = " ".join(str(value or "").split())
    if len(text) < MIN_ADDRESS_LENGTH or len(text) > MAX_ADDRESS_LENGTH:
        return False
    lowered = text.lower()
    if lowered in {"endereço", "address", "contato"}:
        return False
    if sum(ch.isdigit() for ch in text) == 0:
        return False
    return True


def _iter_json_ld_nodes(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_json_ld_nodes(item)
        return
    if not isinstance(payload, dict):
        return
    graph = payload.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            yield from _iter_json_ld_nodes(item)
    yield payload


def _json_ld_type_names(node):
    raw_type = node.get("@type") or node.get("type")
    if isinstance(raw_type, list):
        return [str(item).lower() for item in raw_type]
    if raw_type:
        return [str(raw_type).lower()]
    return []


def _format_postal_address(value):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    parts = []
    for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
        part = value.get(key)
        if part:
            parts.append(str(part).strip())
    return ", ".join(part for part in parts if part)


def extract_json_ld_contacts(html):
    emails = set()
    phones = set()
    addresses = set()
    for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html or "", re.I | re.S):
        raw_json = match.group(1).strip()
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        for node in _iter_json_ld_nodes(payload):
            types = _json_ld_type_names(node)
            if types and not any(any(known in item for known in JSON_LD_TYPES) for item in types):
                continue
            email = node.get("email")
            if isinstance(email, list):
                for item in email:
                    if is_acceptable_email(item):
                        emails.add(normalize_email(item))
            elif is_acceptable_email(email):
                emails.add(normalize_email(email))
            telephone = node.get("telephone")
            if isinstance(telephone, list):
                for item in telephone:
                    display = normalize_display_phone(item)
                    if display:
                        phones.add(display)
            else:
                display = normalize_display_phone(telephone)
                if display:
                    phones.add(display)
            address = _format_postal_address(node.get("address"))
            if is_acceptable_address(address):
                addresses.add(address)
    return emails, phones, addresses


def extract_contacts(html, *, page_url):
    parser = ContactHTMLParser(page_url)
    parser.feed(html or "")
    emails = set()
    for raw in list(parser.mailtos) + EMAIL_RE.findall(parser.text):
        if is_acceptable_email(raw):
            emails.add(normalize_email(raw))
    phones = set()
    for raw in list(parser.tels) + PHONE_RE.findall(parser.text):
        display = normalize_display_phone(raw)
        if display:
            phones.add(display)
    addresses = set()
    for block in parser.address_blocks:
        if is_acceptable_address(block):
            addresses.add(" ".join(block.split()))
    json_emails, json_phones, json_addresses = extract_json_ld_contacts(html)
    emails |= json_emails
    phones |= json_phones
    addresses |= json_addresses
    return emails, phones, addresses, parser.links


def _link_looks_like_contact(link):
    haystack = f"{link.url} {link.label}".lower()
    if any(keyword in haystack for keyword in CONTACT_KEYWORDS):
        return True
    path = urlsplit(link.url).path.lower()
    return any(marker in path for marker in CONTACT_PATH_MARKERS)


def contact_link_candidates(links, *, base_url, max_links):
    candidates = []
    seen = set()
    for link in links:
        if not same_site(link.url, base_url):
            continue
        if not _link_looks_like_contact(link):
            continue
        url = canonical_url(link.url)
        if url in seen:
            continue
        seen.add(url)
        candidates.append(PageCandidate(url=url, label=link.label))
        if len(candidates) >= max_links:
            break
    return candidates
