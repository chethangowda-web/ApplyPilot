import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from typing import Any
import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import jwt
from fastapi import HTTPException, status
from app.core.config import Settings, get_settings

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    raw = password.encode("utf-8")
    if len(raw) > 72:
        raise ValueError("Password exceeds 72-byte bcrypt limit")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(raw, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    raw = plain_password.encode("utf-8")
    if len(raw) > 72:
        return False
    return bcrypt.checkpw(raw, hashed_password.encode("utf-8"))


def create_access_token(subject: str, expires_minutes: int | None = None) -> tuple[str, int]:
    """Create a signed JWT access token and return (token, ttl_seconds)."""
    settings = get_settings()
    ttl_minutes = expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=ttl_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, ttl_minutes * 60


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT access token."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        ) from exc

    if payload.get("type") != "access" or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    return payload


def _derive_aes256_key(secret: str, version: str) -> bytes:
    """Derive a domain-separated 256-bit key for AES-256-GCM from secret material and version."""
    return hashlib.sha256(f"applypilot-aes256-gcm:{version}:{secret}".encode("utf-8")).digest()


def _build_keyring(settings: Settings) -> tuple[str, dict[str, str]]:
    active_ver = (settings.encryption_key_version or "v1").strip()
    active_key = (settings.encryption_key or "").strip()
    ring: dict[str, str] = {}
    raw_prev = (settings.encryption_previous_keys or "").strip()
    if raw_prev:
        if raw_prev.startswith("{"):
            parsed = json.loads(raw_prev)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if str(k).strip() and str(v).strip():
                        ring[str(k).strip()] = str(v).strip()
        else:
            for item in raw_prev.split(","):
                if ":" in item:
                    k, v = item.split(":", 1)
                    if k.strip() and v.strip():
                        ring[k.strip()] = v.strip()
    if active_key:
        ring[active_ver] = active_key
    return active_ver, ring


def _encrypt_with_aad(
    plaintext: str,
    *,
    aad_builder: Any,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Encrypt plaintext with AES-256-GCM using the active keyring version and caller-supplied AAD."""
    if not plaintext:
        raise ValueError("Plaintext secret cannot be empty")
    cfg = settings or get_settings()
    active_ver, ring = _build_keyring(cfg)
    secret = ring.get(active_ver, "")
    if not secret:
        raise ValueError("ENCRYPTION_KEY must be configured to encrypt sensitive data")

    key = _derive_aes256_key(secret, active_ver)
    nonce = os.urandom(12)
    aad = aad_builder(active_ver).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    encoded = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    return encoded, active_ver


def _decrypt_with_aad(
    ciphertext: str,
    *,
    key_version: str,
    aad_builder: Any,
    settings: Settings | None = None,
) -> str:
    """Decrypt and authenticate an AES-256-GCM payload using its recorded key version."""
    cfg = settings or get_settings()
    _, ring = _build_keyring(cfg)
    ver = key_version.strip()
    secret = ring.get(ver, "")
    if not secret:
        raise ValueError(f"Encryption key version '{key_version}' is not available in keyring")

    raw = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    if len(raw) < 29:
        raise ValueError("Malformed ciphertext payload")
    nonce, ct = raw[:12], raw[12:]
    key = _derive_aes256_key(secret, ver)
    aad = aad_builder(ver).encode("utf-8")
    pt = AESGCM(key).decrypt(nonce, ct, aad)
    return pt.decode("utf-8")


def encrypt_sensitive_answer(
    plaintext: str,
    *,
    application_id: str,
    question_key: str,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Authenticate and encrypt sensitive application answers with AES-256-GCM (no plaintext fallback)."""
    if not plaintext:
        raise ValueError("Sensitive answer plaintext cannot be empty")
    return _encrypt_with_aad(
        plaintext,
        aad_builder=lambda ver: f"{application_id}:{question_key}:{ver}",
        settings=settings,
    )


def decrypt_sensitive_answer(
    ciphertext: str,
    *,
    key_version: str,
    application_id: str,
    question_key: str,
    settings: Settings | None = None,
) -> str:
    """Decrypt and authenticate a sensitive application answer using its recorded key version."""
    return _decrypt_with_aad(
        ciphertext,
        key_version=key_version,
        aad_builder=lambda ver: f"{application_id}:{question_key}:{ver}",
        settings=settings,
    )


def rotate_sensitive_answer_ciphertext(
    ciphertext: str,
    *,
    old_key_version: str,
    application_id: str,
    question_key: str,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Re-encrypt an existing sensitive answer under the active ENCRYPTION_KEY_VERSION."""
    plaintext = decrypt_sensitive_answer(
        ciphertext,
        key_version=old_key_version,
        application_id=application_id,
        question_key=question_key,
        settings=settings,
    )
    return encrypt_sensitive_answer(
        plaintext,
        application_id=application_id,
        question_key=question_key,
        settings=settings,
    )


def _oauth_aad(user_id: str, provider: str, token_kind: str, version: str) -> str:
    uid = str(user_id).strip()
    prov = provider.strip()
    kind = token_kind.strip()
    if not uid or not prov or not kind:
        raise ValueError("OAuth AAD requires non-empty user_id, provider, and token_kind")
    return f"oauth:{uid}:{prov}:{kind}:{version}"


def encrypt_oauth_token(
    plaintext: str,
    *,
    user_id: str,
    provider: str,
    token_kind: str = "refresh_token",
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Encrypt an OAuth credential at rest with AES-256-GCM and domain-separated OAuth AAD."""
    if not plaintext:
        raise ValueError("OAuth token plaintext cannot be empty")
    return _encrypt_with_aad(
        plaintext,
        aad_builder=lambda ver: _oauth_aad(user_id, provider, token_kind, ver),
        settings=settings,
    )


def decrypt_oauth_token(
    ciphertext: str,
    *,
    key_version: str,
    user_id: str,
    provider: str,
    token_kind: str = "refresh_token",
    settings: Settings | None = None,
) -> str:
    """Decrypt and authenticate an OAuth credential using its recorded key version and OAuth AAD."""
    return _decrypt_with_aad(
        ciphertext,
        key_version=key_version,
        aad_builder=lambda ver: _oauth_aad(user_id, provider, token_kind, ver),
        settings=settings,
    )


def rotate_oauth_token_ciphertext(
    ciphertext: str,
    *,
    old_key_version: str,
    user_id: str,
    provider: str,
    token_kind: str = "refresh_token",
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Re-encrypt an existing OAuth token under the active ENCRYPTION_KEY_VERSION."""
    plaintext = decrypt_oauth_token(
        ciphertext,
        key_version=old_key_version,
        user_id=user_id,
        provider=provider,
        token_kind=token_kind,
        settings=settings,
    )
    return encrypt_oauth_token(
        plaintext,
        user_id=user_id,
        provider=provider,
        token_kind=token_kind,
        settings=settings,
    )
