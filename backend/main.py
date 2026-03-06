import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET    = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY   = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://goldfish-app-fuu3t.ondigitalocean.app")

import models
import schemas
import auth
import email_service
from database import engine, get_db

logger = logging.getLogger(__name__)

# ── Database setup ────────────────────────────────────────────────────────────
# create_all() is intentionally removed: it has no lock-timeout protection and
# blocks on CREATE TABLE when the old instance holds connections.  All schema
# work (including the initial users table) lives inside run_migrations(), which
# runs each statement in autocommit mode with a 10-second lock_timeout so a
# rolling deploy never hangs waiting for an ACCESS EXCLUSIVE lock.


def run_migrations(eng):
    """Idempotently create / alter all schema objects after initial deployment.

    Each statement runs in its own autocommit transaction so a lock
    timeout on one ALTER TABLE does not roll back the others or block
    the deploy indefinitely.
    """
    stmts = [
        # ── base users table (idempotent; all later columns added below) ──
        """CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR UNIQUE NOT NULL,
            hashed_password VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        )""",
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
        """CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR NOT NULL,
            date_of_birth DATE,
            gender VARCHAR,
            created_at TIMESTAMPTZ DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            title VARCHAR NOT NULL,
            description VARCHAR,
            event_date TIMESTAMPTZ NOT NULL,
            location VARCHAR,
            created_at TIMESTAMPTZ DEFAULT now()
        )""",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS event_type VARCHAR",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS early_price NUMERIC(10,2)",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS regular_price NUMERIC(10,2)",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS early_price_deadline TIMESTAMPTZ",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS max_students INTEGER",
        """CREATE TABLE IF NOT EXISTS event_registrations (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(event_id, user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS event_registration_students (
            id SERIAL PRIMARY KEY,
            registration_id INTEGER NOT NULL REFERENCES event_registrations(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(registration_id, student_id)
        )""",
        # Payment / finalization columns
        "ALTER TABLE event_registrations ADD COLUMN IF NOT EXISTS is_finalized BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE event_registrations ADD COLUMN IF NOT EXISTS payment_status VARCHAR",
        "ALTER TABLE event_registrations ADD COLUMN IF NOT EXISTS stripe_session_id VARCHAR",
        "ALTER TABLE event_registrations ADD COLUMN IF NOT EXISTS amount_paid NUMERIC(10,2)",
        "ALTER TABLE event_registrations ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ",
        # Observer columns + tables
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS observer_price NUMERIC(10,2)",
        """CREATE TABLE IF NOT EXISTS observers (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS event_registration_observers (
            id SERIAL PRIMARY KEY,
            registration_id INTEGER NOT NULL REFERENCES event_registrations(id) ON DELETE CASCADE,
            observer_id INTEGER NOT NULL REFERENCES observers(id) ON DELETE CASCADE
        )""",
        "ALTER TABLE observers ADD COLUMN IF NOT EXISTS linked_student_id INTEGER REFERENCES students(id) ON DELETE SET NULL",
        # Pending payment tracking for admin-added unpaid dancers
        "ALTER TABLE event_registrations ADD COLUMN IF NOT EXISTS pending_student_ids TEXT",
        # Immutable transaction ledger — one row per payment event
        """CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            registration_id INTEGER NOT NULL REFERENCES event_registrations(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount NUMERIC(10,2) NOT NULL DEFAULT 0,
            payment_status VARCHAR NOT NULL,
            stripe_session_id VARCHAR,
            description VARCHAR,
            student_count INTEGER DEFAULT 0,
            observer_count INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT now()
        )""",
    ]
    try:
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # Each DDL runs as its own transaction; set a short lock
            # timeout so ALTER TABLE never hangs the deploy.
            conn.execute(text("SET lock_timeout = '10s'"))
            conn.execute(text("SET statement_timeout = '30s'"))
            for stmt in stmts:
                try:
                    conn.execute(text(stmt))
                except Exception as exc:
                    logger.warning(
                        "Migration skipped (will retry next deploy): %.80s — %s",
                        stmt.strip(),
                        exc,
                    )
    except Exception as exc:
        logger.error("run_migrations: DB connection failed at startup: %s", exc)


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


# ── Background DB startup ─────────────────────────────────────────────────────
# Migrations run in a daemon thread so uvicorn binds port 8080 and passes
# DigitalOcean's health check before any DDL lock is attempted.  This prevents
# the rolling-deploy hang where ALTER TABLE waits on locks held by the old
# instance.  Each DDL statement already has its own 10-second lock_timeout
# inside run_migrations().

def _db_startup() -> None:
    """Run schema migrations and admin promotion in a background thread."""
    try:
        run_migrations(engine)
    except Exception as exc:
        logger.error("run_migrations failed: %s", exc)
    try:
        promote_admins(engine)
    except Exception as exc:
        logger.error("promote_admins failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Modern lifespan handler (replaces deprecated on_event).

    Launches DB migrations in a background daemon thread so uvicorn
    can bind port 8080 and pass health checks before any DDL runs.
    """
    threading.Thread(target=_db_startup, daemon=True, name="db-startup").start()
    logger.info("DB startup thread launched")
    yield  # app runs here
    # shutdown – nothing to clean up


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="RadiatePro API", version="1.0.0", lifespan=lifespan)

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


@app.patch("/users/me", response_model=schemas.UserOut)
def update_me(
    body: schemas.UserUpdate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Allow a logged-in user to update their own profile."""
    user = get_current_user(authorization, db)

    if body.email and body.email != user.email:
        existing = db.query(models.User).filter(models.User.email == body.email).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already in use.")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


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


@app.get("/stripe/config")
def stripe_config():
    """Return the Stripe publishable key so the frontend can initialise Stripe.js."""
    return {"publishable_key": STRIPE_PUBLISHABLE_KEY}


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _effective_price(event: models.Event):
    """Return (price_in_dollars, is_free) for the event right now."""
    from decimal import Decimal as D
    now = datetime.now(timezone.utc)
    if event.early_price is not None and event.early_price_deadline is not None:
        dl = event.early_price_deadline
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=timezone.utc)
        price = event.early_price if now < dl else event.regular_price
    else:
        price = event.regular_price
    if price is None:
        return D("0"), True
    return D(str(price)), False


def _record_transaction(
    db,
    reg: models.EventRegistration,
    amount,
    payment_status: str,
    stripe_session_id: str = None,
    description: str = None,
    student_count: int = 0,
    observer_count: int = 0,
) -> models.Transaction:
    """Insert a Transaction row unless one with this stripe_session_id already exists.
    Stripe may fire both the webhook and the verify-payment response; the second
    caller simply gets the existing row back without a duplicate insert.
    """
    from decimal import Decimal as D
    if stripe_session_id:
        existing = db.query(models.Transaction).filter(
            models.Transaction.stripe_session_id == stripe_session_id
        ).first()
        if existing:
            return existing
    tx = models.Transaction(
        registration_id=reg.id,
        event_id=reg.event_id,
        user_id=reg.user_id,
        amount=D(str(amount)),
        payment_status=payment_status,
        stripe_session_id=stripe_session_id,
        description=description,
        student_count=student_count,
        observer_count=observer_count,
    )
    db.add(tx)
    return tx


