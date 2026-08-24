"""add org_settings.alert_recipient_email + first-SSO-login org_admin gate

Product owner request: org admins should be prompted to register a
dedicated alert-recipient email (a group address, not their own SSO
identity - the whole point is that budget alerts reach an inbox someone
is actually watching, even if the admin who happened to log in first
changes) the first time an org_admin logs in via SSO, before the org
threshold-alert email notifiers have anyone real to send to.

`alert_recipient_email` is deliberately separate from `User.sso_email`
(the existing org_admin-recipient fallback `services.notifiers._load_org_
recipients` already uses) - this is a NEW required-once field, not a
replacement; both are consulted when sending (see that function).

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "org_settings", sa.Column("alert_recipient_email", sa.String(length=320), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("org_settings", "alert_recipient_email")
