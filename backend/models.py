from sqlalchemy import Column, Integer, String, DateTime, Boolean, Date, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Profile
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    studio_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)

    # Roles
    is_admin = Column(Boolean, default=False, nullable=False, server_default="false")

    # Email verification
    email_verified = Column(Boolean, default=False, nullable=False, server_default="false")
    verification_token = Column(String, nullable=True)
    verification_token_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Password reset
    reset_token = Column(String, nullable=True)
    reset_token_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    students = relationship("Student", back_populates="owner", cascade="all, delete-orphan")
    event_registrations = relationship("EventRegistration", back_populates="user", cascade="all, delete-orphan")


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User", back_populates="students")


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    event_date = Column(DateTime(timezone=True), nullable=False)
    location = Column(String, nullable=True)
    event_type = Column(String, nullable=True)  # "convention" or "competition"
    early_price = Column(Numeric(10, 2), nullable=True)          # early-bird price (conventions)
    regular_price = Column(Numeric(10, 2), nullable=True)        # regular / at-door price
    early_price_deadline = Column(DateTime(timezone=True), nullable=True)  # deadline for early price
    max_students = Column(Integer, nullable=True)                 # student capacity cap (None = unlimited)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    registrations = relationship("EventRegistration", back_populates="event", cascade="all, delete-orphan")

    @property
    def registered_count(self) -> int:
        """Total number of individual students registered across all registrations."""
        return sum(len(reg.attending_students) for reg in self.registrations)


class EventRegistration(Base):
    __tablename__ = "event_registrations"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Payment / finalization
    is_finalized      = Column(Boolean, default=False, nullable=False, server_default="false")
    payment_status    = Column(String, nullable=True)   # 'pending' | 'paid' | 'free'
    stripe_session_id = Column(String, nullable=True)
    amount_paid       = Column(Numeric(10, 2), nullable=True)
    finalized_at      = Column(DateTime(timezone=True), nullable=True)

    event = relationship("Event", back_populates="registrations")
    user = relationship("User", back_populates="event_registrations")
    attending_students = relationship("EventRegistrationStudent", back_populates="registration", cascade="all, delete-orphan")


class EventRegistrationStudent(Base):
    __tablename__ = "event_registration_students"

    id = Column(Integer, primary_key=True, index=True)
    registration_id = Column(Integer, ForeignKey("event_registrations.id"), nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)

    registration = relationship("EventRegistration", back_populates="attending_students")
    student = relationship("Student")