def _build_reg_out(reg: models.EventRegistration) -> schemas.EventRegistrationOut:
    return schemas.EventRegistrationOut(
        id=reg.id,
        event_id=reg.event_id,
        user_id=reg.user_id,
        student_ids=[ers.student_id for ers in reg.attending_students],
        observer_ids=[ero.observer_id for ero in reg.attending_observers],
        created_at=reg.created_at,
        is_finalized=reg.is_finalized,
        payment_status=reg.payment_status,
        amount_paid=reg.amount_paid,
    )


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin/users", response_model=list[schemas.UserOut])
def admin_list_users(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
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


@app.patch("/admin/users/{user_id}", response_model=schemas.UserOut)
def admin_update_user(
    user_id: int,
    body: schemas.UserUpdate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    target = db.query(models.User).filter(models.User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if body.email and body.email != target.email:
        existing = db.query(models.User).filter(models.User.email == body.email).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already in use.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(target, field, value)
    db.commit()
    db.refresh(target)
    return target


# ── Student routes ────────────────────────────────────────────────────────────

@app.get("/students", response_model=list[schemas.StudentOut])
def list_students(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    return db.query(models.Student).filter(models.Student.user_id == user.id).order_by(models.Student.name).all()


@app.post("/students", response_model=schemas.StudentOut, status_code=201)
def create_student(
    body: schemas.StudentCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    student = models.Student(user_id=user.id, **body.model_dump())
    db.add(student)
    db.commit()
    db.refresh(student)
    return student


@app.patch("/students/{student_id}", response_model=schemas.StudentOut)
def update_student(
    student_id: int,
    body: schemas.StudentUpdate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    student = db.query(models.Student).filter(
        models.Student.id == student_id,
        models.Student.user_id == user.id,
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(student, field, value)
    db.commit()
    db.refresh(student)
    return student


@app.delete("/students/{student_id}", response_model=schemas.MessageOut)
def delete_student(
    student_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    student = db.query(models.Student).filter(
        models.Student.id == student_id,
        models.Student.user_id == user.id,
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    # Block deletion if dancer is in a finalized upcoming event registration
    upcoming_finalized = (
        db.query(models.EventRegistrationStudent)
        .join(models.EventRegistration,
              models.EventRegistrationStudent.registration_id == models.EventRegistration.id)
        .join(models.Event,
              models.EventRegistration.event_id == models.Event.id)
        .filter(
            models.EventRegistrationStudent.student_id == student_id,
            models.EventRegistration.is_finalized.is_(True),
            models.Event.event_date >= datetime.now(timezone.utc),
        )
        .first()
    )
    if upcoming_finalized:
        raise HTTPException(
            status_code=400,
            detail=(
                f'"{student.name}" cannot be removed — they are registered '
                f"for an upcoming event. Contact the event organizer if changes are needed."
            ),
        )

    # Remove student from any pending (non-finalized) registration association rows
    # to avoid FK constraint violations on delete.
    db.query(models.EventRegistrationStudent).filter(
        models.EventRegistrationStudent.student_id == student_id
    ).delete()

    db.delete(student)
    db.commit()
    return {"message": "Student deleted."}


# ── Observer routes ───────────────────────────────────────────────────────────

@app.get("/observers", response_model=list[schemas.ObserverOut])
def list_observers(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    user = get_current_user(authorization, db)
    return db.query(models.Observer).filter(models.Observer.user_id == user.id).order_by(models.Observer.name).all()


@app.post("/observers", response_model=schemas.ObserverOut, status_code=201)
def create_observer(body: schemas.ObserverCreate, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    user = get_current_user(authorization, db)
    obs = models.Observer(user_id=user.id, name=body.name.strip(), linked_student_id=body.linked_student_id)
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs


@app.put("/observers/{observer_id}", response_model=schemas.ObserverOut)
def update_observer(observer_id: int, body: schemas.ObserverCreate, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    user = get_current_user(authorization, db)
    obs = db.query(models.Observer).filter(models.Observer.id == observer_id, models.Observer.user_id == user.id).first()
    if not obs:
        raise HTTPException(status_code=404, detail="Observer not found.")
    obs.name = body.name.strip()
    obs.linked_student_id = body.linked_student_id
    db.commit()
    db.refresh(obs)
    return obs


@app.delete("/observers/{observer_id}", response_model=schemas.MessageOut)
def delete_observer(observer_id: int, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    user = get_current_user(authorization, db)
    obs = db.query(models.Observer).filter(models.Observer.id == observer_id, models.Observer.user_id == user.id).first()
    if not obs:
        raise HTTPException(status_code=404, detail="Observer not found.")
    db.delete(obs)
    db.commit()
    return {"message": "Observer deleted."}


# ── Event routes ──────────────────────────────────────────────────────────────

def _build_event_out(event: models.Event) -> schemas.EventOut:
    from decimal import Decimal as D
    now = datetime.now(timezone.utc)
    count = event.registered_count

    price = None
    if event.early_price is not None and event.early_price_deadline is not None:
        dl = event.early_price_deadline
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=timezone.utc)
        price = event.early_price if now < dl else event.regular_price
    elif event.regular_price is not None:
        price = event.regular_price

    total_revenue = (D(str(price)) * count) if price is not None else None

    return schemas.EventOut(
        id=event.id,
        title=event.title,
        description=event.description,
        event_date=event.event_date,
        location=event.location,
        event_type=event.event_type,
        early_price=event.early_price,
        regular_price=event.regular_price,
        early_price_deadline=event.early_price_deadline,
        max_students=event.max_students,
        registered_count=count,
        total_revenue=total_revenue,
        observer_price=event.observer_price,
        created_at=event.created_at,
    )


@app.get("/events", response_model=list[schemas.EventOut])
def list_events(db: Session = Depends(get_db)):
    events = db.query(models.Event).order_by(models.Event.event_date).all()
    return [_build_event_out(e) for e in events]


@app.post("/admin/events", response_model=schemas.EventOut, status_code=201)
def create_event(
    body: schemas.EventCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    event = models.Event(**body.model_dump())
    db.add(event)
    db.commit()
    db.refresh(event)
    return _build_event_out(event)


@app.patch("/admin/events/{event_id}", response_model=schemas.EventOut)
def update_event(
    event_id: int,
    body: schemas.EventUpdate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(event, field, value)
    db.commit()
    db.refresh(event)
    return _build_event_out(event)


@app.delete("/admin/events/{event_id}", response_model=schemas.MessageOut)
def delete_event(
    event_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")
    db.delete(event)
    db.commit()
    return {"message": "Event deleted."}


# ── Registration routes ───────────────────────────────────────────────────────

@app.get("/events/my-registrations")
def get_my_registrations(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.user_id == user.id
    ).all()
    result = {}
    for reg in regs:
        student_ids = [ers.student_id for ers in reg.attending_students]
        observer_ids = [ero.observer_id for ero in reg.attending_observers]
        # Parse pending_student_ids into a list of raw tokens (e.g. "5", "o3")
        pending_raw = [x for x in (reg.pending_student_ids or "").split(",") if x.strip()]
        result[str(reg.event_id)] = {
            "registration_id": reg.id,
            "student_ids": student_ids,
            "observer_ids": observer_ids,
            "is_finalized": reg.is_finalized,
            "payment_status": reg.payment_status,
            "amount_paid": float(reg.amount_paid) if reg.amount_paid is not None else None,
            "stripe_session_id": reg.stripe_session_id,
            "pending_student_ids": pending_raw,   # list of "5", "o3"-style tokens
        }
    return result


@app.post("/events/{event_id}/register", response_model=schemas.EventRegistrationOut, status_code=201)
def register_for_event(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    existing = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()

    if existing and existing.is_finalized:
        raise HTTPException(
            status_code=400,
            detail="This registration is finalized. Use the Add Dancers flow to add more students."
        )

    if event.max_students is not None and body.student_ids:
        current_total = sum(len(reg.attending_students) for reg in event.registrations)
        if existing:
            current_total -= len(existing.attending_students)
        available = event.max_students - current_total
        if len(body.student_ids) > available:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough capacity. Only {available} spot(s) remaining."
            )

    if existing:
        db.query(models.EventRegistrationStudent).filter(
            models.EventRegistrationStudent.registration_id == existing.id
        ).delete()
        for sid in body.student_ids:
            s = db.query(models.Student).filter(
                models.Student.id == sid, models.Student.user_id == user.id
            ).first()
            if s:
                db.add(models.EventRegistrationStudent(registration_id=existing.id, student_id=sid))
        db.query(models.EventRegistrationObserver).filter(
            models.EventRegistrationObserver.registration_id == existing.id
        ).delete()
        for oid in body.observer_ids:
            o = db.query(models.Observer).filter(
                models.Observer.id == oid, models.Observer.user_id == user.id
            ).first()
            if o:
                db.add(models.EventRegistrationObserver(registration_id=existing.id, observer_id=oid))
        db.commit()
        db.refresh(existing)
        return _build_reg_out(existing)

    reg = models.EventRegistration(event_id=event_id, user_id=user.id)
    db.add(reg)
    db.flush()
    for sid in body.student_ids:
        s = db.query(models.Student).filter(
            models.Student.id == sid, models.Student.user_id == user.id
        ).first()
        if s:
            db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))
    for oid in body.observer_ids:
        o = db.query(models.Observer).filter(
            models.Observer.id == oid, models.Observer.user_id == user.id
        ).first()
        if o:
            db.add(models.EventRegistrationObserver(registration_id=reg.id, observer_id=oid))
    db.commit()
    db.refresh(reg)
    return _build_reg_out(reg)


@app.delete("/events/{event_id}/register", response_model=schemas.MessageOut)
def unregister_from_event(
    event_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Not registered for this event.")
    if reg.is_finalized:
        raise HTTPException(
            status_code=400,
            detail="Finalized registrations cannot be cancelled. Please contact the event organizer."
        )
    db.delete(reg)
    db.commit()
    return {"message": "Unregistered from event."}


@app.post("/events/{event_id}/register/checkout", response_model=schemas.CheckoutSessionOut, status_code=201)
def create_checkout(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    embedded: bool = Query(False, description="Use Stripe Embedded Checkout (returns client_secret)"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    from decimal import Decimal as D
    user = get_current_user(authorization, db)
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    existing = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()

    # Check for pending payment mode: admin added unpaid dancers to a finalized reg.
    pending_raw = [x for x in (existing.pending_student_ids or "").split(",") if x.strip()] if existing else []
    pending_dancer_ids = [int(x) for x in pending_raw if not x.startswith("o")]
    pending_observer_ids = [int(x[1:]) for x in pending_raw if x.startswith("o")]
    is_pending_payment_mode = bool(pending_raw) and existing and existing.is_finalized

    if existing and existing.is_finalized and not is_pending_payment_mode:
        raise HTTPException(status_code=400, detail="Registration already finalized. Use Add Dancers.")

    price_per_student, _ = _effective_price(event)
    observer_price = D(str(event.observer_price)) if event.observer_price else D("0")

    if is_pending_payment_mode:
        # Charge ONLY for the admin-added pending dancers/observers — not the whole reg.
        dancer_total   = price_per_student * len(pending_dancer_ids)
        observer_total = observer_price    * len(pending_observer_ids)
        grand_total    = dancer_total + observer_total
        amount_to_charge = grand_total
        is_free = amount_to_charge == D("0")
        reg = existing
    else:
        if not body.student_ids and not body.observer_ids:
            raise HTTPException(status_code=400, detail="Select at least one student or observer.")
        if event.max_students is not None and body.student_ids:
            current_total = sum(len(r.attending_students) for r in event.registrations)
            if existing:
                current_total -= len(existing.attending_students)
            available = event.max_students - current_total
            if len(body.student_ids) > available:
                raise HTTPException(status_code=400, detail=f"Only {available} spot(s) remaining.")
        dancer_total   = price_per_student * len(body.student_ids)
        observer_total = observer_price    * len(body.observer_ids)
        grand_total    = dancer_total + observer_total
        amount_to_charge = grand_total
        is_free = amount_to_charge == D("0")

    if not is_pending_payment_mode:
        # Normal flow: replace student/observer associations from body selection.
        if existing:
            db.query(models.EventRegistrationStudent).filter(
                models.EventRegistrationStudent.registration_id == existing.id
            ).delete()
            db.query(models.EventRegistrationObserver).filter(
                models.EventRegistrationObserver.registration_id == existing.id
            ).delete()
            reg = existing
        else:
            reg = models.EventRegistration(event_id=event_id, user_id=user.id)
            db.add(reg)
            db.flush()

        for sid in body.student_ids:
            s = db.query(models.Student).filter(
                models.Student.id == sid, models.Student.user_id == user.id
            ).first()
            if s:
                db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))

        for oid in body.observer_ids:
            o = db.query(models.Observer).filter(
                models.Observer.id == oid, models.Observer.user_id == user.id
            ).first()
            if o:
                db.add(models.EventRegistrationObserver(registration_id=reg.id, observer_id=oid))
    # else: pending payment mode — student/observer associations are already correct in the DB.

    if is_free:
        reg.is_finalized        = True
        reg.payment_status      = "free" if not is_pending_payment_mode else reg.payment_status
        reg.amount_paid         = (reg.amount_paid or D("0"))
        reg.finalized_at        = datetime.now(timezone.utc)
        reg.pending_student_ids = None   # clear pending — no charge needed
        # Record immutable transaction line
        _sc = len(pending_dancer_ids) if is_pending_payment_mode else len(body.student_ids)
        _oc = len(pending_observer_ids) if is_pending_payment_mode else len(body.observer_ids)
        _desc = f"Added {_sc} dancer(s)" if is_pending_payment_mode else f"{_sc} dancer(s)"
        if _oc:
            _desc += f", {_oc} observer(s)"
        _record_transaction(db, reg, D("0"), "free", description=_desc, student_count=_sc, observer_count=_oc)
        db.commit()
        db.refresh(reg)
        try:
            student_names = [ers.student.name for ers in reg.attending_students]
            observer_names = [ero.observer.name for ero in reg.attending_observers]
            ev_date_str = event.event_date.strftime("%B %d, %Y") if event.event_date else ""
            email_service.send_registration_confirmation(
                to_email=user.email,
                studio_name=user.studio_name,
                event_title=event.title,
                event_date=ev_date_str,
                student_names=student_names,
                amount_paid=0,
                observer_names=observer_names,
                observer_amount=0,
            )
        except Exception:
            logger.exception("Failed to send confirmation to %s", user.email)
        return schemas.CheckoutSessionOut(checkout_url="", session_id="free")

    user_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email
    line_items = []
    if is_pending_payment_mode:
        # Charge only for the admin-added pending dancers/observers.
        if dancer_total > 0:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event.title} — {len(pending_dancer_ids)} added dancer(s)"},
                    "unit_amount": int(price_per_student * 100),
                },
                "quantity": len(pending_dancer_ids),
            })
        if observer_total > 0:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event.title} — {len(pending_observer_ids)} added observer(s)"},
                    "unit_amount": int(observer_price * 100),
                },
                "quantity": len(pending_observer_ids),
            })
    else:
        if dancer_total > 0:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event.title} — {len(body.student_ids)} dancer(s)"},
                    "unit_amount": int(price_per_student * 100),
                },
                "quantity": len(body.student_ids),
            })
        if observer_total > 0:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event.title} — {len(body.observer_ids)} observer(s)"},
                    "unit_amount": int(observer_price * 100),
                },
                "quantity": len(body.observer_ids),
            })
    _meta_sc = len(pending_dancer_ids) if is_pending_payment_mode else len(body.student_ids)
    _meta_oc = len(pending_observer_ids) if is_pending_payment_mode else len(body.observer_ids)
    meta = {
        "registration_id": str(reg.id),
        "event_id": str(event_id),
        "user_name": user_name,
        "observer_ids": ",".join(str(i) for i in body.observer_ids),
        "is_pending_payment": "true" if is_pending_payment_mode else "false",
        "student_count": str(_meta_sc),
        "observer_count": str(_meta_oc),
    }
    return_url = (
        f"{FRONTEND_URL}/events.html"
        f"?payment=success&event_id={event_id}&session_id={{CHECKOUT_SESSION_ID}}"
    )
    try:
        if embedded:
            session = stripe.checkout.Session.create(
                ui_mode="embedded",
                line_items=line_items,
                mode="payment",
                customer_email=user.email,
                return_url=return_url,
                metadata=meta,
            )
        else:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=line_items,
                mode="payment",
                customer_email=user.email,
                success_url=return_url,
                cancel_url=f"{FRONTEND_URL}/events.html?payment=cancelled&event_id={event_id}",
                metadata=meta,
            )
    except stripe.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

    reg.payment_status = "pending"
    reg.stripe_session_id = session.id
    db.commit()
    if embedded:
        return schemas.CheckoutSessionOut(checkout_url="", session_id=session.id, client_secret=session.client_secret)
    return schemas.CheckoutSessionOut(checkout_url=session.url, session_id=session.id)


