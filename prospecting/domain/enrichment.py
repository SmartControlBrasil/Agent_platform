from urllib.parse import urlsplit, urlunsplit

from prospecting.domain.dedupe import normalize_text

FORBIDDEN_SECRET_MARKERS = ("apc_", "aep_", "authorization", "cookie", "session", "token", "secret", "credential")


def contains_forbidden_secret(value):
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in FORBIDDEN_SECRET_MARKERS)


def normalize_email(value):
    return " ".join((value or "").strip().lower().split())


def normalize_phone(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits or normalize_text(value)


def normalize_website(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def normalize_domain(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    domain = parsed.netloc or parsed.path.split("/", 1)[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.strip(".")


def normalize_enrichment_value(field, value):
    field = str(field or "").upper()
    if field == "EMAIL":
        return normalize_email(value)
    if field == "PHONE":
        return normalize_phone(value)
    if field == "WEBSITE":
        return normalize_website(value)
    if field == "DOMAIN":
        return normalize_domain(value)
    if field == "ADDRESS":
        return normalize_text(value)
    return normalize_text(value)


def build_source_key(*, source_type, source_search_result=None, source_url="", source_reference=""):
    source_type = str(source_type or "").upper()
    if source_search_result is not None:
        return f"{source_type}:search-result:{source_search_result.id}"
    normalized_url = normalize_website(source_url)
    if normalized_url:
        return f"{source_type}:url:{normalized_url}"
    reference = normalize_text(source_reference)
    if reference:
        return f"{source_type}:ref:{reference}"
    return f"{source_type}:manual"
