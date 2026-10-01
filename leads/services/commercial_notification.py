"""Ciclo único de notificação comercial — lead e handoff compartilham o mesmo envio inicial."""

from __future__ import annotations

from django.utils import timezone

from conversations.models import HandoffRequest
from leads.models import LeadDraft
from leads.services.commercial import has_usable_contact
from assistant_core.qualification import is_valid_email, is_valid_phone

CYCLE_SENT_KEY = "commercial_notification_sent_at"
CYCLE_DRY_RUN_KEY = "commercial_notification_dry_run_at"
LEAD_SENT_KEY = "lead_notification_sent_at"
HANDOFF_SENT_KEY = "handoff_notification_sent_at"


def related_lead(*, lead=None, handoff=None) -> LeadDraft | None:
    if lead is not None:
        return lead
    if handoff is None:
        return None
    if getattr(handoff, "lead_draft", None) is not None:
        return handoff.lead_draft
    conversation = getattr(handoff, "conversation", None)
    tenant = getattr(handoff, "tenant", None)
    if conversation is None or tenant is None:
        return None
    return LeadDraft.objects.filter(tenant=tenant, conversation=conversation).first()


def related_handoffs(*, lead=None, handoff=None) -> list[HandoffRequest]:
    found: list[HandoffRequest] = []
    if handoff is not None:
        found.append(handoff)
    seed = related_lead(lead=lead, handoff=handoff)
    conversation = getattr(seed, "conversation", None) or getattr(handoff, "conversation", None)
    tenant = getattr(seed, "tenant", None) or getattr(handoff, "tenant", None)
    if conversation is None or tenant is None:
        return found
    extras = HandoffRequest.objects.filter(tenant=tenant, conversation=conversation)
    ids = {item.pk for item in found if getattr(item, "pk", None)}
    for item in extras:
        if item.pk not in ids:
            found.append(item)
    return found


def has_usable_cycle_contact(*, lead=None, handoff=None) -> bool:
    seed = related_lead(lead=lead, handoff=handoff)
    if has_usable_contact(seed):
        return True
    if handoff is None:
        for item in related_handoffs(lead=seed):
            if _handoff_has_usable_contact(item):
                return True
        return False
    return _handoff_has_usable_contact(handoff)


def _handoff_has_usable_contact(handoff) -> bool:
    phone = str(getattr(handoff, "visitor_phone", "") or "").strip()
    email = str(getattr(handoff, "visitor_email", "") or "").strip()
    return bool((phone and is_valid_phone(phone)) or (email and is_valid_email(email)))


def cycle_already_notified(*, lead=None, handoff=None) -> bool:
    seed = related_lead(lead=lead, handoff=handoff)
    data = dict(getattr(seed, "qualification_data", None) or {}) if seed is not None else {}
    if data.get(CYCLE_SENT_KEY) or data.get(LEAD_SENT_KEY):
        return True
    for item in related_handoffs(lead=seed, handoff=handoff):
        meta = dict(getattr(item, "metadata", None) or {})
        if meta.get(HANDOFF_SENT_KEY) and not meta.get("handoff_notification_dry_run"):
            return True
    return False


def cycle_already_dry_run(*, lead=None, handoff=None) -> bool:
    seed = related_lead(lead=lead, handoff=handoff)
    data = dict(getattr(seed, "qualification_data", None) or {}) if seed is not None else {}
    if data.get(CYCLE_DRY_RUN_KEY) or data.get("lead_notification_dry_run_at"):
        return True
    for item in related_handoffs(lead=seed, handoff=handoff):
        meta = dict(getattr(item, "metadata", None) or {})
        if meta.get("handoff_notification_dry_run") or meta.get("handoff_notification_dry_run_at"):
            return True
    return False


def mark_cycle_notified(*, lead=None, handoff=None, dry_run: bool = False) -> None:
    now = timezone.now().isoformat()
    seed = related_lead(lead=lead, handoff=handoff)
    if seed is not None:
        data = dict(getattr(seed, "qualification_data", None) or {})
        if dry_run:
            data[CYCLE_DRY_RUN_KEY] = now
            data["lead_notification_dry_run_at"] = now
            data["lead_notification_dry_run"] = True
        else:
            data[CYCLE_SENT_KEY] = now
            data[LEAD_SENT_KEY] = now
        seed.qualification_data = data
        seed.save(update_fields=["qualification_data", "updated_at"])
    for item in related_handoffs(lead=seed, handoff=handoff):
        meta = dict(getattr(item, "metadata", None) or {})
        if dry_run:
            meta["handoff_notification_dry_run"] = True
            meta["handoff_notification_dry_run_at"] = now
        else:
            meta[HANDOFF_SENT_KEY] = now
            meta["handoff_notification_dry_run"] = False
        item.metadata = meta
        update_fields = ["metadata"]
        if hasattr(item, "updated_at"):
            update_fields.append("updated_at")
        item.save(update_fields=update_fields)
