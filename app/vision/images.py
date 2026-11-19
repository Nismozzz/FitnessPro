"""User-scoped image artifacts; only normalized JPEGs are retained (no EXIF)."""
from __future__ import annotations

import hashlib
import io
import re
import warnings
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import resolve_path, settings

IMAGE_MARKER = re.compile(r"\[图片附件：([a-f0-9]{32})\]")


class ImageError(ValueError):
    pass


def image_path(user_id: str, image_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", image_id):
        raise FileNotFoundError("图片不存在或已删除")
    owner = hashlib.sha256(user_id.encode()).hexdigest()
    return resolve_path(settings.vision.upload_dir) / owner / f"{image_id}.jpg"


def save_image(user_id: str, raw: bytes) -> str:
    if not raw or len(raw) > settings.vision.max_upload_bytes:
        raise ImageError("图片为空或超过上传大小限制")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ImageError("仅支持 JPEG、PNG、WebP 图片")
                if source.width * source.height > settings.vision.max_pixels:
                    raise ImageError("图片分辨率过大，请缩小后重试")
                if getattr(source, "n_frames", 1) != 1:
                    raise ImageError("请上传静态图片")
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((settings.vision.max_edge, settings.vision.max_edge))
                # Copy pixels into a new image to discard embedded metadata.
                clean = Image.new("RGB", image.size)
                clean.paste(image)
                output = io.BytesIO()
                clean.save(output, "JPEG", quality=88)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        if isinstance(exc, ImageError):
            raise
        raise ImageError("图片无法解码或分辨率过大，请换一张图片") from exc
    image_id = uuid4().hex
    path = image_path(user_id, image_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output.getvalue())
    return image_id


def require_images(user_id: str, image_ids: list[str]) -> None:
    for image_id in image_ids:
        if not image_path(user_id, image_id).is_file():
            raise FileNotFoundError("图片不存在或已删除，请重新上传")
