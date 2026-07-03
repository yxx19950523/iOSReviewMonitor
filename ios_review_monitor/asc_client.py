from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
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
        params = params or {}
        response = self._request(path, params)
        if response.status_code >= 400 and "sort" in params:
            detail = self._response_detail(response)
            if self._is_sort_parameter_error(detail):
                retry_params = dict(params)
                retry_params.pop("sort", None)
                response = self._request(path, retry_params)

        if response.status_code >= 400:
            detail = self._response_detail(response)
            raise ASCError(
                f"App Store Connect API 请求失败：HTTP {response.status_code}；"
                f"接口：{path}；参数：{params}；详情：{detail}"
            )
        return response.json()

    def _request(self, path: str, params: dict[str, Any]) -> requests.Response:
        response = requests.get(
            f"{API_ROOT}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._jwt()}"},
            timeout=30,
        )
        return response

    def _response_detail(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text

    def _is_sort_parameter_error(self, detail: Any) -> bool:
        text = str(detail).lower()
        return "sort" in text and "parameter" in text and ("not allowed" in text or "illegal" in text)

    def _is_in_review_state(self, state: Any) -> bool:
        text = str(state or "").upper()
        return text == "IN_REVIEW" or "IN_REVIEW" in text

    def _is_waiting_review_state(self, state: Any) -> bool:
        text = str(state or "").upper()
        return text == "WAITING_FOR_REVIEW" or "WAITING" in text or "READY_FOR_REVIEW" in text

    def _is_active_review_state(self, state: Any) -> bool:
        return self._is_in_review_state(state) or self._is_waiting_review_state(state)

    def _status_priority(self, item: dict[str, Any]) -> int:
        state = item.get("state")
        if self._is_in_review_state(state):
            return 40
        if self._is_waiting_review_state(state):
            return 30
        text = str(state or "").upper()
        if any(token in text for token in ("APPROVED", "ACCEPTED", "COMPLETE", "COMPLETED", "READY_FOR_SALE")):
            return 20
        if text in {"NOT_IN_REVIEW", "NOT_FOUND"}:
            return 5
        return 0

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

    def app_exists(self, app_id: str) -> dict[str, Any]:
        app_id = app_id.strip()
        if not app_id:
            raise ASCError("App ID 为空。")
        data = self._get(f"/apps/{app_id}")
        item = data.get("data") or {}
        attrs = item.get("attributes") or {}
        return {
            "id": item.get("id", app_id),
            "name": attrs.get("name", ""),
            "bundle_id": attrs.get("bundleId", ""),
            "sku": attrs.get("sku", ""),
        }

    def app_review_status(self, app_id: str | None = None) -> dict[str, Any]:
        app_id = (app_id or "").strip() or self.resolve_app_id()
        submission_status = self.app_review_submission_status(app_id)
        if submission_status:
            return submission_status

        data = self._get(
            f"/apps/{app_id}/appStoreVersions",
            {
                "filter[platform]": "IOS",
                "sort": "-createdDate",
                "limit": 10,
            },
        )
        items = self._sort_items(data.get("data") or [], "createdDate", reverse=True)
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

    def unified_review_status(self, app_id: str | None = None) -> dict[str, Any]:
        app_id = (app_id or "").strip() or self.resolve_app_id()
        candidates: list[dict[str, Any]] = []
        try:
            candidates.append(self.app_review_status(app_id))
        except Exception as exc:
            candidates.append({
                "app_id": app_id,
                "monitor_type": "app_review",
                "name": "App 提审",
                "version": "--",
                "state": "UNKNOWN",
                "source": f"app_review_error:{exc}",
            })
        try:
            candidates.append(self.product_page_optimization_status(app_id))
        except Exception as exc:
            candidates.append({
                "app_id": app_id,
                "monitor_type": "product_page_optimization",
                "name": "产品页面优化",
                "version": "--",
                "state": "UNKNOWN",
                "source": f"ppo_error:{exc}",
            })

        chosen = sorted(candidates, key=self._status_priority, reverse=True)[0]
        detail = "；".join(
            f"{item.get('name') or item.get('monitor_type')}={item.get('state', 'UNKNOWN')}({item.get('source', '')})"
            for item in candidates
        )
        chosen = dict(chosen)
        chosen["monitor_type"] = "unified_review"
        chosen["detail"] = detail
        return chosen

    def app_review_submission_status(self, app_id: str) -> dict[str, Any] | None:
        submissions = self._list_review_submissions(app_id)
        for submission, _included in submissions:
            attrs = submission.get("attributes") or {}
            state = attrs.get("state") or "UNKNOWN"
            if not self._is_active_review_state(state):
                continue
            submission_id = str(submission.get("id", ""))
            submitted_date = attrs.get("submittedDate") or attrs.get("createdDate") or ""
            return {
                "app_id": app_id,
                "monitor_type": "app_review",
                "name": "App 提审",
                "version_id": submission_id,
                "version": submission_id or "--",
                "state": state,
                "source": "review_submission",
                "created_date": submitted_date,
                "copyright": "",
            }
        return None

    def product_page_optimization_status(self, app_id: str | None = None) -> dict[str, Any]:
        app_id = (app_id or "").strip() or self.resolve_app_id()
        review_status = self.product_page_optimization_review_status(app_id)
        if review_status:
            return review_status
        experiment_status = self.product_page_optimization_experiment_status(app_id)
        if experiment_status:
            return experiment_status

        return {
            "app_id": app_id,
            "monitor_type": "product_page_optimization",
            "name": "产品页面优化",
            "version": "--",
            "state": "NOT_IN_REVIEW",
            "source": "review_submission_scan",
            "experiment_id": "",
            "experiment_name": "未发现正在审核的产品页面优化",
        }

    def product_page_optimization_experiment_status(self, app_id: str) -> dict[str, Any] | None:
        experiments = self._list_product_page_optimizations(app_id)
        if not experiments:
            return None

        active = []
        for item in experiments:
            attrs = item.get("attributes") or {}
            state = attrs.get("state") or "UNKNOWN"
            if self._is_active_review_state(state):
                active.append(item)

        chosen = self._sort_items(active, "createdDate", reverse=True)[0] if active else None
        if not chosen:
            return None

        attrs = chosen.get("attributes") or {}
        relationships = chosen.get("relationships") or {}
        app_store_version = relationships.get("appStoreVersion", {}).get("data") or {}
        return {
            "app_id": app_id,
            "monitor_type": "product_page_optimization",
            "name": attrs.get("name") or "产品页面优化",
            "version": app_store_version.get("id") or "--",
            "state": attrs.get("state", "UNKNOWN"),
            "source": "active_experiment",
            "experiment_id": chosen.get("id", ""),
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
                if not self._is_active_review_state(state):
                    continue
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

            if self._is_active_review_state(submission_state) and self._submission_mentions_ppo(submission, included, experiment_ids):
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
                sorted_submissions = self._sort_items(submissions, "submittedDate", reverse=True)
                return [(item, included) for item in sorted_submissions]
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
                return self._sort_items(items, "createdDate", reverse=True)
        except Exception as exc:
            errors.append(str(exc))

        versions = self._get(
            f"/apps/{app_id}/appStoreVersions",
            {"filter[platform]": "IOS", "sort": "-createdDate", "limit": 10},
        ).get("data") or []
        for version in self._sort_items(versions, "createdDate", reverse=True)[:5]:
            version_id = version.get("id")
            if not version_id:
                continue
            try:
                data = self._get(f"/appStoreVersions/{version_id}/appStoreVersionExperiments", params)
                items = data.get("data") or []
                if items:
                    return self._sort_items(items, "createdDate", reverse=True)
            except Exception as exc:
                errors.append(str(exc))

        return []

    def _sort_items(self, items: list[dict[str, Any]], attr: str, reverse: bool = False) -> list[dict[str, Any]]:
        def key(item: dict[str, Any]) -> tuple[int, str]:
            attrs = item.get("attributes") or {}
            value = attrs.get(attr) or ""
            parsed = self._parse_date(value)
            if parsed:
                return (1, parsed)
            return (0, str(value))

        return sorted(items, key=key, reverse=reverse)

    def _parse_date(self, value: Any) -> str:
        if not value:
            return ""
        text = str(value).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).isoformat()
        except Exception:
            return str(value)
