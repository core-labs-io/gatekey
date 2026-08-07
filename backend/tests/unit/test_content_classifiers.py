"""Unit tests for `services/content_classifiers.py` (Phase 5 -
Differentiators, 5.3 Content-Classification-Aware Routing, AC5.3.1/AC5.3.7).

True positives, true negatives, and edge cases for the `source_code` and
`legal` heuristics - both are regex/keyword-based (no ML/embeddings
dependency), so these tests exercise the actual, deterministic behavior a
caller will see, not a mocked approximation of it.
"""

from __future__ import annotations

import pytest

from gatekey.services.content_classifiers import is_legal_content, is_source_code

# ---------------------------------------------------------------------------
# source_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "```python\ndef foo():\n    return 1\n```",
        "```\nSELECT * FROM users;\n```",
        "function add(a, b) { return a + b; }",
        "public class Foo { private int x; public Foo() { x = 0; } }",
        "def foo(x):\n    if x:\n        return x\n    for i in range(10):\n        print(i)",
        "import os\nimport sys\nclass Config:\n    def __init__(self):\n        self.value = None",
        "const x = 1; let y = 2; function add() { return x + y; }",
    ],
)
def test_is_source_code_true_positives(text: str) -> None:
    assert is_source_code(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Hello, how are you today? I hope you are well.",
        "Please import the documents and organize the files for the meeting.",
        "It is important to plan for the future: think carefully before deciding.",
        "Can you recommend a good class on public speaking for beginners?",
        "",
    ],
)
def test_is_source_code_true_negatives(text: str) -> None:
    assert is_source_code(text) is False


def test_is_source_code_single_incidental_keyword_does_not_trigger() -> None:
    """A lone, ordinary-English use of a code-adjacent word (below the
    keyword-hit threshold, no fence, no brace density) must not false-flag
    a request as source code."""
    assert is_source_code("Please import this file into the shared drive today.") is False


def test_is_source_code_short_snippet_without_fence_or_keywords_is_negative() -> None:
    assert is_source_code("x = 1") is False


def test_is_source_code_bare_code_fence_alone_is_positive() -> None:
    """A fence is the strongest signal - present even with no recognizable
    keyword inside."""
    assert is_source_code("```\nsome opaque payload\n```") is True


# ---------------------------------------------------------------------------
# legal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This memo is attorney-client privileged and confidential.",
        "Please sign the NDA before we discuss this litigation.",
        "See 42 U.S.C. 1983 for the relevant statute.",
        "The case Roe v. Wade established precedent in this area.",
        "As cited in 410 U.S. 113, the court held that...",
        "As held in 123 F.3d 456, the defendant's motion was denied.",
        "We received a subpoena and need to respond within the deadline.",
        "This letter serves as a formal cease and desist notice.",
    ],
)
def test_is_legal_content_true_positives(text: str) -> None:
    assert is_legal_content(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Can you help me plan a birthday party for my friend?",
        "What is the weather like today in San Francisco?",
        "Please summarize this quarterly engineering status update.",
        "",
    ],
)
def test_is_legal_content_true_negatives(text: str) -> None:
    assert is_legal_content(text) is False


def test_is_legal_content_case_insensitive_keyword_match() -> None:
    assert is_legal_content("This is PRIVILEGED AND CONFIDENTIAL material.") is True


def test_is_legal_content_partial_word_does_not_false_positive() -> None:
    """"NDA" as a substring of an unrelated word (e.g. "PANDA") must not
    match - `\\bNDA\\b` is word-boundary-anchored, not a bare substring
    search."""
    assert is_legal_content("The San Diego zoo just welcomed a new baby PANDA this week.") is False
