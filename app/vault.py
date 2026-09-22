"""Encrypted Canvas token vault (server-side only)."""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nacl import secret, utils
from nacl.exceptions import CryptoError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _hint_of(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 4:
        return f"****{text[-4:]}"
    return "****"


def _derive_key(secret_key: str) -> bytes:
    """Derive a 32-byte SecretBox key from GRADING_SECRET_KEY."""
    raw = secret_key.encode("utf-8")
    if len(raw) >= 32:
        return raw[:32]
    # Pad short keys (dev only); production should use 32+ bytes.
    return (raw + b"\0" * 32)[:32]


class TokenVault:
    """AEAD-encrypted token storage. Never logs or returns full token."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = self.settings.resolve_path(self.settings.vault_path)
        self._box = secret.SecretBox(_derive_key(self.settings.grading_secret_key))
        self._memory_token: str | None = None
        self._meta: dict[str, Any] = {}

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, token: str, validated_at: datetime | None = None) -> dict[str, Any]:
        token = (token or "").strip()
        if not token:
            raise ValueError("token must not be empty")
        hint = _hint_of(token)
        meta = {
            "hint": hint,
            "validated_at": (validated_at or datetime.now(timezone.utc)).isoformat(),
            "present": True,
        }
        payload = json.dumps({"token": token, "meta": meta}).encode("utf-8")
        nonce = utils.random(secret.SecretBox.NONCE_SIZE)
        ciphertext = self._box.encrypt(payload, nonce)
        self._ensure_parent()
        self.path.write_bytes(ciphertext)
        self._memory_token = token
        self._meta = meta
        logger.info("Canvas token vault updated (hint=%s)", hint)
        return {"present": True, "hint": hint, "validated_at": meta["validated_at"]}

    def clear(self) -> None:
        self._memory_token = None
        self._meta = {}
        if self.path.exists():
            self.path.unlink()
        logger.info("Canvas token vault cleared")

    def _load_disk(self) -> None:
        if not self.path.exists():
            return
        try:
            data = self._box.decrypt(self.path.read_bytes())
            obj = json.loads(data.decode("utf-8"))
            self._memory_token = obj.get("token")
            self._meta = obj.get("meta") or {}
        except (CryptoError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to decrypt vault: %s", type(exc).__name__)
            self._memory_token = None
            self._meta = {"present": False, "error": "decrypt_failed"}

    def get_token(self) -> str | None:
        """Return plaintext token for Canvas client use only. Do not log."""
        if self._memory_token:
            return self._memory_token
        # Optional env seed
        if self.settings.canvas_token:
            self.save(self.settings.canvas_token)
            return self._memory_token
        self._load_disk()
        return self._memory_token

    def status(self) -> dict[str, Any]:
        if not self._memory_token and not self._meta:
            if self.settings.canvas_token and not self.path.exists():
                # Seed without calling get_token (avoids circular log)
                try:
                    return self.save(self.settings.canvas_token)
                except Exception:
                    pass
            self._load_disk()
        if self._memory_token or self._meta.get("present"):
            return {
                "present": True,
                "hint": self._meta.get("hint", "****"),
                "validated_at": self._meta.get("validated_at"),
                "valid": self._meta.get("error") != "decrypt_failed",
            }
        return {"present": False, "hint": None, "validated_at": None, "valid": False}


# Process-wide vault singleton
_vault: TokenVault | None = None


def get_vault() -> TokenVault:
    global _vault
    if _vault is None:
        _vault = TokenVault()
    return _vault


class ModelKeyVault:
    """AEAD-encrypted model API key. Same GRADING_SECRET_KEY as the Canvas token vault.

    Status and logs expose only a hint. Never log the key or Authorization header.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = self.settings.resolve_path(self.settings.model_vault_path)
        self._box = secret.SecretBox(_derive_key(self.settings.grading_secret_key))
        self._memory_key: str | None = None
        self._meta: dict[str, Any] = {}

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, api_key: str, validated_at: datetime | None = None) -> dict[str, Any]:
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("api_key must not be empty")
        hint = _hint_of(api_key)
        meta = {
            "hint": hint,
            "validated_at": (validated_at or datetime.now(timezone.utc)).isoformat(),
            "present": True,
        }
        payload = json.dumps({"api_key": api_key, "meta": meta}).encode("utf-8")
        nonce = utils.random(secret.SecretBox.NONCE_SIZE)
        ciphertext = self._box.encrypt(payload, nonce)
        self._ensure_parent()
        self.path.write_bytes(ciphertext)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._memory_key = api_key
        self._meta = meta
        logger.info("Model API key vault updated (hint=%s)", hint)
        return {"present": True, "hint": hint, "validated_at": meta["validated_at"]}

    def clear(self) -> None:
        self._memory_key = None
        self._meta = {}
        if self.path.exists():
            self.path.unlink()
        logger.info("Model API key vault cleared")

    def _load_disk(self) -> None:
        if not self.path.exists():
            return
        try:
            data = self._box.decrypt(self.path.read_bytes())
            obj = json.loads(data.decode("utf-8"))
            self._memory_key = obj.get("api_key")
            self._meta = obj.get("meta") or {}
        except (CryptoError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to decrypt model key vault: %s", type(exc).__name__)
            self._memory_key = None
            self._meta = {"present": False, "error": "decrypt_failed"}

    def has_stored_key(self) -> bool:
        """True only when ciphertext decrypts to a key. Does not seed from env."""
        if self._memory_key:
            return True
        if not self.path.is_file():
            return False
        self._load_disk()
        return bool(self._memory_key)

    def get_key(self) -> str | None:
        """Plaintext key for the model client only. Do not log the return value."""
        if self._memory_key:
            return self._memory_key
        file_existed = self.path.is_file()
        self._load_disk()
        if self._memory_key:
            return self._memory_key
        env_key = self.settings.env_model_key()
        if not env_key:
            return None
        # Unreadable existing file: use env in memory, do not clobber ciphertext.
        if file_existed:
            logger.warning("Model key vault unreadable; using env key without rewriting vault")
            self._memory_key = env_key
            self._meta = {"hint": _hint_of(env_key), "present": True, "validated_at": None}
            return self._memory_key
        try:
            self.save(env_key)
        except Exception:
            logger.warning("Model key vault seed failed")
            self._memory_key = env_key
            self._meta = {"hint": _hint_of(env_key), "present": True, "validated_at": None}
        return self._memory_key

    def status(self) -> dict[str, Any]:
        """Public status: present, hint, validated_at. Seeds an empty vault from env."""
        if not self._memory_key and not self._meta:
            self.get_key()
        if self._memory_key or self._meta.get("present"):
            hint = self._meta.get("hint")
            if not hint and self._memory_key:
                hint = _hint_of(self._memory_key)
            return {
                "present": True,
                "hint": hint or "****",
                "validated_at": self._meta.get("validated_at"),
            }
        return {"present": False, "hint": None, "validated_at": None}


_model_vault: ModelKeyVault | None = None


def get_model_vault() -> ModelKeyVault:
    global _model_vault
    if _model_vault is None:
        _model_vault = ModelKeyVault()
    return _model_vault
