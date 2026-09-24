"""Data models.

Defect: one row per detected defect (seeded from the UVeye/Snowflake export).
  Adds workflow fields the analytics dashboard makes writeable:
    - status: New | Reviewed | Resolved
    - flagged: follow-up flag
Note: free-text comments attached to a defect (one-to-many).
"""
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from database import Base


class Defect(Base):
    __tablename__ = "defects"

    id = Column(Integer, primary_key=True, index=True)
    inspection_id = Column(String, index=True)
    vin = Column(String, index=True)
    make = Column(String, index=True)
    model = Column(String)
    year = Column(String)
    inspected_at = Column(String, index=True)
    part_name = Column(String, index=True)
    module = Column(String, index=True)
    description = Column(Text)
    cost = Column(Float, default=0.0)
    score = Column(Float, default=0.0, index=True)   # QTYSCORE — the core Northwind quality metric
    line = Column(String, index=True)                # production line (LN1..LN6)
    plant = Column(String, index=True)
    conversion = Column(String, index=True)          # conversion type (side/rear entry)
    zone = Column(String)
    failure_mode = Column(String, index=True)
    source = Column(String, default="quality", index=True)   # 'quality' (QS2.0) or 'uveye'

    # ----- writeable workflow fields (the CRUD layer) -----
    status = Column(String, default="New", index=True)   # New | Reviewed | Resolved
    flagged = Column(Boolean, default=False, index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    notes = relationship("Note", back_populates="defect",
                         cascade="all, delete-orphan", order_by="Note.created_at")


class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    defect_id = Column(Integer, ForeignKey("defects.id", ondelete="CASCADE"), index=True)
    author = Column(String, default="Analyst")
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    defect = relationship("Defect", back_populates="notes")


class ActionItem(Base):
    """A tracked quality issue, usually created from an insight. Turns 'we spotted
    a problem' into 'someone owns it and it has a status until it's closed.'"""
    __tablename__ = "action_items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    detail = Column(Text, default="")
    source = Column(String, default="quality", index=True)   # quality | pdi | uveye
    dimension = Column(String, default="")                    # line | failure_mode | ...
    entity = Column(String, default="")                       # LN2, ALIGNMENT, ...
    severity = Column(String, default="medium")               # high | medium | low
    status = Column(String, default="open", index=True)       # open | in_progress | resolved
    owner_role = Column(String, default="velocity")           # which role owns it
    created_by = Column(String, default="")                   # role that opened it
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = Column(DateTime, nullable=True)
    updates = Column(Text, default="")                        # JSON list of progress notes



# ==================================================================
# Admin — user directory, sign-off departments, role assignment.
# Replaces the legacy Quality App's Admin > User Management section.
# Lives in the app database (SQLite/Postgres), not Snowflake: users are
# an application concern and shouldn't need a warehouse grant to manage.
# ==================================================================

class AppUser(Base):
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, index=True)
    employee_name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    dept = Column(String, default="", index=True)      # home department (QUAL, LN1, ...)
    role = Column(String, default="wav", index=True)   # wav | dealer | velocity | admin
    scope = Column(String, default="")                 # production line for inspectors, blank otherwise
    password_hash = Column(String, default="")
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime, nullable=True)

    signoff_depts = relationship("UserSignoffDept", back_populates="user",
                                 cascade="all, delete-orphan")


class UserSignoffDept(Base):
    """Which departments a user is allowed to stamp sign-offs for.
    Legacy equivalent: Admin > Manage User Sign-off Depts."""
    __tablename__ = "user_signoff_depts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), index=True)
    dept = Column(String, nullable=False, index=True)
    active = Column(Boolean, default=True)

    user = relationship("AppUser", back_populates="signoff_depts")


class SignoffDept(Base):
    """The sign-off department dropdown list itself.
    Legacy equivalent: Admin > Manage Sign-Off Drop Down Depts."""
    __tablename__ = "signoff_depts"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    label = Column(String, default="")
    active = Column(Boolean, default=True, index=True)


class AuditEvent(Base):
    """Every mutating action taken in Admin, so 'who changed this and when'
    has an answer. This is the app-side half of the Audit screen; the other
    half (sign-offs, holds) already has its own timestamps in Snowflake and
    is read live rather than duplicated here."""
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, index=True)
    actor = Column(String, default="", index=True)      # username/email from the session token
    action = Column(String, nullable=False, index=True)  # e.g. user.role_changed
    target = Column(String, default="", index=True)      # what it was done to
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
