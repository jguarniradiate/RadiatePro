from sqlalchemy import Column, Integer, String, DateTime, Boolean
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
