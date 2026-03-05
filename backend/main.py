import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text

import models
import schemas
import auth
import email_service
from database import engine, get_db

logger = logging.getLogger(__name__)

# ── Database setup ────────────────────────────────────────────────────────────

models.Base.metadata.create_all(bind=engine)


def run_migrations(eng):
    """Idempotently add columns introduced after initial deployment."""
    stmts = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token_expires_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS studio_name VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false",
    ]
    with eng.connect() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.commit()


run_migrations(engine)


def promote_admins(eng):
    """Grant is_admin=true to every email listed in the ADMIN_EMAILS env var."""
    raw = os.getenv("ADMIN_EMAILS", "")
    emails = [e.strip().lower() for e in raw.split(",") if e.strip()]
    if not emails:
        return
    with eng.connect() as conn:
        for email in emails:
            conn.execute(
                text("UPDATE users SET is_admin = true WHERE LOWER(email) = :email"),
                {"email": email},
            )
        conn.commit()
    logger.info("Admin promotion applied for: %s", emails)


promote_admins(engine)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="RadiatePro API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Constants ─────────────────────────────────────────────────────────────────

VERIFICATION_EXPIRE_HOURS = 24
RESET_EXPIRE_HOURS = 1

# ── Auth routes ───────────────────────────────────────────────────────────────


@app.post("/auth/register", response_model=schemas.MessageOut, status_code=201)
def register(user: schemas.UserCreate, db: Session = Depends(get_db)):
    """Create a new user account and send a verification email."""
    existing = db.query(models.User).filter(models.User.email == user.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    if len(user.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    token = auth.generate_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_EXPIRE_HOURS)

    new_user = models.User(
        email=user.email,
        hashed_password=auth.hash_password(user.password),
        email_verified=False,
        verification_token=token,
        verification_token_expires_at=expires,
        first_name=user.first_name,
        last_name=user.last_name,
        studio_name=user.studio_name,
        phone=user.phone,
    )
    db.add(new_user)
    db.commit()

    try:
        email_service.send_verification_email(user.email, token)
    except Exception:
        logger.exception("Failed to send verification email to %s", user.email)

    return {"message": "Account created. Please check your email to verify your account."}


@app.post("/auth/login", response_model=schemas.Token)
def login(user: schemas.UserLogin, db: Session = Depends(get_db)):
    """Authenticate and return a JWT access token."""
    db_user = db.query(models.User).filter(models.User.email == user.email).first()
    if not db_user or not auth.verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not db_user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before logging in.",
        )

    token = auth.create_access_token({"sub": db_user.email, "user_id": db_user.id})
    return {"access_token": token, "token_type": "bearer"}


@app.post("/auth/verify-email", response_model=schemas.MessageOut)
def verify_email(body: schemas.VerifyEmailRequest, db: Session = Depends(get_db)):
    """Mark the user's email as verified using a one-time token."""
    now = datetime.now(timezone.utc)
    user = (
        db.query(models.User)
        .filter(models.User.verification_token == body.token)
        .first()
    )

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link.")

    if user.verification_token_expires_at is None or user.verification_token_expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=400, detail="Verification link has expired. Please register again.")

    if user.email_verified:
        return {"message": "Email already verified. You can sign in."}

    user.email_verified = True
    user.verification_token = None
    user.verification_token_expires_at = None
    db.commit()

    return {"message": "Email verified successfully. You can now sign in."}


@app.post("/auth/request-password-reset", response_model=schemas.MessageOut)
def request_password_reset(body: schemas.PasswordResetRequest, db: Session = Depends(get_db)):
    """Send a password-reset email. Always returns success to prevent account enumeration."""
    user = db.query(models.User).filter(models.User.email == body.email).first()

    if user:
        token = auth.generate_token()
        expires = datetime.now(timezone.utc) + timedelta(hours=RESET_EXPIRE_HOURS)
        user.reset_token = token
        user.reset_token_expires_at = expires
        db.commit()

        try:
            email_service.send_reset_email(user.email, token)
        except Exception:
            logger.exception("Failed to send reset email to %s", user.email)

    return {"message": "If that email is registered, you'll receive a reset link."}


@app.post("/auth/reset-password", response_model=schemas.MessageOut)
def reset_password(body: schemas.PasswordResetConfirm, db: Session = Depends(get_db)):
    """Validate a reset token and update the user's password."""
    now = datetime.now(timezone.utc)
    user = (
        db.query(models.User)
        .filter(models.User.reset_token == body.token)
        .first()
    )

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

    if user.reset_token_expires_at is None or user.reset_token_expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=400, detail="Reset link has expired. Please request a new one.")

    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

    user.hashed_password = auth.hash_password(body.new_password)
    user.reset_token = None
    user.reset_token_expires_at = None
    db.commit()

    return {"message": "Password reset successfully. You can now sign in."}


@app.get("/auth/me", response_model=schemas.UserOut)
def me(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return the currently authenticated user."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = auth.decode_access_token(authorization.split(" ", 1)[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.query(models.User).filter(models.User.email == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Admin helpers ─────────────────────────────────────────────────────────────

def get_current_user(authorization: Optional[str], db: Session) -> models.User:
    """Resolve a Bearer token to a User, raising 401/404 as needed."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = auth.decode_access_token(authorization.split(" ", 1)[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(models.User).filter(models.User.email == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin/users", response_model=list[schemas.UserOut])
def admin_list_users(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return all users. Admin only."""
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return db.query(models.User).order_by(models.User.id).all()


@app.delete("/admin/users/{user_id}", response_model=schemas.MessageOut)
def admin_delete_user(
    user_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Delete a user by ID. Admin only. Cannot delete yourself."""
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    if current.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    target = db.query(models.User).filter(models.User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    db.delete(target)
    db.commit()
    return {"message": f"User {user_id} deleted."}
