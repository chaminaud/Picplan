# SPDX-License-Identifier: LGPL-3.0-or-later
# This file is part of PicPlan. See COPYING.LESSER and COPYING for license details.

from datetime import date, datetime, time, timezone
from enum import Enum

from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Enum as SqlEnum, ForeignKey, Index, Integer, JSON, String, Table, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


event_participants = Table(
    "event_participants",
    Base.metadata,
    Column("event_id", ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "participant_id",
        ForeignKey("participants.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class BookingStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_events_valid_date_range"),
        CheckConstraint("max_slots_per_participant_per_day >= 1", name="ck_events_positive_daily_slot_limit"),
        CheckConstraint("max_pending_bookings_per_slot >= 1", name="ck_events_positive_booking_queue_limit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    max_slots_per_participant_per_day: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_pending_bookings_per_slot: Mapped[int] = mapped_column(Integer, default=3, server_default="3", nullable=False)
    allow_booking_same_intervener_per_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    locations: Mapped[list["Location"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    time_slots: Mapped[list["TimeSlot"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    participants: Mapped[list["Participant"]] = relationship(
        secondary=event_participants,
        back_populates="events",
    )


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    floor_plan_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    event: Mapped["Event"] = relationship(back_populates="locations")
    photos: Mapped[list["LocationPhoto"]] = relationship(back_populates="location", cascade="all, delete-orphan")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="location")


class LocationPhoto(Base):
    __tablename__ = "location_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    location: Mapped["Location"] = relationship(back_populates="photos")


class ParticipantType(Base):
    __tablename__ = "participant_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_intervener: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(32))
    discord_role_id: Mapped[str | None] = mapped_column(String(32))

    participants: Mapped[list["Participant"]] = relationship(back_populates="participant_type")


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)
    type_id: Mapped[int] = mapped_column(ForeignKey("participant_types.id", ondelete="RESTRICT"), nullable=False, index=True)
    discord_handle: Mapped[str | None] = mapped_column(String(100))
    social_url: Mapped[str | None] = mapped_column(String(2048))
    avatar_url: Mapped[str | None] = mapped_column(String(2048))
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    discord_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    discord_avatar: Mapped[str | None] = mapped_column(String(255))
    discord_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    participant_type: Mapped["ParticipantType"] = relationship(back_populates="participants")
    photos: Mapped[list["ParticipantPhoto"]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
        order_by="ParticipantPhoto.id",
    )
    time_slots: Mapped[list["TimeSlot"]] = relationship(back_populates="intervener")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="participant")
    events: Mapped[list["Event"]] = relationship(
        secondary=event_participants,
        back_populates="participants",
    )


class ParticipantPhoto(Base):
    __tablename__ = "participant_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    photo_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    participant: Mapped["Participant"] = relationship(back_populates="photos")


class TimeSlot(Base):
    __tablename__ = "time_slots"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="ck_time_slots_valid_time_range"),
        CheckConstraint("capacity >= 1", name="ck_time_slots_positive_capacity"),
        Index("ix_time_slots_event_date", "event_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    intervener_id: Mapped[int] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    is_lunch_break: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    event: Mapped["Event"] = relationship(back_populates="time_slots")
    intervener: Mapped["Participant"] = relationship(back_populates="time_slots")
    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="time_slot",
        cascade="all, delete-orphan",
        order_by="Booking.created_at, Booking.id",
    )


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("time_slot_id", "participant_id", name="uq_bookings_slot_participant"),
        Index("ix_bookings_slot_status", "time_slot_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    time_slot_id: Mapped[int] = mapped_column(ForeignKey("time_slots.id", ondelete="CASCADE"), nullable=False)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[BookingStatus] = mapped_column(
        SqlEnum(BookingStatus, native_enum=False, values_callable=lambda enum: [item.value for item in enum]),
        default=BookingStatus.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    time_slot: Mapped["TimeSlot"] = relationship(back_populates="bookings")
    participant: Mapped["Participant"] = relationship(back_populates="bookings")
    location: Mapped["Location"] = relationship(back_populates="bookings")
    photos: Mapped[list["BookingPhoto"]] = relationship(back_populates="booking", cascade="all, delete-orphan")


class BookingPhoto(Base):
    __tablename__ = "booking_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    booking: Mapped["Booking"] = relationship(back_populates="photos")
