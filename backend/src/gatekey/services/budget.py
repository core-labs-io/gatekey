"""Per-user budget gate + atomic usage charging (Phase 1.4 - Budget Basic).

See `docs/design/phase-1.4-budget-basic-design.md` for the full design
rationale this module implements: ADR-1 (NUMERIC(20,10) precision),
section 4 (pipeline placement), section 5 (this module's exact shape),
section 6 (idempotency/atomicity semantics), section 10 (concurrency
guarantees).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.org_settings import OrgSettings
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership
from gatekey.db.models.user import User
from gatekey.providers.pricing import get_pricing_entry
from gatekey.services.team_periods import TeamPeriodInfo

logger = logging.getLogger("gatekey")


@dataclass(frozen=True)
class UserBudgetState:
    id: uuid.UUID
    name: str
    budget_usd: Decimal | None
    current_spend_usd: Decimal


@dataclass(frozen=True)
class TeamMembershipBudgetState:
    """Phase 2 (BD-8/A6): the (team, user) budget counter plus the team
    period fields `ensure_current_period`'s cheap comparison needs - pulled
    by one joined query (design doc section 8: the team-aware budget check
    is the same single round trip as Phase 1.4's, broadened by a join).

    `name` is the owning user's name, so `BudgetExhaustedError`'s message
    keeps its existing shape on the team path.
    """

    membership_id: uuid.UUID
    team_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    budget_usd: Decimal | None
    current_spend_usd: Decimal
    period: TeamPeriodInfo


async def get_team_membership_budget_state(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> TeamMembershipBudgetState | None:
    """Single joined SELECT of one (team, user) membership's budget/spend
    state plus the team's period fields. Like `get_budget_state`, always
    reads through to the database - per-request mutable state, never
    cacheable the `ModelPolicyCache` way.

    `removed_at IS NULL` (added by `0049`): a removed membership must
    resolve to `None` here exactly like a never-existing one - the caller
    (`api.v1.gateway.common.check_budget_available`) turns that into a
    clean `TeamMembershipRemovedError` (403), not a budget-state crash."""
    stmt = (
        select(
            TeamMembership.id,
            TeamMembership.team_id,
            TeamMembership.user_id,
            TeamMembership.budget_usd,
            TeamMembership.current_spend_usd,
            User.name,
            Team.period_type,
            Team.current_period_started_at,
        )
        .join(User, User.id == TeamMembership.user_id)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(
            TeamMembership.team_id == team_id,
            TeamMembership.user_id == user_id,
            TeamMembership.removed_at.is_(None),
        )
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return TeamMembershipBudgetState(
        membership_id=row.id,
        team_id=row.team_id,
        user_id=row.user_id,
        name=row.name,
        budget_usd=row.budget_usd,
        current_spend_usd=row.current_spend_usd,
        period=TeamPeriodInfo(
            id=row.team_id,
            period_type=row.period_type,
            current_period_started_at=row.current_period_started_at,
        ),
    )


async def get_budget_state(session: AsyncSession, user_id: uuid.UUID) -> UserBudgetState | None:
    """Single indexed-PK SELECT of one user's current budget/spend state.

    Deliberately not cacheable the way `ModelPolicyCache` caches the
    org-wide policy snapshot: `current_spend_usd`/`budget_usd` are
    per-user mutable state that changes on every charged request, so this
    always reads through to the database.
    """
    stmt = select(User.id, User.name, User.budget_usd, User.current_spend_usd).where(
        User.id == user_id
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return UserBudgetState(
        id=row.id, name=row.name, budget_usd=row.budget_usd, current_spend_usd=row.current_spend_usd
    )


@dataclass(frozen=True)
class OrgBudgetState:
    """The org-wide safeguard counter (added alongside `0045` - see that
    migration's docstring). Unlike `UserBudgetState`/`TeamMembershipBudgetState`,
    there's no guaranteed-to-exist FK-referenced row backing this - absence
    of an `org_settings` row is ADR-2's normal "no ceiling configured"
    default state, so `get_org_budget_state` below always returns a value
    (never `None`), defaulting to unmetered."""

    budget_usd: Decimal | None
    current_spend_usd: Decimal


async def get_org_budget_state(session: AsyncSession) -> OrgBudgetState:
    """Single indexed-PK SELECT of the org's live spend-safeguard state.
    Absence of a row (ADR-2: never configured) resolves to the same
    "unmetered, zero spend" default `services.org_settings.
    get_effective_org_settings` uses for every other org-settings field.

    Deliberately not cacheable, same rationale as `get_budget_state`/
    `get_team_membership_budget_state` above - this is checked on EVERY
    gateway request (`api.v1.gateway.common.check_budget_available`), a
    permanent added cost accepted deliberately: a single indexed point
    lookup, same class of cost as those two existing checks, in exchange
    for a real org-wide circuit breaker instead of only alerting."""
    stmt = select(OrgSettings.budget_ceiling_usd, OrgSettings.current_spend_usd).where(
        OrgSettings.org_id == DEFAULT_ORG_ID
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return OrgBudgetState(budget_usd=None, current_spend_usd=Decimal(0))
    return OrgBudgetState(budget_usd=row.budget_ceiling_usd, current_spend_usd=row.current_spend_usd)


def is_budget_exhausted(
    state: UserBudgetState | TeamMembershipBudgetState | OrgBudgetState,
) -> bool:
    """`NULL` budget is never exhausted (unmetered); exhausted means
    `current_spend_usd >= budget_usd` - "exhausted" means fully used
    (`>=`, not `>`), so `budget_usd = 0` blocks the very first request,
    distinct from `budget_usd = NULL` which is never blocked here.

    Phase 2: identical semantics for both the legacy flat `User` counter
    and a `TeamMembership` counter (design doc section 3.2) - one shared
    predicate, not two implementations that could drift. The org-wide
    safeguard counter (added alongside `0045`) reuses the exact same
    predicate one level up, for the same reason.
    """
    return state.budget_usd is not None and state.current_spend_usd >= state.budget_usd


def compute_cost(model: str, *, prompt_tokens: int, completion_tokens: int | None) -> Decimal:
    """Compute USD cost from actual provider-reported token counts.

    `completion_tokens=None` selects the embeddings formula (no output-token
    term); an int (including `0`) selects the chat/completions formula.
    Raises `providers.pricing.PricingEntryMissingError` for an unpriced
    model - never returns `$0` as a substitute for a missing entry.
    """
    entry = get_pricing_entry(model)
    cost = (entry.input_price_per_million_usd * prompt_tokens) / Decimal(1_000_000)
    if completion_tokens is not None:
        assert entry.output_price_per_million_usd is not None, (
            f"model {model!r} has completion_tokens but no output price - "
            "PRICING_TABLE completeness invariant violated."
        )
        cost += (entry.output_price_per_million_usd * completion_tokens) / Decimal(1_000_000)
    return cost


async def record_usage_charge(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    model: str,
    prompt_tokens: int,
    completion_tokens: int | None,
    precomputed_cost_usd: Decimal | None = None,
) -> Decimal:
    """Charge `user_id` for actual provider-reported usage on `model`.

    The write is a single `UPDATE users SET current_spend_usd =
    current_spend_usd + :cost WHERE id = :user_id RETURNING
    current_spend_usd` statement - never a read-modify-write in application
    code, mirroring `services.provider_keys.add_or_replace_key` /
    `services.model_policy.set_policy`'s atomic-upsert pattern. This is what
    makes concurrent charges from the same user never lose an update.

    Phase 5 (5.5, design doc section 2.3(c)): `precomputed_cost_usd`, when
    given, is used directly as `cost` instead of calling `compute_cost()` -
    `model` never needs a `PRICING_TABLE` entry on this path (the caller,
    `api.v1.gateway.common.record_usage_charge`, only ever passes this for a
    self-hosted request, whose cost comes from `providers.pricing.
    compute_self_hosted_cost()` instead). `None` (every other caller)
    preserves byte-for-byte pre-Phase-5 behavior.

    Call this ONLY after a provider response with confirmed, complete usage
    has been received. Raises `providers.pricing.PricingEntryMissingError`
    if `model` has no pricing entry AND `precomputed_cost_usd` is `None` -
    callers in the non-streaming path must let this propagate uncaught (it
    becomes a logged 500); the streaming path cannot offer that same
    guarantee once bytes are already on the wire (see gateway route
    handlers for how each path handles this).
    """
    cost = (
        precomputed_cost_usd
        if precomputed_cost_usd is not None
        else compute_cost(model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    )
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(current_spend_usd=User.current_spend_usd + cost)
        .returning(User.current_spend_usd)
    )
    result = await session.execute(stmt)
    new_total = result.scalar_one_or_none()
    await session.commit()
    if new_total is None:
        # Should be unreachable: user_id is FK-enforced off the
        # authenticated ServiceAccountKey row, and a referenced user can
        # never be deleted (ON DELETE RESTRICT).
        logger.error(
            "record_usage_charge_missing_user", extra={"user_id": str(user_id), "model": model}
        )
    return cost


@dataclass(frozen=True)
class ChargeResult:
    """Result of one usage charge (Phase 2, BD-8).

    `team_old_total`/`team_new_total` are the team's denormalized aggregate
    spend immediately before/after this charge, read for free from the
    charge `UPDATE`'s own RETURNING clause (design doc section 3.4) - the
    threshold-alert notifier (BD-18) compares `old/ceiling` vs
    `new/ceiling` against 0.8/1.0 to detect a false->true crossing. Both
    are None on the legacy flat-user path (no team aggregate exists) and in
    the should-be-unreachable missing-row case.

    BD-18: the team name/ceiling/alert-config fields ride the SAME RETURNING
    clause (zero extra queries, per section 3.4's stated budget) so the
    threshold check is pure arithmetic on values already in hand - see
    `services.notifiers.crossed_thresholds`.
    """

    cost: Decimal
    team_old_total: Decimal | None = None
    team_new_total: Decimal | None = None
    team_name: str | None = None
    team_ceiling_usd: Decimal | None = None
    team_alert_80_enabled: bool = False
    team_alert_100_enabled: bool = False
    team_webhook_alert_enabled: bool = False
    team_email_alert_enabled: bool = False


async def record_team_membership_usage_charge(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    model: str,
    prompt_tokens: int,
    completion_tokens: int | None,
    precomputed_cost_usd: Decimal | None = None,
) -> ChargeResult:
    """Charge the (team, user) `TeamMembership` counter for actual
    provider-reported usage on `model` (Phase 2, design doc section 3.2).

    Phase 5 (5.5): `precomputed_cost_usd`, when given, is used directly
    instead of calling `compute_cost()` - same contract/rationale as
    `record_usage_charge()`'s own parameter of the same name above.

    Same idempotency/atomicity contract as `record_usage_charge` - single
    `UPDATE ... RETURNING` statements, never a read-modify-write, called
    ONLY after a provider response with confirmed, complete usage. Updates
    BOTH the membership's own `current_spend_usd` (the spend-cutoff
    counter) AND the team's denormalized `current_spend_usd` aggregate
    (ADR-7) in the SAME transaction, via two single-row statements - never
    a SUM() aggregate query. The team RETURNING clause also computes the
    pre-charge total (`current_spend_usd - cost`) so threshold detection
    costs zero extra queries (section 3.4).

    Statement-order note (deviation from the design sketch's
    membership-then-team order): the team row is updated FIRST so this
    transaction acquires row locks parent-then-child, the same order
    `services.team_periods.ensure_current_period` locks in - the sketch's
    child-then-parent order could deadlock against a concurrent period
    reset. Same transaction, same RETURNING values either way.

    The membership row is addressed by `(team_id, user_id)` (unique-indexed
    - one row by construction, design doc section 3.1) rather than a
    `membership_id` parameter, so callers on the streaming path don't have
    to thread an extra id they never otherwise need.
    """
    cost = (
        precomputed_cost_usd
        if precomputed_cost_usd is not None
        else compute_cost(model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    )

    team_stmt = (
        update(Team)
        .where(Team.id == team_id)
        .values(current_spend_usd=Team.current_spend_usd + cost)
        .returning(
            # In UPDATE ... RETURNING, column references see NEW values -
            # `current_spend_usd - cost` is therefore the pre-charge total.
            (Team.current_spend_usd - cost).label("old_total"),
            Team.current_spend_usd.label("new_total"),
            # BD-18: alert-config fields for threshold detection, free from
            # the same statement (design doc section 3.4).
            Team.name,
            Team.budget_ceiling_usd,
            Team.alert_threshold_80_enabled,
            Team.alert_threshold_100_enabled,
            Team.webhook_alert_enabled,
            Team.email_alert_enabled,
        )
    )
    team_row = (await session.execute(team_stmt)).one_or_none()

    membership_stmt = (
        update(TeamMembership)
        .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        .values(current_spend_usd=TeamMembership.current_spend_usd + cost)
        .returning(TeamMembership.current_spend_usd)
    )
    new_membership_total = (await session.execute(membership_stmt)).scalar_one_or_none()
    await session.commit()

    if team_row is None or new_membership_total is None:
        # Should be unreachable: the membership is guaranteed by
        # construction (a personal/team-attributed key can only exist while
        # its owner holds the membership - ADR-4 blocks removal while keys
        # exist), and the team row is FK-referenced by that membership.
        logger.error(
            "record_team_membership_usage_charge_missing_row",
            extra={"team_id": str(team_id), "user_id": str(user_id), "model": model},
        )
        return ChargeResult(cost=cost)
    return ChargeResult(
        cost=cost,
        team_old_total=team_row.old_total,
        team_new_total=team_row.new_total,
        team_name=team_row.name,
        team_ceiling_usd=team_row.budget_ceiling_usd,
        team_alert_80_enabled=team_row.alert_threshold_80_enabled,
        team_alert_100_enabled=team_row.alert_threshold_100_enabled,
        team_webhook_alert_enabled=team_row.webhook_alert_enabled,
        team_email_alert_enabled=team_row.email_alert_enabled,
    )


@dataclass(frozen=True)
class OrgChargeResult:
    """Result of the org-wide spend increment (added alongside `0045` -
    mirrors `ChargeResult`'s `team_*` fields one level up). Always
    populated (never `None` fields) - unlike the team charge, there's no
    "legacy path with no aggregate" case to account for."""

    old_total: Decimal
    new_total: Decimal
    ceiling_usd: Decimal | None
    # 50/75/100% (added by `0046`) - NOT team's 80/100 set, see that
    # migration's docstring.
    alert_50_enabled: bool
    alert_75_enabled: bool
    alert_100_enabled: bool
    webhook_alert_enabled: bool
    email_alert_enabled: bool


async def record_org_usage_charge(session: AsyncSession, *, cost: Decimal) -> OrgChargeResult:
    """Atomically increment the org's denormalized `current_spend_usd`
    (added alongside `0045` - see that migration's docstring).

    `api.v1.gateway.common.record_usage_charge` is the single choke point
    that calls this, ALWAYS (regardless of whether the charge went through
    the legacy flat-`User` path or the team-scoped path) - the org
    safeguard is meant to catch total spend across every path, not just
    team-scoped ones.

    A single `INSERT ... ON CONFLICT (org_id) DO UPDATE` (upsert-increment)
    rather than the plain `UPDATE ... RETURNING` `record_usage_charge`/
    `record_team_membership_usage_charge` use above: those two update rows
    that are GUARANTEED to already exist (FK-referenced `User`/
    `TeamMembership` rows); an `org_settings` row is NOT guaranteed to
    exist (ADR-2: absence-of-row is the normal default state for an org
    that has never touched org settings) - the upsert creates it on first
    spend rather than requiring an admin to have configured something
    first.

    Deliberately its OWN transaction/commit, separate from whichever of
    `record_usage_charge`/`record_team_membership_usage_charge` already
    committed the user/team charge immediately before this is called (see
    the gateway wrapper) - there is no cross-table invariant to preserve
    atomically between them (unlike `record_team_membership_usage_charge`'s
    own team+membership pair, which DOES need one transaction for ADR-7).
    A crash between the two commits leaves the org total a cost-of-one-
    request short of perfectly in sync - an accepted, tiny race, same
    class as the pre-request check-then-charge race already accepted
    everywhere else in this module.
    """
    stmt = (
        postgresql.insert(OrgSettings)
        .values(org_id=DEFAULT_ORG_ID, current_spend_usd=cost)
        .on_conflict_do_update(
            index_elements=[OrgSettings.org_id],
            set_={"current_spend_usd": OrgSettings.current_spend_usd + cost},
        )
        .returning(
            # Same NEW-values-visible-in-RETURNING trick as the team
            # charge above: `current_spend_usd - cost` is the pre-charge
            # total (0 for a freshly-inserted row, correctly representing
            # "started at 0, now at cost").
            (OrgSettings.current_spend_usd - cost).label("old_total"),
            OrgSettings.current_spend_usd.label("new_total"),
            OrgSettings.budget_ceiling_usd,
            OrgSettings.alert_threshold_50_enabled,
            OrgSettings.alert_threshold_75_enabled,
            OrgSettings.alert_threshold_100_enabled,
            OrgSettings.webhook_alert_enabled,
            OrgSettings.email_alert_enabled,
        )
    )
    row = (await session.execute(stmt)).one()
    await session.commit()
    return OrgChargeResult(
        old_total=row.old_total,
        new_total=row.new_total,
        ceiling_usd=row.budget_ceiling_usd,
        alert_50_enabled=row.alert_threshold_50_enabled,
        alert_75_enabled=row.alert_threshold_75_enabled,
        alert_100_enabled=row.alert_threshold_100_enabled,
        webhook_alert_enabled=row.webhook_alert_enabled,
        email_alert_enabled=row.email_alert_enabled,
    )


async def reset_org_spend(session: AsyncSession) -> OrgSettings:
    """Explicit admin action that zeroes the org-wide spend counter (added
    alongside `0045` - see that migration's docstring for why this is
    manual-only, never an automatic period reset). Upserts the row first
    (same `on_conflict_do_nothing` pattern `services.team_budget.
    set_org_budget_ceiling` uses) so there is always a row to update, even
    for an org that has never touched org settings. Flushes, does not
    commit - the caller writes its own audit entry on the same transaction
    (design doc section 7's established convention)."""
    await session.execute(
        postgresql.insert(OrgSettings)
        .values(org_id=DEFAULT_ORG_ID)
        .on_conflict_do_nothing(index_elements=[OrgSettings.org_id])
    )
    row = (
        await session.execute(select(OrgSettings).where(OrgSettings.org_id == DEFAULT_ORG_ID))
    ).scalar_one()
    row.current_spend_usd = Decimal(0)
    await session.flush()
    return row
