"""Static, in-code per-model USD pricing table (Phase 1.4 - Budget Basic).

Mirrors `providers/model_registry.py`'s "pure module, zero I/O, hand-curated
dict at import time" pattern exactly - not a DB table, not admin-editable in
this slice (see `gatekey/phase-1-core-gateway.md` 1.6's admin capability list,
which does not mention pricing).

Completeness invariant (hard requirement): every key in `MODEL_REGISTRY` MUST
have a corresponding entry here, and every `ModelCapability.CHAT` entry MUST
have a non-`None` `output_price_per_million_usd`. A model that is routable
but unpriced must never silently cost `$0` - see `test_pricing.py`'s
`test_pricing_table_covers_every_registry_model` for the build-time guard,
and `get_pricing_entry()` below for the runtime guard.

Sourcing note
--------------
Figures below are standard, non-cached, non-batch published per-million-token
rates, as publicly documented by each provider as of the `as_of` date on each
entry - freshly verified via live web access on 2026-07-28, which is also
when the previous pilot model list (Claude 3.5/3-era models, Gemini 1.5,
`text-embedding-004`) was discovered to have been fully retired by both
providers since this table was first written, not merely repriced. That
retired list has been replaced in `model_registry.py` with each provider's
current equivalent models, priced below. **An operator deploying Gatekey
should still confirm these against the live pricing pages before relying on
them for real budget enforcement** - provider pricing and model line-ups
both continue to change after this date, and Claude Sonnet 5 in particular
carries a temporary introductory rate (see its entry below) that will lapse
on a known date. Update this table (a code change + redeploy) whenever a
provider reprices or retires a model in active use.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability


@dataclass(frozen=True)
class PricingEntry:
    """USD-per-million-token rate for one `MODEL_REGISTRY` model.

    A record (not a bare 2-`Decimal` tuple) so a future per-character/
    per-request rate shape can be added as new optional fields here, not a
    schema rewrite.
    """

    input_price_per_million_usd: Decimal
    output_price_per_million_usd: Decimal | None  # None only for EMBEDDINGS routes
    as_of: str  # ISO date the figure was sourced/last verified
    source: str  # Citation - provider's own pricing page


class PricingEntryMissingError(Exception):
    """Raised by `get_pricing_entry()` for a model with no pricing entry.

    Never caught-and-treated-as-$0 by any caller - see
    `services/budget.py`'s `compute_cost()`. Left to propagate to the
    app-wide unhandled-exception handler (`errors.register_exception_handlers`),
    which logs loudly and returns a generic 500 rather than a silent free
    request.
    """


# Every rate below is constructed from a `Decimal` string literal
# (`Decimal("2.50")`, never `Decimal(2.50)`) to avoid reintroducing
# float-precision risk through the back door.
PRICING_TABLE: dict[str, PricingEntry] = {
    # --- OpenAI - chat ---
    "gpt-4o": PricingEntry(
        input_price_per_million_usd=Decimal("2.50"),
        output_price_per_million_usd=Decimal("10.00"),
        as_of="2026-07-17",
        source="https://openai.com/api/pricing/",
    ),
    "gpt-4o-mini": PricingEntry(
        input_price_per_million_usd=Decimal("0.15"),
        output_price_per_million_usd=Decimal("0.60"),
        as_of="2026-07-17",
        source="https://openai.com/api/pricing/",
    ),
    # --- OpenAI - embeddings ---
    "text-embedding-3-small": PricingEntry(
        input_price_per_million_usd=Decimal("0.02"),
        output_price_per_million_usd=None,
        as_of="2026-07-17",
        source="https://openai.com/api/pricing/",
    ),
    "text-embedding-3-large": PricingEntry(
        input_price_per_million_usd=Decimal("0.13"),
        output_price_per_million_usd=None,
        as_of="2026-07-17",
        source="https://openai.com/api/pricing/",
    ),
    # --- Anthropic - chat ---
    "claude-haiku-4-5-20251001": PricingEntry(
        input_price_per_million_usd=Decimal("1.00"),
        output_price_per_million_usd=Decimal("5.00"),
        as_of="2026-07-28",
        source="https://platform.claude.com/docs/en/about-claude/models/overview",
    ),
    # NOTE: Claude Sonnet 5 carries a temporary introductory rate of
    # $2.00/$10.00 per million tokens through August 31, 2026, after which
    # it reverts to the standard $3.00/$15.00 rate used below. This table
    # deliberately prices at the standard (higher) rate rather than the
    # introductory one: it means budget enforcement is briefly conservative
    # (slightly over-charges relative to the real invoice) instead of
    # silently under-charging once the introductory window lapses and
    # nobody's updated this file yet - consistent with this module's
    # "never risk undercounting" stance. Switch to the introductory rate
    # explicitly if that briefly-lower cost needs to be reflected exactly,
    # and revert by September 1, 2026 regardless.
    "claude-sonnet-5": PricingEntry(
        input_price_per_million_usd=Decimal("3.00"),
        output_price_per_million_usd=Decimal("15.00"),
        as_of="2026-07-28",
        source="https://platform.claude.com/docs/en/about-claude/models/overview",
    ),
    "claude-opus-5": PricingEntry(
        input_price_per_million_usd=Decimal("5.00"),
        output_price_per_million_usd=Decimal("25.00"),
        as_of="2026-07-28",
        source="https://platform.claude.com/docs/en/about-claude/models/overview",
    ),
    # --- Vertex AI - chat ---
    "gemini-2.5-flash": PricingEntry(
        input_price_per_million_usd=Decimal("0.30"),
        output_price_per_million_usd=Decimal("2.50"),
        as_of="2026-07-28",
        source="https://cloud.google.com/vertex-ai/generative-ai/pricing",
    ),
    # NOTE: Gemini 2.5 Pro has a higher rate above a 200k-input-token
    # threshold ($2.50/$15.00 vs. the $1.25/$10.00 base tier priced below).
    # This entry uses the base/standard tier only - a request whose prompt
    # exceeds 200k tokens will be undercharged relative to Google's actual
    # invoice. Verify against the current Vertex AI pricing page whether
    # this tier still applies before relying on this for large-context
    # traffic (same caveat this entry's predecessor, gemini-1.5-pro, had).
    "gemini-2.5-pro": PricingEntry(
        input_price_per_million_usd=Decimal("1.25"),
        output_price_per_million_usd=Decimal("10.00"),
        as_of="2026-07-28",
        source="https://cloud.google.com/vertex-ai/generative-ai/pricing",
    ),
    # --- Vertex AI - embeddings ---
    # gemini-embedding-001 supersedes the retired text-embedding-004/005 as
    # Google's current recommended embeddings model.
    "gemini-embedding-001": PricingEntry(
        input_price_per_million_usd=Decimal("0.15"),
        output_price_per_million_usd=None,
        as_of="2026-07-28",
        source="https://ai.google.dev/gemini-api/docs/pricing",
    ),
    # --- Ollama - chat, self-hosted (Phase 1 addition, AC-E3-*) ---
    # Sourcing note (AC-E3-3), read this before touching the entries below:
    #   (a) $0.00 is NOT a real cost basis. It reflects that there is no
    #       per-token provider invoice to charge against for a self-hosted
    #       target - it does not capture the real infrastructure/GPU
    #       operating cost of running these models.
    #   (b) A full self-hosted cost-basis model (e.g. GPU-hour-rate-based
    #       estimation, normalized alongside token-based provider pricing)
    #       is a known, already-anticipated future gap, tracked in
    #       `phase-5-differentiators.md` section 5.5 ("Unified Governance
    #       for BYOK + Self-Hosted OSS Models").
    #   (c) This Phase 1 addition is intentionally simpler than that and is
    #       NOT a preview or partial implementation of that eventual
    #       design - do not treat the shape of these entries as a hint
    #       toward what section 5.5 will look like.
    "ollama/llama3.1": PricingEntry(
        input_price_per_million_usd=Decimal("0.00"),
        output_price_per_million_usd=Decimal("0.00"),
        as_of="2026-07-28",
        source=(
            "Self-hosted: no per-token provider charge; $0.00 does not "
            "represent real infrastructure/GPU cost."
        ),
    ),
    "ollama/mistral": PricingEntry(
        input_price_per_million_usd=Decimal("0.00"),
        output_price_per_million_usd=Decimal("0.00"),
        as_of="2026-07-28",
        source=(
            "Self-hosted: no per-token provider charge; $0.00 does not "
            "represent real infrastructure/GPU cost."
        ),
    ),
    "ollama/qwen2.5": PricingEntry(
        input_price_per_million_usd=Decimal("0.00"),
        output_price_per_million_usd=Decimal("0.00"),
        as_of="2026-07-28",
        source=(
            "Self-hosted: no per-token provider charge; $0.00 does not "
            "represent real infrastructure/GPU cost."
        ),
    ),
    # --- OpenRouter - chat, no-markup pass-through (Phase 1 addition,
    # AC-E4-*) ---
    # Sourcing note (AC-E4-2): OpenRouter passes through the underlying
    # model's own per-token price with no markup on token costs
    # (confirmed - the figures below match direct OpenAI pricing for the
    # same model, corroborating this). A separate ~5.5% fee applies only to
    # *credit purchases* at the account level and is out of scope for
    # per-request cost accounting here - Gatekey has no visibility into, or
    # role in, an org's OpenRouter credit-purchase transactions. Do not
    # "fix" this table by adding a markup to account for that fee; it does
    # not apply to per-request token costs.
    #
    # As with every other entry in this table (see module docstring), an
    # operator deploying Gatekey should still independently confirm this
    # figure against OpenRouter's live pricing page before relying on it
    # for real budget enforcement - pricing can change after `as_of`.
    "openrouter/openai/gpt-4o-mini": PricingEntry(
        input_price_per_million_usd=Decimal("0.15"),
        output_price_per_million_usd=Decimal("0.60"),
        as_of="2026-07-28",
        source="https://openrouter.ai/openai/gpt-4o-mini",
    ),
    "openrouter/meta/muse-spark-1.2": PricingEntry(
        input_price_per_million_usd=Decimal("1.25"),
        output_price_per_million_usd=Decimal("4.25"),
        as_of="2026-08-22",
        source="https://openrouter.ai/meta/muse-spark-1.2",
    ),
}


def get_pricing_entry(model: str) -> PricingEntry:
    """Look up `model`'s pricing entry.

    `model` MUST already be a literal `MODEL_REGISTRY` key that has passed
    `resolve_route()` in this same request - same discipline as
    `check_model_policy()`'s docstring. Raises `PricingEntryMissingError` if
    missing - never returns a synthetic $0 entry.
    """
    try:
        return PRICING_TABLE[model]
    except KeyError:
        raise PricingEntryMissingError(
            f"No pricing entry for model {model!r} - internal configuration "
            "error (every MODEL_REGISTRY key must have a matching "
            "PRICING_TABLE entry), never a valid $0 charge."
        ) from None


def compute_self_hosted_cost(
    cost_basis_per_gpu_hour: Decimal, *, wall_clock_latency_seconds: Decimal | float
) -> Decimal:
    """AC5.5.7 (Phase 5 - Differentiators, 5.5): the self-hosted cost-
    estimation formula, used INSTEAD OF `get_pricing_entry()`/`compute_cost()`
    for any request whose `ModelRoute.provider == "self_hosted"` - a
    self-hosted model id is never a `MODEL_REGISTRY`/`PRICING_TABLE` key (it
    comes from an admin-registered `self_hosted_providers.models` entry, not
    this static table), so the normal per-token lookup path does not apply.

    Formula: `cost_basis_per_gpu_hour * (wall_clock_latency_seconds / 3600)`
    - a rough proxy that ignores queueing delay, multi-tenant GPU sharing,
    and cold-start latency (the phase spec names only "configured GPU-hour
    rate", not an estimation method - see `gatekey/phase-5-product-spec.md`
    section 9 judgment call #10). The result still lands in the same
    `usage_logs.cost_usd` column every other provider's cost does, so
    budgets/degradation/dashboards work unmodified - but the admin UI MUST
    visibly label self-hosted cost figures as "estimated", never
    invoice-grade, per that same judgment call.

    `wall_clock_latency_seconds` is the provider's own round-trip time (the
    delta between the pre-dispatch and provider-response-received
    `LatencyTimer` marks - see `api.v1.gateway.common.LatencyTimer`), not
    total request latency including DLP/budget-check overhead - callers must
    pass the narrower figure. Accepts `float` for caller convenience
    (`time.perf_counter()` deltas are floats) but always computes in
    `Decimal` - a `float` argument is converted via `str()` first (never a
    direct `Decimal(float)` construction) to avoid reintroducing
    float-precision risk through the back door, matching this module's own
    `PRICING_TABLE` construction discipline.
    """
    latency_seconds = (
        wall_clock_latency_seconds
        if isinstance(wall_clock_latency_seconds, Decimal)
        else Decimal(str(wall_clock_latency_seconds))
    )
    return cost_basis_per_gpu_hour * (latency_seconds / Decimal(3600))


def _validate_completeness() -> None:
    """Fail at import time if the table drifts from `MODEL_REGISTRY`.

    Belt-and-suspenders alongside `tests/unit/test_pricing.py`'s explicit
    assertion of the same invariant - this makes the gap discoverable even
    if the test suite isn't run (e.g. a stripped-down deploy), never only at
    request time.
    """
    missing = MODEL_REGISTRY.keys() - PRICING_TABLE.keys()
    if missing:
        raise RuntimeError(
            f"PRICING_TABLE is missing entries for MODEL_REGISTRY models: {sorted(missing)}"
        )
    for model, route in MODEL_REGISTRY.items():
        entry = PRICING_TABLE[model]
        if route.capability is ModelCapability.CHAT and entry.output_price_per_million_usd is None:
            raise RuntimeError(
                f"PRICING_TABLE entry for CHAT model {model!r} must have a non-null "
                "output_price_per_million_usd."
            )


_validate_completeness()
