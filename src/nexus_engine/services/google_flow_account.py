"""Encrypted persistence for multi-account Google Flow pool & per-model quotas."""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path

from nexus_engine.paths import (
    GOOGLE_FLOW_ACCOUNT_PATH,
    GOOGLE_FLOW_ACCOUNTS_PATH,
    google_flow_profile_dir,
)
from nexus_engine.services.secret_store import (
    decrypt_secret,
    encrypt_secret,
    write_private_text,
)

logger = logging.getLogger(__name__)

SECRET_FIELDS = ("password", "totp_secret")
COOKIE_FIELD = "session_cookies"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GOOGLE_COOKIE_DOMAINS = ("google.com", "labs.google")
GOOGLE_AUTH_COOKIE_NAMES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PSIDTS",
        "__Secure-3PSIDTS",
    }
)

FLOW_MODELS_CATALOG = [
    {
        "id": "flow_creative_agent",
        "name": "Flow Creative Agent",
        "category": "agent",
        "credit_label": "Theo lượt hội thoại",
        "description": "Tác nhân tự động hội thoại & điều phối cảnh",
    },
    {
        "id": "nano_banana_2_1",
        "name": "Nano Banana 2.1",
        "category": "image",
        "credit_label": "~1 credit/ảnh",
        "description": "Model tạo ảnh mặc định mới nhất, sắc nét & bám sát prompt",
        "is_default": True,
    },
    {
        "id": "nano_banana_2_lite",
        "name": "Nano Banana 2 Lite",
        "category": "image",
        "credit_label": "~1 credit/ảnh",
        "description": "Model tạo ảnh siêu tốc (2–3s), tiết kiệm quota",
    },
    {
        "id": "nano_banana_pro",
        "name": "Nano Banana Pro",
        "category": "image",
        "credit_label": "~1 credit/ảnh",
        "description": "Model Pro ổn định thế hệ trước",
    },
    {
        "id": "omni_1_1_flash",
        "name": "Omni 1.1 Flash",
        "category": "video",
        "credit_label": "~10 credit/clip",
        "description": "Model video AI chuẩn mới nhất của Google Flow, tốc độ cao",
        "is_default": True,
    },
    {
        "id": "veo_3_1_lite",
        "name": "Veo 3.1 – Lite",
        "category": "video",
        "credit_label": "~8 credit/clip",
        "description": "Bản Veo rút gọn, tiết kiệm credit",
    },
    {
        "id": "veo_3_1_fast",
        "name": "Veo 3.1 – Fast",
        "category": "video",
        "credit_label": "~12 credit/clip",
        "description": "Veo 3.1 phiên bản tối ưu tốc độ",
    },
    {
        "id": "veo_3_1_quality",
        "name": "Veo 3.1 – Quality",
        "category": "video",
        "credit_label": "~15–20 credit/clip",
        "description": "Veo 3.1 chất lượng điện ảnh cao cấp nhất",
    },
    {
        "id": "google_veo_intro",
        "name": "Google Veo Intro",
        "category": "video",
        "credit_label": "~10–20 credit/clip",
        "description": "Cấu hình chuyên dụng cho video intro",
    },
]


