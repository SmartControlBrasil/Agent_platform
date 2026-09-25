from django.core.exceptions import PermissionDenied

from .models import Tenant, TenantMembership

CAPABILITY_PORTAL_VIEW_DASHBOARD = "portal.view_dashboard"
CAPABILITY_CONVERSATIONS_VIEW = "conversations.view"
CAPABILITY_LEADS_VIEW = "leads.view"
CAPABILITY_LEADS_RETRY_CRM = "leads.retry_crm"
CAPABILITY_HANDOFFS_VIEW = "handoffs.view"
CAPABILITY_HANDOFFS_CHANGE_STATUS = "handoffs.change_status"
CAPABILITY_COMMERCIAL_VIEW = "commercial.view"
CAPABILITY_COMMERCIAL_MANAGE = "commercial.manage"
CAPABILITY_TENANT_VIEW = "tenant.view"
CAPABILITY_TENANT_MANAGE = "tenant.manage"
CAPABILITY_ASSISTANT_PROFILE_VIEW = "assistant_profile.view"
CAPABILITY_ASSISTANT_PROFILE_CHANGE = "assistant_profile.change"
CAPABILITY_MEMBERSHIPS_VIEW = "memberships.view"
CAPABILITY_MEMBERSHIPS_MANAGE = "memberships.manage"
CAPABILITY_KNOWLEDGE_BASE_VIEW = "knowledge_base.view"
CAPABILITY_KNOWLEDGE_BASE_CONFIGURE = "knowledge_base.configure"
CAPABILITY_KNOWLEDGE_BASE_OPERATE = "knowledge_base.operate"
CAPABILITY_KNOWLEDGE_BASE_REINDEX = "knowledge_base.reindex"

ALL_CAPABILITIES = frozenset(
    {
        CAPABILITY_PORTAL_VIEW_DASHBOARD,
        CAPABILITY_CONVERSATIONS_VIEW,
        CAPABILITY_LEADS_VIEW,
        CAPABILITY_LEADS_RETRY_CRM,
        CAPABILITY_HANDOFFS_VIEW,
        CAPABILITY_HANDOFFS_CHANGE_STATUS,
        CAPABILITY_COMMERCIAL_VIEW,
        CAPABILITY_COMMERCIAL_MANAGE,
        CAPABILITY_TENANT_VIEW,
        CAPABILITY_TENANT_MANAGE,
        CAPABILITY_ASSISTANT_PROFILE_VIEW,
        CAPABILITY_ASSISTANT_PROFILE_CHANGE,
        CAPABILITY_MEMBERSHIPS_VIEW,
        CAPABILITY_MEMBERSHIPS_MANAGE,
        CAPABILITY_KNOWLEDGE_BASE_VIEW,
        CAPABILITY_KNOWLEDGE_BASE_CONFIGURE,
        CAPABILITY_KNOWLEDGE_BASE_OPERATE,
        CAPABILITY_KNOWLEDGE_BASE_REINDEX,
    }
)

ROLE_CAPABILITIES = {
    TenantMembership.Role.TENANT_ADMIN: ALL_CAPABILITIES,
    TenantMembership.Role.MANAGER: frozenset(
        {
            CAPABILITY_PORTAL_VIEW_DASHBOARD,
            CAPABILITY_CONVERSATIONS_VIEW,
            CAPABILITY_LEADS_VIEW,
            CAPABILITY_LEADS_RETRY_CRM,
            CAPABILITY_HANDOFFS_VIEW,
            CAPABILITY_HANDOFFS_CHANGE_STATUS,
            CAPABILITY_COMMERCIAL_VIEW,
            CAPABILITY_COMMERCIAL_MANAGE,
            CAPABILITY_TENANT_VIEW,
            CAPABILITY_TENANT_MANAGE,
            CAPABILITY_ASSISTANT_PROFILE_VIEW,
            CAPABILITY_KNOWLEDGE_BASE_VIEW,
            CAPABILITY_KNOWLEDGE_BASE_CONFIGURE,
            CAPABILITY_KNOWLEDGE_BASE_OPERATE,
        }
    ),
    TenantMembership.Role.OPERATOR: frozenset(
        {
            CAPABILITY_PORTAL_VIEW_DASHBOARD,
            CAPABILITY_CONVERSATIONS_VIEW,
            CAPABILITY_LEADS_VIEW,
            CAPABILITY_HANDOFFS_VIEW,
            CAPABILITY_HANDOFFS_CHANGE_STATUS,
            CAPABILITY_COMMERCIAL_VIEW,
            CAPABILITY_COMMERCIAL_MANAGE,
            CAPABILITY_TENANT_VIEW,
            CAPABILITY_KNOWLEDGE_BASE_VIEW,
            CAPABILITY_KNOWLEDGE_BASE_OPERATE,
        }
    ),
    TenantMembership.Role.VIEWER: frozenset(
        {
            CAPABILITY_PORTAL_VIEW_DASHBOARD,
            CAPABILITY_CONVERSATIONS_VIEW,
            CAPABILITY_LEADS_VIEW,
            CAPABILITY_HANDOFFS_VIEW,
            CAPABILITY_COMMERCIAL_VIEW,
            CAPABILITY_TENANT_VIEW,
            CAPABILITY_KNOWLEDGE_BASE_VIEW,
        }
    ),
}


def get_membership_tenants(user):
    if not getattr(user, "is_authenticated", False):
        return Tenant.objects.none()
    return (
        Tenant.objects.filter(memberships__user=user, memberships__is_active=True, is_active=True)
        .distinct()
        .order_by("name")
    )


def get_accessible_tenants(user):
    if not getattr(user, "is_authenticated", False):
        return Tenant.objects.none()
    if user.is_superuser:
        return Tenant.objects.filter(is_active=True).order_by("name")
    return get_membership_tenants(user)


def get_active_membership(user, tenant):
    if not getattr(user, "is_authenticated", False) or tenant is None:
        return None
    return (
        TenantMembership.objects.select_related("tenant", "user")
        .filter(user=user, tenant=tenant, is_active=True, tenant__is_active=True)
        .first()
    )


def user_has_tenant_capability(user, tenant, capability):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return capability in ALL_CAPABILITIES
    membership = get_active_membership(user, tenant)
    if membership is None:
        return False
    return capability in ROLE_CAPABILITIES.get(membership.role, frozenset())


def require_tenant_capability(user, tenant, capability):
    if not user_has_tenant_capability(user, tenant, capability):
        raise PermissionDenied
    return True


def capabilities_for_user(user, tenant):
    if not getattr(user, "is_authenticated", False):
        return frozenset()
    if user.is_superuser:
        return ALL_CAPABILITIES
    membership = get_active_membership(user, tenant)
    if membership is None:
        return frozenset()
    return ROLE_CAPABILITIES.get(membership.role, frozenset())
