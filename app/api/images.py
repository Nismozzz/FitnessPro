"""Authenticated raw-body uploads avoid buffering arbitrary multipart files."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.security import get_current_user_id
from app.config import settings
from app.vision.images import ImageError, image_path, save_image
from app.vision.service import is_available

router = APIRouter(prefix="/images", tags=["images"])


@router.get("/config")
async def config(user_id: str = Depends(get_current_user_id)):
    profile = settings.llm.vision
    from urllib.parse import urlsplit
    return {"available": is_available(), "max_upload_bytes": settings.vision.max_upload_bytes,
            "provider": urlsplit(profile.base_url).hostname if profile else None,
            "notice": "图片会发送至配置的视觉模型服务。请仅上传器械照片，避免拍入人脸等个人信息。"}


@router.post("", status_code=201)
async def upload(request: Request, user_id: str = Depends(get_current_user_id)):
    if not is_available():
        raise HTTPException(503, "图片识别尚未配置，请管理员检查视觉模型与其 api_key_env 对应的 Key")
    if request.headers.get("content-type", "").split(";")[0] not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "仅支持 JPEG、PNG、WebP 图片")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > settings.vision.max_upload_bytes:
            raise HTTPException(413, "图片超过上传大小限制")
        data.extend(chunk)
    try:
        image_id = await asyncio.to_thread(save_image, user_id, bytes(data))
    except ImageError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": image_id}


@router.get("/{image_id}")
async def read(image_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        data = await asyncio.to_thread(image_path(user_id, image_id).read_bytes)
    except FileNotFoundError:
        raise HTTPException(404, "图片不存在或已删除")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.delete("/{image_id}", status_code=204)
async def delete(image_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        await asyncio.to_thread(image_path(user_id, image_id).unlink)
    except FileNotFoundError:
        raise HTTPException(404, "图片不存在或已删除")
