"""Upload remote images to Feishu and return image_key values."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images"

_TOKEN_CACHE: dict[str, Any] = {}
_IMAGE_CACHE: dict[str, str] = {}


def configured() -> bool:
    return bool(os.getenv("FEISHU_APP_ID", "").strip() and os.getenv("FEISHU_APP_SECRET", "").strip())


def tenant_access_token() -> str | None:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        return None

    now = int(time.time())
    cached = _TOKEN_CACHE.get("token")
    expires_at = int(_TOKEN_CACHE.get("expires_at", 0))
    if cached and expires_at - 120 > now:
        return str(cached)

    payload = {
        "app_id": app_id,
        "app_secret": app_secret,
    }
    request = urllib.request.Request(
        TOKEN_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "surveil-feishu-image/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"获取飞书 tenant_access_token 失败：HTTP {exc.code}\n{detail}")
        return None
    except urllib.error.URLError as exc:
        print(f"获取飞书 tenant_access_token 网络失败：{exc}")
        return None

    result = json.loads(body)
    if result.get("code") != 0:
        print(f"获取飞书 tenant_access_token 失败：{body}")
        return None

    token = result.get("tenant_access_token")
    expire = int(result.get("expire", 7200))
    if token:
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = now + expire
        return str(token)
    return None


def download_image(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 surveil-feishu-image/0.1",
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
                return response.read(), content_type
        except Exception as exc:  # noqa: BLE001 - transient image CDN failures are common
            last_error = exc
            if attempt == 2:
                break
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"图片下载失败，重试后仍失败：{last_error}")


def multipart_body(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----surveil-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for name, (filename, data, content_type) in files.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def upload_image_bytes(image: bytes, content_type: str, token: str) -> str | None:
    body, boundary = multipart_body(
        {"image_type": "message"},
        {"image": ("x-image.jpg", image, content_type)},
    )
    request = urllib.request.Request(
        IMAGE_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "surveil-feishu-image/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"上传飞书图片失败：HTTP {exc.code}\n{detail}")
        return None
    except urllib.error.URLError as exc:
        print(f"上传飞书图片网络失败：{exc}")
        return None

    result = json.loads(raw)
    if result.get("code") != 0:
        print(f"上传飞书图片失败：{raw}")
        return None
    data = result.get("data") or {}
    image_key = data.get("image_key")
    return str(image_key) if image_key else None


def image_key_from_url(url: str) -> str | None:
    if not configured():
        return None
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]
    token = tenant_access_token()
    if not token:
        return None
    try:
        image, content_type = download_image(url)
    except Exception as exc:
        print(f"下载 X 图片失败：{exc}")
        return None
    image_key = upload_image_bytes(image, content_type, token)
    if image_key:
        _IMAGE_CACHE[url] = image_key
    return image_key
