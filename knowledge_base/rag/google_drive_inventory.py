from __future__ import annotations

import logging
import re
import unicodedata
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from django.conf import settings

from knowledge_base.rag.html_extraction import extract_html_for_rag
from knowledge_base.rag.pdf_extraction import extract_pdf_text_with_fallback

logger = logging.getLogger(__name__)

GOOGLE_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DRIVE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,120}$")


class GoogleDriveError(Exception):
    """Base error for Google Drive inventory failures."""


class GoogleDriveConfigurationError(GoogleDriveError):
    """Raised when local credential/configuration is invalid."""


class GoogleDriveAuthenticationError(GoogleDriveError):
    """Raised when authentication with Google API fails."""


class GoogleDrivePermissionError(GoogleDriveError):
    """Raised when the approved folder cannot be accessed safely."""


class GoogleDriveApiError(GoogleDriveError):
    """Raised for API failures that are not configuration/auth errors."""


def sanitize_external_error_message(message: str) -> str:
    text = " ".join(str(message or "").split())
    text = text[:500]
    lowered = text.lower()
    if any(fragment in lowered for fragment in ("private_key", "token", "authorization", "secret")):
        return "External provider returned a sensitive error and it was masked."
    return text or "External provider error."


@dataclass(frozen=True)
class InventoryFileItem:
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str
    parent_folder_id: str
    relative_path: str


@dataclass(frozen=True)
class InventorySummary:
    files: list[InventoryFileItem]
    traversed_folders: int
    blocked_shortcuts: int
    scanned_items: int


def validate_drive_folder_id(folder_id: str) -> str:
    value = str(folder_id or "").strip()
    lowered = value.lower()
    if not value:
        raise ValueError("approved folder id is required.")
    if "http://" in lowered or "https://" in lowered or "/" in value:
        raise ValueError("approved folder id must be a Drive folder ID, not a URL.")
    if not FOLDER_ID_RE.match(value):
        raise ValueError("approved folder id is invalid.")
    return value


def build_google_drive_readonly_service():
    service_account_file = str(getattr(settings, "LIVIA_GOOGLE_SERVICE_ACCOUNT_FILE", "") or "").strip()
    if not service_account_file:
        raise GoogleDriveConfigurationError("LIVIA_GOOGLE_SERVICE_ACCOUNT_FILE is required.")

    credential_path = Path(service_account_file)
    if not credential_path.exists():
        raise GoogleDriveConfigurationError("Google service account file was not found.")
    if not credential_path.is_file():
        raise GoogleDriveConfigurationError("Google service account path is not a file.")

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise GoogleDriveConfigurationError(
            "Google API dependencies are missing. Install google-api-python-client and google-auth."
        ) from exc

    try:
        credentials = Credentials.from_service_account_file(
            str(credential_path),
            scopes=[GOOGLE_DRIVE_READONLY_SCOPE],
        )
    except ValueError as exc:
        raise GoogleDriveConfigurationError("Google service account JSON is invalid.") from exc
    except Exception as exc:  # pragma: no cover - provider safeguard
        raise GoogleDriveConfigurationError("Could not load Google service account credentials.") from exc

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


