from __future__ import annotations

import re
from html.parser import HTMLParser
from io import StringIO
from urllib.parse import urlparse

_VIDEO_HOST_MARKERS = ("youtube.com", "youtu.be", "vimeo.com", "drive.google.com/file")


class _HtmlTextCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []
        self.links: list[str] = []
        self.srcs: list[str] = []
        self.titles: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
            return
        attrs_dict = dict(attrs)
        if tag == "a":
            href = str(attrs_dict.get("href") or "").strip()
            if href:
                self.links.append(href)
        if tag in {"img", "iframe", "embed", "source", "video"}:
            src = str(attrs_dict.get("src") or attrs_dict.get("data-src") or "").strip()
            if src:
                self.srcs.append(src)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"} and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = str(data or "").strip()
        if not text:
            return
        if self.in_title:
            self.titles.append(text)
        self.parts.append(text)


def _normalize_url(url: str) -> str:
    value = str(url or "").strip()
    if not value or value.startswith("#"):
        return ""
    return value


def _is_video_reference(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in _VIDEO_HOST_MARKERS) or "watch?v=" in lowered


def extract_html_for_rag(raw: bytes) -> tuple[str, dict]:
    """Extract title, visible text and relevant links from HTML without external crawling."""
    text = raw.decode("utf-8", errors="ignore")
    parser = _HtmlTextCollector()
    parser.feed(text)
    parser.close()

    title = " ".join(parser.titles).strip()
    visible = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts))
    visible = re.sub(r"[ \t]+", " ", visible)
    visible = re.sub(r"\n ", "\n", visible).strip()

    links = []
    for href in parser.links:
        normalized = _normalize_url(href)
        if normalized:
            links.append(normalized)
    srcs = [_normalize_url(item) for item in parser.srcs]
    srcs = [item for item in srcs if item]

    video_refs = sorted({item for item in links + srcs if _is_video_reference(item)})
    unique_links = sorted(set(links))

    sections: list[str] = []
    if title:
        sections.append(f"Título: {title}")
    if visible:
        sections.append("Conteúdo:\n" + visible)
    if unique_links:
        sections.append("Links:\n" + "\n".join(f"- {item}" for item in unique_links))
    if video_refs:
        sections.append("Referências de vídeo:\n" + "\n".join(f"- {item}" for item in video_refs))

    normalized_output = "\n\n".join(sections).strip()
    metadata = {
        "title": title or None,
        "link_count": len(unique_links),
        "video_reference_count": len(video_refs),
        "visible_text_chars": len(visible),
        "links": unique_links,
        "video_references": video_refs,
    }
    return normalized_output, metadata
