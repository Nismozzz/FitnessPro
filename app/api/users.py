from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.auth.security import get_current_user_id
from app.database.models import User
from app.database.session import SessionLocal
from app.users.profile import ProfileData, get_profile, update_profile

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def me(user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
    assert user is not None
    return {"id": user.id, "username": user.username, "created_at": user.created_at.isoformat()}


@router.get("/me/profile")
async def read_profile(user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    return await get_profile(user_id)


@router.put("/me/profile")
async def write_profile(
    body: ProfileData, user_id: str = Depends(get_current_user_id)
) -> dict[str, Any]:
    return await update_profile(user_id, body)
