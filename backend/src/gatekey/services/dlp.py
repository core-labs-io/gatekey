"""Presidio-backed PII/DLP scanning (Phase 3 - Security & Compliance
Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 2 (NFR
accounting) and section 10 fork #2 for why `build_analyzer_engine()` uses a
restricted, pattern-recognizer-only registry (SSN/credit-card/email/phone -
AC2.2, none NER-dependent) and the small `en_core_web_sm` spaCy model rather
than `RecognizerRegistry.load_predefined_recognizers()`'s full default set -
this is what keeps the synchronous redact/block scan path under AC2.10's
~50ms p99 target (measured ~10ms warm for a typical prompt on this build -
see `api/v1/gateway/common.py`'s pipeline wiring and the QA load-test note).

Action precedence (AC2.4, a two-layer system - do not add a per-key
override): built-in-detector findings use `resolve_builtin_action` (team
override, if any, else the org default); custom-pattern findings always use
that pattern's own independent `action`, never the org/team default.

Sync vs. async execution (AC2.5/AC2.6/AC2.8/AC2.9): `requires_sync_scan()`
decides, from POLICY CONFIG alone (never from a particular request's scan
results, which aren't known yet), whether a request must be scanned
synchronously (redact/block anywhere in the configured policy, or an enabled
content-aware 'pii' rule) before being forwarded, or may run log-only and
best-effort. The caller (`api/v1/gateway/common.py`) is responsible for
running the synchronous path inline and the async path via `BackgroundTasks`
- see that module's docstring.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import presidio_analyzer.analyzer_engine as _presidio_analyzer_engine_module
import presidio_analyzer.pattern_recognizer as _presidio_pattern_recognizer_module
from presidio_analyzer import AnalyzerEngine, EntityRecognizer, Pattern, PatternRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    PhoneRecognizer,
    UsSsnRecognizer,
)
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.dlp_custom_pattern import DlpCustomPattern
from gatekey.db.models.dlp_policy import DlpAction, DlpPolicy
from gatekey.db.models.dlp_scan_result import DlpScanResult
from gatekey.db.models.team_dlp_action_override import TeamDlpActionOverride
from gatekey.errors import GatekeyError
from gatekey.services.content_classifiers import is_legal_content, is_source_code

if TYPE_CHECKING:
    # Local, TYPE_CHECKING-only import (Fix 3) - `services.response_cache`
    # does not itself import `services.dlp`, so this isn't strictly a
    # circular-import risk today, but kept as a type-only import for
    # consistency with `services.residency`'s identical pattern and to
    # avoid ever introducing one as either module grows.
    from gatekey.services.response_cache import CacheInvalidator

logger = logging.getLogger("gatekey")

# AC2.2 - the four built-in detectors, mapped to the Presidio entity type
# each toggle enables.
_DETECTOR_ENTITY_MAP: dict[str, str] = {
    "ssn": "US_SSN",
    "credit_card": "CREDIT_CARD",
    "email": "EMAIL_ADDRESS",
    "phone": "PHONE_NUMBER",
}

# Phase 5 (5.3, AC5.3.1): "financial_data" built-in patterns, added to the
# SAME Presidio engine/registry `_DETECTOR_ENTITY_MAP`'s four detectors use -
# reuses the existing scan engine rather than building a parallel one. These
# are NOT gated by a `dlp_policies` detector toggle (no
# `financial_data_detector_enabled` column exists, by design - see the
# design doc's data-model checklist, section 8: no new `dlp_policies` column
# this phase); the only admin-facing enable/disable point for this category
# is the `content_aware_rules` row itself (`scan_texts`'s
# `content_aware_categories_enabled` parameter gates whether these entity
# types are even included in a given scan - see that function's docstring).
#
# Deterministic (score=1.0), keyword-anchored regex patterns - NOT
# Presidio's own predefined `UsBankRecognizer`/`AbaRoutingRecognizer`
# (which ship with deliberately low base scores, e.g. 0.05, meant to be
# boosted by nearby context words under a Presidio deployment's own
# `default_score_threshold`, typically > 0). `build_analyzer_engine()`
# constructs its `AnalyzerEngine` with the library default
# `default_score_threshold=0` (unchanged this phase, to avoid affecting the
# existing SSN/credit-card/email/phone recognizers' calibration) - at
# threshold 0, an unboosted 0.05-score match would still be returned,
# which would make virtually any 8-17 digit number a "financial_data"
# finding. Keyword-anchoring the regex itself, at score 1.0 (mirroring this
# module's existing custom-DLP-pattern idiom - see `_scan_segment_sync`'s
# `ad_hoc_recognizers` construction), avoids that false-positive explosion.
_FINANCIAL_DATA_ENTITY_TYPES: tuple[str, ...] = (
    "GATEKEY_IBAN",
    "GATEKEY_SWIFT_BIC",
    "GATEKEY_BANK_ACCOUNT",
    "GATEKEY_FINANCIAL_PROXIMITY",
)

# Every Presidio entity type this module's built-in recognizers can ever
# produce, mapped to the content-classification category it feeds
# (Phase 5, 5.3, AC5.3.1/AC5.3.2's `category_findings`). A finding whose
# entity type isn't listed here (i.e. a custom org-authored DLP pattern,
# named `"custom:<pattern name>"`) is treated as `"pii"` - preserving the
# pre-Phase-5 semantics where ANY finding (built-in or custom) set the old
# `pii_detected` flag (see `_category_for_finding` below).
_ENTITY_CATEGORY_MAP: dict[str, str] = {
    "US_SSN": "pii",
    "CREDIT_CARD": "pii",
    "EMAIL_ADDRESS": "pii",
    "PHONE_NUMBER": "pii",
    "GATEKEY_IBAN": "financial_data",
    "GATEKEY_SWIFT_BIC": "financial_data",
    "GATEKEY_BANK_ACCOUNT": "financial_data",
    "GATEKEY_FINANCIAL_PROXIMITY": "financial_data",
}


def _category_for_finding(name: str) -> str:
    """Maps a `DlpFinding.name` to its content-classification category -
    `"custom:<pattern name>"` (an org-authored ad-hoc pattern) always maps
    to `"pii"`, matching the pre-Phase-5 `pii_detected = bool(findings)`
    semantics (any finding at all, built-in or custom, counted)."""
    if name.startswith("custom:"):
        return "pii"
    return _ENTITY_CATEGORY_MAP.get(name, "pii")


def _build_financial_data_recognizers() -> list[PatternRecognizer]:
    """Bank account/routing number, IBAN, SWIFT/BIC, and currency-near-
    keyword-proximity ("revenue"/"EBITDA"/"wire transfer") pattern
    recognizers (AC5.3.1's "financial_data" bullet), registered into the
    same `RecognizerRegistry` `build_analyzer_engine()` builds - see the
    module-level comment on `_FINANCIAL_DATA_ENTITY_TYPES` for why these are
    hand-written, keyword-anchored, score-1.0 recognizers rather than
    Presidio's own low-base-score predefined ones."""
    currency = r"(?:\$\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP)\b)"
    keyword = r"\b(?:revenue|EBITDA|wire transfer)\b"
    return [
        PatternRecognizer(
            supported_entity="GATEKEY_IBAN",
            patterns=[
                Pattern(
                    name="iban",
                    # 2-letter country code + 2-digit checksum + 11-30
                    # alphanumeric BBAN chars (real-world IBAN lengths run
                    # 15-34 total, not necessarily a multiple of 4 -
                    # deliberately not grouped into rigid 4-char blocks).
                    regex=r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
                    score=1.0,
                )
            ],
        ),
        PatternRecognizer(
            supported_entity="GATEKEY_SWIFT_BIC",
            patterns=[
                Pattern(
                    name="swift_bic",
                    regex=r"\b(?:SWIFT|BIC)\b(?:\s*code)?\s*[:#]?\s*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b",
                    score=1.0,
                )
            ],
        ),
        PatternRecognizer(
            supported_entity="GATEKEY_BANK_ACCOUNT",
            patterns=[
                Pattern(
                    name="routing_number",
                    regex=r"\b(?:routing\s*(?:number|no\.?|#)|ABA\s*(?:number|no\.?|#)?)\s*[:#]?\s*(\d{9})\b",
                    score=1.0,
                ),
                Pattern(
                    name="account_number",
                    regex=r"\b(?:account|acct\.?)\s*(?:number|no\.?|#)\s*[:#]?\s*(\d{6,17})\b",
                    score=1.0,
                ),
            ],
        ),
        PatternRecognizer(
            supported_entity="GATEKEY_FINANCIAL_PROXIMITY",
            patterns=[
                Pattern(
                    name="currency_near_keyword",
                    regex=rf"{currency}[^\n]{{0,40}}{keyword}|{keyword}[^\n]{{0,40}}{currency}",
                    score=1.0,
                )
            ],
        ),
    ]


_SPACY_MODEL_NAME = "en_core_web_sm"
_REDACTION_PLACEHOLDER = "[REDACTED]"
_ACTION_SEVERITY: dict[DlpAction, int] = {DlpAction.LOG: 0, DlpAction.REDACT: 1, DlpAction.BLOCK: 2}

# Security review finding 3: `PatternRecognizer`/`AnalyzerEngine` (both
# built-in detectors AC2.2 uses and org-supplied `dlp_custom_patterns` -
# `validate_pattern_regex` below only checks compilability, not backtracking
# cost) run every regex match through the `regex` library's own timeout via
# a module-level `REGEX_TIMEOUT_SECONDS` each of those two Presidio modules
# reads from the `REGEX_TIMEOUT_SECONDS` env var ONCE AT IMPORT TIME
# (defaults to 60s - presidio_analyzer/pattern_recognizer.py), which would
# blow AC2.10's <50ms p99 synchronous scan-path budget on one pathological
# custom pattern. Setting the env var here would be an import-order race
# (other Gatekey modules, e.g. api/deps.py, also import presidio_analyzer,
# possibly first) - overwriting the already-imported module attribute
# directly is the reliable mechanism, since both modules look it up by name
# at call time, not at their own import time.
_presidio_pattern_recognizer_module.REGEX_TIMEOUT_SECONDS = 2
_presidio_analyzer_engine_module.REGEX_TIMEOUT_SECONDS = 2


@functools.lru_cache(maxsize=1)
def build_analyzer_engine() -> AnalyzerEngine:
    """Build the process-lifetime Presidio `AnalyzerEngine` singleton.

    Fork #2 (design doc section 10): the registry holds exactly the four
    pattern-based recognizers AC2.2 requires - not the full predefined
    recognizer set, which would pull in NER-based recognizers (person,
    location, ...) that would blow the <50ms p99 budget for no benefit this
    phase needs. `en_core_web_sm` is still required (Presidio's
    `AnalyzerEngine` always needs an `NlpEngine` for tokenization/context
    features, even for purely regex-based recognizers) - the small model
    keeps that overhead minimal.

    Expensive (loads a spaCy model, ~1-2s) - `main.py`'s lifespan calls this
    once and stores the result on `app.state`. `@lru_cache` (not a bare
    module-level global) additionally memoizes it PROCESS-wide across
    however many times `create_app()` itself runs in one process (every
    gateway unit test builds a fresh app via `TestClient(app)`'s lifespan) -
    the engine carries no per-org/per-app state, so sharing one instance
    across app instances within a process is safe and avoids re-paying the
    spaCy load cost dozens of times in the test suite.
    """
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": _SPACY_MODEL_NAME}],
        }
    )
    nlp_engine = provider.create_engine()

    registry = RecognizerRegistry()
    registry.add_recognizer(UsSsnRecognizer())
    registry.add_recognizer(CreditCardRecognizer())
    registry.add_recognizer(EmailRecognizer())
    registry.add_recognizer(PhoneRecognizer())
    # Phase 5 (5.3, AC5.3.1): "financial_data" - see
    # `_build_financial_data_recognizers()`'s docstring for why these are
    # registered here (always available in the registry) but only ever
    # included in a given scan's `entities` list conditionally - see
    # `_scan_segment_sync`.
    for recognizer in _build_financial_data_recognizers():
        registry.add_recognizer(recognizer)

    return AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["en"])


