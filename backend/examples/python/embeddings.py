"""Embeddings: direct OpenAI SDK vs. Gatekey (BD-11).

Endpoint demonstrated: POST /v1/embeddings
Model: gemini-embedding-001 -> routed by Gatekey to the Vertex AI provider.
Again, the caller uses the official OpenAI SDK's `.embeddings.create(...)`
unmodified - Gatekey translates the OpenAI-shaped embeddings request/
response into Vertex AI's native shape server-side
(see backend/src/gatekey/providers/vertex_ai.py:create_embeddings).

Run:
    pip install openai
    export OPENAI_API_KEY=sk-...                  # only needed for the "before" call
    export GATEKEY_BASE_URL=http://localhost:8000/v1
    export GATEKEY_SERVICE_ACCOUNT_KEY=gk_sk_...
    python embeddings.py

See ../README.md for prerequisites and the full "error shape" explanation.
"""

from __future__ import annotations

import os

from openai import OpenAI

INPUT_TEXT = "Gatekey is an AI gateway."


# ---------------------------------------------------------------------------
# BEFORE: calling OpenAI's embeddings API directly. No Gatekey involved.
# ---------------------------------------------------------------------------
def embed_direct_openai() -> None:
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        # base_url unset -> SDK default "https://api.openai.com/v1".
    )
    response = client.embeddings.create(model="text-embedding-3-small", input=INPUT_TEXT)
    print(f"[direct openai] embedding length={len(response.data[0].embedding)}")


# ---------------------------------------------------------------------------
# AFTER: the identical shape of call, pointed at Gatekey, using a Vertex AI
# embeddings model. Only `base_url` + `api_key` differ from
# embed_direct_openai() above (plus `model`, to pick a Vertex-routed model
# instead of an OpenAI one - demonstrating that model choice, not client
# configuration, is what selects the provider).
# ---------------------------------------------------------------------------
def embed_via_gatekey() -> None:
    client = OpenAI(
        api_key=os.environ["GATEKEY_SERVICE_ACCOUNT_KEY"],  # gk_sk_... - NOT a provider key
        base_url=os.environ.get("GATEKEY_BASE_URL", "http://localhost:8000/v1"),
    )
    response = client.embeddings.create(model="gemini-embedding-001", input=INPUT_TEXT)
    print(f"[gatekey -> vertex_ai] embedding length={len(response.data[0].embedding)}")


if __name__ == "__main__":
    embed_direct_openai()
    embed_via_gatekey()
