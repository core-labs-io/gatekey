"""Application-wide constants.

`DEFAULT_ORG_ID` must match the fixed UUID literal seeded by
`alembic/versions/0001_create_orgs_and_provider_keys.py` exactly
(`DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"` there). Phase 1.1
is a single-org slice - no org signup/CRUD flow exists yet, so every
service call in this slice operates against this one row rather than
accepting an `org_id` from the request. Do not thread a caller-supplied
`org_id` through any endpoint/service in this slice; when Phase 2 adds real
multi-org support, this constant (and the assumption it encodes) goes away
in favor of resolving org_id from the authenticated caller.
"""

from __future__ import annotations

import uuid

DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