class InvalidCustomPatternRegexError(GatekeyError):
    """A `dlp_custom_patterns.pattern` value doesn't compile as a regex
    (design doc section 1.4: "validated compilable at write time"). The
    submitted pattern source is caller input, not secret material - safe in
    `message`."""

    status_code = 422
    code = "invalid_dlp_custom_pattern_regex"


class DuplicateCustomPatternNameError(GatekeyError):
    """`(org_id, name)` must be unique (db/models/dlp_custom_pattern.py)."""

    status_code = 409
    code = "dlp_custom_pattern_name_conflict"


class InboundScanningNotImplementedError(GatekeyError):
    """Security review finding 4: `dlp_policies.scan_inbound_responses` is a
    persisted, round-tripped column, but scanning PROVIDER RESPONSES (as
    opposed to inbound prompts) was deliberately deferred (product-spec
    ambiguity A4) - no code path in `api/v1/gateway/*.py` ever scans one.
    Rejecting an attempt to turn it on keeps the toggle from looking
    functional when it silently does nothing; `false`/absent (the default,
    already a no-op) stays accepted."""

    status_code = 422
    code = "inbound_scanning_not_implemented"


def validate_scan_inbound_responses(scan_inbound_responses: bool) -> None:
    if scan_inbound_responses:
        raise InboundScanningNotImplementedError(
            "scan_inbound_responses is not yet implemented: no code path scans provider "
            "responses. Leave this false - setting it true would not actually scan anything."
        )


