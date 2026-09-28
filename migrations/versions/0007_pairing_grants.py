"""add single-use device pairing grants

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pairing_grants",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("organization_id", sa.String(length=128), nullable=False),
        sa.Column("membership_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["membership_id"], ["memberships.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pairing_grants_organization_id", "pairing_grants", ["organization_id"])
    op.create_index("ix_pairing_grants_membership_id", "pairing_grants", ["membership_id"])
    op.create_index("ix_pairing_grants_user_id", "pairing_grants", ["user_id"])
    op.create_index("ix_pairing_grants_code_hash", "pairing_grants", ["code_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_pairing_grants_code_hash", table_name="pairing_grants")
    op.drop_index("ix_pairing_grants_user_id", table_name="pairing_grants")
    op.drop_index("ix_pairing_grants_membership_id", table_name="pairing_grants")
    op.drop_index("ix_pairing_grants_organization_id", table_name="pairing_grants")
    op.drop_table("pairing_grants")
