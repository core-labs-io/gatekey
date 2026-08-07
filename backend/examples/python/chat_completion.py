"""Non-streaming chat completion: direct OpenAI SDK vs. Gatekey (BD-11).

Endpoint demonstrated: POST /v1/chat/completions (non-streaming)
Model: gpt-4o -> routed by Gatekey to the OpenAI provider.

Run:
    pip install openai
    export OPENAI_API_KEY=sk-...                  # only needed for the "before" call
    export GATEKEY_BASE_URL=http://localhost:8000/v1
    export GATEKEY_SERVICE_ACCOUNT_KEY=gk_sk_...
    python chat_completion.py

See ../README.md for prerequisites (minting a service-account key,
configuring a provider key) and the full "error shape" explanation
summarized in the comment above call_via_gatekey() below.
"""

from __future__ import annotations

import os

from openai import APIStatusError, OpenAI

MESSAGES = [{"role": "user", "content": "Say hello in one short sentence."}]


# ---------------------------------------------------------------------------
# BEFORE: calling OpenAI directly. No Gatekey involved.
# ---------------------------------------------------------------------------
def call_direct_openai() -> None:
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],  # OpenAI's own secret key
        # base_url is left unset -> SDK default "https://api.openai.com/v1".
    )
    response = client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    print("[direct openai]", response.choices[0].message.content)


# ---------------------------------------------------------------------------
# AFTER: the identical call, now pointed at Gatekey.
#
# The ONLY two lines that differ from call_direct_openai() above are the
# `base_url` and `api_key` arguments passed to OpenAI(...). The call to
# `.chat.completions.create(...)` itself - method name, arguments, response
# shape - is completely unchanged. This is the whole point of BD-11: an
# internal app switches over by touching only its client construction, not
# its call sites.
# ---------------------------------------------------------------------------
def call_via_gatekey() -> None:
    client = OpenAI(
        api_key=os.environ["GATEKEY_SERVICE_ACCOUNT_KEY"],  # gk_sk_... - NOT a provider key
        base_url=os.environ.get("GATEKEY_BASE_URL", "http://localhost:8000/v1"),
    )
    try:
        response = client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    except APIStatusError as exc:
        # ERROR SHAPE CAVEAT (full detail in ../README.md "Error shape
        # caveat"): Gatekey's JSON error body is {"error": {"code",
        # "message"}} - not OpenAI's {"error": {"message", "type", "param",
        # "code"}}. Concretely against Gatekey:
        #   - exc.status_code and the exception *class* (here,
        #     APIStatusError; would be openai.AuthenticationError for a 401,
        #     openai.NotFoundError for a 404, etc.) are still correct, since
        #     the SDK picks the class from the HTTP status code, not the body.
        #   - exc.code IS populated, but holds Gatekey's own code string
        #     (e.g. "provider_upstream_error", "model_not_found") rather
        #     than an OpenAI code like "insufficient_quota" - don't
        #     pattern-match on OpenAI-specific code values.
        #   - exc.type is always None from Gatekey (no "type" key in the
        #     envelope at all); real OpenAI always sets this.
        #   - exc.param is always None from Gatekey.
        # So: branch on the exception class / exc.status_code, not on
        # exc.type or a specific exc.code literal, if this handler also
        # needs to work against real OpenAI.
        print(f"[gatekey] request failed: status={exc.status_code} code={exc.code} message={exc.message}")
        raise
    print("[gatekey -> openai]", response.choices[0].message.content)


if __name__ == "__main__":
    call_direct_openai()
    call_via_gatekey()
