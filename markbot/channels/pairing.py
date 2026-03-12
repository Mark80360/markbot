"""Pairing manager for channel access requests."""

import json
from datetime import datetime, timedelta
from pathlib import Path


class PairingManager:
    """Manages pairing requests for channel access."""

    def __init__(self, workspace: Path):
        self.pairing_file = Path.home() / ".markbot" / "gateway" / "pairings.json"
        self.pairing_file.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self.pairing_file.exists():
            return {}
        with open(self.pairing_file) as f:
            return json.load(f)

    def _save(self, data: dict) -> None:
        with open(self.pairing_file, "w") as f:
            json.dump(data, f, indent=2)

    def get_pairing(self, code: str) -> dict | None:
        data = self._load()
        return data.get(code)

    def list_pairings(self) -> list[dict]:
        data = self._load()
        return [{"code": k, **v} for k, v in data.items()]

    def approve_pairing(self, code: str) -> bool:
        data = self._load()
        if code not in data:
            return False
        del data[code]
        self._save(data)
        return True

    def cancel_pairing(self, code: str) -> bool:
        data = self._load()
        if code not in data:
            return False
        del data[code]
        self._save(data)
        return True

    def cleanup_expired(self, hours: int = 24) -> None:
        data = self._load()
        now = datetime.now()
        expired = []
        for code, info in data.items():
            created = datetime.fromisoformat(info.get("created_at", ""))
            if now - created > timedelta(hours=hours):
                expired.append(code)
        for code in expired:
            del data[code]
        if expired:
            self._save(data)