@app.post("/events/{event_id}/register/verify-payment", response_model=schemas.EventRegistrationOut)
def verify_payment(
    event_id: int,
    body: schemas.VerifyPaymentRequest,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(authorization, db)
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")

    # Always retrieve the Stripe session to get metadata for add-students flows
    try:
        session = stripe.checkout.Session.retrieve(body.session_id)
    except stripe.StripeError as e:
        # If reg is already finalized (e.g. webhook beat us here), just return current state
        if reg.is_finalized:
            return _build_reg_out(reg)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

    if session.payment_status == "paid":
        from decimal import Decimal as D
        meta = session.get("metadata", {}) if hasattr(session, "get") else {}
        new_student_ids_raw = meta.get("new_student_ids", "")
        observer_ids_raw = meta.get("observer_ids", "")
        is_add_students_flow = bool(new_student_ids_raw or observer_ids_raw)

        if is_add_students_flow:
            # Add-students flow: reg already finalized, add new students from metadata
            existing_ids = {ers.student_id for ers in reg.attending_students}
            new_sids_added = []
            for sid_str in (new_student_ids_raw.split(",") if new_student_ids_raw else []):
                try:
                    sid = int(sid_str.strip())
                    if sid not in existing_ids:
                        s = db.query(models.Student).filter(
                            models.Student.id == sid, models.Student.user_id == user.id
                        ).first()
                        if s:
                            db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))
                            existing_ids.add(sid)
                            new_sids_added.append(sid)
                except ValueError:
                    pass
            existing_obs_ids = {ero.observer_id for ero in reg.attending_observers}
            new_oids_added = []
            for oid_str in (observer_ids_raw.split(",") if observer_ids_raw else []):
                try:
                    oid = int(oid_str.strip())
                    if oid not in existing_obs_ids:
                        o = db.query(models.Observer).filter(
                            models.Observer.id == oid, models.Observer.user_id == user.id
                        ).first()
                        if o:
                            db.add(models.EventRegistrationObserver(registration_id=reg.id, observer_id=oid))
                            existing_obs_ids.add(oid)
                            new_oids_added.append(oid)
                except ValueError:
                    pass
            # Update amount_paid to include new payment
            add_amount = D(str(session.amount_total)) / 100
            reg.amount_paid = (reg.amount_paid or D("0")) + add_amount
            reg.finalized_at = datetime.now(timezone.utc)
            # Record immutable transaction line for this add-students payment
            _sc = len(new_sids_added)
            _oc = len(new_oids_added)
            _desc = f"Added {_sc} dancer(s)" if _sc else ""
            if _oc:
                _desc += f"{', ' if _desc else 'Added '}{_oc} observer(s)"
            _record_transaction(db, reg, add_amount, "paid",
                                stripe_session_id=body.session_id,
                                description=_desc or "Added dancers/observers",
                                student_count=_sc, observer_count=_oc)
            db.commit()
            db.refresh(reg)
        else:
            # Initial registration flow or pending-payment flow (admin added unpaid dancers):
            # finalize and accumulate amount_paid so prior partial payments are preserved.
            is_pending_mode = meta.get("is_pending_payment") == "true"
            reg.is_finalized = True
            reg.payment_status = "paid"
            amount = D(str(session.amount_total)) / 100
            reg.amount_paid = (reg.amount_paid or D("0")) + amount
            reg.finalized_at = datetime.now(timezone.utc)
            if is_pending_mode:
                reg.pending_student_ids = None   # payment received — clear pending
            # Record immutable transaction line
            _sc = int(meta.get("student_count", "0") or "0")
            _oc = int(meta.get("observer_count", "0") or "0")
            _desc = f"Added {_sc} dancer(s)" if is_pending_mode else f"{_sc} dancer(s)"
            if _oc:
                _desc += f", {_oc} observer(s)"
            _record_transaction(db, reg, amount, "paid",
                                stripe_session_id=body.session_id,
                                description=_desc, student_count=_sc, observer_count=_oc)
            db.commit()
            db.refresh(reg)
            try:
                event = db.query(models.Event).filter(models.Event.id == event_id).first()
                student_names = [ers.student.name for ers in reg.attending_students]
                observer_names = [ero.observer.name for ero in reg.attending_observers]
                ev_date_str = event.event_date.strftime("%B %d, %Y") if event and event.event_date else ""
                observer_price_val = float(event.observer_price) if event and event.observer_price else 0
                observer_amount = observer_price_val * len(observer_names)
                email_service.send_registration_confirmation(
                    to_email=user.email,
                    studio_name=user.studio_name,
                    event_title=event.title if event else "Event",
                    event_date=ev_date_str,
                    student_names=student_names,
                    amount_paid=float(amount),
                    observer_names=observer_names,
                    observer_amount=observer_amount,
                )
            except Exception:
                logger.exception("Failed to send confirmation to %s", user.email)

    return _build_reg_out(reg)


