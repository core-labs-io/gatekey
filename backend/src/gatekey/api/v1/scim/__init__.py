"""SCIM 2.0 provisioning routers (Phase 3, BD-22/BD-23) - mounted at
`/scim/v2/...` in `main.py`, deliberately separate from `/v1/...`. See
`services/scim.py`'s module docstring for the RFC 7644 error-shape
rationale.
"""

from __future__ import annotations

from gatekey.api.v1.scim.groups import router as groups_router
from gatekey.api.v1.scim.users import router as users_router

__all__ = ["groups_router", "users_router"]