def validate_pattern_regex(pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise InvalidCustomPatternRegexError(
            f"'{pattern}' is not a valid regular expression: {exc}"
        ) from None


# ---------------------------------------------------------------------------
# Policy configuration (no in-process cache - see design doc section 2; only
# ResidencyRuleCache/ModelPolicyCache-style caches are called for by name in
# the design, and a single indexed-row read here is the same "not zero-I/O,
# but cheap" tradeoff `check_budget_available` already makes on this hot
# path).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DlpPolicyConfig:
    detectors_enabled: dict[str, bool]
    default_action: DlpAction
    store_raw_flagged_content: bool
    scan_inbound_responses: bool


_DEFAULT_POLICY = DlpPolicyConfig(
    detectors_enabled=dict.fromkeys(_DETECTOR_ENTITY_MAP, False),
    default_action=DlpAction.LOG,
    store_raw_flagged_content=False,
    scan_inbound_responses=False,
)


@dataclass(frozen=True)
class CustomPatternConfig:
    name: str
    pattern: str
    action: DlpAction


async def load_dlp_policy(session: AsyncSession) -> DlpPolicyConfig:
    """Absence of a row = every detector off, `default_action="log"` (ADR-1
    style default - see `db/models/dlp_policy.py`)."""
    row = (
        await session.execute(select(DlpPolicy).where(DlpPolicy.org_id == DEFAULT_ORG_ID))
    ).scalar_one_or_none()
    if row is None:
        return _DEFAULT_POLICY
    return DlpPolicyConfig(
        detectors_enabled={
            "ssn": row.ssn_detector_enabled,
            "credit_card": row.credit_card_detector_enabled,
            "email": row.email_detector_enabled,
            "phone": row.phone_detector_enabled,
        },
        default_action=row.default_action,
        store_raw_flagged_content=row.store_raw_flagged_content,
        scan_inbound_responses=row.scan_inbound_responses,
    )


async def load_custom_patterns(session: AsyncSession) -> list[CustomPatternConfig]:
    rows = (
        await session.execute(
            select(DlpCustomPattern).where(DlpCustomPattern.org_id == DEFAULT_ORG_ID)
        )
    ).scalars().all()
    return [CustomPatternConfig(name=r.name, pattern=r.pattern, action=r.action) for r in rows]


async def get_team_dlp_override(session: AsyncSession, team_id: uuid.UUID) -> DlpAction | None:
    row = (
        await session.execute(
            select(TeamDlpActionOverride).where(TeamDlpActionOverride.team_id == team_id)
        )
    ).scalar_one_or_none()
    return row.action if row is not None else None


def resolve_builtin_action(default_action: DlpAction, team_override: DlpAction | None) -> DlpAction:
    """AC2.4: two-layer, most-specific-wins precedence for built-in-detector
    findings only - custom patterns never consult this (they carry their own
    independent `action`)."""
    return team_override if team_override is not None else default_action


def has_any_scanning_enabled(policy: DlpPolicyConfig, custom_patterns: list[CustomPatternConfig]) -> bool:
    """Fast no-op check: nothing configured to look for at all -> the
    caller can skip Presidio entirely."""
    return any(policy.detectors_enabled.values()) or bool(custom_patterns)


def requires_sync_scan(
    *,
    effective_builtin_action: DlpAction,
    custom_patterns: list[CustomPatternConfig],
    content_aware_classification_enabled: bool,
) -> bool:
    """Pure function of POLICY CONFIG only (AC2.6) - never of a particular
    request's scan results, which aren't known before the scan runs. True
    when ANY configured action anywhere (org/team default, or any individual
    custom pattern) could redact or block, or ANY enabled content-aware
    routing category (AC2.9's original 'pii'-only rule, generalized in
    Phase 5/5.3 to 'pii'/'financial_data'/'source_code'/'legal' alike -
    AC5.3.2's multi-category resolution needs a completed, synchronous scan
    before routing can finalize regardless of WHICH category triggered it;
    a category classified only after the response has already been
    dispatched can never actually restrict routing).

    `content_aware_classification_enabled` is `True` whenever ANY of the
    four content-classification categories has an enabled
    `content_aware_rules` row for this org - see `api.v1.gateway.common.
    run_dlp_scan`'s call site (parameter renamed from the pre-Phase-5
    `content_aware_pii_enabled` - every pre-Phase-5 caller that only ever
    had a 'pii' rule to consider passes byte-identical behavior through
    unchanged, since 'pii' is one of the four categories this now checks)."""
    if effective_builtin_action != DlpAction.LOG:
        return True
    if any(p.action != DlpAction.LOG for p in custom_patterns):
        return True
    return content_aware_classification_enabled


# ---------------------------------------------------------------------------
# Scanning + redaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DlpFinding:
    name: str  # Presidio entity type (built-in) or "custom:<pattern name>"
    action: DlpAction


@dataclass(frozen=True)
class SegmentScanResult:
    redacted_text: str
    findings: list[DlpFinding]
    blocked: bool
    # Phase 5 (5.3): "source_code"/"legal" have no DLP action (AC5.3.1 -
    # "redact doesn't make sense for code") - these two booleans feed only
    # `category_findings`, never `findings`/`blocked`/redaction.
    source_code_detected: bool = False
    legal_detected: bool = False


def _scan_segment_sync(
    engine: AnalyzerEngine,
    text: str,
    *,
    policy: DlpPolicyConfig,
    custom_patterns: list[CustomPatternConfig],
    team_override: DlpAction | None,
    financial_data_needed: bool = False,
    source_code_needed: bool = False,
    legal_needed: bool = False,
) -> SegmentScanResult:
    """Blocking - Presidio's spaCy pipeline is synchronous, CPU-bound code.
    Must be called via `asyncio.to_thread` (see `scan_texts`), never awaited
    directly on the event loop.

    Phase 5 (5.3): `financial_data_needed`/`source_code_needed`/
    `legal_needed` gate whether each NEW category is evaluated for this
    segment at all (see `scan_texts`'s docstring for the full gating
    rationale) - `source_code`/`legal` are evaluated independently of
    Presidio's `entities` list (they're not Presidio-engine-based at all),
    so they still run even when `entities` ends up empty (e.g. an org with
    every PII detector off but `source_code` content-aware routing
    enabled).
    """
    enabled_entities = [
        entity for key, entity in _DETECTOR_ENTITY_MAP.items() if policy.detectors_enabled.get(key)
    ]
    if financial_data_needed:
        enabled_entities = enabled_entities + list(_FINANCIAL_DATA_ENTITY_TYPES)
    custom_entity_names = [f"CUSTOM_{i}" for i in range(len(custom_patterns))]
    # Typed as the base class - `AnalyzerEngine.analyze`'s `ad_hoc_recognizers`
    # param is `list[EntityRecognizer]`; mypy's list-invariance rule rejects a
    # `list[PatternRecognizer]` there even though every element genuinely is
    # one (PatternRecognizer subclasses EntityRecognizer).
    ad_hoc_recognizers: list[EntityRecognizer] = [
        PatternRecognizer(
            supported_entity=custom_entity_names[i],
            patterns=[Pattern(name=cp.name, regex=cp.pattern, score=1.0)],
        )
        for i, cp in enumerate(custom_patterns)
    ]
    entities = enabled_entities + custom_entity_names

    findings: list[DlpFinding] = []
    to_redact = []
    blocked = False
    redacted_text = text

    if entities:
        results = engine.analyze(
            text=text, language="en", entities=entities, ad_hoc_recognizers=ad_hoc_recognizers
        )

        builtin_action = resolve_builtin_action(policy.default_action, team_override)
        for result in results:
            if result.entity_type.startswith("CUSTOM_"):
                idx = int(result.entity_type.removeprefix("CUSTOM_"))
                cp = custom_patterns[idx]
                name = f"custom:{cp.name}"
                action = cp.action
            else:
                name = result.entity_type
                action = builtin_action
            findings.append(DlpFinding(name=name, action=action))
            if action == DlpAction.BLOCK:
                blocked = True
            elif action == DlpAction.REDACT:
                to_redact.append(result)

        # A blocked request never reaches the provider, so redaction of a
        # blocked segment is pointless work - skip it.
        if to_redact and not blocked:
            anonymizer = AnonymizerEngine()
            redacted_text = anonymizer.anonymize(
                text=text,
                # presidio_anonymizer's own `RecognizerResult` type (a
                # distinct class from presidio_analyzer's, structurally
                # identical - start/end/entity_type/score) is what this
                # signature declares; presidio_analyzer's results are
                # accepted at runtime (this is the documented, standard
                # analyzer -> anonymizer handoff) but mypy sees two
                # same-shaped classes from different packages.
                analyzer_results=to_redact,  # type: ignore[arg-type]
                operators={"DEFAULT": OperatorConfig("replace", {"new_value": _REDACTION_PLACEHOLDER})},
            ).text

    source_code_detected = source_code_needed and is_source_code(text)
    legal_detected = legal_needed and is_legal_content(text)

    return SegmentScanResult(
        redacted_text=redacted_text,
        findings=findings,
        blocked=blocked,
        source_code_detected=source_code_detected,
        legal_detected=legal_detected,
    )


@dataclass(frozen=True)
class DlpScanOutcome:
    ran: bool  # False = nothing configured to scan for (fast no-op path)
    blocked: bool
    findings: list[DlpFinding]
    redacted_texts: list[str] | None  # same length/order as the input `texts`; None if nothing redacted
    # Phase 5 (5.3, AC5.3.1): the set of triggered content-classification
    # categories for this request ("pii"/"financial_data"/"source_code"/
    # "legal") - feeds `services.model_policy.resolve_content_
    # classification`'s multi-category intersection (AC5.3.2). Does NOT
    # include the sensitivity-label short-circuit's pre-trusted categories
    # (AC5.3.5) - `api.v1.gateway.common.run_dlp_scan` ORs those in
    # separately, since this function has no knowledge of that header.
    category_findings: frozenset[str] = frozenset()
    # Kept for backward compatibility (design doc section 2.4's explicit
    # "no forked code path" decision) - a PURE DERIVATION of
    # `category_findings`, computed once at construction, never
    # independently maintained. Every pre-Phase-5 direct reader of this
    # field keeps working unchanged.
    pii_detected: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "pii_detected", "pii" in self.category_findings)