@app.post("/events/{event_id}/register/add-students-save", response_model=schemas.EventRegistrationOut, status_code=200)
def add_students_save(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Add new dancers/observers to a registration without requiring payment now."""
    user = get_current_user(authorization, db)
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()

    if not reg:
        reg = models.EventRegistration(event_id=event_id, user_id=user.id)
        db.add(reg)
        db.flush()

    existing_ids = {ers.student_id for ers in reg.attending_students}
    for sid in body.student_ids:
        if sid in existing_ids:
            continue
        s = db.query(models.Student).filter(
            models.Student.id == sid, models.Student.user_id == user.id
        ).first()
        if s:
            db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))

    existing_obs_ids = {ero.observer_id for ero in reg.attending_observers}
    for oid in body.observer_ids:
        if oid in existing_obs_ids:
            continue
        o = db.query(models.Observer).filter(
            models.Observer.id == oid, models.Observer.user_id == user.id
        ).first()
        if o:
            db.add(models.EventRegistrationObserver(registration_id=reg.id, observer_id=oid))

    db.commit()
    db.refresh(reg)
    return _build_reg_out(reg)


@app.post("/events/{event_id}/register/add-students", response_model=schemas.CheckoutSessionOut, status_code=201)
def add_students_to_finalized(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    embedded: bool = Query(False, description="Use Stripe Embedded Checkout"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    from decimal import Decimal as D
    user = get_current_user(authorization, db)
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()
    if not reg or not reg.is_finalized:
        raise HTTPException(status_code=400, detail="No finalized registration found for this event.")
    if not body.student_ids and not body.observer_ids:
        raise HTTPException(status_code=400, detail="Select at least one new student or observer.")

    existing_ids = {ers.student_id for ers in reg.attending_students}
    new_ids = [sid for sid in body.student_ids if sid not in existing_ids]

    existing_obs_ids = {ero.observer_id for ero in reg.attending_observers}
    new_obs_ids = [oid for oid in body.observer_ids if oid not in existing_obs_ids]

    if not new_ids and not new_obs_ids:
        raise HTTPException(status_code=400, detail="All selected students/observers are already registered.")

    if event.max_students is not None and new_ids:
        current_total = sum(len(r.attending_students) for r in event.registrations)
        available = event.max_students - current_total
        if len(new_ids) > available:
            raise HTTPException(status_code=400, detail=f"Only {available} spot(s) remaining.")

    price_per_student, _ = _effective_price(event)
    observer_price = D(str(event.observer_price)) if event.observer_price else D("0")

    dancer_total = price_per_student * len(new_ids)
    observer_total = observer_price * len(new_obs_ids)
    grand_total = dancer_total + observer_total
    is_free = grand_total == D("0")

    if is_free:
        for sid in new_ids:
            s = db.query(models.Student).filter(
                models.Student.id == sid, models.Student.user_id == user.id
            ).first()
            if s:
                db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))
        for oid in new_obs_ids:
            o = db.query(models.Observer).filter(
                models.Observer.id == oid, models.Observer.user_id == user.id
            ).first()
            if o:
                db.add(models.EventRegistrationObserver(registration_id=reg.id, observer_id=oid))
        db.commit()
        return schemas.CheckoutSessionOut(checkout_url="", session_id="free")

    user_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email
    line_items = []
    if dancer_total > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"{event.title} — {len(new_ids)} additional dancer(s)"},
                "unit_amount": int(price_per_student * 100),
            },
            "quantity": len(new_ids),
        })
    if observer_total > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"{event.title} — {len(new_obs_ids)} additional observer(s)"},
                "unit_amount": int(observer_price * 100),
            },
            "quantity": len(new_obs_ids),
        })
    meta2 = {
        "registration_id": str(reg.id),
        "event_id": str(event_id),
        "new_student_ids": ",".join(str(i) for i in new_ids),
        "observer_ids": ",".join(str(i) for i in new_obs_ids),
        "user_name": user_name,
    }
    return_url2 = (
        f"{FRONTEND_URL}/events.html"
        f"?payment=success&event_id={event_id}&session_id={{CHECKOUT_SESSION_ID}}"
    )
    try:
        if embedded:
            session = stripe.checkout.Session.create(
                ui_mode="embedded",
                line_items=line_items,
                mode="payment",
                customer_email=user.email,
                return_url=return_url2,
                metadata=meta2,
            )
        else:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=line_items,
                mode="payment",
                customer_email=user.email,
                success_url=return_url2 + f"&new_students={','.join(str(i) for i in new_ids)}",
                cancel_url=f"{FRONTEND_URL}/events.html?payment=cancelled&event_id={event_id}",
                metadata=meta2,
            )
    except stripe.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

    reg.stripe_session_id = session.id
    db.commit()
    if embedded:
        return schemas.CheckoutSessionOut(checkout_url="", session_id=session.id, client_secret=session.client_secret)
    return schemas.CheckoutSessionOut(checkout_url=session.url, session_id=session.id)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.")
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook error.")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        reg_id = int(session.get("metadata", {}).get("registration_id", 0))
        new_student_ids_raw = session.get("metadata", {}).get("new_student_ids", "")
        observer_ids_raw = session.get("metadata", {}).get("observer_ids", "")
        reg = db.query(models.EventRegistration).filter(models.EventRegistration.id == reg_id).first()
        if reg:
            from decimal import Decimal as D
            if new_student_ids_raw:
                for sid_str in new_student_ids_raw.split(","):
                    try:
                        sid = int(sid_str.strip())
                        already = db.query(models.EventRegistrationStudent).filter(
                            models.EventRegistrationStudent.registration_id == reg.id,
                            models.EventRegistrationStudent.student_id == sid,
                        ).first()
                        if not already:
                            db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))
                    except ValueError:
                        pass
            if observer_ids_raw:
                for oid_str in observer_ids_raw.split(","):
                    try:
                        oid = int(oid_str.strip())
                        already = db.query(models.EventRegistrationObserver).filter(
                            models.EventRegistrationObserver.registration_id == reg.id,
                            models.EventRegistrationObserver.observer_id == oid,
                        ).first()
                        if not already:
                            db.add(models.EventRegistrationObserver(registration_id=reg.id, observer_id=oid))
                    except ValueError:
                        pass
            amount_total = session.get("amount_total") or 0
            paid_amount = D(str(amount_total)) / 100
            prev_paid = reg.amount_paid or D("0")
            new_amount = prev_paid + paid_amount
            meta_wh = session.get("metadata", {})
            is_pending_mode = meta_wh.get("is_pending_payment") == "true"
            reg.is_finalized = True
            reg.payment_status = "paid"
            reg.amount_paid = new_amount
            reg.finalized_at = datetime.now(timezone.utc)
            if is_pending_mode:
                reg.pending_student_ids = None   # payment received — clear pending
            # Record immutable transaction — _record_transaction deduplicates if
            # verify-payment already created a row for this session.
            _wh_sc = int(meta_wh.get("student_count", "0") or "0")
            _wh_oc = int(meta_wh.get("observer_count", "0") or "0")
            _wh_is_add = bool(meta_wh.get("new_student_ids", ""))
            if _wh_is_add:
                _wh_desc = f"Added {_wh_sc} dancer(s)" if _wh_sc else ""
                if _wh_oc:
                    _wh_desc += f"{', ' if _wh_desc else 'Added '}{_wh_oc} observer(s)"
            elif is_pending_mode:
                _wh_desc = f"Added {_wh_sc} dancer(s)"
                if _wh_oc:
                    _wh_desc += f", {_wh_oc} observer(s)"
            else:
                _wh_desc = f"{_wh_sc} dancer(s)"
                if _wh_oc:
                    _wh_desc += f", {_wh_oc} observer(s)"
            _record_transaction(db, reg, paid_amount, "paid",
                                stripe_session_id=session.get("id"),
                                description=_wh_desc or "Payment",
                                student_count=_wh_sc, observer_count=_wh_oc)
            db.commit()
            db.refresh(reg)
            try:
                evt = reg.event
                user = reg.user
                student_names = [ers.student.name for ers in reg.attending_students]
                observer_names = [ero.observer.name for ero in reg.attending_observers]
                ev_date_str = evt.event_date.strftime("%B %d, %Y") if evt and evt.event_date else ""
                observer_price_val = float(evt.observer_price) if evt and evt.observer_price else 0
                observer_amount = observer_price_val * len(observer_names)
                email_service.send_registration_confirmation(
                    to_email=user.email,
                    studio_name=user.studio_name,
                    event_title=evt.title if evt else "Event",
                    event_date=ev_date_str,
                    student_names=student_names,
                    amount_paid=float(new_amount),
                    observer_names=observer_names,
                    observer_amount=observer_amount,
                )
            except Exception:
                logger.exception("Failed to send confirmation in webhook for reg %s", reg_id)

    return {"ok": True}


# ── Payment history ───────────────────────────────────────────────────────────

@app.get("/events/my-payments")
def get_my_payments(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return one row per Transaction (immutable ledger).
    For old registrations that pre-date the Transaction table, fall back to
    the EventRegistration record so history is not lost.
    """
    user = get_current_user(authorization, db)
    result = []

    # Primary: one row per Transaction record
    txs = db.query(models.Transaction).filter(
        models.Transaction.user_id == user.id,
    ).order_by(models.Transaction.created_at.desc()).all()
    reg_ids_with_txs = {tx.registration_id for tx in txs}
    for tx in txs:
        ev = tx.event
        result.append({
            "transaction_id": tx.id,
            "registration_id": tx.registration_id,
            "event_id": tx.event_id,
            "event_title": ev.title if ev else "—",
            "event_date": ev.event_date.isoformat() if ev and ev.event_date else None,
            "student_count": tx.student_count or 0,
            "observer_count": tx.observer_count or 0,
            "description": tx.description,
            "amount_paid": float(tx.amount),
            "payment_status": tx.payment_status,
            "finalized_at": tx.created_at.isoformat(),
        })

    # Fallback: old registrations with no Transaction rows
    old_regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.user_id == user.id,
        models.EventRegistration.payment_status.in_(["paid", "free", "admin-paid"]),
        ~models.EventRegistration.id.in_(reg_ids_with_txs) if reg_ids_with_txs else True,
    ).order_by(models.EventRegistration.finalized_at.desc()).all()
    for reg in old_regs:
        result.append({
            "transaction_id": None,
            "registration_id": reg.id,
            "event_id": reg.event_id,
            "event_title": reg.event.title if reg.event else "—",
            "event_date": reg.event.event_date.isoformat() if reg.event and reg.event.event_date else None,
            "student_count": len(reg.attending_students),
            "observer_count": len(reg.attending_observers),
            "description": None,
            "amount_paid": float(reg.amount_paid) if reg.amount_paid is not None else 0,
            "payment_status": reg.payment_status,
            "finalized_at": reg.finalized_at.isoformat() if reg.finalized_at else None,
        })

    result.sort(key=lambda x: x["finalized_at"] or "", reverse=True)
    return result


