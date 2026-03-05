import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://goldfish-app-fuu3t.ondigitalocean.app")

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


@app.patch("/admin/users/{user_id}", response_model=schemas.UserOut)
def admin_update_user(
    user_id: int,
    body: schemas.UserUpdate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Update a user's profile fields. Admin only."""
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    target = db.query(models.User).filter(models.User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Check email uniqueness if changing email
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
    """Return all students belonging to the current user."""
    user = get_current_user(authorization, db)
    return db.query(models.Student).filter(models.Student.user_id == user.id).order_by(models.Student.name).all()


@app.post("/students", response_model=schemas.StudentOut, status_code=201)
def create_student(
    body: schemas.StudentCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Add a student to the current user's account."""
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
    """Update a student. Must belong to the current user."""
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
    """Delete a student. Must belong to the current user."""
    user = get_current_user(authorization, db)
    student = db.query(models.Student).filter(
        models.Student.id == student_id,
        models.Student.user_id == user.id,
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    # Block deletion if dancer is in a finalized registration for an upcoming event
    from datetime import timezone as tz
    upcoming_finalized = (
        db.query(models.EventRegistrationStudent)
        .join(models.EventRegistration,
              models.EventRegistrationStudent.registration_id == models.EventRegistration.id)
        .join(models.Event,
              models.EventRegistration.event_id == models.Event.id)
        .filter(
            models.EventRegistrationStudent.student_id == student_id,
            models.EventRegistration.is_finalized.is_(True),
            models.Event.event_date >= datetime.now(tz.utc),
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

    db.delete(student)
    db.commit()
    return {"message": "Student deleted."}


# ── Event routes (public read, admin write) ───────────────────────────────────

def _build_event_out(event: models.Event) -> schemas.EventOut:
    """Build an EventOut including live registered_count and total_revenue."""
    from decimal import Decimal as D
    now = datetime.now(timezone.utc)
    count = event.registered_count

    # Determine effective price (early-bird vs regular vs none)
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
        created_at=event.created_at,
    )


@app.get("/events", response_model=list[schemas.EventOut])
def list_events(db: Session = Depends(get_db)):
    """Return all events ordered by date ascending, including capacity & revenue data."""
    events = db.query(models.Event).order_by(models.Event.event_date).all()
    return [_build_event_out(e) for e in events]


@app.post("/admin/events", response_model=schemas.EventOut, status_code=201)
def create_event(
    body: schemas.EventCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Create a new event. Admin only."""
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
    """Update an event. Admin only."""
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


@app.get("/events/my-registrations")
def get_my_registrations(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return a map of event_id → {registration_id, student_ids} for the current user."""
    user = get_current_user(authorization, db)
    regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.user_id == user.id
    ).all()
    result = {}
    for reg in regs:
        student_ids = [ers.student_id for ers in reg.attending_students]
        result[str(reg.event_id)] = {
            "registration_id": reg.id,
            "student_ids": student_ids,
            "is_finalized": reg.is_finalized,
            "payment_status": reg.payment_status,
            "amount_paid": float(reg.amount_paid) if reg.amount_paid is not None else None,
            "stripe_session_id": reg.stripe_session_id,
        }
    return result


@app.post("/events/{event_id}/register", response_model=schemas.EventRegistrationOut, status_code=201)
def register_for_event(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Register current user for an event, with optional attending students."""
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

    # Capacity check
    if event.max_students is not None and body.student_ids:
        # Count all students registered for this event excluding the current user's existing registration
        current_total = sum(len(reg.attending_students) for reg in event.registrations)
        if existing:
            current_total -= len(existing.attending_students)
        available = event.max_students - current_total
        if len(body.student_ids) > available:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough capacity. Only {available} spot(s) remaining for this event."
            )

    if existing:
        # Update attending students
        db.query(models.EventRegistrationStudent).filter(
            models.EventRegistrationStudent.registration_id == existing.id
        ).delete()
        for sid in body.student_ids:
            s = db.query(models.Student).filter(
                models.Student.id == sid, models.Student.user_id == user.id
            ).first()
            if s:
                db.add(models.EventRegistrationStudent(registration_id=existing.id, student_id=sid))
        db.commit()
        db.refresh(existing)
        return schemas.EventRegistrationOut(
            id=existing.id, event_id=existing.event_id, user_id=existing.user_id,
            student_ids=[ers.student_id for ers in existing.attending_students],
            created_at=existing.created_at,
        )

    reg = models.EventRegistration(event_id=event_id, user_id=user.id)
    db.add(reg)
    db.flush()
    for sid in body.student_ids:
        s = db.query(models.Student).filter(
            models.Student.id == sid, models.Student.user_id == user.id
        ).first()
        if s:
            db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))
    db.commit()
    db.refresh(reg)
    return schemas.EventRegistrationOut(
        id=reg.id, event_id=reg.event_id, user_id=reg.user_id,
        student_ids=[ers.student_id for ers in reg.attending_students],
        created_at=reg.created_at,
    )


@app.delete("/events/{event_id}/register", response_model=schemas.MessageOut)
def unregister_from_event(
    event_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Unregister current user from an event."""
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


def _build_reg_out(reg: models.EventRegistration) -> schemas.EventRegistrationOut:
    return schemas.EventRegistrationOut(
        id=reg.id,
        event_id=reg.event_id,
        user_id=reg.user_id,
        student_ids=[ers.student_id for ers in reg.attending_students],
        created_at=reg.created_at,
        is_finalized=reg.is_finalized,
        payment_status=reg.payment_status,
        amount_paid=reg.amount_paid,
    )


@app.post("/events/{event_id}/register/checkout", response_model=schemas.CheckoutSessionOut, status_code=201)
def create_checkout(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Create or update an unfinalized registration and return a Stripe Checkout URL."""
    user = get_current_user(authorization, db)
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")
    if not body.student_ids:
        raise HTTPException(status_code=400, detail="Select at least one student.")

    existing = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()
    if existing and existing.is_finalized:
        raise HTTPException(status_code=400, detail="Registration already finalized. Use Add Dancers.")

    # Capacity check
    if event.max_students is not None:
        current_total = sum(len(r.attending_students) for r in event.registrations)
        if existing:
            current_total -= len(existing.attending_students)
        available = event.max_students - current_total
        if len(body.student_ids) > available:
            raise HTTPException(status_code=400, detail=f"Only {available} spot(s) remaining.")

    price_per_student, is_free = _effective_price(event)

    # Upsert registration + students
    if existing:
        db.query(models.EventRegistrationStudent).filter(
            models.EventRegistrationStudent.registration_id == existing.id
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

    if is_free:
        reg.is_finalized = True
        reg.payment_status = "free"
        reg.amount_paid = 0
        reg.finalized_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(reg)
        # Send confirmation email
        try:
            student_names = [ers.student.name for ers in reg.attending_students]
            ev_date_str = event.event_date.strftime("%B %d, %Y") if event.event_date else ""
            email_service.send_registration_confirmation(
                to_email=user.email,
                studio_name=user.studio_name,
                event_title=event.title,
                event_date=ev_date_str,
                student_names=student_names,
                amount_paid=0,
            )
        except Exception:
            logger.exception("Failed to send registration confirmation to %s", user.email)
        return schemas.CheckoutSessionOut(checkout_url="", session_id="free")

    # Paid event — create Stripe Checkout Session
    amount_cents = int(price_per_student * len(body.student_ids) * 100)
    user_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event.title} — {len(body.student_ids)} dancer(s)"},
                    "unit_amount": int(price_per_student * 100),
                },
                "quantity": len(body.student_ids),
            }],
            mode="payment",
            customer_email=user.email,
            success_url=(
                f"{FRONTEND_URL}/events.html"
                f"?payment=success&event_id={event_id}&session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{FRONTEND_URL}/events.html?payment=cancelled&event_id={event_id}",
            metadata={"registration_id": str(reg.id), "event_id": str(event_id), "user_name": user_name},
        )
    except stripe.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

    reg.payment_status = "pending"
    reg.stripe_session_id = session.id
    db.commit()
    return schemas.CheckoutSessionOut(checkout_url=session.url, session_id=session.id)


@app.post("/events/{event_id}/register/verify-payment", response_model=schemas.EventRegistrationOut)
def verify_payment(
    event_id: int,
    body: schemas.VerifyPaymentRequest,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Verify a Stripe Checkout session and finalize the registration if paid."""
    user = get_current_user(authorization, db)
    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    if reg.is_finalized:
        return _build_reg_out(reg)

    try:
        session = stripe.checkout.Session.retrieve(body.session_id)
    except stripe.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

    if session.payment_status == "paid":
        from decimal import Decimal as D
        reg.is_finalized = True
        reg.payment_status = "paid"
        amount = D(str(session.amount_total)) / 100
        reg.amount_paid = amount
        reg.finalized_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(reg)
        # Send confirmation email
        try:
            event = db.query(models.Event).filter(models.Event.id == event_id).first()
            student_names = [ers.student.name for ers in reg.attending_students]
            ev_date_str = event.event_date.strftime("%B %d, %Y") if event and event.event_date else ""
            email_service.send_registration_confirmation(
                to_email=user.email,
                studio_name=user.studio_name,
                event_title=event.title if event else "Event",
                event_date=ev_date_str,
                student_names=student_names,
                amount_paid=float(amount),
            )
        except Exception:
            logger.exception("Failed to send registration confirmation to %s", user.email)

    return _build_reg_out(reg)


@app.post("/events/{event_id}/register/add-students-save", response_model=schemas.EventRegistrationOut, status_code=200)
def add_students_save(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Add new dancers to a registration (finalized or not) without requiring payment now."""
    user = get_current_user(authorization, db)
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    reg = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.EventRegistration.user_id == user.id,
    ).first()

    if not reg:
        # No existing registration — create one
        reg = models.EventRegistration(event_id=event_id, user_id=user.id)
        db.add(reg)
        db.flush()

    # Add only students not already in the registration
    existing_ids = {ers.student_id for ers in reg.attending_students}
    for sid in body.student_ids:
        if sid in existing_ids:
            continue
        s = db.query(models.Student).filter(
            models.Student.id == sid, models.Student.user_id == user.id
        ).first()
        if s:
            db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))

    db.commit()
    db.refresh(reg)
    return _build_reg_out(reg)


@app.post("/events/{event_id}/register/add-students", response_model=schemas.CheckoutSessionOut, status_code=201)
def add_students_to_finalized(
    event_id: int,
    body: schemas.EventRegistrationCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Add new dancers to a finalized registration and return a Stripe Checkout URL."""
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
    if not body.student_ids:
        raise HTTPException(status_code=400, detail="Select at least one new student.")

    existing_ids = {ers.student_id for ers in reg.attending_students}
    new_ids = [sid for sid in body.student_ids if sid not in existing_ids]
    if not new_ids:
        raise HTTPException(status_code=400, detail="All selected students are already registered.")

    # Capacity check for new students only
    if event.max_students is not None:
        current_total = sum(len(r.attending_students) for r in event.registrations)
        available = event.max_students - current_total
        if len(new_ids) > available:
            raise HTTPException(status_code=400, detail=f"Only {available} spot(s) remaining.")

    price_per_student, is_free = _effective_price(event)

    if is_free:
        for sid in new_ids:
            s = db.query(models.Student).filter(
                models.Student.id == sid, models.Student.user_id == user.id
            ).first()
            if s:
                db.add(models.EventRegistrationStudent(registration_id=reg.id, student_id=sid))
        db.commit()
        return schemas.CheckoutSessionOut(checkout_url="", session_id="free")

    # Paid — create checkout for new students only
    user_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event.title} — {len(new_ids)} additional dancer(s)"},
                    "unit_amount": int(price_per_student * 100),
                },
                "quantity": len(new_ids),
            }],
            mode="payment",
            customer_email=user.email,
            success_url=(
                f"{FRONTEND_URL}/events.html"
                f"?payment=success&event_id={event_id}&session_id={{CHECKOUT_SESSION_ID}}"
                f"&new_students={','.join(str(i) for i in new_ids)}"
            ),
            cancel_url=f"{FRONTEND_URL}/events.html?payment=cancelled&event_id={event_id}",
            metadata={
                "registration_id": str(reg.id),
                "event_id": str(event_id),
                "new_student_ids": ",".join(str(i) for i in new_ids),
                "user_name": user_name,
            },
        )
    except stripe.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

    reg.stripe_session_id = session.id
    db.commit()
    return schemas.CheckoutSessionOut(checkout_url=session.url, session_id=session.id)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Stripe webhook events — finalizes registrations on checkout.session.completed."""
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
        reg = db.query(models.EventRegistration).filter(models.EventRegistration.id == reg_id).first()
        if reg:
            from decimal import Decimal as D
            # Add new students if this was an add-dancers checkout
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
            amount_total = session.get("amount_total") or 0
            prev_paid = reg.amount_paid or D("0")
            new_amount = prev_paid + D(str(amount_total)) / 100
            reg.is_finalized = True
            reg.payment_status = "paid"
            reg.amount_paid = new_amount
            reg.finalized_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(reg)
            # Send confirmation email (webhook path)
            try:
                evt = reg.event
                user = reg.user
                student_names = [ers.student.name for ers in reg.attending_students]
                ev_date_str = evt.event_date.strftime("%B %d, %Y") if evt and evt.event_date else ""
                email_service.send_registration_confirmation(
                    to_email=user.email,
                    studio_name=user.studio_name,
                    event_title=evt.title if evt else "Event",
                    event_date=ev_date_str,
                    student_names=student_names,
                    amount_paid=float(new_amount),
                )
            except Exception:
                logger.exception("Failed to send registration confirmation in webhook for reg %s", reg_id)

    return {"ok": True}


@app.get("/events/my-payments")
def get_my_payments(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return all finalized/paid registrations for the current user as transaction history."""
    user = get_current_user(authorization, db)
    regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.user_id == user.id,
        models.EventRegistration.payment_status.in_(["paid", "free"]),
    ).order_by(models.EventRegistration.finalized_at.desc()).all()
    result = []
    for reg in regs:
        result.append({
            "registration_id": reg.id,
            "event_id": reg.event_id,
            "event_title": reg.event.title,
            "event_date": reg.event.event_date.isoformat() if reg.event.event_date else None,
            "student_count": len(reg.attending_students),
            "amount_paid": float(reg.amount_paid) if reg.amount_paid is not None else 0,
            "payment_status": reg.payment_status,
            "finalized_at": reg.finalized_at.isoformat() if reg.finalized_at else None,
        })
    return result


@app.get("/admin/payments")
def admin_list_payments(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return all paid registrations across all users. Admin only."""
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.payment_status.in_(["paid", "free"]),
    ).order_by(models.EventRegistration.finalized_at.desc()).all()
    result = []
    for reg in regs:
        user_name = f"{reg.user.first_name or ''} {reg.user.last_name or ''}".strip() or reg.user.email
        result.append({
            "registration_id": reg.id,
            "event_id": reg.event_id,
            "event_title": reg.event.title,
            "user_id": reg.user_id,
            "user_name": user_name,
            "studio_name": reg.user.studio_name,
            "student_count": len(reg.attending_students),
            "amount_paid": float(reg.amount_paid) if reg.amount_paid is not None else 0,
            "payment_status": reg.payment_status,
            "finalized_at": reg.finalized_at.isoformat() if reg.finalized_at else None,
        })
    return result


@app.get("/admin/events/{event_id}/registrations")
def admin_list_registrations(
    event_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """List all registrations for an event, with per-student details. Admin only."""
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    regs = db.query(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id
    ).all()
    result = []
    for reg in regs:
        students = [{"id": ers.student_id, "name": ers.student.name} for ers in reg.attending_students]
        name = f"{reg.user.first_name or ''} {reg.user.last_name or ''}".strip() or reg.user.email
        result.append({
            "id": reg.id,
            "user_id": reg.user_id,
            "user_name": name,
            "studio_name": reg.user.studio_name,
            "students": students,
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
    """Create or update a registration for any user. Admin only. Body: {user_id, student_ids}."""
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

    # Capacity check
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
    """Remove a user's entire event registration. Admin only."""
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
    """Remove one student from an event registration. Admin only."""
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    ers = db.query(models.EventRegistrationStudent).filter(
        models.EventRegistrationStudent.registration_id == reg_id,
        models.EventRegistrationStudent.student_id == student_id,
    ).first()
    if not ers:
        raise HTTPException(status_code=404, detail="Student not in registration.")
    db.delete(ers)
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
    """Add a student to an event registration. Admin only."""
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
    db.commit()
    return {"message": "Student added to registration."}


@app.delete("/admin/events/{event_id}", response_model=schemas.MessageOut)
def delete_event(
    event_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Delete an event. Admin only."""
    current = get_current_user(authorization, db)
    if not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")
    db.delete(event)
    db.commit()
    return {"message": "Event deleted."}


# ── Admin student view ────────────────────────────────────────────────────────

@app.get("/admin/users/{user_id}/students", response_model=list[schemas.StudentOut])
def admin_list_students(
    user_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Return all students for a given user. Admin only."""
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
    """Create a student for a given user. Admin only."""
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
    """Update a student belonging to a specific user. Admin only."""
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
    """Delete a student belonging to a specific user. Admin only."""
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
