"""ORM models for Phase 1.1 (Provider & Key Management), Phase 1.2
(Unified API / Gateway Core), Phase 1.3 (Model Access Governance - Basic),
Phase 1.4 (Budget - Basic), Phase 1.5 (Logging & Observability - Basic),
Phase 2 (Multi-Tenant Governance), Phase 3 (Security & Compliance
Hardening), and Phase 4 (Reliability & Cost Efficiency).

Every model module must be imported here so `Base.metadata` (see
`gatekey.db.base`) is fully populated before Alembic autogenerate or
`alembic/env.py` inspects it.

Note: the `sessions` table's model class is `UserSession` (not `Session`) -
deliberately named to never clash with SQLAlchemy's `Session`/`AsyncSession`
imports used throughout the codebase (see `db/models/session.py`).
"""

from __future__ import annotations

from gatekey.db.models.access_schedule import AccessSchedule, AccessScheduleScopeType
from gatekey.db.models.audit_entry import AuditEntry
from gatekey.db.models.backup_group import BackupGroup
from gatekey.db.models.cache_lookup_event import CacheLookupEvent
from gatekey.db.models.caching_settings import CachingSettings
from gatekey.db.models.canary_baseline import CanaryBaseline
from gatekey.db.models.canary_model_setting import CanaryModelSetting
from gatekey.db.models.canary_prompt import CanaryPrompt
from gatekey.db.models.canary_run import CanaryRun
from gatekey.db.models.cli_refresh_credential import CliRefreshCredential
from gatekey.db.models.compliance_settings import ComplianceSettings
from gatekey.db.models.content_aware_rule import ContentAwareRule
from gatekey.db.models.custom_model import CustomModel
from gatekey.db.models.degradation_event import DegradationEvent
from gatekey.db.models.degradation_policy import DegradationPolicy, DegradationScopeType
from gatekey.db.models.dlp_custom_pattern import DlpCustomPattern
from gatekey.db.models.dlp_policy import DlpAction, DlpPolicy
from gatekey.db.models.dlp_scan_result import DlpScanResult
from gatekey.db.models.drift_alert import DriftAlert
from gatekey.db.models.emergency_override import EmergencyOverride
from gatekey.db.models.failover_event import FailoverEvent
from gatekey.db.models.holiday_date import HolidayDate
from gatekey.db.models.join_request import (
    JoinRequest,
    JoinRequestRoutedTo,
    JoinRequestStatus,
)
from gatekey.db.models.known_ai_tool_hostname import KnownAiToolHostname
from gatekey.db.models.model_policy import ModelPolicy, ModelPolicyMode
from gatekey.db.models.org import Org
from gatekey.db.models.org_settings import OrgSettings
from gatekey.db.models.personal_api_key import PersonalApiKey
from gatekey.db.models.provider_key import ProviderKey, ProviderName
from gatekey.db.models.rate_limit_rejection_event import (
    RateLimitRejectionEvent,
    RateLimitRejectionOutcome,
)
from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitRule, RateLimitScopeType
from gatekey.db.models.residency_rule import ResidencyRule, ResidencyViolationBehavior
from gatekey.db.models.rotation_policy import RotationMode, RotationPolicy, RotationScopeType
from gatekey.db.models.scim_config import ScimConfig
from gatekey.db.models.self_hosted_provider import SelfHostedProvider
from gatekey.db.models.sensitivity_label_mapping import SensitivityLabelMapping
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.db.models.session import UserSession
from gatekey.db.models.shadow_ai_ingest_config import ShadowAiIngestConfig
from gatekey.db.models.shadow_ai_ingest_event import ShadowAiIngestEvent
from gatekey.db.models.team import Team, TeamPeriodEnd, TeamPeriodType
from gatekey.db.models.team_dlp_action_override import TeamDlpActionOverride
from gatekey.db.models.team_failover_override import TeamFailoverOverride
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.models.team_model_policy import TeamModelPolicy
from gatekey.db.models.usage_log import UsageLog
from gatekey.db.models.user import User, UserOrgRole

__all__ = [
    "AccessSchedule",
    "AccessScheduleScopeType",
    "AuditEntry",
    "CacheLookupEvent",
    "CachingSettings",
    "CanaryBaseline",
    "CanaryModelSetting",
    "CanaryPrompt",
    "CanaryRun",
    "CliRefreshCredential",
    "ComplianceSettings",
    "ContentAwareRule",
    "CustomModel",
    "DegradationEvent",
    "DegradationPolicy",
    "DegradationScopeType",
    "DlpAction",
    "DlpCustomPattern",
    "DlpPolicy",
    "DlpScanResult",
    "DriftAlert",
    "EmergencyOverride",
    "FailoverEvent",
    "HolidayDate",
    "BackupGroup",
    "JoinRequest",
    "JoinRequestRoutedTo",
    "JoinRequestStatus",
    "KnownAiToolHostname",
    "ModelPolicy",
    "ModelPolicyMode",
    "Org",
    "OrgSettings",
    "PersonalApiKey",
    "ProviderKey",
    "ProviderName",
    "RateLimitOnLimit",
    "RateLimitRejectionEvent",
    "RateLimitRejectionOutcome",
    "RateLimitRule",
    "RateLimitScopeType",
    "ResidencyRule",
    "ResidencyViolationBehavior",
    "RotationMode",
    "RotationPolicy",
    "RotationScopeType",
    "ScimConfig",
    "SelfHostedProvider",
    "SensitivityLabelMapping",
    "ServiceAccountKey",
    "ShadowAiIngestConfig",
    "ShadowAiIngestEvent",
    "Team",
    "TeamDlpActionOverride",
    "TeamFailoverOverride",
    "TeamMembership",
    "TeamModelPolicy",
    "TeamPeriodEnd",
    "TeamPeriodType",
    "TeamRole",
    "UsageLog",
    "User",
    "UserOrgRole",
    "UserSession",
]
