import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_text(value):
    return " ".join((value or "").strip().lower().split())


def normalize_maps_url(value):
    if not value:
        return ""
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def build_search_result_dedupe_key(*, external_id=None, maps_url=None, name=None, address=None):
    external = normalize_text(external_id)
    if external:
        return f"external:{external}"
    maps = normalize_maps_url(maps_url)
    if maps:
        return f"maps:{maps}"
    identity = f"{normalize_text(name)}|{normalize_text(address)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"name-address:{digest}"
