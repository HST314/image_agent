"""Canonical image asset contract shared by every generation path."""
from __future__ import annotations

import hashlib
import io
import json
from typing import Any, Callable
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

from storage.project_store import ArtifactStore

MAX_IMAGE_BYTES = 25 * 1024 * 1024
ALLOWED_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


class AssetPersistenceError(RuntimeError):
    """Provider output could not be safely copied into controlled storage."""


def _download(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "image-agent-asset-ingest/1"})
    try:
        with urlopen(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            if status < 200 or status >= 300:
                raise AssetPersistenceError(f"供应商图片下载失败（HTTP {status}）。")
            content_type = response.headers.get_content_type().lower()
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_IMAGE_BYTES:
                raise AssetPersistenceError("供应商图片超过 25 MiB 限制。")
            content = response.read(MAX_IMAGE_BYTES + 1)
    except AssetPersistenceError:
        raise
    except Exception as exc:
        raise AssetPersistenceError(f"供应商图片下载失败：{type(exc).__name__}") from exc
    if len(content) > MAX_IMAGE_BYTES:
        raise AssetPersistenceError("供应商图片超过 25 MiB 限制。")
    return content, content_type


def persist_image_asset(response: dict[str, Any], store: ArtifactStore, *,
                        fetcher: Callable[[str], tuple[bytes, str]] = _download) -> dict[str, Any]:
    """Download, decode-check and persist one provider image before success."""
    raw = dict(response)
    url = raw.get("url") or raw.get("uri")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise AssetPersistenceError("生图服务未返回可下载的 HTTP 图片地址。")
    content, media_type = fetcher(url)
    media_type = media_type.split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_TYPES:
        raise AssetPersistenceError("供应商响应 MIME 不是受支持的图片类型。")
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise AssetPersistenceError("供应商图片为空或超过大小限制。")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
            detected = Image.MIME.get(image.format)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AssetPersistenceError("供应商响应无法解码为有效图片。") from exc
    if detected != media_type and not ({detected, media_type} <= {"image/jpeg", "image/jpg"}):
        raise AssetPersistenceError("供应商响应 MIME 与图片内容不一致。")
    metadata = {
        "media_type": media_type,
        "provider": str(raw.get("provider") or "unknown"),
        "model": str(raw.get("model") or "unknown"),
        "mock": False,
    }
    try:
        saved = store.save_bytes(content, suffix=ALLOWED_TYPES[media_type], metadata=metadata)
    except Exception as exc:
        raise AssetPersistenceError(f"供应商图片写入受控资产库失败：{type(exc).__name__}") from exc
    return {**metadata, **saved, "reference_hash": saved["sha256"]}


def normalize_image_asset(response: dict[str, Any], *, provider: str | None = None,
                          model: str | None = None) -> dict[str, Any]:
    """Return a stable, complete asset; reject unusable provider responses."""
    raw = dict(response)
    uri = raw.get("uri") or raw.get("url")
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("生图服务未返回可保存的图片地址。")
    content = raw.get("content") or raw.get("bytes")
    reference_hash = raw.get("reference_hash")
    if content is not None:
        material = content if isinstance(content, bytes) else str(content).encode("utf-8")
        reference_hash = hashlib.sha256(material).hexdigest()
    elif not reference_hash:
        # A persistent URI is the provider's content reference. Hash the exact
        # reference rather than the mutable response envelope.
        reference_hash = hashlib.sha256(uri.strip().encode("utf-8")).hexdigest()
    sha256 = str(raw.get("sha256") or reference_hash)
    if len(sha256) != 64:
        sha256 = hashlib.sha256(sha256.encode("utf-8")).hexdigest()
    normalized = {
        **raw,
        "uri": uri.strip(),
        "reference_hash": str(reference_hash),
        "sha256": sha256,
        "provider": str(raw.get("provider") or provider or "unknown"),
        "model": str(raw.get("model") or model or "unknown"),
        "mock": bool(raw.get("mock", False)),
    }
    normalized.pop("url", None)
    normalized.pop("bytes", None)
    if content is not None and isinstance(content, bytes):
        normalized["content"] = content.hex()
    # Ensure the asset can always cross a checkpoint boundary.
    json.dumps(normalized, ensure_ascii=False)
    return normalized