@app.get("/admin/payments")
def admin_list_payments(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return one row per Transaction (immutable ledger).
    For old registrations that pre-date the Transaction table, fall back to
    the EventRegistration record so history is not lost.
    """
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    result = []

    # Primary: one row per Transaction record
    txs = db.query(models.Transaction).order_by(models.Transaction.created_at.desc()).all()
    reg_ids_with_txs = {tx.registration_id for tx in txs}
    for tx in txs:
        ev = tx.event
        u = tx.user
        user_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email if u else "—"
        result.append({
            "transaction_id": tx.id,
            "registration_id": tx.registration_id,
            "event_id": tx.event_id,
            "event_title": ev.title if ev else "—",
            "event_date": ev.event_date.isoformat() if ev and ev.event_date else None,
            "user_id": tx.user_id,
            "user_name": user_name,
            "studio_name": u.studio_name if u else None,
            "student_count": tx.student_count or 0,
            "observer_count": tx.observer_count or 0,
            "description": tx.description,
            "amount_paid": float(tx.amount),
            "payment_status": tx.payment_status,
            "finalized_at": tx.created_at.isoformat(),
        })

    # Fallback: old registrations with no Transaction rows
    old_regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.payment_status.in_(["paid", "free", "admin-paid"]),
        ~models.EventRegistration.id.in_(reg_ids_with_txs) if reg_ids_with_txs else True,
    ).order_by(models.EventRegistration.finalized_at.desc()).all()
    for reg in old_regs:
        u = reg.user
        user_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email if u else "—"
        result.append({
            "transaction_id": None,
            "registration_id": reg.id,
            "event_id": reg.event_id,
            "event_title": reg.event.title if reg.event else "—",
            "event_date": reg.event.event_date.isoformat() if reg.event and reg.event.event_date else None,
            "user_id": reg.user_id,
            "user_name": user_name,
            "studio_name": u.studio_name if u else None,
            "student_count": len(reg.attending_students),
            "observer_count": len(reg.attending_observers),
            "description": None,
            "amount_paid": float(reg.amount_paid) if reg.amount_paid is not None else 0,
            "payment_status": reg.payment_status,
            "finalized_at": reg.finalized_at.isoformat() if reg.finalized_at else None,
        })

    result.sort(key=lambda x: x["finalized_at"] or "", reverse=True)
    return result


# ── Admin event registrations ─────────────────────────────────────────────────

@app.get("/admin/events/{event_id}/registrations")
def admin_list_registrations(
    event_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id
    ).all()
    result = []
    for reg in regs:
        students = [{"id": ers.student_id, "name": ers.student.name} for ers in reg.attending_students]
        observers = [{"id": ero.observer_id, "name": ero.observer.name} for ero in reg.attending_observers]
        name = f"{reg.user.first_name or ''} {reg.user.last_name or ''}".strip() or reg.user.email
        result.append({
            "id": reg.id,
            "user_id": reg.user_id,
            "user_name": name,
            "studio_name": reg.user.studio_name,
            "students": students,
            "observers": observers,
            "created_at": reg.created_at.isoformat() if reg.created_at else None,
            "is_finalized": reg.is_finalized,
            "payment_status": reg.payment_status,
            "amount_paid": float(reg.amount_paid) if reg.amount_paid is not None else None,
            "finalized_at": reg.finalized_at.isoformat() if reg.finalized_at else None,
        })
    return result


@app.post("/admin/events/{event_id}/registrations/for-user", status_code=201)
def admin_create_registration(
    event_id: int,
    body: dict,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")

    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    user_id = body.get("user_id")
    student_ids = body.get("student_ids", [])

    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found.")

    if event.max_students is not None and student_ids:
        existing_reg = db.query(models.EventRegistration).filter(
            models.EventRegistration.event_id == event_id,
            models.EventRegistration.user_id == user_id,
        ).first()
        current_total = sum(len(reg.attending_students) for reg in event.registrations)
        if existing_reg:
            current_total -= len(existing_reg.attending_students)
        available = event.max_students - current_total
        if len(student_ids) > available:
            raise HTTPException(status_code=400, detail=f"Only {available} spot(s) remaining.")

    existing = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user_id,
    ).first()

    if existing:
        db.query(models.EventRegistrationStudent).filter(
            models.EventRegistrationStudent.registration_id == existing.id
        ).delete()
        for sid in student_ids:
            s = db.query(models.Student).filter(
                models.Student.id == sid, models.Student.user_id == user_id
            ).first()
            if s:
                db.add(models.EventRegistrationStudent(registration_id=existing.id, student_id=sid))
        db.commit()
        reg = existing
    else:
        reg = models.EventRegistration(event_id=event_id, user_id=user_id)
        db.add(reg)
        db.flush()
        for sid in student_ids:
            s = db.query(models.Student).filter(
                models.Student.id == sid, models.Student.user_id == user_id
            ).first()
            if s:
                db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))
        db.commit()
        db.refresh(reg)

    students = [{"id": ers.student_id, "name": ers.student.name} for ers in reg.attending_students]
    name = f"{target_user.first_name or ''} {target_user.last_name or ''}".strip() or target_user.email
    return {
        "id": reg.id,
        "user_id": user_id,
        "user_name": name,
        "studio_name": target_user.studio_name,
        "students": students,
        "created_at": reg.created_at.isoformat() if reg.created_at else None,
    }


@app.delete("/admin/events/{event_id}/registrations/{reg_id}", response_model=schemas.MessageOut)
def admin_remove_registration(
    event_id: int,
    reg_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.id == reg_id,
        models.EventRegistration.event_id == event_id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    db.delete(reg)
    db.commit()
    return {"message": "Registration removed."}


@app.delete("/admin/events/{event_id}/registrations/{reg_id}/students/{student_id}", response_model=schemas.MessageOut)
def admin_remove_reg_student(
    event_id: int,
    reg_id: int,
    student_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.id == reg_id,
        models.EventRegistration.event_id == event_id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    ers = db.query(models.EventRegistrationStudent).filter(
        models.EventRegistrationStudent.registration_id == reg_id,
        models.EventRegistrationStudent.student_id == student_id,
    ).first()
    if not ers:
        raise HTTPException(status_code=404, detail="Student not in registration.")
    db.delete(ers)
    # If this dancer was tracked as an unpaid pending item, remove them from the list.
    # If no pending items remain, clear the field so the 'Pay Outstanding Balance'
    # button and badge disappear from the user portal.
    pending = [x for x in (reg.pending_student_ids or "").split(",") if x.strip()]
    pending = [x for x in pending if x != str(student_id)]
    reg.pending_student_ids = ",".join(pending) if pending else None
    db.commit()
    return {"message": "Student removed from registration."}


@app.post("/admin/events/{event_id}/registrations/{reg_id}/students/{student_id}", response_model=schemas.MessageOut)
def admin_add_reg_student(
    event_id: int,
    reg_id: int,
    student_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.id == reg_id,
        models.EventRegistration.event_id == event_id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    student = db.query(models.Student).filter(
        models.Student.id == student_id,
        models.Student.user_id == reg.user_id,
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found for this user.")
    existing = db.query(models.EventRegistrationStudent).filter(
        models.EventRegistrationStudent.registration_id == reg_id,
        models.EventRegistrationStudent.student_id == student_id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Student already in registration.")
    db.add(models.EventRegistrationStudent(registration_id=reg_id, student_id=student_id))
    if reg.is_finalized:
        # Registration was already paid/finalized — track this specific dancer as
        # pending payment so only they are charged, not the whole registration.
        existing_pending = [x for x in (reg.pending_student_ids or "").split(",") if x.strip()]
        if str(student_id) not in existing_pending:
            existing_pending.append(str(student_id))
        reg.pending_student_ids = ",".join(existing_pending)
        # Leave is_finalized = True so existing paid dancers stay locked in the UI.
        # admin-finalize (called if admin selects "Paid") will clear pending_student_ids.
    # If reg is not yet finalized, the whole registration is already pending —
    # no need to track individually.
    db.commit()
    return {"message": "Student added to registration."}


@app.post("/admin/events/{event_id}/registrations/{reg_id}/admin-finalize", response_model=schemas.MessageOut)
def admin_finalize_registration(
    event_id: int,
    reg_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Admin manually marks a registration as paid (cash / offline payment).

    Sets payment_status = 'admin-paid', is_finalized = True, and records
    finalized_at so the frontend can distinguish admin approvals from
    Stripe payments.
    """
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.id == reg_id,
        models.EventRegistration.event_id == event_id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    from decimal import Decimal as D
    _sc = len(reg.attending_students)
    _oc = len(reg.attending_observers)
    _desc = f"{_sc} dancer(s)"
    if _oc:
        _desc += f", {_oc} observer(s)"
    reg.is_finalized        = True
    reg.payment_status      = "admin-paid"
    reg.finalized_at        = datetime.now(timezone.utc)
    reg.pending_student_ids = None   # Admin confirmed payment — clear any pending
    _record_transaction(db, reg, D("0"), "admin-paid",
                        description=_desc,
                        student_count=_sc, observer_count=_oc)
    db.commit()
    return {"message": "Registration marked as paid by admin."}


# ── Admin student management ──────────────────────────────────────────────────

@app.get("/admin/users/{user_id}/students", response_model=list[schemas.StudentOut])
def admin_list_students(
    user_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return db.query(models.Student).filter(models.Student.user_id == user_id).order_by(models.Student.name).all()


@app.post("/admin/users/{user_id}/students", response_model=schemas.StudentOut, status_code=201)
def admin_create_student(
    user_id: int,
    body: schemas.StudentCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    student = models.Student(user_id=user_id, **body.model_dump())
    db.add(student)
    db.commit()
    db.refresh(student)
    return student


@app.patch("/admin/users/{user_id}/students/{student_id}", response_model=schemas.StudentOut)
def admin_update_student(
    user_id: int,
    student_id: int,
    body: schemas.StudentUpdate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    student = db.query(models.Student).filter(
        models.Student.id == student_id,
        models.Student.user_id == user_id,
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(student, field, value)
    db.commit()
    db.refresh(student)
    return student


@app.delete("/admin/users/{user_id}/students/{student_id}", response_model=schemas.MessageOut)
def admin_delete_student(
    user_id: int,
    student_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    student = db.query(models.Student).filter(
        models.Student.id == student_id,
        models.Student.user_id == user_id,
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")
    db.delete(student)
    db.commit()
    return {"message": "Student deleted."}


# ── Admin observer management ─────────────────────────────────────────────────

@app.get("/admin/users/{user_id}/observers", response_model=list[schemas.ObserverOut])
def admin_list_observers(user_id: int, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return db.query(models.Observer).filter(models.Observer.user_id == user_id).order_by(models.Observer.name).all()


@app.post("/admin/users/{user_id}/observers", response_model=schemas.ObserverOut, status_code=201)
def admin_create_observer(user_id: int, body: schemas.ObserverCreate, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    target = db.query(models.User).filter(models.User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    obs = models.Observer(user_id=user_id, name=body.name.strip())
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs


@app.delete("/admin/users/{user_id}/observers/{observer_id}", response_model=schemas.MessageOut)
def admin_delete_observer(user_id: int, observer_id: int, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    obs = db.query(models.Observer).filter(models.Observer.id == observer_id, models.Observer.user_id == user_id).first()
    if not obs:
        raise HTTPException(status_code=404, detail="Observer not found.")
    db.delete(obs)
    db.commit()
    return {"message": "Observer deleted."}


@app.post("/admin/events/{event_id}/registrations/{reg_id}/observers/{observer_id}")
def admin_add_reg_observer(
    event_id: int,
    reg_id: int,
    observer_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.id == reg_id,
        models.EventRegistration.event_id == event_id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    obs = db.query(models.Observer).filter(
        models.Observer.id == observer_id,
        models.Observer.user_id == reg.user_id,
    ).first()
    if not obs:
        raise HTTPException(status_code=404, detail="Observer not found for this user.")
    existing = db.query(models.EventRegistrationObserver).filter(
        models.EventRegistrationObserver.registration_id == reg_id,
        models.EventRegistrationObserver.observer_id == observer_id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Observer already in registration.")
    db.add(models.EventRegistrationObserver(registration_id=reg_id, observer_id=observer_id))

    pending = [x for x in (reg.pending_student_ids or "").split(",") if x.strip()]
    if reg.is_finalized:
        obs_key = f"o{observer_id}"
        if obs_key not in pending:
            pending.append(obs_key)

    # Auto-add the observer's linked dancer if not already in the registration.
    linked_student_id = None
    linked_student_name = None
    if obs.linked_student_id:
        already_in_reg = db.query(models.EventRegistrationStudent).filter(
            models.EventRegistrationStudent.registration_id == reg_id,
            models.EventRegistrationStudent.student_id == obs.linked_student_id,
        ).first()
        if not already_in_reg:
            linked = db.query(models.Student).filter(
                models.Student.id == obs.linked_student_id,
            ).first()
            if linked:
                db.add(models.EventRegistrationStudent(
                    registration_id=reg_id, student_id=obs.linked_student_id
                ))
                if reg.is_finalized:
                    sid_str = str(obs.linked_student_id)
                    if sid_str not in pending:
                        pending.append(sid_str)
                linked_student_id = linked.id
                linked_student_name = linked.name

    reg.pending_student_ids = ",".join(pending) if pending else None
    db.commit()
    return {
        "message": "Observer added to registration.",
        "linked_student_id": linked_student_id,
        "linked_student_name": linked_student_name,
    }


@app.delete("/admin/events/{event_id}/registrations/{reg_id}/observers/{observer_id}", response_model=schemas.MessageOut)
def admin_remove_reg_observer(
    event_id: int,
    reg_id: int,
    observer_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.id == reg_id,
        models.EventRegistration.event_id == event_id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    ero = db.query(models.EventRegistrationObserver).filter(
        models.EventRegistrationObserver.registration_id == reg_id,
        models.EventRegistrationObserver.observer_id == observer_id,
    ).first()
    if not ero:
        raise HTTPException(status_code=404, detail="Observer not in registration.")
    db.delete(ero)
    # If this observer was tracked as an unpaid pending item, remove them from the list.
    # If no pending items remain, clear the field so the 'Pay Outstanding Balance'
    # button and badge disappear from the user portal.
    pending = [x for x in (reg.pending_student_ids or "").split(",") if x.strip()]
    pending = [x for x in pending if x != f"o{observer_id}"]
    reg.pending_student_ids = ",".join(pending) if pending else None
    db.commit()
    return {"message": "Observer removed from registration."}
