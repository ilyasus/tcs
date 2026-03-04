from __future__ import annotations

from typing import Any

import requests


class TeslaWallConnectorClient:
    def __init__(self, base_url: str, timeout_seconds: float = 4.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def read_sample(self) -> dict[str, Any]:
        vitals = self._get("/api/1/vitals")
        return {
            "vitals": vitals,
        }
