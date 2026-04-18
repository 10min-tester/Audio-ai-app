from __future__ import annotations

import mimetypes
import os
import shutil
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

try:
    import boto3
except Exception:  # pragma: no cover - optional dependency
    boto3 = None


_STORAGE_MODE = os.getenv("STORAGE_MODE", "local").strip().lower()
_LOCAL_ROOT = os.path.abspath(os.getenv("LOCAL_STORAGE_ROOT", "storage_data"))
_LOCAL_INPUT_PREFIX = "inputs"
_LOCAL_OUTPUT_PREFIX = "outputs"
_LOCAL_ARCHIVE_PREFIX = "archives"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def get_storage_mode() -> str:
    if _STORAGE_MODE == "s3":
        if boto3 is None:
            return "local"
        if not os.getenv("S3_BUCKET", "").strip():
            return "local"
    return "s3" if _STORAGE_MODE == "s3" else "local"


def _s3_client():
    endpoint_url = os.getenv("S3_ENDPOINT_URL", "").strip() or None
    region_name = os.getenv("S3_REGION", "").strip() or None
    access_key = os.getenv("S3_ACCESS_KEY_ID", "").strip() or None
    secret_key = os.getenv("S3_SECRET_ACCESS_KEY", "").strip() or None
    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def _s3_bucket() -> str:
    bucket = os.getenv("S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET is required when STORAGE_MODE=s3")
    return bucket


def _s3_parse_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError("Invalid s3 uri")
    payload = uri[5:]
    if "/" not in payload:
        raise ValueError("Invalid s3 uri key")
    bucket, key = payload.split("/", 1)
    return bucket, key


def _ensure_local_dirs():
    os.makedirs(_LOCAL_ROOT, exist_ok=True)
    os.makedirs(os.path.join(_LOCAL_ROOT, _LOCAL_INPUT_PREFIX), exist_ok=True)
    os.makedirs(os.path.join(_LOCAL_ROOT, _LOCAL_OUTPUT_PREFIX), exist_ok=True)
    os.makedirs(os.path.join(_LOCAL_ROOT, _LOCAL_ARCHIVE_PREFIX), exist_ok=True)


def _safe_basename(name: str) -> str:
    return os.path.basename(name).replace(" ", "_")


def create_upload_session(filename: str, content_type: str | None = None) -> dict:
    file_id = str(uuid.uuid4())
    base_name = _safe_basename(filename or f"audio_{file_id}.wav")
    ctype = content_type or _guess_content_type(base_name)
    mode = get_storage_mode()

    if mode == "s3":
        key = f"{_LOCAL_INPUT_PREFIX}/{_utc_stamp()}_{file_id}_{base_name}"
        bucket = _s3_bucket()
        client = _s3_client()
        expires_in = int(os.getenv("UPLOAD_URL_EXPIRES_SEC", "1800"))
        upload_url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": ctype},
            ExpiresIn=expires_in,
        )
        return {
            "storage_mode": "s3",
            "file_id": file_id,
            "object_key": key,
            "content_type": ctype,
            "input_uri": f"s3://{bucket}/{key}",
            "upload": {
                "method": "PUT",
                "url": upload_url,
                "headers": {"Content-Type": ctype},
            },
        }

    _ensure_local_dirs()
    key = f"{_utc_stamp()}_{file_id}_{base_name}"
    rel_path = os.path.join(_LOCAL_INPUT_PREFIX, key).replace("\\", "/")
    return {
        "storage_mode": "local",
        "file_id": file_id,
        "object_key": rel_path,
        "content_type": ctype,
        "input_uri": f"local://{rel_path}",
        "upload": {
            "method": "PUT",
            "url": f"/api/v2/storage/upload/{file_id}?key={quote(rel_path)}",
            "headers": {"Content-Type": ctype},
        },
    }


def resolve_local_path_from_uri(uri: str) -> str:
    if not uri.startswith("local://"):
        raise ValueError("Invalid local uri")
    rel = uri[len("local://"):].lstrip("/").replace("\\", "/")
    abs_path = os.path.abspath(os.path.join(_LOCAL_ROOT, rel))
    root = os.path.abspath(_LOCAL_ROOT)
    if not abs_path.startswith(root):
        raise ValueError("Unsafe local path")
    return abs_path


def save_upload_bytes_local(uri: str, payload: bytes):
    _ensure_local_dirs()
    path = resolve_local_path_from_uri(uri)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(payload)


def read_to_local_temp(input_uri: str, temp_path: str):
    mode = get_storage_mode()
    if mode == "s3" and input_uri.startswith("s3://"):
        bucket, key = _s3_parse_uri(input_uri)
        client = _s3_client()
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        client.download_file(bucket, key, temp_path)
        return

    path = resolve_local_path_from_uri(input_uri)
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    shutil.copyfile(path, temp_path)


def upload_from_local_temp(local_path: str, filename_hint: str, prefix: str) -> str:
    base_name = _safe_basename(filename_hint)
    key_tail = f"{_utc_stamp()}_{uuid.uuid4()}_{base_name}"
    mode = get_storage_mode()

    if mode == "s3":
        key = f"{prefix}/{key_tail}"
        bucket = _s3_bucket()
        client = _s3_client()
        content_type = _guess_content_type(base_name)
        client.upload_file(
            local_path,
            bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return f"s3://{bucket}/{key}"

    _ensure_local_dirs()
    rel = f"{prefix}/{key_tail}".replace("\\", "/")
    dest = resolve_local_path_from_uri(f"local://{rel}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(local_path, dest)
    return f"local://{rel}"


def create_download_url(uri: str, filename: str | None = None, expires_in: int = 3600) -> str:
    mode = get_storage_mode()
    if mode == "s3" and uri.startswith("s3://"):
        bucket, key = _s3_parse_uri(uri)
        client = _s3_client()
        params = {"Bucket": bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{_safe_basename(filename)}"'
        return client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)

    if uri.startswith("local://"):
        rel = uri[len("local://"):].lstrip("/")
        return f"/api/v2/storage/object/{quote(rel)}"
    raise ValueError("Unsupported uri")


def cleanup_local_storage(hours: int = 24):
    if get_storage_mode() != "local":
        return
    _ensure_local_dirs()
    cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    for root, _, files in os.walk(_LOCAL_ROOT):
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                continue