async def scan_texts(
    engine: AnalyzerEngine,
    texts: list[str],
    *,
    policy: DlpPolicyConfig,
    custom_patterns: list[CustomPatternConfig],
    team_override: DlpAction | None,
    content_aware_categories_enabled: frozenset[str] = frozenset(),
    skip_categories: frozenset[str] = frozenset(),
) -> DlpScanOutcome:
    """Scan every segment (e.g. one per chat message) independently via
    `asyncio.to_thread` (AC2.1: in-process, no external call - just kept off
    the event loop since Presidio's own call is blocking).

    Phase 5 (5.3): `content_aware_categories_enabled` is the subset of
    `{"financial_data", "source_code", "legal"}` that has an ENABLED
    `content_aware_rules` row for this org (deliberately excludes "pii" -
    that category's own gating is unchanged, driven entirely by
    `policy.detectors_enabled`/`has_any_scanning_enabled`, per AC2.2/AC2.9).
    `skip_categories` (AC5.3.5's sensitivity-label short-circuit) is the set
    of categories whose CONTENT-CLASSIFICATION-ROUTING heuristic (i.e. which
    category `category_findings` should attribute this request to, for
    `resolve_content_classification`'s model-routing decision) need not be
    re-evaluated here, because the caller already knows (via a trusted,
    pre-set upstream label) this request belongs to that category and will
    OR it into the final result itself - see `api.v1.gateway.common.
    run_dlp_scan`'s docstring.

    Security invariant (fixed - previously violated for "financial_data",
    see the design doc's Security Considerations table: the sensitivity-
    label header "can never suppress DLP redaction/block actions"):
    `skip_categories` ONLY ever short-circuits the classification/routing
    signal. It must NEVER suppress the underlying Presidio entity scan
    itself. Concretely, "financial_data"'s Presidio entity types
    (`_FINANCIAL_DATA_ENTITY_TYPES`) are included in this scan's `entities`
    list whenever "financial_data" content-aware routing is enabled,
    REGARDLESS of `skip_categories` - a pretrusted label may only skip the
    heuristic-classifier evaluation for "source_code"/"legal" (which have no
    DLP action at all - AC5.3.1 - so short-circuiting their heuristic
    classifiers has zero DLP consequence), never the entity scan that drives
    redaction/blocking."""
    # Redaction/blocking (the Presidio entity scan) must always run for
    # "financial_data" whenever that category's content-aware routing is
    # enabled - a pretrusted label never suppresses this, only the
    # classification-routing signal below (via `skip_categories`, which
    # deliberately does NOT subtract from this line - see docstring above).
    financial_data_needed = "financial_data" in content_aware_categories_enabled
    # "source_code"/"legal" have no DLP action (AC5.3.1) - their heuristic
    # classifiers are purely a routing signal, safe to short-circuit via a
    # trusted label.
    source_code_needed = "source_code" in content_aware_categories_enabled - skip_categories
    legal_needed = "legal" in content_aware_categories_enabled - skip_categories

    if not (
        has_any_scanning_enabled(policy, custom_patterns)
        or financial_data_needed
        or source_code_needed
        or legal_needed
    ):
        return DlpScanOutcome(ran=False, blocked=False, findings=[], redacted_texts=None)

    segment_results = await asyncio.gather(
        *(
            asyncio.to_thread(
                _scan_segment_sync,
                engine,
                segment,
                policy=policy,
                custom_patterns=custom_patterns,
                team_override=team_override,
                financial_data_needed=financial_data_needed,
                source_code_needed=source_code_needed,
                legal_needed=legal_needed,
            )
            for segment in texts
        )
    )

    findings: list[DlpFinding] = []
    redacted_texts: list[str] = []
    blocked = False
    any_redacted = False
    category_findings: set[str] = set()
    for original, result in zip(texts, segment_results, strict=True):
        findings.extend(result.findings)
        redacted_texts.append(result.redacted_text)
        if result.redacted_text != original:
            any_redacted = True
        if result.blocked:
            blocked = True
        if result.source_code_detected:
            category_findings.add("source_code")
        if result.legal_detected:
            category_findings.add("legal")

    for finding in findings:
        category_findings.add(_category_for_finding(finding.name))

    return DlpScanOutcome(
        ran=True,
        blocked=blocked,
        findings=findings,
        redacted_texts=redacted_texts if any_redacted else None,
        category_findings=frozenset(category_findings),
    )


