from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

SessionId = Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,99}$", examples=["SESSION-PYTHON-01"])]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Student(StrictModel):
    email: EmailStr
    firstName: str = Field(min_length=1, max_length=100)
    lastName: str = Field(min_length=1, max_length=100)

    @field_validator("email")
    @classmethod
    def lower_email(cls, value: str) -> str:
        return value.lower()


class Company(StrictModel):
    name: str = Field(min_length=1, max_length=140)


class RegistrationRequest(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "student": {"email": "alice@example.org", "firstName": "Alice", "lastName": "Martin"},
                "company": {"name": "ACME"},
                "sessionId": "SESSION-PYTHON-01",
            }
        }
    )

    student: Student
    company: Company
    sessionId: SessionId


class RegistrationAccepted(BaseModel):
    registrationId: str
    status: str


class StatusHistoryEntry(BaseModel):
    status: str
    detail: str | None
    at: datetime


class RegistrationView(BaseModel):
    registrationId: str
    status: str
    sessionId: str
    student: Student
    company: Company
    correlationId: str
    moodleUserId: int | None = None
    courseId: int | None = None
    erpLeadId: str | None = None
    erpCustomerId: str | None = None
    erpSalesOrderId: str | None = None
    attempts: int
    lastError: str | None
    createdAt: datetime
    updatedAt: datetime
    history: list[StatusHistoryEntry]


class SessionCreate(StrictModel):
    sessionId: SessionId
    capacity: int = Field(ge=1, le=10_000)
    moodleCourseShortname: str = Field(min_length=1, max_length=100, examples=["PYTHON-ADV"])
    erpItemCode: str = Field(min_length=1, max_length=140, examples=["FORMATION-PYTHON"])


class SessionView(BaseModel):
    sessionId: str
    capacity: int
    remainingSeats: int
    activeRegistrations: int
    moodleCourseShortname: str
    erpItemCode: str


class CatalogEntry(BaseModel):
    sessionId: str
    courseName: str | None
    courseId: int | None
    remainingSeats: int
    capacity: int
    price: float | None
    currency: str | None
    moodleCourseShortname: str
    erpItemCode: str


class ErrorResponse(BaseModel):
    error: str
    message: str


class HealthView(BaseModel):
    status: str


class ReadinessView(BaseModel):
    status: str
    checks: dict[str, str]
    pendingOutboxEvents: int | None = None
