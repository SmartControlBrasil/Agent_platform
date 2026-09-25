from prospecting.domain.dedupe import normalize_maps_url, normalize_text


def build_prospect_identity_key(search_result):
    external = normalize_text(search_result.external_id)
    if external:
        return f"external:{external}"
    maps = normalize_maps_url(search_result.maps_url)
    if maps:
        return f"maps:{maps}"
    return f"search-result:{search_result.id}"
