from __future__ import annotations

import argparse
import json
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


WAITING_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "READY_FOR_REVIEW",
    "READY_TO_SUBMIT",
    "WAITING_FOR_REVIEW",
    "INVALID_BINARY",
    "NOT_IN_REVIEW",
}
DONE_STATES = {
    "PENDING_DEVELOPER_RELEASE",
    "PENDING_APPLE_RELEASE",
    "PROCESSING_FOR_APP_STORE",
    "READY_FOR_SALE",
    "REJECTED",
    "METADATA_REJECTED",
    "APPROVED",
    "ACCEPTED",
    "RUNNING",
    "COMPLETE",
    "COMPLETED",
    "ENDED",
    "STOPPED",
}
STATE_LABELS = {
    "WAITING_FOR_REVIEW": "等待审核",
    "IN_REVIEW": "正在审核",
    "PENDING_DEVELOPER_RELEASE": "等待开发者发布",
    "PENDING_APPLE_RELEASE": "等待 Apple 发布",
    "PROCESSING_FOR_APP_STORE": "正在上架处理",
    "READY_FOR_SALE": "已完成 / 可销售",
    "REJECTED": "审核完成 / 已拒绝",
    "METADATA_REJECTED": "审核完成 / 元数据被拒",
    "APPROVED": "已审核通过",
    "ACCEPTED": "已审核通过",
    "NOT_IN_REVIEW": "未在审核中",
}


@dataclass
class EmailConfig:
    enabled: bool
    smtp_host: str
    smtp_port: int
    username: str
    password: str
    sender: str
    recipients: list[str]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_text()}] {message}", flush=True)


def classify_state(state: str) -> str:
    normalized = (state or "").upper()
    if normalized == "IN_REVIEW" or "IN_REVIEW" in normalized:
        return "in_review"
    if normalized in DONE_STATES or any(token in normalized for token in ("APPROVED", "ACCEPTED", "COMPLETE", "COMPLETED")):
        return "done"
    if normalized in WAITING_STATES or "WAITING" in normalized or "READY_FOR_REVIEW" in normalized:
        return "waiting"
    return "unknown"


def status_text(state: str) -> str:
    normalized = (state or "").upper()
    return STATE_LABELS.get(normalized, state or "未知")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def load_email_config(config: dict[str, Any]) -> EmailConfig | None:
    raw = config.get("email") or {}
    if not raw.get("enabled"):
        return None
    recipients = raw.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    return EmailConfig(
        enabled=True,
        smtp_host=str(raw.get("smtp_host") or ""),
        smtp_port=int(raw.get("smtp_port") or 465),
        username=str(raw.get("username") or ""),
        password=str(raw.get("password") or ""),
        sender=str(raw.get("from") or raw.get("username") or ""),
        recipients=[str(item) for item in recipients if str(item).strip()],
    )


def send_email(email_config: EmailConfig | None, subject: str, body: str) -> None:
    if not email_config:
        return
    if not all([email_config.smtp_host, email_config.username, email_config.password, email_config.sender, email_config.recipients]):
        log("邮件配置不完整，跳过发送邮件")
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_config.sender
    message["To"] = ", ".join(email_config.recipients)
    message.set_content(body)

    with smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port, timeout=30) as smtp:
        smtp.login(email_config.username, email_config.password)
        smtp.send_message(message)
    log(f"邮件已发送：{subject}")


def notify(config: dict[str, Any], email_config: EmailConfig | None, category: str, account_name: str, result: dict[str, Any]) -> None:
    app_id = str(result.get("app_id") or "--")
    state = str(result.get("state") or "UNKNOWN")
    label = status_text(state)
    source = str(result.get("source") or "")
    detail = str(result.get("detail") or "")

    if config.get("sound_enabled", True):
        from ios_review_monitor.sound import play_sound

        play_sound(category)

    subject = f"iOS 审核提醒：{app_id} {label}"
    body = "\n".join(
        [
            f"账号：{account_name}",
            f"App ID：{app_id}",
            f"状态：{label} ({state})",
            f"来源：{source}",
            f"详情：{detail}",
            f"时间：{now_text()}",
        ]
    )
    send_email(email_config, subject, body)


def check_once(config: dict[str, Any], state: dict[str, str], email_config: EmailConfig | None) -> dict[str, str]:
    from ios_review_monitor.asc_client import ASCConfig, AppStoreConnectClient

    accounts = config.get("accounts") or []
    for account in accounts:
        account_name = str(account.get("name") or "未命名账号")
        app_ids = [str(item).strip() for item in account.get("app_ids", []) if str(item).strip()]
        client = AppStoreConnectClient(
            ASCConfig(
                key_id=str(account.get("key_id") or ""),
                issuer_id=str(account.get("issuer_id") or ""),
                p8_path=str(account.get("p8_path") or ""),
            )
        )

        for app_id in app_ids:
            key = f"{account_name}:{app_id}"
            try:
                result = client.unified_review_status(app_id)
            except Exception as exc:
                log(f"{account_name} / App {app_id} 查询失败：{exc}")
                continue

            raw_state = str(result.get("state") or "UNKNOWN")
            category = classify_state(raw_state)
            label = status_text(raw_state)
            source = str(result.get("source") or "")
            detail = str(result.get("detail") or "")
            log(f"{account_name} / App {app_id}：{label} ({raw_state})，来源：{source}，详情：{detail}")

            previous_category = state.get(key)
            if category != previous_category:
                if category == "in_review":
                    log(f"提醒：{account_name} / App {app_id} 进入正在审核")
                    notify(config, email_config, "in_review", account_name, result)
                elif category == "done":
                    log(f"提醒：{account_name} / App {app_id} 审核流程已完成")
                    notify(config, email_config, "done", account_name, result)
            state[key] = category
    return state


def validate_config(config: dict[str, Any]) -> None:
    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit("配置错误：accounts 不能为空。")
    for index, account in enumerate(accounts, start=1):
        missing = [name for name in ("key_id", "issuer_id", "p8_path") if not str(account.get(name) or "").strip()]
        app_ids = account.get("app_ids")
        if not isinstance(app_ids, list) or not app_ids:
            missing.append("app_ids")
        if missing:
            raise SystemExit(f"配置错误：第 {index} 个账号缺少 {', '.join(missing)}。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mac 终端版 iOS 审核状态监控")
    parser.add_argument("--config", default="scripts/mac_monitor.config.json", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只检查一次后退出")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.exists():
        raise SystemExit(f"找不到配置文件：{config_path}\n请先复制 scripts/mac_monitor.config.example.json 并填好配置。")

    config = load_json(config_path, {})
    validate_config(config)
    email_config = load_email_config(config)
    state_path = Path(config.get("state_file") or "scripts/mac_monitor_state.json").expanduser()
    if not state_path.is_absolute():
        state_path = ROOT / state_path

    interval = max(30, int(config.get("interval_seconds") or 60))
    state = load_json(state_path, {})
    log(f"开始监听，配置：{config_path}，检查间隔：{interval} 秒")

    while True:
        state = check_once(config, state, email_config)
        save_json(state_path, state)
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
