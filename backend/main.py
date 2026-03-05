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
    db.delete(student)
    db.commit()
    return {"message": "Student deleted."}


# ── Event routes (public read, admin write) ───────────────────────────────────

@app.get("/events", response_model=list[schemas.EventOut])
def list_events(db: Session = Depends(get_db)):
    """Return all events ordered by date ascending."""
    return db.query(models.Event).order_by(models.Event.event_date).all()


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
    return event


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
    return event


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
        result[str(reg.event_id)] = {"registration_id": reg.id, "student_ids": student_ids}
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
    db.delete(reg)
    db.commit()
    return {"message": "Unregistered from event."}


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
        })
    return result


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
