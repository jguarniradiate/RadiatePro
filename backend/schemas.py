from datetime import date, datetime
from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    studio_name: str
    phone: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str


class UserOut(BaseModel):
    id: int
    email: str
    first_name: str | None = None
    last_name: str | None = None
    studio_name: str | None = None
    phone: str | None = None
    is_admin: bool = False
    email_verified: bool = False
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    studio_name: str | None = None
    phone: str | None = None
    email: EmailStr | None = None


# ── New schemas ───────────────────────────────────────────────────────────────

class MessageOut(BaseModel):
    message: str


class VerifyEmailRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str


# ── Student schemas ───────────────────────────────────────────────────────────

class StudentCreate(BaseModel):
    name: str
    date_of_birth: date | None = None
    gender: str | None = None


class StudentUpdate(BaseModel):
    name: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None


class StudentOut(BaseModel):
    id: int
    name: str
    date_of_birth: date | None = None
    gender: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── Event schemas ─────────────────────────────────────────────────────────────

class EventCreate(BaseModel):
    title: str
    description: str | None = None
    event_date: datetime
    location: str | None = None


class EventUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    event_date: datetime | None = None
    location: str | None = None


class EventOut(BaseModel):
    id: int
    title: str
    description: str | None = None
    event_date: datetime
    location: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