class GoogleDriveInventoryService:
    def __init__(self, service: Any):
        self.service = service
        self.last_extraction_metadata: dict[str, object] = {}

    def inventory_approved_folder(self, approved_folder_id: str) -> InventorySummary:
        root_folder_id = validate_drive_folder_id(approved_folder_id)
        self._assert_folder_access(root_folder_id)

        queue: deque[tuple[str, str]] = deque([(root_folder_id, "")])
        traversed_folders = 0
        blocked_shortcuts = 0
        scanned_items = 0
        files: list[InventoryFileItem] = []

        while queue:
            parent_folder_id, parent_relative_path = queue.popleft()
            traversed_folders += 1
            for item in self._list_children(parent_folder_id):
                scanned_items += 1
                mime_type = str(item.get("mimeType") or "")
                item_name = str(item.get("name") or "")
                child_relative_path = _join_relative_path(parent_relative_path, item_name)
                if mime_type == GOOGLE_DRIVE_FOLDER_MIME:
                    queue.append((str(item["id"]), child_relative_path))
                    continue
                if mime_type == GOOGLE_DRIVE_SHORTCUT_MIME:
                    blocked_shortcuts += 1
                    continue
                files.append(
                    InventoryFileItem(
                        file_id=str(item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        mime_type=mime_type,
                        size_bytes=_to_int_or_none(item.get("size")),
                        modified_time=str(item.get("modifiedTime") or ""),
                        parent_folder_id=parent_folder_id,
                        relative_path=child_relative_path,
                    )
                )

        files.sort(key=lambda record: (record.name.lower(), record.file_id))
        logger.info(
            "google_drive_inventory_completed folder_id=%s files=%s folders=%s scanned_items=%s blocked_shortcuts=%s",
            root_folder_id,
            len(files),
            traversed_folders,
            scanned_items,
            blocked_shortcuts,
        )
        return InventorySummary(
            files=files,
            traversed_folders=traversed_folders,
            blocked_shortcuts=blocked_shortcuts,
            scanned_items=scanned_items,
        )

    def _assert_folder_access(self, folder_id: str) -> None:
        try:
            payload = (
                self.service.files()
                .get(
                    fileId=folder_id,
                    fields="id,name,mimeType,trashed",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:
            self._raise_api_error(exc, fallback_message="Could not verify approved folder access.")
            raise
        mime_type = str(payload.get("mimeType") or "")
        if mime_type != GOOGLE_DRIVE_FOLDER_MIME:
            raise GoogleDrivePermissionError("Approved folder id does not point to a folder.")
        if bool(payload.get("trashed")):
            raise GoogleDrivePermissionError("Approved folder is in trash and cannot be inventoried.")

    def _list_children(self, parent_folder_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        next_page_token = None
        while True:
            try:
                response = (
                    self.service.files()
                    .list(
                        q=f"'{parent_folder_id}' in parents and trashed = false",
                        fields=(
                            "nextPageToken,files("
                            "id,name,mimeType,size,modifiedTime,parents,"
                            "shortcutDetails(targetId,targetMimeType)"
                            ")"
                        ),
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        pageToken=next_page_token,
                        pageSize=200,
                        orderBy="name asc",
                    )
                    .execute()
                )
            except Exception as exc:
                self._raise_api_error(exc, fallback_message="Could not list approved folder files.")
                raise

            records.extend(response.get("files", []))
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
        return records

    def export_google_doc_text(self, file_id: str) -> bytes:
        return self.export_file_text(file_id, "application/vnd.google-apps.document")

    def export_file_text(self, file_id: str, mime_type: str) -> bytes:
        self.last_extraction_metadata = {}
        mime = str(mime_type or "").strip()
        if mime == "application/vnd.google-apps.document":
            try:
                payload = (
                    self.service.files()
                    .export(
                        fileId=file_id,
                        mimeType="text/plain",
                    )
                    .execute()
                )
            except Exception as exc:
                self._raise_api_error(exc, fallback_message="Could not export Google Docs file.")
                raise
            if isinstance(payload, bytes):
                return payload
            if isinstance(payload, str):
                return payload.encode("utf-8", errors="ignore")
            raise GoogleDriveApiError("Could not export Google Docs file.")

        try:
            request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
            payload = request.execute()
        except Exception as exc:
            self._raise_api_error(exc, fallback_message="Could not download Drive file for text extraction.")
            raise
        if not isinstance(payload, (bytes, bytearray)):
            raise GoogleDriveApiError("Downloaded Drive payload is invalid.")
        raw = bytes(payload)
        if mime in {
            "application/pdf",
        }:
            try:
                result = extract_pdf_text_with_fallback(raw)
            except RuntimeError as exc:
                raise GoogleDriveApiError(str(exc)) from exc
            self.last_extraction_metadata = {
                "extraction_method": result.method,
                "pdf_empty_cause": result.empty_cause or None,
                "ocr_pages": result.ocr_pages or None,
            }
            return result.text_bytes
        if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return _extract_docx_text_bytes(raw)
        if mime in {"text/html", "application/xhtml+xml"}:
            text, html_meta = extract_html_for_rag(raw)
            if not text.strip():
                raise GoogleDriveApiError("HTML text extraction returned empty content.")
            self.last_extraction_metadata = {"extraction_method": "html_parser", **html_meta}
            return text.encode("utf-8")
        if mime in {"text/plain", "text/markdown"}:
            try:
                return raw.decode("utf-8").encode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1", errors="ignore").encode("utf-8")
        raise GoogleDriveApiError(f"Unsupported export mime type: {mime}")

    def _raise_api_error(self, exc: Exception, *, fallback_message: str) -> None:
        status_code = _extract_http_status(exc)
        message = str(exc)
        if status_code in {401, 403}:
            raise GoogleDriveAuthenticationError("Google Drive authentication failed.") from exc
        if status_code == 404:
            raise GoogleDrivePermissionError("Approved folder was not found or is not accessible.") from exc
        if "insufficient" in message.lower() or "permission" in message.lower():
            raise GoogleDrivePermissionError("Service account has no read access to approved folder.") from exc
        raise GoogleDriveApiError(fallback_message) from exc


def _extract_http_status(exc: Exception) -> int | None:
    response = getattr(exc, "resp", None)
    if response is None:
        return None
    status = getattr(response, "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value) -> int | None:
    if value in ("", None):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_text_for_rag(raw_text: str) -> str:
    value = unicodedata.normalize("NFC", str(raw_text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in value.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)
    value = "\n".join(lines)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def compute_text_sha256(normalized_text: str) -> str:
    return sha256(str(normalized_text).encode("utf-8")).hexdigest()


def decode_google_text_payload(payload: bytes) -> str:
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GoogleDriveApiError("Google Docs exported payload is not valid UTF-8.") from exc


def _extract_pdf_text_bytes(raw: bytes) -> bytes:
    try:
        result = extract_pdf_text_with_fallback(raw)
    except RuntimeError as exc:
        raise GoogleDriveApiError(str(exc)) from exc
    return result.text_bytes


def _extract_docx_text_bytes(raw: bytes) -> bytes:
    import re
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        raise GoogleDriveApiError("Could not read DOCX content.") from exc
    text = re.sub(r"<w:tab[^/]*/>", "\t", xml)
    text = re.sub(r"</w:p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise GoogleDriveApiError("DOCX text extraction returned empty content.")
    return text.encode("utf-8")


def _join_relative_path(parent_path: str, child_name: str) -> str:
    parent_path = str(parent_path or "").strip("/")
    child = str(child_name or "").strip()
    if not parent_path:
        return child
    return f"{parent_path}/{child}"
