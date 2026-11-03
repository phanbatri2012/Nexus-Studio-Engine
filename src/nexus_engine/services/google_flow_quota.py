"""Google Flow per-model & agent quota inspector via Playwright CDP."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from playwright.async_api import Page, Locator

from nexus_engine.services.google_flow_account import FLOW_MODELS_CATALOG
from nexus_engine.services.google_flow_login import FLOW_HOME_URL, is_google_flow_url
from nexus_engine.services.google_flow_worker import (
    _is_flow_agent_limit_error,
    _is_flow_credit_exhausted_error,
    _is_flow_image_credit_exhausted_error,
    _is_flow_video_credit_exhausted_error,
)

logger = logging.getLogger(__name__)

QUOTA_INSPECTION_TIMEOUT_SECONDS = 25.0


async def inspect_google_flow_quotas(page: Page, account: dict | None = None) -> dict[str, Any]:
    """Inspect and extract quota status for all Flow models & agent within a bounded timeout."""
    start_time = time.monotonic()
    current_url = str(page.url or "")
    if not is_google_flow_url(current_url):
        try:
            await page.goto(FLOW_HOME_URL, wait_until="domcontentloaded", timeout=15_000)
            await asyncio.sleep(1.5)
        except Exception as exc:
            logger.warning("Không thể điều hướng tới Google Flow: %s", exc)

    # 1. Detect Account Tier / Plan from UI Header
    plan_tier = "Standard"
    try:
        header_text = await page.locator("header, nav, [role='banner']").inner_text(timeout=2000)
        lower_header = header_text.lower()
        if "google one" in lower_header or "ai premium" in lower_header or "premium" in lower_header:
            plan_tier = "Google One AI Premium"
        elif "ultra" in lower_header:
            plan_tier = "Ultra"
        elif "workspace" in lower_header:
            plan_tier = "Google Workspace"
    except Exception:
        pass

    # 2. Check for Global or Recent Error Banners on Page
    page_text = ""
    try:
        body_locator = page.locator("body")
        page_text = await body_locator.inner_text(timeout=3000)
    except Exception:
        pass

    agent_exhausted = _is_flow_agent_limit_error(page_text)
    image_credit_exhausted = _is_flow_image_credit_exhausted_error(page_text)
    video_credit_exhausted = _is_flow_video_credit_exhausted_error(page_text)

    # Check alert / snackbar elements explicitly
    try:
        alerts = page.locator(
            "mat-snack-bar-container, .alert, .toast, [role='alert'], .error-banner, flow-banner"
        )
        count = await alerts.count()
        for i in range(min(count, 5)):
            alert_text = (await alerts.nth(i).inner_text(timeout=500)).strip()
            if _is_flow_agent_limit_error(alert_text):
                agent_exhausted = True
            if _is_flow_image_credit_exhausted_error(alert_text):
                image_credit_exhausted = True
            if _is_flow_video_credit_exhausted_error(alert_text):
                video_credit_exhausted = True
    except Exception:
        pass

    # 3. Check Agent Prompt Box Readiness
    agent_box_disabled = False
    try:
        agent_box = page.locator("flow-creative-agent-prompt-box, flow-prompt-box").first
        if await agent_box.is_visible(timeout=1000):
            box_text = await agent_box.inner_text(timeout=500)
            if _is_flow_agent_limit_error(box_text):
                agent_exhausted = True
            textarea = agent_box.locator("textarea, [contenteditable='true']").first
            if await textarea.is_visible(timeout=500):
                is_disabled = await textarea.is_disabled()
                aria_disabled = await textarea.get_attribute("aria-disabled")
                if is_disabled or aria_disabled == "true":
                    agent_box_disabled = True
    except Exception:
        pass

    # 4. Check Dropdown Options for Model Specific Limits
    model_options_status: dict[str, str] = {}
    try:
        # Look for model select trigger if present
        model_triggers = page.locator(
            "button:has(mat-icon:text-is('arrow_drop_down')), [role='combobox'], mat-select"
        )
        if await model_triggers.first.is_visible(timeout=1000):
            await model_triggers.first.click(timeout=1000)
            await asyncio.sleep(0.3)
            options = page.locator("[role='option'], mat-option")
            opt_count = await options.count()
            for idx in range(min(opt_count, 15)):
                opt = options.nth(idx)
                opt_text = (await opt.inner_text(timeout=300)).strip().lower()
                is_opt_disabled = await opt.is_disabled()
                aria_opt_disabled = await opt.get_attribute("aria-disabled")
                if is_opt_disabled or aria_opt_disabled == "true" or "limit" in opt_text:
                    for model_info in FLOW_MODELS_CATALOG:
                        m_name = model_info["name"].lower()
                        if m_name in opt_text or model_info["id"] in opt_text:
                            model_options_status[model_info["id"]] = "exhausted"
            # Close dropdown cleanly
            await page.keyboard.press("Escape")
    except Exception:
        pass

    # 5. Compile Quota Map for All Catalog Models
    existing_quotas = (account or {}).get("quotas", {})
    quotas: dict[str, Any] = {}
    now_ts = int(time.time())

    for model in FLOW_MODELS_CATALOG:
        m_id = model["id"]
        category = model["category"]
        existing = existing_quotas.get(m_id, {})
        status = "eligible"
        status_label = "Sẵn sàng"
        message = "Hoạt động bình thường"

        if category == "agent":
            if agent_exhausted or agent_box_disabled:
                status = "exhausted"
                status_label = "Hết lượt hôm nay"
                message = "Đã đạt giới hạn hội thoại của Tác nhân Flow (hãy dùng chế độ thủ công hoặc chờ qua ngày)."
            else:
                status = "eligible"
                status_label = "Sẵn sàng"
                message = "Tác nhân sẵn sàng nhận lệnh."
        elif category == "image":
            if model_options_status.get(m_id) == "exhausted" or image_credit_exhausted:
                status = "exhausted"
                status_label = "Hết hạn mức"
                message = f"Hết credit tạo ảnh {model['name']} hôm nay."
            elif existing.get("status") == "exhausted" and (now_ts - int(existing.get("exhausted_at", 0))) < 43200:
                # Retain exhaustion if flagged within 12 hours unless confirmed cleared
                status = "exhausted"
                status_label = "Hết hạn mức"
                message = existing.get("message") or "Đã ghi nhận cạn hạn mức trong phiên làm việc."
            else:
                status = "eligible"
                status_label = "Còn hạn mức"
                message = f"Sẵn sàng sinh ảnh ({model['credit_label']})."
        elif category == "video":
            if model_options_status.get(m_id) == "exhausted":
                status = "exhausted"
                status_label = "Hết hạn mức"
                message = f"Model {model['name']} bị giới hạn lượt tạo."
            elif m_id.startswith("veo_") and video_credit_exhausted:
                status = "exhausted"
                status_label = "Hết hạn mức"
                message = "Đã chạm trần credit tạo video Veo trong ngày."
            elif existing.get("status") == "exhausted" and (now_ts - int(existing.get("exhausted_at", 0))) < 43200:
                status = "exhausted"
                status_label = "Hết hạn mức"
                message = existing.get("message") or "Đã ghi nhận cạn credit video trong phiên làm việc."
            elif m_id == "omni_1_1_flash" and video_credit_exhausted and not model_options_status.get(m_id):
                # Omni Flash often remains available even when Veo Quality is exhausted
                status = "warning"
                status_label = "Khả dụng"
                message = "Veo có thể đã hết nhưng Omni Flash sẵn sàng thay thế."
            else:
                status = "eligible"
                status_label = "Còn hạn mức"
                message = f"Sẵn sàng sinh video ({model['credit_label']})."

        quotas[m_id] = {
            "id": m_id,
            "name": model["name"],
            "category": category,
            "credit_label": model["credit_label"],
            "description": model["description"],
            "status": status,
            "status_label": status_label,
            "message": message,
            "exhausted_at": existing.get("exhausted_at") if status == "exhausted" else None,
        }

    elapsed = round(time.monotonic() - start_time, 2)
    logger.info("Hoàn tất quét hạn mức Google Flow trong %ss (Tier: %s)", elapsed, plan_tier)

    return {
        "success": True,
        "plan_tier": plan_tier,
        "checked_at": now_ts,
        "elapsed_seconds": elapsed,
        "quotas": quotas,
    }
