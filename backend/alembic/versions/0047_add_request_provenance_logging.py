"""add source_ip/client_user_agent to usage_logs, device_label to personal_api_keys

Product owner request: log which system each user used to call the
gateway (source IP, best-effort client User-Agent), to help spot
off-network usage and leaked-key indicators.

Two real, honest signals, not the literal ask: a MAC address is not
obtainable from a remote HTTP client under any circumstance (no browser or
standard HTTP client API exposes it to a server - a decades-old OS/network
privacy boundary, not a Gatekey gap), and `User-Agent` is client-supplied
and trivially spoofable (a weak hint, not a security control). `source_ip`
reuses the exact same trusted-proxy-aware resolution the audit trail
already uses (`api.deps.get_source_ip`) - see the gateway route handlers
for where it's now captured per request.

`personal_api_keys.device_label` is a THIRD, stronger (but narrower)
signal: the CLI-sync tool (`POST /v1/auth/device/start`, called directly
by the machine being paired, not the browser) can now self-report a real
hostname/device label at pairing time - see
`services.cli_refresh_credentials.DeviceAuthStore`. Only ever populated
for personal keys minted through the CLI-sync device-code flow; NULL for
every other key (self-service-portal-minted personal keys, all
service-account keys).

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("usage_logs", sa.Column("source_ip", sa.String(length=45), nullable=True))
    op.add_column("usage_logs", sa.Column("client_user_agent", sa.Text(), nullable=True))
    op.add_column(
        "personal_api_keys", sa.Column("device_label", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("personal_api_keys", "device_label")
    op.drop_column("usage_logs", "client_user_agent")
    op.drop_column("usage_logs", "source_ip")
