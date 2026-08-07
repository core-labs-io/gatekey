"""Lightweight regex/keyword heuristic content classifiers for the
`source_code` and `legal` content-classification categories (Phase 5 -
Differentiators, 5.3 Content-Classification-Aware Routing, AC5.3.1/AC5.3.7).

No ML/embeddings dependency - pure, synchronous, deterministic functions,
consistent with `services/dlp.py`'s existing regex-based detection approach
and the Drift Detector's own no-embeddings choice (`services/drift_
detector.py`) for the same cost/dependency/determinism reasons (see
`gatekey/phase-5-technical-design.md` section 2.4).

Unlike `pii`/`financial_data` (both Presidio-engine-based - see
`services/dlp.py`'s `_FINANCIAL_DATA_ENTITY_TYPES`), these two categories
have no DLP action (no redact/block concept - "redact doesn't make sense for
code," per AC5.3.1) - their only consumer is content-aware routing
(`services.model_policy.resolve_content_classification`). Callers only
invoke these when the corresponding `content_aware_rules` row is enabled
(see `services/dlp.py::scan_texts`'s `content_aware_categories_enabled`
gating) - they are never run unconditionally on every gateway request.

Both classifiers are heuristics, not exact detectors - false positives/
negatives are expected and explicitly flagged for extra QA/security scrutiny
per the product spec's own judgment calls (`legal` in particular has no
existing schema-scaffolding precedent and is the least-grounded of the four
positive content categories - phase-5-product-spec.md section 9, judgment
call #12).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# source_code
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```|~~~")

# Common keyword/syntax signals across several mainstream languages
# (Python/JS/TS/Java/C#/C/C++/Go) - deliberately broad rather than
# per-language, since this is a routing signal, not a language identifier.
_CODE_KEYWORD_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\bdef\b",
        r"\bclass\b",
        r"\bimport\b",
        r"\bfunction\b",
        r"\bfunc\b",
        r"\breturn\b",
        r"\bconst\b",
        r"\blet\b",
        r"\bpublic\s+(?:class|static|void)\b",
        r"\bprivate\s+\w+\b",
        r"#include\b",
        r"\busing\s+namespace\b",
        r"console\.log\(",
        r"=>",
        r"\bself\.\w+",
        r"\bNone\b",
        r"\bif\s*\(",
        r"\bfor\s*\(",
        r"\belse\s*\{",
        r"\belif\b",
        r"\bexcept\b",
        r"\bprint\(",
        r"\brange\(",
        r":\s*\n",  # a bare trailing colon (Python block-start) - strong signal
    )
)

_BRACE_SEMICOLON_CHARS = "{};"
_MIN_LENGTH_FOR_DENSITY_CHECK = 20
_BRACE_DENSITY_THRESHOLD = 0.02
_MIN_KEYWORD_HITS_WITH_DENSITY = 1
_MIN_KEYWORD_HITS_ALONE = 3


def is_source_code(text: str) -> bool:
    """AC5.3.1: code-fence markers, brace/semicolon density, and
    keyword-density signals (`def`/`class`/`import`/`function`/`{`/`};`)
    across common languages.

    Deliberately tolerant of imperfect/mixed content (a request pasting a
    code snippet alongside a natural-language question is still flagged) -
    any ONE of the three signals below is sufficient:

      1. A markdown code-fence marker (``` ` ``` `` or `~~~`) is present -
         the strongest, near-unambiguous signal.
      2. At least `_MIN_KEYWORD_HITS_ALONE` distinct code keywords appear -
         catches fenceless, low-brace-density snippets (e.g. an
         indentation-based Python function body with no `{`/`}`/`;` at
         all).
      3. The text is long enough to make a density measurement meaningful
         (`_MIN_LENGTH_FOR_DENSITY_CHECK`) AND has a brace/semicolon
         density at or above `_BRACE_DENSITY_THRESHOLD` AND at least one
         code keyword hit - catches brace-heavy snippets (C-family
         languages) that don't happen to repeat a keyword three times.

    A single incidental keyword hit alone (e.g. the word "import" used in
    an ordinary English sentence) never trips this by itself - real code
    either repeats several keywords or carries real syntax density.
    """
    if not text:
        return False
    if _CODE_FENCE_RE.search(text):
        return True

    keyword_hits = sum(1 for pattern in _CODE_KEYWORD_PATTERNS if pattern.search(text))
    if keyword_hits >= _MIN_KEYWORD_HITS_ALONE:
        return True

    if len(text) >= _MIN_LENGTH_FOR_DENSITY_CHECK:
        brace_count = sum(text.count(c) for c in _BRACE_SEMICOLON_CHARS)
        brace_density = brace_count / len(text)
        if brace_density >= _BRACE_DENSITY_THRESHOLD and keyword_hits >= _MIN_KEYWORD_HITS_WITH_DENSITY:
            return True

    return False


# ---------------------------------------------------------------------------
# legal
# ---------------------------------------------------------------------------

_LEGAL_KEYWORD_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"attorney[\s-]client privileg\w*",
        r"privileged and confidential",
        r"\bnon-disclosure agreement\b",
        r"\bNDA\b",
        r"\blitigation\b",
        r"\bplaintiff\b",
        r"\bdefendant\b",
        r"\bsubpoena\b",
        r"\bindemnif\w*",
        r"\bgoverning law\b",
        r"\bwithout prejudice\b",
        r"\bcease and desist\b",
    )
)

# Statute citation, e.g. "42 U.S.C. 1983" / "42 U.S.C. § 1983".
_STATUTE_CITATION_RE = re.compile(r"\b\d+\s+U\.S\.C\.?\s*§?\s*\d+")
# Case-reporter citation, e.g. "410 U.S. 113" / "123 F.3d 456" /
# "999 F. Supp. 2d 1".
_CASE_CITATION_RE = re.compile(r"\b\d+\s+(?:U\.S\.|F\.\s?(?:2d|3d)?|F\.\s?Supp\.\s?\d?d?)\s+\d+")
# Case name, e.g. "Smith v. Jones" / "Roe v. Wade".
_CASE_NAME_RE = re.compile(r"\b[A-Z][a-zA-Z]+\s+v\.\s+[A-Z][a-zA-Z]+")

_LEGAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    *_LEGAL_KEYWORD_PATTERNS,
    _STATUTE_CITATION_RE,
    _CASE_CITATION_RE,
    _CASE_NAME_RE,
)


def is_legal_content(text: str) -> bool:
    """AC5.3.1: keyword/regex heuristic covering attorney-client-privilege
    language, common litigation terminology, and statute/case-citation
    patterns.

    Flagged (product spec section 9, judgment call #12) as the
    least-grounded of the four positive content-classification categories -
    no existing schema-scaffolding precedent (unlike `pii`/`source_code`/
    `financial_data`), extra QA/security scrutiny warranted on its
    false-positive/negative rate.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _LEGAL_PATTERNS)
