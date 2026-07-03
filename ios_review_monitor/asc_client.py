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

    def validate_credentials(self) -> dict[str, Any]:
        data = self._get("/apps", {"limit": 1})
        return {
            "ok": True,
            "app_count_hint": len(data.get("data") or []),
        }

    def app_review_status(self, app_id: str | None = None) -> dict[str, Any]:
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
            "monitor_type": "app_review",
            "name": "App 提审",
            "version_id": item.get("id", ""),
            "version": attrs.get("versionString", "未知版本"),
            "state": attrs.get("appStoreState", "UNKNOWN"),
            "source": "app_store_version",
            "created_date": attrs.get("createdDate", ""),
            "copyright": attrs.get("copyright", ""),
        }

    def product_page_optimization_status(self, app_id: str | None = None) -> dict[str, Any]:
        app_id = (app_id or "").strip() or self.resolve_app_id()
        review_status = self.product_page_optimization_review_status(app_id)
        if review_status:
            return review_status

        experiments = self._list_product_page_optimizations(app_id)
        if not experiments:
            return {
                "app_id": app_id,
                "monitor_type": "product_page_optimization",
                "name": "产品页面优化",
                "version": "--",
                "state": "NOT_FOUND",
                "source": "experiment",
                "experiment_id": "",
                "experiment_name": "未找到产品页面优化",
            }

        item = experiments[0]
        attrs = item.get("attributes") or {}
        relationships = item.get("relationships") or {}
        app_store_version = relationships.get("appStoreVersion", {}).get("data") or {}
        return {
            "app_id": app_id,
            "monitor_type": "product_page_optimization",
            "name": attrs.get("name") or "产品页面优化",
            "version": app_store_version.get("id") or "--",
            "state": attrs.get("state", "UNKNOWN"),
            "source": "experiment",
            "experiment_id": item.get("id", ""),
            "experiment_name": attrs.get("name", ""),
        }

    def product_page_optimization_review_status(self, app_id: str) -> dict[str, Any] | None:
        submissions = self._list_review_submissions(app_id)
        if not submissions:
            return None

        try:
            experiments = self._list_product_page_optimizations(app_id)
        except Exception:
            experiments = []
        experiment_ids = {str(item.get("id", "")) for item in experiments if item.get("id")}

        for submission, included in submissions:
            submission_attrs = submission.get("attributes") or {}
            submission_state = submission_attrs.get("state") or "UNKNOWN"
            submission_id = str(submission.get("id", ""))
            items = self._review_submission_items(submission, included)
            for item in items:
                if not self._looks_like_ppo_review_item(item, experiment_ids):
                    continue
                attrs = item.get("attributes") or {}
                relationships = item.get("relationships") or {}
                state = attrs.get("state") or submission_state
                name = attrs.get("name") or attrs.get("type") or "产品页面优化"
                related_id = self._relationship_id(relationships)
                return {
                    "app_id": app_id,
                    "monitor_type": "product_page_optimization",
                    "name": str(name),
                    "version": related_id or submission_id or "--",
                    "state": state,
                    "source": "review_submission_item",
                    "experiment_id": related_id,
                    "review_submission_id": submission_id,
                }

            if self._submission_mentions_ppo(submission, included, experiment_ids):
                return {
                    "app_id": app_id,
                    "monitor_type": "product_page_optimization",
                    "name": "产品页面优化",
                    "version": submission_id or "--",
                    "state": submission_state,
                    "source": "review_submission",
                    "experiment_id": "",
                    "review_submission_id": submission_id,
                }

        return None

    def _list_review_submissions(self, app_id: str) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        attempts = [
            (f"/apps/{app_id}/reviewSubmissions", {"limit": 10, "sort": "-submittedDate", "include": "items"}),
            ("/reviewSubmissions", {"filter[app]": app_id, "limit": 10, "sort": "-submittedDate", "include": "items"}),
            ("/reviewSubmissions", {"filter[app]": app_id, "limit": 10, "include": "items"}),
        ]
        for path, params in attempts:
            try:
                data = self._get(path, params)
            except Exception:
                continue
            submissions = data.get("data") or []
            included = data.get("included") or []
            if submissions:
                return [(item, included) for item in submissions]
        return []

    def _review_submission_items(self, submission: dict[str, Any], included: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = [item for item in included if "reviewSubmissionItem" in str(item.get("type", ""))]
        if items:
            return items

        submission_id = submission.get("id")
        if not submission_id:
            return []
        for path in (
            f"/reviewSubmissions/{submission_id}/items",
            f"/reviewSubmissions/{submission_id}/reviewSubmissionItems",
        ):
            try:
                data = self._get(path, {"limit": 50})
            except Exception:
                continue
            items = data.get("data") or []
            if items:
                return items
        return []

    def _looks_like_ppo_review_item(self, item: dict[str, Any], experiment_ids: set[str]) -> bool:
        blob = self._json_blob(item)
        if any(experiment_id and experiment_id in blob for experiment_id in experiment_ids):
            return True
        keywords = (
            "APP_STORE_VERSION_EXPERIMENT",
            "APPSTOREVERSIONEXPERIMENT",
            "PRODUCT_PAGE_OPTIMIZATION",
            "PRODUCTPAGEOPTIMIZATION",
            "PRODUCT_PAGE",
            "PPO",
        )
        return any(keyword in blob.upper() for keyword in keywords)

    def _submission_mentions_ppo(
        self,
        submission: dict[str, Any],
        included: list[dict[str, Any]],
        experiment_ids: set[str],
    ) -> bool:
        blob = self._json_blob({"submission": submission, "included": included})
        if any(experiment_id and experiment_id in blob for experiment_id in experiment_ids):
            return True
        return "APP_STORE_VERSION_EXPERIMENT" in blob.upper() or "PRODUCT_PAGE_OPTIMIZATION" in blob.upper()

    def _relationship_id(self, relationships: dict[str, Any]) -> str:
        for relation in relationships.values():
            data = relation.get("data") if isinstance(relation, dict) else None
            if isinstance(data, dict) and data.get("id"):
                return str(data["id"])
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("id"):
                        return str(item["id"])
        return ""

    def _json_blob(self, value: Any) -> str:
        import json

        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)

    def _list_product_page_optimizations(self, app_id: str) -> list[dict[str, Any]]:
        params = {"sort": "-createdDate", "limit": 10, "include": "appStoreVersion"}
        errors: list[str] = []

        try:
            data = self._get(f"/apps/{app_id}/appStoreVersionExperiments", params)
            items = data.get("data") or []
            if items:
                return items
        except Exception as exc:
            errors.append(str(exc))

        versions = self._get(
            f"/apps/{app_id}/appStoreVersions",
            {"filter[platform]": "IOS", "sort": "-createdDate", "limit": 5},
        ).get("data") or []
        for version in versions:
            version_id = version.get("id")
            if not version_id:
                continue
            try:
                data = self._get(f"/appStoreVersions/{version_id}/appStoreVersionExperiments", params)
                items = data.get("data") or []
                if items:
                    return items
            except Exception as exc:
                errors.append(str(exc))

        return []
