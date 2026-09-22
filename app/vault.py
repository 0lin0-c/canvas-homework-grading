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
        hint = f"****{token[-4:]}" if len(token) >= 4 else "****"
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
