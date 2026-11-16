from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from uuid import uuid4

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.observability.context import TraceContext, trace_context
from app.database.models import User
from app.database.session import SessionLocal

_bearer = HTTPBearer(auto_error=False)


async def hash_password(password: str) -> str:
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


async def verify_password(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(
        bcrypt.checkpw, password.encode("utf-8"), password_hash.encode("utf-8")
    )


def create_access_token(user_id: str) -> str:
    cfg = settings.auth
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=cfg.access_token_minutes),
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


def decode_token(token: str) -> str | None:
    cfg = settings.auth
    try:
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或登录已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    user_id = decode_token(credentials.credentials)
    if user_id is None:
        raise unauthorized
    async with SessionLocal() as db:
        if await db.get(User, user_id) is None:
            raise unauthorized
    ctx = trace_context.get() or TraceContext(request_id=uuid4().hex)
    trace_context.set(replace(ctx, user_id=user_id))
    return user_id
