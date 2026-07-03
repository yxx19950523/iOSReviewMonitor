from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import requests


API_ROOT = "https://api.appstoreconnect.apple.com/v1"


class ASCError(RuntimeError):
    pass


@dataclass
class ASCConfig:
    key_id: str
    issuer_id: str
    p8_path: str
    app_id: str = ""
    bundle_id: str = ""


class AppStoreConnectClient:
    def __init__(self, config: ASCConfig) -> None:
        self.config = config
        self._token = ""
        self._token_expires_at = 0.0

    def _jwt(self) -> str:
        now = int(time.time())
        if self._token and now < self._token_expires_at - 60:
            return self._token

        p8 = Path(self.config.p8_path).expanduser()
        if not p8.exists():
            raise ASCError("找不到 .p8 私钥文件，请重新选择文件。")

        payload = {
            "iss": self.config.issuer_id.strip(),
            "iat": now,
            "exp": now + 20 * 60,
            "aud": "appstoreconnect-v1",
        }
        headers = {"kid": self.config.key_id.strip(), "typ": "JWT"}
        self._token = jwt.encode(payload, p8.read_text("utf-8"), algorithm="ES256", headers=headers)
        self._token_expires_at = now + 20 * 60
        return self._token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.get(
            f"{API_ROOT}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {self._jwt()}"},
            timeout=30,
        )
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise ASCError(f"App Store Connect API 请求失败：HTTP {response.status_code} {detail}")
        return response.json()

    def resolve_app_id(self) -> str:
        if self.config.app_id.strip():
            return self.config.app_id.strip()
        bundle_id = self.config.bundle_id.strip()
        if not bundle_id:
            raise ASCError("请填写 App ID，或填写 Bundle ID 用于自动查找。")
        data = self._get("/apps", {"filter[bundleId]": bundle_id, "limit": 1})
        items = data.get("data") or []
        if not items:
            raise ASCError(f"没有找到 Bundle ID：{bundle_id}")
        return str(items[0]["id"])

    def latest_ios_version(self, app_id: str | None = None) -> dict[str, Any]:
        app_id = (app_id or "").strip() or self.resolve_app_id()
        data = self._get(
            f"/apps/{app_id}/appStoreVersions",
            {
                "filter[platform]": "IOS",
                "sort": "-createdDate",
                "limit": 1,
            },
        )
        items = data.get("data") or []
        if not items:
            raise ASCError("没有找到 iOS App Store 版本。")
        item = items[0]
        attrs = item.get("attributes") or {}
        return {
            "app_id": app_id,
            "version_id": item.get("id", ""),
            "version": attrs.get("versionString", "未知版本"),
            "state": attrs.get("appStoreState", "UNKNOWN"),
            "created_date": attrs.get("createdDate", ""),
            "copyright": attrs.get("copyright", ""),
        }
