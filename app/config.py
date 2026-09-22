"""Application settings from environment."""
from __future__ import annotations

from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    grading_secret_key: str = "dev-only-change-me-please-use-long-secret"
    canvas_token: str | None = None
    canvas_base_url: str = "https://oc.sjtu.edu.cn/api/v1"
    canvas_host: str = "oc.sjtu.edu.cn"
    host: str = "127.0.0.1"
    port: int = 8765
    question_bank_pdf: str = "local/question-bank.pdf"
    writeback_max_batch: int = 1
    openai_api_key: str | None = None
    model_api_key: str | None = None
    dashscope_api_key: str | None = None
    # DashScope OpenAI-compatible Qwen-VL. Scores still come from scoring.py.
    model_provider: str = "dashscope"
    model_name: str = "qwen3.8-max"
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_vault_path: str = "local/secrets/model_api_key.enc"
    model_max_pages: int = 20
    model_timeout_sec: float = 180
    model_confidence_floor: float = 0.55
    database_path: str = "local/grading.db"
    vault_path: str = "local/secrets/canvas_token.enc"
    rules_version: str = "100-minus-unique-big-questions-floor95-v1"

    # Sample assignment binding (docs/02)
    sample_course_id: int = 94139
    sample_assignment_id: int = 428317
    default_question_numbers: list[int] = [1, 2, 6, 7, 8, 10, 11, 12, 15]

    def env_model_key(self) -> str | None:
        """First non-empty env key. Precedence: MODEL_API_KEY, DASHSCOPE_API_KEY, OPENAI_API_KEY."""
        for value in (self.model_api_key, self.dashscope_api_key, self.openai_api_key):
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    def _vault_has_model_key(self) -> bool:
        path = self.resolve_path(self.model_vault_path)
        if not path.is_file():
            return False
        # Lazy import: vault imports Settings from this module.
        from app.vault import ModelKeyVault

        try:
            return ModelKeyVault(self).has_stored_key()
        except Exception:
            return False

    @property
    def has_model_key(self) -> bool:
        """True when an env key or the encrypted model vault has a key. Does not log the key."""
        if self.env_model_key():
            return True
        return self._vault_has_model_key()

    def repo_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            path = self.repo_root() / path
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
