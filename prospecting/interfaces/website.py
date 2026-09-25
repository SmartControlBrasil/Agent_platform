from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WebsiteFetchResult:
    url: str
    status_code: int
    content_type: str
    body: str


class WebsiteFetcherPort(Protocol):
    def fetch(self, url: str) -> WebsiteFetchResult:
        ...
