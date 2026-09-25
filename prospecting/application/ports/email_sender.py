from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailSendResult:
    success: bool
    provider_message_id: str = ""
    error_code: str = ""
    error_message: str = ""


class EmailSenderPort(Protocol):
    def send_plain_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> EmailSendResult:
        ...


def get_default_email_sender() -> EmailSenderPort:
    from prospecting.infrastructure.django_email_sender import DjangoEmailSenderPort

    return DjangoEmailSenderPort()
