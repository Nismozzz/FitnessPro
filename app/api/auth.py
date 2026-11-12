from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.auth.security import create_access_token, hash_password, verify_password
from app.database.models import User, UserProfile
from app.database.session import SessionLocal

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("密码过长（最多 72 字节）")
        return v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: Credentials) -> TokenResponse:
    password_hash = await hash_password(body.password)
    async with SessionLocal() as db:
        exists = (
            await db.execute(select(User.id).where(User.username == body.username))
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
        user = User(username=body.username, password_hash=password_hash)
        db.add(user)
        await db.flush()
        db.add(UserProfile(user_id=user.id))
        await db.commit()
    return TokenResponse(access_token=create_access_token(user.id), user_id=user.id)


@router.post("/login", response_model=TokenResponse)
async def login(body: Credentials) -> TokenResponse:
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == body.username))
        ).scalar_one_or_none()
    if user is None or not await verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    return TokenResponse(access_token=create_access_token(user.id), user_id=user.id)
