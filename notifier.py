"""
PetKit -> Telegram repeat notifier.

Polls PetKit cloud for multiple device states (litter box fullness, feeder
food / desiccant, water fountain water / filter / battery) and sends repeating
Telegram alerts on a backoff schedule until the underlying issue is resolved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp


LOG = logging.getLogger("petpoke")

BACKOFF_MINUTES: tuple[int, ...] = (15, 30, 60, 120)
BACKOFF_CAP_MINUTES: int = 120

# label_active = ข้อความตอนเริ่ม / ส่งซ้ำ
# label_cleared = ข้อความตอนหายเป็นปกติ
RULE_LABELS: dict[str, tuple[str, str, str]] = {
    "box_full": ("\U0001F6A8", "ถังขยะเต็มแล้ว", "ถังขยะถูกเทแล้ว"),
    "food_empty": ("\U0001F37D️", "อาหารหมดแล้ว", "เติมอาหารแล้ว"),
    "food_low": ("\U0001F37D️", "อาหารใกล้หมด", "อาหารกลับมาเต็มแล้ว"),
    "water_low": ("\U0001F6B0", "น้ำในน้ำพุใกล้หมด", "เติมน้ำเรียบร้อย"),
    "filter_change": ("\U0001F9FD", "ใกล้ต้องเปลี่ยน filter น้ำพุแล้ว", "เปลี่ยน filter เรียบร้อย"),
    "battery_low": ("\U0001F50B", "แบตเตอรี่ต่ำ", "แบตเตอรี่กลับมาปกติแล้ว"),
    "device_error": ("⚠️", "พบความผิดปกติของอุปกรณ์", "อุปกรณ์กลับสู่สภาพปกติแล้ว"),
    "device_offline": ("\U0001F4E1", "อุปกรณ์ออฟไลน์/ไม่ตอบสนอง", "อุปกรณ์กลับมาออนไลน์แล้ว"),
    "pet_error": ("\U0001F198", "ตรวจพบความผิดปกติของน้อง! ตรวจสอบด่วน", "น้องปลอดภัยแล้ว"),
}

# One product photo per device (PetKit Shopify CDN URLs).
# All alerts from the same device share the same image so the message
# clearly points to which physical device the alert refers to.
# To disable photos, set DEVICE_PHOTOS = {} or remove specific entries.
# To override, replace any URL with your own (must be a direct image URL).
DEVICE_PHOTOS: dict[str, str] = {
    "Litter": "https://petkit.com/cdn/shop/files/puramax-2-automatic-cat-litter-box-app-control-2-year-warranty.png",
    "Feeder": "https://petkit.com/cdn/shop/files/yumshare-solo-2-automatic-cat-feeder-app-control-2-year-warranty.png",
    "WaterFountain": "https://petkit.com/cdn/shop/files/eversweet-max-cordless-pet-water-fountain-app-control.png",
}


def photo_for_alert(alert: "Alert") -> str | None:
    return DEVICE_PHOTOS.get(alert.device_class)


@dataclass
class Config:
    petkit_username: str
    petkit_password: str
    petkit_region: str
    petkit_timezone: str
    telegram_bot_token: str
    telegram_chat_id: str
    state_file: Path
    debug_log_raw: bool

    @classmethod
    def from_env(cls) -> "Config":
        missing = [
            key
            for key in (
                "PETKIT_USERNAME",
                "PETKIT_PASSWORD",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID",
            )
            if not os.environ.get(key)
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        return cls(
            petkit_username=os.environ["PETKIT_USERNAME"],
            petkit_password=os.environ["PETKIT_PASSWORD"],
            petkit_region=os.environ.get("PETKIT_REGION", "TH"),
            petkit_timezone=os.environ.get("PETKIT_TIMEZONE", "Asia/Bangkok"),
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            state_file=Path(os.environ.get("STATE_FILE", "state.json")),
            debug_log_raw=_env_bool("DEBUG_LOG_RAW", default=False),
        )


@dataclass
class AlertState:
    """Per-alert tracking persisted across runs (one row per device:code)."""

    device_name: str = ""
    is_active: bool = False
    alert_count: int = 0
    first_alert_ts: int | None = None
    last_alert_ts: int | None = None
    next_alert_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_name": self.device_name,
            "is_active": self.is_active,
            "alert_count": self.alert_count,
            "first_alert_ts": self.first_alert_ts,
            "last_alert_ts": self.last_alert_ts,
            "next_alert_at": self.next_alert_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlertState":
        # Back-compat with the original schema (was_full).
        is_active = data.get("is_active")
        if is_active is None:
            is_active = data.get("was_full", False)
        return cls(
            device_name=data.get("device_name", ""),
            is_active=bool(is_active),
            alert_count=int(data.get("alert_count", 0)),
            first_alert_ts=_maybe_int(data.get("first_alert_ts")),
            last_alert_ts=_maybe_int(data.get("last_alert_ts")),
            next_alert_at=_maybe_int(data.get("next_alert_at")),
        )


@dataclass
class Alert:
    """Snapshot of one alert condition for one device, at one poll."""

    device_id: str
    device_name: str
    code: str
    is_active: bool
    device_class: str = ""  # e.g. Litter, Feeder, WaterFountain — used for photo lookup
    detail: str = ""
    info_lines: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.device_id}:{self.code}"


@dataclass
class StateStore:
    alerts: dict[str, AlertState] = field(default_factory=dict)

    def get(self, key: str) -> AlertState:
        return self.alerts.setdefault(key, AlertState())

    def reset(self, key: str) -> None:
        self.alerts[key] = AlertState()

    def to_dict(self) -> dict[str, Any]:
        return {key: state.to_dict() for key, state in self.alerts.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateStore":
        alerts: dict[str, AlertState] = {}
        for raw_key, payload in (data or {}).items():
            key = str(raw_key)
            # Migrate legacy keys that were just <device_id> (the original
            # single-purpose litter box schema).
            if ":" not in key:
                key = f"{key}:box_full"
            alerts[key] = AlertState.from_dict(payload or {})
        return cls(alerts=alerts)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_state(path: Path) -> StateStore:
    if not path.exists():
        return StateStore()
    try:
        return StateStore.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Could not read state file %s (%s); starting empty", path, exc)
        return StateStore()


def save_state(path: Path, store: StateStore) -> None:
    payload = json.dumps(store.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(payload + "\n", encoding="utf-8")


def backoff_minutes_for(alert_count: int) -> int:
    """Backoff after the Nth alert has been sent (count >= 1)."""
    if alert_count <= 0:
        return BACKOFF_MINUTES[0]
    idx = min(alert_count - 1, len(BACKOFF_MINUTES) - 1)
    return (
        BACKOFF_MINUTES[idx]
        if alert_count <= len(BACKOFF_MINUTES)
        else BACKOFF_CAP_MINUTES
    )


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on", "full"}:
            return True
        if lowered in {"false", "0", "no", "n", "off", "empty"}:
            return False
    return None


def _read_attr(obj: Any, *names: str) -> Any:
    for name in names:
        if obj is None:
            return None
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _dump_raw(device: Any) -> str:
    try:
        if hasattr(device, "model_dump_json"):
            return device.model_dump_json(indent=2)
        if hasattr(device, "model_dump"):
            return json.dumps(
                device.model_dump(), default=str, indent=2, ensure_ascii=False
            )
        if hasattr(device, "__dict__"):
            return json.dumps(
                vars(device), default=str, indent=2, ensure_ascii=False
            )
    except Exception as exc:
        return f"<could not dump device: {exc}>"
    return repr(device)


def extract_alerts(device: Any, debug_log_raw: bool) -> list[Alert]:
    """Return zero or more Alert snapshots for a single pypetkitapi device."""
    cls = type(device).__name__
    device_id_raw = _read_attr(device, "id", "device_id", "deviceId")
    if device_id_raw is None:
        return []
    device_id = str(device_id_raw)
    name = str(
        _read_attr(device, "name", "device_name", "deviceName") or device_id
    )

    if debug_log_raw:
        LOG.info("[DEBUG_LOG_RAW] %s (%s) dump:\n%s", name, cls, _dump_raw(device))

    if cls == "Litter":
        return _extract_litter_alerts(device_id, name, device)
    if cls == "Feeder":
        return _extract_feeder_alerts(device_id, name, device)
    if cls == "WaterFountain":
        return _extract_water_alerts(device_id, name, device)
    return []


def _extract_common_problem_alerts(
    device: Any, device_id: str, name: str, device_class: str
) -> list[Alert]:
    """
    Cross-device alerts: hardware errors, offline state, and (litter only)
    pet_error. Reads several optional fields defensively so a missing one
    just gets reported as "no signal" (not a false-active alert).
    """
    alerts: list[Alert] = []
    state = _read_attr(device, "state") or {}

    # --- device_error: PetKit returns explicit error info -----------------
    error_code = _read_attr(state, "error_code")
    error_msg = _read_attr(state, "error_msg")
    error_level = _read_attr(state, "error_level")
    breakdown = _read_attr(device, "breakdown_warning")

    has_state_error = bool(error_code) or bool(error_msg)
    has_breakdown = isinstance(breakdown, (int, float)) and breakdown > 0
    is_errored = has_state_error or has_breakdown

    info: list[str] = []
    if error_code not in (None, 0, "", "0"):
        info.append(f"\U0001F6A8 รหัส error: <b>{_html_escape(str(error_code))}</b>")
    if error_msg:
        info.append(f"\U0001F4DD ข้อความ: <b>{_html_escape(str(error_msg))}</b>")
    if error_level not in (None, 0, "", "0"):
        info.append(f"\U0001F4CA ระดับ: <b>{_html_escape(str(error_level))}</b>")
    if has_breakdown:
        info.append(f"⚙️ breakdown_warning = <b>{int(breakdown)}</b>")

    alerts.append(
        Alert(
            device_id=device_id,
            device_name=name,
            device_class=device_class,
            code="device_error",
            is_active=is_errored,
            detail=(
                f"error_code={error_code} error_msg={error_msg} "
                f"breakdown_warning={breakdown}"
            ),
            info_lines=info,
        )
    )

    # --- device_offline: PetKit cloud explicitly says device dropped off --
    offline_ts = _read_attr(state, "offline_time")
    is_offline = offline_ts is not None and offline_ts != 0
    offline_info: list[str] = []
    if is_offline:
        try:
            offline_dt = datetime.fromtimestamp(int(offline_ts), tz=timezone.utc)
            offline_info.append(
                f"\U0001F551 หลุดเมื่อ: <b>{offline_dt.strftime('%H:%M %d/%m/%Y UTC')}</b>"
            )
        except (TypeError, ValueError, OSError):
            offline_info.append(f"\U0001F551 offline_time = {offline_ts}")
    alerts.append(
        Alert(
            device_id=device_id,
            device_name=name,
            device_class=device_class,
            code="device_offline",
            is_active=is_offline,
            detail=f"offline_time={offline_ts}",
            info_lines=offline_info,
        )
    )

    # --- pet_error: Pura MAX safety sensor -----------------------------
    if device_class == "Litter":
        pet_error = _coerce_bool(_read_attr(state, "pet_error"))
        if pet_error is not None:
            alerts.append(
                Alert(
                    device_id=device_id,
                    device_name=name,
                    device_class=device_class,
                    code="pet_error",
                    is_active=pet_error,
                    detail=f"pet_error={pet_error}",
                    info_lines=[
                        "\U0001F43E ตรวจสอบน้องในกล่องทันที — อาจติดหรือมีปัญหาด้านความปลอดภัย",
                    ]
                    if pet_error
                    else [],
                )
            )

    return alerts


def _extract_litter_alerts(device_id: str, name: str, device: Any) -> list[Alert]:
    state = _read_attr(device, "state", "device_detail", "deviceDetail")
    alerts: list[Alert] = []

    raw = _read_attr(state, "box_full", "boxFull", "is_full", "isFull")
    is_full = _coerce_bool(raw)
    if is_full is None:
        ratio = _read_attr(state, "litter_percent", "sand_percent")
        if isinstance(ratio, (int, float)):
            is_full = ratio >= 90
    if is_full is not None:
        alerts.append(
            Alert(
                device_id=device_id,
                device_name=name,
                device_class="Litter",
                code="box_full",
                is_active=is_full,
                detail=f"box_full={raw}",
                info_lines=[],
            )
        )
    else:
        LOG.warning("Litter %s (%s): no box_full field found", name, device_id)

    alerts.extend(_extract_common_problem_alerts(device, device_id, name, "Litter"))
    return alerts


def _extract_feeder_alerts(device_id: str, name: str, device: Any) -> list[Alert]:
    """
    Feeder alerts: food level only.

    food convention (PetKit cloud): 0 = empty, 1 = low, 2 = ok/full.
    Treat 0 as `food_empty` and 1 as `food_low`. They are mutually exclusive
    so the inactive condition resets both codes.
    """
    alerts: list[Alert] = []
    state = _read_attr(device, "state")

    food = _read_attr(state, "food")
    feed_state = _read_attr(state, "feed_state") or {}
    eat_count = _read_attr(feed_state, "eat_count")

    food_info: list[str] = []
    if isinstance(eat_count, (int, float)) and eat_count > 0:
        food_info.append(
            f"\U0001F43E กินไปแล้ววันนี้: <b>{int(eat_count)} ครั้ง</b>"
        )

    if isinstance(food, (int, float)):
        is_empty = food <= 0
        is_low = food == 1
        alerts.append(
            Alert(
                device_id=device_id,
                device_name=name,
                device_class="Feeder",
                code="food_empty",
                is_active=is_empty,
                detail=f"food={food}",
                info_lines=list(food_info),
            )
        )
        alerts.append(
            Alert(
                device_id=device_id,
                device_name=name,
                device_class="Feeder",
                code="food_low",
                is_active=is_low,
                detail=f"food={food}",
                info_lines=list(food_info),
            )
        )

    alerts.extend(_extract_common_problem_alerts(device, device_id, name, "Feeder"))
    return alerts


def _extract_water_alerts(device_id: str, name: str, device: Any) -> list[Alert]:
    """
    WaterFountain alerts.

    Field locations on EVERSWEET-class devices are mostly top-level
    (not inside `state`). Battery percentage lives in `electricity.battery_percent`.
    """
    alerts: list[Alert] = []
    filter_pct = _read_attr(device, "filter_percent")
    filter_warn = _read_attr(device, "filter_warning")
    filter_expected = _read_attr(device, "filter_expected_days")
    low_batt = _read_attr(device, "low_battery")
    lack = _read_attr(device, "lack_warning")
    electricity = _read_attr(device, "electricity") or {}
    battery_pct = _read_attr(electricity, "battery_percent")

    # water_low: alert is self-explanatory; no useful numeric exposed.
    if isinstance(lack, (int, float)):
        alerts.append(
            Alert(
                device_id=device_id,
                device_name=name,
                device_class="WaterFountain",
                code="water_low",
                is_active=lack > 0,
                detail=f"lack_warning={lack}",
                info_lines=[],
            )
        )

    # filter_change set the device_class below; first add filter_change alert.
    if filter_pct is not None or filter_warn is not None:
        needs_change = False
        details: list[str] = []
        filter_info: list[str] = []
        if isinstance(filter_warn, (int, float)) and filter_warn > 0:
            needs_change = True
            details.append(f"filter_warning={filter_warn}")
        if isinstance(filter_pct, (int, float)):
            details.append(f"filter_percent={filter_pct}")
            if filter_pct < 10:
                needs_change = True
            if 0 <= filter_pct <= 100:
                filter_info.append(
                    f"\U0001F9FD Filter เหลือ: <b>{int(filter_pct)}%</b>"
                )
        if isinstance(filter_expected, (int, float)) and filter_expected > 0:
            filter_info.append(
                f"\U0001F4C5 อยู่ได้อีก: <b>{int(filter_expected)} วัน</b>"
            )
        alerts.append(
            Alert(
                device_id=device_id,
                device_name=name,
                device_class="WaterFountain",
                code="filter_change",
                is_active=needs_change,
                detail=", ".join(details),
                info_lines=filter_info,
            )
        )

    # battery_low: battery%, estimated remaining days, plug status.
    if isinstance(low_batt, (int, float)):
        battery_info: list[str] = []
        if isinstance(battery_pct, (int, float)) and 0 <= battery_pct <= 100:
            battery_info.append(
                f"\U0001F50B แบตเตอรี่: <b>{int(battery_pct)}%</b>"
            )
        expected_days = _read_attr(device, "expected_use_electricity")
        if isinstance(expected_days, (int, float)) and expected_days > 0:
            battery_info.append(
                f"\U0001F4C5 อยู่ได้อีกประมาณ: <b>{int(expected_days)} วัน</b>"
            )
        supply_v = _read_attr(electricity, "supply_voltage")
        if isinstance(supply_v, (int, float)):
            if supply_v > 0:
                battery_info.append("\U0001F50C เสียบสายชาร์จอยู่")
            else:
                battery_info.append("\U0001F50B โหมดไร้สาย (ไม่ได้เสียบสาย)")
        alerts.append(
            Alert(
                device_id=device_id,
                device_name=name,
                device_class="WaterFountain",
                code="battery_low",
                is_active=low_batt > 0,
                detail=(
                    f"low_battery={low_batt} battery_percent={battery_pct} "
                    f"expected_days={expected_days}"
                ),
                info_lines=battery_info,
            )
        )

    alerts.extend(
        _extract_common_problem_alerts(device, device_id, name, "WaterFountain")
    )
    return alerts


async def fetch_alerts(config: Config) -> list[Alert]:
    """Login to PetKit and collect alerts from every supported device."""
    try:
        from pypetkitapi import PetKitClient  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pypetkitapi is not installed. Run: pip install -r requirements.txt"
        ) from exc

    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = PetKitClient(
            username=config.petkit_username,
            password=config.petkit_password,
            region=config.petkit_region,
            timezone=config.petkit_timezone,
            session=session,
        )
        await client.get_devices_data()

        entities = getattr(client, "petkit_entities", None) or {}
        iterable = (
            entities.values() if isinstance(entities, dict) else entities or []
        )

        all_alerts: list[Alert] = []
        for device in iterable:
            all_alerts.extend(extract_alerts(device, config.debug_log_raw))
        return all_alerts


async def send_telegram(
    session: aiohttp.ClientSession,
    config: Config,
    text: str,
    photo_url: str | None = None,
) -> bool:
    """
    Send a Telegram message, optionally with a photo.

    When `photo_url` is provided, uses sendPhoto with the text as caption.
    If Telegram rejects the photo (e.g. URL unreachable), falls back to a
    plain text message automatically.
    """
    if photo_url:
        api_url = (
            f"https://api.telegram.org/bot{config.telegram_bot_token}/sendPhoto"
        )
        payload = {
            "chat_id": config.telegram_chat_id,
            "photo": photo_url,
            "caption": text,
            "parse_mode": "HTML",
        }
    else:
        api_url = (
            f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
        )
        payload = {
            "chat_id": config.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    try:
        async with session.post(api_url, json=payload) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            LOG.error("Telegram returned %s: %s", resp.status, body[:300])
            if photo_url:
                LOG.info("Retrying as text-only message")
                return await send_telegram(session, config, text, photo_url=None)
            return False
    except aiohttp.ClientError as exc:
        LOG.error("Telegram request failed: %s", exc)
        if photo_url:
            return await send_telegram(session, config, text, photo_url=None)
        return False


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def format_duration_th(ms: int) -> str:
    """Render a millisecond span as a short Thai duration string."""
    seconds = max(0, ms // 1000)
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} วัน")
    if hours:
        parts.append(f"{hours} ชม.")
    if minutes and not days:
        parts.append(f"{minutes} นาที")
    if not parts:
        parts.append("ไม่ถึงนาที")
    return " ".join(parts)


def format_active_message(
    alert: Alert, state: AlertState, now_local: datetime
) -> str:
    emoji, label_active, _ = RULE_LABELS.get(alert.code, ("⚠️", alert.code, alert.code))
    when = now_local.strftime("%H:%M น. (%d/%m/%Y)")

    lines: list[str] = [
        f"{emoji} <b>{_html_escape(alert.device_name)}</b>",
        label_active,
    ]
    if alert.info_lines:
        lines.append("")
        lines.extend(alert.info_lines)

    # Show how long the problem has been going on if this isn't the first alert.
    if state.alert_count > 1 and state.first_alert_ts:
        now_ms = int(now_local.timestamp() * 1000)
        duration = format_duration_th(now_ms - state.first_alert_ts)
        lines.append("")
        lines.append(f"⏱️ ค้างอยู่นาน: <b>{duration}</b>")

    lines.append(f"แจ้งครั้งที่ <b>{state.alert_count}</b> · {when}")
    return "\n".join(lines)


def format_cleared_message(
    alert: Alert, state: AlertState, now_local: datetime
) -> str:
    _, _, label_cleared = RULE_LABELS.get(alert.code, ("✅", alert.code, alert.code))
    when = now_local.strftime("%H:%M น. (%d/%m/%Y)")

    lines: list[str] = [
        f"✅ <b>{_html_escape(alert.device_name)}</b>",
        f"{label_cleared} · {when}",
    ]
    if state.first_alert_ts:
        now_ms = int(now_local.timestamp() * 1000)
        duration = format_duration_th(now_ms - state.first_alert_ts)
        lines.append(f"⏱️ ปัญหาค้างอยู่นาน: <b>{duration}</b>")
    lines.append(f"(แจ้งเตือนไปทั้งหมด {state.alert_count} ครั้ง)")
    return "\n".join(lines)


async def process_alerts(
    config: Config,
    store: StateStore,
    alerts: list[Alert],
    now_utc: datetime,
    tz: ZoneInfo | timezone,
) -> None:
    if not alerts:
        LOG.warning("No supported devices found on this account")
        return

    now_ms = int(now_utc.timestamp() * 1000)
    now_local = now_utc.astimezone(tz)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        for alert in alerts:
            state = store.get(alert.key)
            state.device_name = alert.device_name

            if alert.is_active:
                await _handle_active(
                    session=session,
                    config=config,
                    alert=alert,
                    state=state,
                    now_ms=now_ms,
                    now_local=now_local,
                )
            else:
                await _handle_cleared(
                    session=session,
                    config=config,
                    alert=alert,
                    state=state,
                    store=store,
                    now_ms=now_ms,
                    now_local=now_local,
                )


async def _handle_active(
    *,
    session: aiohttp.ClientSession,
    config: Config,
    alert: Alert,
    state: AlertState,
    now_ms: int,
    now_local: datetime,
) -> None:
    is_first_alert = not state.is_active or state.alert_count == 0
    due = is_first_alert or (
        state.next_alert_at is not None and now_ms >= state.next_alert_at
    )
    if not due:
        LOG.info(
            "%s [%s] still active; next alert at %s",
            alert.device_name,
            alert.code,
            _format_eta(state.next_alert_at, now_local.tzinfo),
        )
        return

    tentative_count = state.alert_count + 1
    tentative = AlertState(
        device_name=alert.device_name,
        is_active=True,
        alert_count=tentative_count,
        first_alert_ts=state.first_alert_ts or now_ms,
        last_alert_ts=now_ms,
        next_alert_at=now_ms + backoff_minutes_for(tentative_count) * 60_000,
    )
    message = format_active_message(alert, tentative, now_local)
    photo_url = photo_for_alert(alert)

    sent = await send_telegram(session, config, message, photo_url=photo_url)
    if not sent:
        LOG.warning(
            "Telegram send failed for %s [%s]; state not advanced",
            alert.device_name,
            alert.code,
        )
        return

    state.is_active = tentative.is_active
    state.alert_count = tentative.alert_count
    state.first_alert_ts = tentative.first_alert_ts
    state.last_alert_ts = tentative.last_alert_ts
    state.next_alert_at = tentative.next_alert_at
    LOG.info(
        "Alerted %s [%s] count=%d next=+%d min",
        alert.device_name,
        alert.code,
        state.alert_count,
        backoff_minutes_for(state.alert_count),
    )


async def _handle_cleared(
    *,
    session: aiohttp.ClientSession,
    config: Config,
    alert: Alert,
    state: AlertState,
    store: StateStore,
    now_ms: int,
    now_local: datetime,
) -> None:
    if not state.is_active:
        return

    message = format_cleared_message(alert, state, now_local)
    photo_url = photo_for_alert(alert)
    sent = await send_telegram(session, config, message, photo_url=photo_url)
    if sent:
        LOG.info(
            "Cleared %s [%s] after %d alerts",
            alert.device_name,
            alert.code,
            state.alert_count,
        )
    else:
        LOG.warning(
            "Telegram clear message failed for %s [%s]; resetting state anyway",
            alert.device_name,
            alert.code,
        )
    store.reset(alert.key)


def _format_eta(timestamp_ms: int | None, tz: Any) -> str:
    if timestamp_ms is None:
        return "unknown"
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=tz)
    return dt.strftime("%H:%M %d/%m/%Y")


async def main_async() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = Config.from_env()
    except RuntimeError as exc:
        LOG.error(str(exc))
        return 2

    try:
        tz: ZoneInfo | timezone = ZoneInfo(config.petkit_timezone)
    except Exception:
        LOG.warning(
            "Unknown timezone %r (is the 'tzdata' package installed on Windows?); "
            "falling back to UTC",
            config.petkit_timezone,
        )
        try:
            tz = ZoneInfo("UTC")
        except Exception:
            tz = timezone.utc

    now_utc = datetime.now(tz=timezone.utc)

    store = load_state(config.state_file)

    try:
        alerts = await fetch_alerts(config)
    except Exception as exc:
        LOG.exception("PetKit fetch failed: %s", exc)
        return 1

    try:
        await process_alerts(config, store, alerts, now_utc, tz)
    finally:
        save_state(config.state_file, store)

    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
