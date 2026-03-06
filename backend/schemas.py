from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    studio_name: str | None = None   # None = independent dancer
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


# ── Misc ──────────────────────────────────────────────────────────────────────

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


# ── Observer schemas ──────────────────────────────────────────────────────────

class ObserverOut(BaseModel):
    id: int
    name: str
    linked_student_id: int | None = None
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


class ObserverCreate(BaseModel):
    name: str
    linked_student_id: int | None = None


# ── Event schemas ─────────────────────────────────────────────────────────────

class EventCreate(BaseModel):
    title: str
    description: str | None = None
    event_date: datetime
    location: str | None = None
    event_type: str | None = None  # "convention" or "competition"
    early_price: Decimal | None = None
    regular_price: Decimal | None = None
    early_price_deadline: datetime | None = None
    max_students: int | None = None  # None = unlimited
    observer_price: Decimal | None = None


class EventUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    event_date: datetime | None = None
    location: str | None = None
    event_type: str | None = None
    early_price: Decimal | None = None
    regular_price: Decimal | None = None
    early_price_deadline: datetime | None = None
    max_students: int | None = None
    observer_price: Decimal | None = None


class EventOut(BaseModel):
    id: int
    title: str
    description: str | None = None
    event_date: datetime
    location: str | None = None
    event_type: str | None = None
    early_price: Decimal | None = None
    regular_price: Decimal | None = None
    early_price_deadline: datetime | None = None
    max_students: int | None = None
    registered_count: int = 0
    total_revenue: Decimal | None = None
    observer_price: Decimal | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── Event Registration schemas ────────────────────────────────────────────────

class EventRegistrationCreate(BaseModel):
    student_ids: list[int] = []
    observer_ids: list[int] = []


class EventRegistrationOut(BaseModel):
    id: int
    event_id: int
    user_id: int
    student_ids: list[int] = []
    observer_ids: list[int] = []
    created_at: datetime | None = None
    is_finalized: bool = False
    payment_status: str | None = None
    amount_paid: Decimal | None = None

    model_config = {"from_attributes": True}


class CheckoutSessionOut(BaseModel):
    checkout_url: str        # empty string = free or embedded
    session_id: str          # 'free' for free events
    client_secret: str | None = None  # set when embedded=true


class VerifyPaymentRequest(BaseModel):
    session_id: str