def _read_raw_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        val = json.loads(path.read_text(encoding="utf-8"))
        return val if isinstance(val, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _decrypt_account_record(raw: dict) -> dict:
    acc = {
        k: v
        for k, v in raw.items()
        if k not in {
            *SECRET_FIELDS,
            *(f"{f}_encrypted" for f in SECRET_FIELDS),
            COOKIE_FIELD,
            f"{COOKIE_FIELD}_encrypted",
        }
    }
    for field in SECRET_FIELDS:
        enc = raw.get(f"{field}_encrypted")
        if enc is None:
            enc = raw.get(field, "")
        acc[field] = decrypt_secret(str(enc or ""))

    enc_cookies = raw.get(f"{COOKIE_FIELD}_encrypted")
    if enc_cookies:
        try:
            cookies = json.loads(decrypt_secret(str(enc_cookies)))
        except json.JSONDecodeError:
            cookies = []
    else:
        cookies = raw.get(COOKIE_FIELD, [])
    acc[COOKIE_FIELD] = cookies if isinstance(cookies, list) else []
    return acc


def _encrypt_account_record(acc: dict) -> dict:
    stored = {
        k: v
        for k, v in dict(acc).items()
        if k not in {
            *SECRET_FIELDS,
            *(f"{f}_encrypted" for f in SECRET_FIELDS),
            COOKIE_FIELD,
            f"{COOKIE_FIELD}_encrypted",
        }
    }
    for field in SECRET_FIELDS:
        val = str(acc.get(field) or "")
        if val:
            stored[f"{field}_encrypted"] = encrypt_secret(val)
    cookies = acc.get(COOKIE_FIELD)
    if isinstance(cookies, list) and cookies:
        stored[f"{COOKIE_FIELD}_encrypted"] = encrypt_secret(
            json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
        )
    return stored


def _migrate_legacy_single_account() -> dict:
    """Migrate single google_flow_account.json into multi-account store if needed."""
    single_raw = _read_raw_json(GOOGLE_FLOW_ACCOUNT_PATH)
    if not single_raw:
        return {
            "active_account_id": "",
            "auto_rotation_enabled": True,
            "rotation_strategy": "model_priority",
            "accounts": [],
        }

    legacy_acc = _decrypt_account_record(single_raw)
    email = str(legacy_acc.get("email") or "").strip()
    acc_id = "acc_flow_1"
    account_entry = {
        "id": acc_id,
        "email": email,
        "password": legacy_acc.get("password", ""),
        "totp_secret": legacy_acc.get("totp_secret", ""),
        "profile_name": "PROFILE_GOOGLE_FLOW_1",
        "enabled": True,
        "plan_tier": "Standard",
        "last_checked_at": None,
        "quotas": {},
        COOKIE_FIELD: legacy_acc.get(COOKIE_FIELD, []),
    }

    doc = {
        "active_account_id": acc_id,
        "auto_rotation_enabled": True,
        "rotation_strategy": "model_priority",
        "accounts": [account_entry],
    }
    save_accounts_document(doc)
    return doc


def load_accounts_document() -> dict:
    """Load all accounts with decrypted secrets."""
    if not GOOGLE_FLOW_ACCOUNTS_PATH.exists():
        if GOOGLE_FLOW_ACCOUNT_PATH.exists():
            return _migrate_legacy_single_account()
        return {
            "active_account_id": "",
            "auto_rotation_enabled": True,
            "rotation_strategy": "model_priority",
            "accounts": [],
        }

    raw = _read_raw_json(GOOGLE_FLOW_ACCOUNTS_PATH)
    if not raw or not isinstance(raw.get("accounts"), list):
        if GOOGLE_FLOW_ACCOUNT_PATH.exists():
            return _migrate_legacy_single_account()
        return {
            "active_account_id": "",
            "auto_rotation_enabled": True,
            "rotation_strategy": "model_priority",
            "accounts": [],
        }

    accounts = [_decrypt_account_record(a) for a in raw.get("accounts", [])]
    active_id = str(raw.get("active_account_id") or "")
    if not active_id and accounts:
        active_id = accounts[0].get("id", "")

    return {
        "active_account_id": active_id,
        "auto_rotation_enabled": bool(raw.get("auto_rotation_enabled", True)),
        "rotation_strategy": str(raw.get("rotation_strategy") or "model_priority"),
        "accounts": accounts,
    }


def save_accounts_document(doc: dict) -> None:
    """Persist all accounts with encrypted secrets and sync legacy single account."""
    accounts = doc.get("accounts", [])
    encrypted_accounts = [_encrypt_account_record(a) for a in accounts]
    active_id = str(doc.get("active_account_id") or "")
    if not active_id and accounts:
        active_id = accounts[0].get("id", "")

    stored = {
        "active_account_id": active_id,
        "auto_rotation_enabled": bool(doc.get("auto_rotation_enabled", True)),
        "rotation_strategy": str(doc.get("rotation_strategy") or "model_priority"),
        "accounts": encrypted_accounts,
    }
    write_private_text(
        GOOGLE_FLOW_ACCOUNTS_PATH,
        json.dumps(stored, ensure_ascii=False, indent=2) + "\n",
    )

    # Sync active account to legacy single file for full backward compatibility
    active_acc = next((a for a in accounts if a.get("id") == active_id), None)
    if active_acc:
        write_private_text(
            GOOGLE_FLOW_ACCOUNT_PATH,
            json.dumps(_encrypt_account_record(active_acc), ensure_ascii=False, indent=2) + "\n",
        )
    elif not accounts:
        GOOGLE_FLOW_ACCOUNT_PATH.unlink(missing_ok=True)


def list_accounts(include_secrets: bool = False) -> list[dict]:
    """Return list of accounts. By default, secrets are redacted and sanitized."""
    doc = load_accounts_document()
    active_id = doc.get("active_account_id")
    result = []
    for acc in doc.get("accounts", []):
        entry = {
            "id": acc.get("id"),
            "email": acc.get("email", ""),
            "profile_name": acc.get("profile_name", "PROFILE_GOOGLE_FLOW_1"),
            "is_active": acc.get("id") == active_id,
            "enabled": acc.get("enabled", True),
            "plan_tier": acc.get("plan_tier", "Standard"),
            "last_checked_at": acc.get("last_checked_at"),
            "password_configured": bool(acc.get("password")),
            "totp_configured": bool(acc.get("totp_secret")),
            "session_configured": bool(acc.get(COOKIE_FIELD)),
            "cookie_count": len(acc.get(COOKIE_FIELD, [])),
            "quotas": acc.get("quotas", {}),
        }
        if include_secrets:
            entry["password"] = acc.get("password", "")
            entry["totp_secret"] = acc.get("totp_secret", "")
            entry[COOKIE_FIELD] = acc.get(COOKIE_FIELD, [])
        result.append(entry)
    return result


def get_account(account_id: str) -> dict | None:
    doc = load_accounts_document()
    for acc in doc.get("accounts", []):
        if acc.get("id") == account_id:
            return acc
    return None


def get_active_account() -> dict:
    doc = load_accounts_document()
    active_id = doc.get("active_account_id")
    for acc in doc.get("accounts", []):
        if acc.get("id") == active_id:
            return acc
    if doc.get("accounts"):
        return doc["accounts"][0]
    return {}


def upsert_account(payload: dict) -> dict:
    email = str(payload.get("email") or "").strip()
    if email and not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Email Google không hợp lệ.")

    doc = load_accounts_document()
    accounts = doc.get("accounts", [])
    account_id = str(payload.get("id") or "").strip()

    existing = next((a for a in accounts if a.get("id") == account_id), None) if account_id else None

    if existing:
        if email:
            existing["email"] = email
        if payload.get("password"):
            existing["password"] = str(payload["password"]).strip()
        if payload.get("totp_secret"):
            existing["totp_secret"] = str(payload["totp_secret"]).strip()
        if "enabled" in payload:
            existing["enabled"] = bool(payload["enabled"])
        if "plan_tier" in payload:
            existing["plan_tier"] = str(payload["plan_tier"])
        if COOKIE_FIELD in payload and isinstance(payload[COOKIE_FIELD], list):
            existing[COOKIE_FIELD] = payload[COOKIE_FIELD]
        target_account = existing
    else:
        # Determine next available profile name
        used_profiles = {a.get("profile_name") for a in accounts if a.get("profile_name")}
        idx = 1
        while f"PROFILE_GOOGLE_FLOW_{idx}" in used_profiles:
            idx += 1
        profile_name = f"PROFILE_GOOGLE_FLOW_{idx}"

        new_id = account_id or f"acc_flow_{uuid.uuid4().hex[:8]}"
        target_account = {
            "id": new_id,
            "email": email,
            "password": str(payload.get("password") or "").strip(),
            "totp_secret": str(payload.get("totp_secret") or "").strip(),
            "profile_name": profile_name,
            "enabled": bool(payload.get("enabled", True)),
            "plan_tier": str(payload.get("plan_tier") or "Standard"),
            "last_checked_at": None,
            "quotas": {},
            COOKIE_FIELD: payload.get(COOKIE_FIELD, []),
        }
        accounts.append(target_account)
        if not doc.get("active_account_id"):
            doc["active_account_id"] = new_id

    doc["accounts"] = accounts
    save_accounts_document(doc)
    return target_account


def delete_account(account_id: str) -> bool:
    doc = load_accounts_document()
    accounts = [a for a in doc.get("accounts", []) if a.get("id") != account_id]
    if len(accounts) == len(doc.get("accounts", [])):
        return False
    doc["accounts"] = accounts
    if doc.get("active_account_id") == account_id:
        doc["active_account_id"] = accounts[0]["id"] if accounts else ""
    save_accounts_document(doc)
    return True


def set_active_account(account_id: str) -> dict:
    doc = load_accounts_document()
    found = any(a.get("id") == account_id for a in doc.get("accounts", []))
    if not found:
        raise ValueError(f"Không tìm thấy tài khoản {account_id}")
    doc["active_account_id"] = account_id
    save_accounts_document(doc)
    return get_active_account()


def save_account_quotas(account_id: str, quotas: dict, plan_tier: str = "") -> dict:
    doc = load_accounts_document()
    account = next((a for a in doc.get("accounts", []) if a.get("id") == account_id), None)
    if not account:
        raise ValueError(f"Không tìm thấy tài khoản {account_id}")

    import time
    account["quotas"] = quotas
    account["last_checked_at"] = int(time.time())
    if plan_tier:
        account["plan_tier"] = plan_tier

    save_accounts_document(doc)
    return account


def get_fleet_summary() -> dict:
    doc = load_accounts_document()
    accounts = doc.get("accounts", [])
    active_id = doc.get("active_account_id")

    veo_ready = 0
    image_ready = 0
    agent_ready = 0

    for a in accounts:
        if not a.get("enabled", True):
            continue
        quotas = a.get("quotas", {})
        # If not checked yet or status != exhausted, consider available
        veo_status = quotas.get("veo_3_1_quality", {}).get("status", "eligible")
        if veo_status != "exhausted":
            veo_ready += 1
        image_status = quotas.get("nano_banana_2_1", {}).get("status", "eligible")
        if image_status != "exhausted":
            image_ready += 1
        agent_status = quotas.get("flow_creative_agent", {}).get("status", "eligible")
        if agent_status != "exhausted":
            agent_ready += 1

    return {
        "total_accounts": len(accounts),
        "active_account_id": active_id,
        "auto_rotation_enabled": doc.get("auto_rotation_enabled", True),
        "rotation_strategy": doc.get("rotation_strategy", "model_priority"),
        "veo_ready": veo_ready,
        "image_ready": image_ready,
        "agent_ready": agent_ready,
    }


def select_eligible_account_for_model(model_id: str, media_type: str = "image") -> dict | None:
    doc = load_accounts_document()
    accounts = [a for a in doc.get("accounts", []) if a.get("enabled", True)]
    if not accounts:
        return None

    active_id = doc.get("active_account_id")
    # 1. Prefer current active account if eligible
    active_acc = next((a for a in accounts if a.get("id") == active_id), None)
    if active_acc:
        model_quota = active_acc.get("quotas", {}).get(model_id, {})
        if model_quota.get("status") != "exhausted":
            return active_acc

    # 2. If auto-rotation enabled, find another eligible account
    if doc.get("auto_rotation_enabled", True):
        for acc in accounts:
            if acc.get("id") == active_id:
                continue
            model_quota = acc.get("quotas", {}).get(model_id, {})
            if model_quota.get("status") != "exhausted":
                return acc

    # 3. Fallback to active account even if exhausted or unknown
    return active_acc or accounts[0]


# --- Legacy Compatibility Functions ---

def load_account() -> dict:
    return get_active_account()


def save_account(account: dict) -> None:
    doc = load_accounts_document()
    active_id = doc.get("active_account_id") or account.get("id")
    if active_id:
        account["id"] = active_id
    upsert_account(account)


def save_credentials(payload: dict) -> dict:
    doc = load_accounts_document()
    active_id = doc.get("active_account_id")
    if active_id:
        payload["id"] = active_id
    acc = upsert_account(payload)
    return get_account_status()


def get_account_status() -> dict:
    active = get_active_account()
    fleet = get_fleet_summary()
    return {
        "email": str(active.get("email") or ""),
        "account_id": active.get("id", ""),
        "profile_name": active.get("profile_name", "PROFILE_GOOGLE_FLOW_1"),
        "password_configured": bool(active.get("password")),
        "totp_configured": bool(active.get("totp_secret")),
        "session_configured": bool(active.get(COOKIE_FIELD)),
        "plan_tier": active.get("plan_tier", "Standard"),
        "last_checked_at": active.get("last_checked_at"),
        "quotas": active.get("quotas", {}),
        "fleet": fleet,
    }


def clear_account() -> None:
    doc = load_accounts_document()
    active_id = doc.get("active_account_id")
    if active_id:
        delete_account(active_id)


def is_google_flow_cookie(cookie: dict) -> bool:
    domain = str(cookie.get("domain") or "").strip().lstrip(".").casefold()
    return any(
        domain == allowed_domain or domain.endswith(f".{allowed_domain}")
        for allowed_domain in GOOGLE_COOKIE_DOMAINS
    )


def has_google_flow_auth_cookie(cookies: list[dict] | None) -> bool:
    return any(
        is_google_flow_cookie(cookie)
        and str(cookie.get("name") or "") in GOOGLE_AUTH_COOKIE_NAMES
        and bool(str(cookie.get("value") or ""))
        for cookie in (cookies or [])
        if isinstance(cookie, dict)
    )


def select_restorable_google_flow_cookies(
    saved_cookies: list[dict] | None,
    current_cookies: list[dict] | None,
) -> list[dict]:
    if has_google_flow_auth_cookie(current_cookies):
        return []
    return [
        dict(cookie)
        for cookie in (saved_cookies or [])
        if isinstance(cookie, dict) and is_google_flow_cookie(cookie)
    ]
