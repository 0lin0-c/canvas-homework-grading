import tempfile
from pathlib import Path

from app.config import Settings
from app.vault import TokenVault


def test_vault_roundtrip_no_full_token_in_status():
    with tempfile.TemporaryDirectory() as td:
        settings = Settings(
            grading_secret_key="unit-test-secret-key-32bytes-long!!",
            vault_path=str(Path(td) / "tok.enc"),
            canvas_token=None,
        )
        v = TokenVault(settings)
        token = "canvas-secret-token-XYZ9"
        st = v.save(token)
        assert st["present"] is True
        assert st["hint"] == "****XYZ9"
        assert token not in str(st)
        assert v.get_token() == token
        st2 = v.status()
        assert token not in str(st2)
        v.clear()
        assert v.get_token() is None
