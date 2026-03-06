from sqlalchemy import Column, Integer, String, DateTime, Boolean, Date, ForeignKey, Numeric, Text
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
    observers = relationship("Observer", back_populates="user", cascade="all, delete-orphan")
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
    venue_name = Column(String, nullable=True)  # e.g. "King of Prussia Convention Center"
    location   = Column(String, nullable=True)  # street address / city
    event_type = Column(String, nullable=True)  # "convention" or "competition"
    early_price = Column(Numeric(10, 2), nullable=True)
    regular_price = Column(Numeric(10, 2), nullable=True)
    early_price_deadline = Column(DateTime(timezone=True), nullable=True)
    max_students = Column(Integer, nullable=True)
    observer_price = Column(Numeric(10, 2), nullable=True)
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
    is_finalized         = Column(Boolean, default=False, nullable=False, server_default="false")
    payment_status       = Column(String, nullable=True)   # 'pending' | 'paid' | 'free' | 'admin-paid'
    stripe_session_id    = Column(String, nullable=True)
    amount_paid          = Column(Numeric(10, 2), nullable=True)
    finalized_at         = Column(DateTime(timezone=True), nullable=True)
    # Comma-separated IDs of admin-added dancers not yet paid for.
    # Set when admin adds an unpaid dancer to a finalized registration.
    # Cleared when the user completes payment or admin marks as paid.
    pending_student_ids  = Column(Text, nullable=True)
    # Comma-separated IDs of dancers individually paid cash/offline by admin.
    # Same token format as pending_student_ids ("5" for student, "o3" for observer).
    cash_student_ids     = Column(Text, nullable=True)
    # Accumulated credit from paid attendees who were later removed by admin.
    # Can be applied to offset pending balances on this registration.
    credit_amount        = Column(Numeric(10, 2), nullable=True, default=0)
    # Comma-separated tokens of attendees whose balance was settled via credit (not new cash).
    # Same token format: "5" for student id 5, "o3" for observer id 3.
    credit_applied_ids   = Column(Text, nullable=True)

    event = relationship("Event", back_populates="registrations")
    user = relationship("User", back_populates="event_registrations")
    attending_students = relationship("EventRegistrationStudent", back_populates="registration", cascade="all, delete-orphan")
    attending_observers = relationship("EventRegistrationObserver", back_populates="registration", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="registration", cascade="all, delete-orphan")


class EventRegistrationStudent(Base):
    __tablename__ = "event_registration_students"

    id = Column(Integer, primary_key=True, index=True)
    registration_id = Column(Integer, ForeignKey("event_registrations.id"), nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)

    registration = relationship("EventRegistration", back_populates="attending_students")
    student = relationship("Student")


class Observer(Base):
    __tablename__ = "observers"
    id                = Column(Integer, primary_key=True, index=True)
    user_id           = Column(Integer, ForeignKey("users.id"), nullable=False)
    name              = Column(String, nullable=False)
    linked_student_id = Column(Integer, ForeignKey("students.id", ondelete="SET NULL"), nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    user         = relationship("User", back_populates="observers")
    registrations = relationship("EventRegistrationObserver", back_populates="observer", cascade="all, delete-orphan")


class EventRegistrationObserver(Base):
    __tablename__ = "event_registration_observers"
    id              = Column(Integer, primary_key=True)
    registration_id = Column(Integer, ForeignKey("event_registrations.id"), nullable=False)
    observer_id     = Column(Integer, ForeignKey("observers.id"), nullable=False)
    registration = relationship("EventRegistration", back_populates="attending_observers")
    observer     = relationship("Observer", back_populates="registrations")


class Transaction(Base):
    """Immutable record of a single payment event. One row per payment — never updated."""
    __tablename__ = "transactions"

    id              = Column(Integer, primary_key=True, index=True)
    registration_id = Column(Integer, ForeignKey("event_registrations.id"), nullable=False, index=True)
    event_id        = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount          = Column(Numeric(10, 2), nullable=False, default=0)
    payment_status  = Column(String, nullable=False)   # 'paid' | 'free' | 'admin-paid'
    stripe_session_id = Column(String, nullable=True)  # used to deduplicate webhook vs verify-payment
    description     = Column(String, nullable=True)    # e.g. "3 dancer(s)", "1 added dancer"
    student_count   = Column(Integer, nullable=True, default=0)
    observer_count  = Column(Integer, nullable=True, default=0)
    # Optional: which specific dancer this transaction covers (null for batch/whole-reg txns)
    student_id      = Column(Integer, ForeignKey("students.id"), nullable=True)
    observer_id     = Column(Integer, ForeignKey("observers.id"), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    registration = relationship("EventRegistration", back_populates="transactions")
    event        = relationship("Event")
    user         = relationship("User")
    student      = relationship("Student",  foreign_keys=[student_id])
    observer     = relationship("Observer", foreign_keys=[observer_id])