def overall_action_taken(findings: list[DlpFinding]) -> DlpAction:
    """The most restrictive action among every finding - what actually
    happened to the request as a whole (`dlp_scan_results.action_taken`).
    Per-finding fidelity is preserved separately in `findings` (AC2.7)."""
    if not findings:
        return DlpAction.LOG
    return max((f.action for f in findings), key=lambda a: _ACTION_SEVERITY[a])


async def record_scan_result(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: str,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    model: str,
    ran_sync: bool,
    findings: list[DlpFinding],
    raw_texts: list[str] | None,
    store_raw: bool,
) -> None:
    """INSERT one `DlpScanResult` row and commit (design doc section 1.9 -
    keyed by `request_id`, deliberately decoupled from `usage_logs`'
    lifecycle). `raw_texts` (the flagged segment(s), verbatim) is only ever
    persisted when `store_raw` is True (ratified #3, `dlp_policies.
    store_raw_flagged_content`)."""
    session.add(
        DlpScanResult(
            org_id=org_id,
            request_id=request_id,
            team_id=team_id,
            user_id=user_id,
            model=model,
            ran_sync=ran_sync,
            action_taken=overall_action_taken(findings),
            findings=[{"detector_or_pattern_name": f.name, "action": f.action.value} for f in findings],
            raw_flagged_content=list(raw_texts) if (store_raw and raw_texts) else None,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Admin CRUD (api/v1/admin/dlp_policy.py)
# ---------------------------------------------------------------------------


async def get_dlp_policy_row(session: AsyncSession) -> DlpPolicy | None:
    return (
        await session.execute(select(DlpPolicy).where(DlpPolicy.org_id == DEFAULT_ORG_ID))
    ).scalar_one_or_none()


async def set_dlp_policy(
    session: AsyncSession,
    *,
    ssn_detector_enabled: bool,
    credit_card_detector_enabled: bool,
    email_detector_enabled: bool,
    phone_detector_enabled: bool,
    default_action: DlpAction,
    store_raw_flagged_content: bool,
    scan_inbound_responses: bool,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> DlpPolicy:
    """Full-replace upsert, mirrors `services.model_policy.set_policy`.

    Raises `InboundScanningNotImplementedError` (422) if `scan_inbound_
    responses` is True - checked here, the only caller, so any future
    caller inherits the guard for free.

    Fix 3 (security review, BLOCKING): tightening the org DLP policy (e.g.
    default_action log -> block, or enabling a previously-off detector) must
    not leave a response cached under the OLD, more permissive policy still
    servable for the rest of its TTL (up to 24h) - `api.v1.gateway.common.
    check_response_cache()` returns a cache HIT before `run_dlp_scan()` ever
    runs. Org-wide (`clear_all()`), same rationale as `services.residency.
    set_org_residency_rule`'s identical fix - this policy has no narrower
    "just this team" blast radius. Best-effort/fail-open, never blocks this
    write - see that function's docstring for the full reasoning.
    """
    validate_scan_inbound_responses(scan_inbound_responses)
    insert_stmt = postgresql.insert(DlpPolicy).values(
        org_id=DEFAULT_ORG_ID,
        ssn_detector_enabled=ssn_detector_enabled,
        credit_card_detector_enabled=credit_card_detector_enabled,
        email_detector_enabled=email_detector_enabled,
        phone_detector_enabled=phone_detector_enabled,
        default_action=default_action,
        store_raw_flagged_content=store_raw_flagged_content,
        scan_inbound_responses=scan_inbound_responses,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[DlpPolicy.org_id],
        set_={
            "ssn_detector_enabled": insert_stmt.excluded.ssn_detector_enabled,
            "credit_card_detector_enabled": insert_stmt.excluded.credit_card_detector_enabled,
            "email_detector_enabled": insert_stmt.excluded.email_detector_enabled,
            "phone_detector_enabled": insert_stmt.excluded.phone_detector_enabled,
            "default_action": insert_stmt.excluded.default_action,
            "store_raw_flagged_content": insert_stmt.excluded.store_raw_flagged_content,
            "scan_inbound_responses": insert_stmt.excluded.scan_inbound_responses,
            "updated_at": text("now()"),
        },
    ).returning(DlpPolicy)
    # Hardening pass item 1: `populate_existing=True` is REQUIRED, not
    # decorative - see `services.residency.set_org_residency_rule`'s
    # docstring for the full SQLAlchemy-identity-map mechanics (`api/v1/
    # admin/dlp_policy.py`'s PUT handler pre-reads the current row into
    # this same session's identity map, for its audit-entry `old_value`,
    # before calling this function). This module has no in-process policy
    # cache for `set_dlp_policy` to silently re-arm stale (DLP policy is
    # read fresh from the DB on every request - see `load_dlp_policy`'s
    # docstring), so unlike the residency-rule case this is not an
    # enforcement-correctness bug - only the PUT response body itself
    # (`row`, echoed back to the caller as confirmation of the write) would
    # otherwise show the OLD, pre-update values on a second-and-later write.
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache_invalidator is not None:
        await cache_invalidator.clear_all()
    return row


async def list_custom_patterns(session: AsyncSession) -> list[DlpCustomPattern]:
    stmt = (
        select(DlpCustomPattern)
        .where(DlpCustomPattern.org_id == DEFAULT_ORG_ID)
        .order_by(DlpCustomPattern.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_custom_pattern(session: AsyncSession, pattern_id: uuid.UUID) -> DlpCustomPattern | None:
    row = await session.get(DlpCustomPattern, pattern_id)
    if row is None or row.org_id != DEFAULT_ORG_ID:
        return None
    return row


async def create_custom_pattern(
    session: AsyncSession,
    *,
    name: str,
    pattern: str,
    action: DlpAction,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> DlpCustomPattern:
    """Flushes (populating `row.id`), does NOT commit - mirrors
    `services.personal_keys.create_personal_key`'s shape so the route layer
    (`api/v1/admin/dlp_policy.py`) can write the `dlp_policy.custom_pattern.
    create` audit entry (which needs the now-flushed `row.id` as its
    `target_id`) in the SAME transaction before committing once.

    Hardening pass item 2 (consistency with `set_dlp_policy`/`set_team_dlp_
    override`'s Fix-3 invalidation): `cache_invalidator`, when given, is
    invoked here (`clear_all()` - a new custom pattern applies org-wide, same
    rationale as `set_dlp_policy`'s) rather than left to the router to call
    separately, so a future caller of this function from anywhere else can't
    forget it. This function still never commits (see paragraph above) - the
    invalidation therefore fires right after the successful `flush()`, ahead
    of the caller's own eventual `commit()`, not after it. This is a
    deliberate, narrow exception to "invalidate strictly after commit": a
    Redis invalidation firing slightly early is harmless (a false-positive
    cache clear, never a stale-serve) even in the vanishingly unlikely case
    the caller's later commit fails, whereas leaving the call in the router
    would reintroduce exactly the "easy to forget" problem this fix closes.
    """
    validate_pattern_regex(pattern)
    row = DlpCustomPattern(org_id=DEFAULT_ORG_ID, name=name, pattern=pattern, action=action)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise DuplicateCustomPatternNameError(
            f"A custom DLP pattern named '{name}' already exists."
        ) from None
    if cache_invalidator is not None:
        await cache_invalidator.clear_all()
    return row


async def update_custom_pattern(
    session: AsyncSession,
    pattern_id: uuid.UUID,
    *,
    name: str,
    pattern: str,
    action: DlpAction,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> DlpCustomPattern | None:
    """Hardening pass item 2: `cache_invalidator` (when given) is invoked
    here, AFTER the commit below succeeds - same "commit, then invalidate"
    ordering `set_dlp_policy` already uses - instead of being left to the
    router to call separately."""
    validate_pattern_regex(pattern)
    row = await get_custom_pattern(session, pattern_id)
    if row is None:
        return None
    row.name = name
    row.pattern = pattern
    row.action = action
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise DuplicateCustomPatternNameError(
            f"A custom DLP pattern named '{name}' already exists."
        ) from None
    if cache_invalidator is not None:
        await cache_invalidator.clear_all()
    return row


async def delete_custom_pattern(
    session: AsyncSession,
    pattern_id: uuid.UUID,
    *,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> bool:
    """Hardening pass item 2: `cache_invalidator` (when given) is invoked
    here, AFTER the commit below succeeds, instead of being left to the
    router to call separately - see `update_custom_pattern`'s docstring."""
    row = await get_custom_pattern(session, pattern_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    if cache_invalidator is not None:
        await cache_invalidator.clear_all()
    return True


async def set_team_dlp_override(
    session: AsyncSession,
    team_id: uuid.UUID,
    action: DlpAction,
    *,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> TeamDlpActionOverride:
    """Full-replace upsert, mirrors `TeamModelPolicy`'s `team_id`-as-PK
    shape (AC2.4 - the action override only, never a pattern override).

    Fix 3 (security review, BLOCKING) - see `set_dlp_policy`'s docstring for
    the full rationale; a team override change only needs to invalidate
    that team's own cached entries (`cache_invalidator.clear_team(team_id)`),
    not every team's.

    Hardening pass item 1: `populate_existing=True` below - see `set_dlp_
    policy`'s identical note (`api/v1/teams.py`'s PUT handler for this
    route pre-reads the current override into this same session's identity
    map first).
    """
    insert_stmt = postgresql.insert(TeamDlpActionOverride).values(team_id=team_id, action=action)
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[TeamDlpActionOverride.team_id],
        set_={"action": insert_stmt.excluded.action},
    ).returning(TeamDlpActionOverride)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache_invalidator is not None:
        await cache_invalidator.clear_team(team_id)
    return row
