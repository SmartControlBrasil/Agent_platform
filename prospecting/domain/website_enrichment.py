import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from prospecting.domain.enrichment import normalize_domain, normalize_email, normalize_phone, normalize_website

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.+-])", re.I)
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
IGNORED_EMAILS = {"example@example.com", "nome@dominio.com", "teste@teste.com", "test@test.com"}
CONTACT_KEYWORDS = ("contato", "contact", "fale-conosco", "faleconosco", "atendimento", "sobre", "empresa")


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
        self._link_href = None
        self._link_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get("href", "")
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
        if tag == "a" and self._link_href:
            self.links.append(PageCandidate(url=urljoin(self.base_url, self._link_href), label=" ".join(self._link_text)))
            self._link_href = None
            self._link_text = []

    def handle_data(self, data):
        if data.strip():
            self.text_parts.append(data)
            if self._link_href:
                self._link_text.append(data)

    @property
    def text(self):
        return " ".join(self.text_parts)


def canonical_url(url):
    parsed = urlsplit(normalize_website(url))
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))


def same_site(url, base_url):
    return normalize_domain(url) == normalize_domain(base_url)


def extract_contacts(html, *, page_url):
    parser = ContactHTMLParser(page_url)
    parser.feed(html or "")
    emails = set()
    for raw in list(parser.mailtos) + EMAIL_RE.findall(parser.text):
        email = normalize_email(raw)
        if email and email not in IGNORED_EMAILS and not email.endswith("@example.com"):
            emails.add(email)
    phones = set()
    for raw in list(parser.tels) + PHONE_RE.findall(parser.text):
        normalized = normalize_phone(raw)
        if 8 <= len(normalized) <= 15:
            phones.add(raw.strip())
    return emails, phones, parser.links


def contact_link_candidates(links, *, base_url, max_links):
    candidates = []
    seen = set()
    for link in links:
        if not same_site(link.url, base_url):
            continue
        haystack = f"{link.url} {link.label}".lower()
        if not any(keyword in haystack for keyword in CONTACT_KEYWORDS):
            continue
        url = canonical_url(link.url)
        if url in seen:
            continue
        seen.add(url)
        candidates.append(PageCandidate(url=url, label=link.label))
        if len(candidates) >= max_links:
            break
    return candidates
