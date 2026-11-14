"""Create the current database schema as a squashed baseline.

Revision ID: c591a26d37f4
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "c591a26d37f4"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_closed", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("max_slots_per_participant_per_day", sa.Integer(), nullable=False),
        sa.Column(
            "max_pending_bookings_per_slot",
            sa.Integer(),
            server_default="3",
            nullable=False,
        ),
        sa.Column("allow_booking_same_intervener_per_day", sa.Boolean(), nullable=False),
        sa.CheckConstraint("end_date >= start_date", name="ck_events_valid_date_range"),
        sa.CheckConstraint(
            "max_pending_bookings_per_slot >= 1",
            name="ck_events_positive_booking_queue_limit",
        ),
        sa.CheckConstraint(
            "max_slots_per_participant_per_day >= 1",
            name="ck_events_positive_daily_slot_limit",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "participant_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_intervener", sa.Boolean(), nullable=False),
        sa.Column("emoji", sa.String(length=32), nullable=True),
        sa.Column("discord_role_id", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("floor_plan_url", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_locations_event_id"), "locations", ["event_id"], unique=False)
    op.create_table(
        "participants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("type_id", sa.Integer(), nullable=False),
        sa.Column("discord_handle", sa.String(length=100), nullable=True),
        sa.Column("social_url", sa.String(length=2048), nullable=True),
        sa.Column("avatar_url", sa.String(length=2048), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discord_id", sa.String(length=32), nullable=True),
        sa.Column("discord_avatar", sa.String(length=255), nullable=True),
        sa.Column("discord_roles", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["type_id"], ["participant_types.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discord_id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index(op.f("ix_participants_type_id"), "participants", ["type_id"], unique=False)
    op.create_table(
        "event_participants",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id", "participant_id"),
    )
    op.create_table(
        "location_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("photo_url", sa.String(length=2048), nullable=False),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_location_photos_location_id"),
        "location_photos",
        ["location_id"],
        unique=False,
    )
    op.create_table(
        "participant_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=False),
        sa.Column("photo_url", sa.String(length=2048), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_participant_photos_participant_id"),
        "participant_photos",
        ["participant_id"],
        unique=False,
    )
    op.create_table(
        "time_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("intervener_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("is_lunch_break", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.CheckConstraint("capacity >= 1", name="ck_time_slots_positive_capacity"),
        sa.CheckConstraint("end_time > start_time", name="ck_time_slots_valid_time_range"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["intervener_id"], ["participants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_time_slots_event_date",
        "time_slots",
        ["event_id", "date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_time_slots_intervener_id"),
        "time_slots",
        ["intervener_id"],
        unique=False,
    )
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("time_slot_id", sa.Integer(), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "cancelled",
                name="bookingstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["time_slot_id"], ["time_slots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "time_slot_id",
            "participant_id",
            name="uq_bookings_slot_participant",
        ),
    )
    op.create_index(
        "ix_bookings_slot_status",
        "bookings",
        ["time_slot_id", "status"],
        unique=False,
    )
    op.create_table(
        "booking_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("photo_url", sa.String(length=2048), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_booking_photos_booking_id"),
        "booking_photos",
        ["booking_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_booking_photos_booking_id"), table_name="booking_photos")
    op.drop_table("booking_photos")
    op.drop_index("ix_bookings_slot_status", table_name="bookings")
    op.drop_table("bookings")
    op.drop_index(op.f("ix_time_slots_intervener_id"), table_name="time_slots")
    op.drop_index("ix_time_slots_event_date", table_name="time_slots")
    op.drop_table("time_slots")
    op.drop_index(
        op.f("ix_participant_photos_participant_id"),
        table_name="participant_photos",
    )
    op.drop_table("participant_photos")
    op.drop_index(op.f("ix_location_photos_location_id"), table_name="location_photos")
    op.drop_table("location_photos")
    op.drop_table("event_participants")
    op.drop_index(op.f("ix_participants_type_id"), table_name="participants")
    op.drop_table("participants")
    op.drop_index(op.f("ix_locations_event_id"), table_name="locations")
    op.drop_table("locations")
    op.drop_table("participant_types")
    op.drop_table("events")
