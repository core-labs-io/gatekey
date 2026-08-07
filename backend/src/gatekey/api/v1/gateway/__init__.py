"""Gateway route handlers (Phase 1.2, BD-9): the OpenAI-compatible surface.

`router` combines the three endpoint-specific routers
(`chat.py`/`completions.py`/`embeddings.py`) into one for `main.create_app`
to mount - see `common.py` for the auth/model-resolution/credential-fetch
logic shared across all three.
"""

from __future__ import annotations

from fastapi import APIRouter

from gatekey.api.v1.gateway.chat import router as _chat_router
from gatekey.api.v1.gateway.completions import router as _completions_router
from gatekey.api.v1.gateway.embeddings import router as _embeddings_router

router = APIRouter()
router.include_router(_chat_router)
router.include_router(_completions_router)
router.include_router(_embeddings_router)

__all__ = ["router"]
