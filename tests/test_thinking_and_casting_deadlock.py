# tests/test_thinking_and_casting_deadlock.py
"""
Unit tests kiểm chứng:
1. Nhận diện và loại bỏ các thinking indicator mới của ChatGPT (calculating, optimizing, etc.).
2. extract_generated_video_title từ chối thinking headers lọt vào mục tiêu đề.
3. validate_cast_policy bảo lưu các phân vai được phê duyệt (approval_status == 'approved' / manual_override) khi manifest hash thay đổi.
"""
import pytest
from nexus_engine.services.chatgpt_worker import (
    _is_pure_thinking_indicator,
    THINKING_INDICATOR_PATTERN,
)
from nexus_engine.services.database import (
    extract_generated_video_title,
    _looks_like_thinking_or_meta,
)
from nexus_engine.services.auto_casting import (
    validate_cast_policy,
    AutoCastingAttentionRequired,
)


def test_extended_thinking_indicator_recognition():
    # Các thinking summary của ChatGPT sinh ra gần đây
    assert _is_pure_thinking_indicator("Calculating and optimizing Vietnamese audio story title length") is True
    assert _is_pure_thinking_indicator("Optimizing title for CTR and SEO") is True
    assert _is_pure_thinking_indicator("Crafting engaging emotional title hooks") is True
    assert _is_pure_thinking_indicator("Translating and refining tone of voice") is True
    assert _is_pure_thinking_indicator("Brainstorming narrative conflict") is True

    # Tiêu đề thực tế tiếng Việt phải KHÔNG bị coi là thinking
    assert _is_pure_thinking_indicator("Đại Úy Công An Cứu Cô Gái Bị Trói Trong Cốp Xe") is False
    assert _is_pure_thinking_indicator("Lấy Chồng Xa Xứ, 8 Năm Tủi Nhục Làm Dâu") is False


def test_looks_like_thinking_or_meta():
    assert _looks_like_thinking_or_meta("Calculating and optimizing Vietnamese audio story title length") is True
    assert _looks_like_thinking_or_meta("Optimizing YouTube title length") is True
    assert _looks_like_thinking_or_meta("Crafting high-CTR title") is True
    assert _looks_like_thinking_or_meta("Đại Úy Công An Cứu Cô Gái") is False
    assert _looks_like_thinking_or_meta("8 Năm Làm Vợ Cho Cả Năm Anh Em") is False


def test_extract_generated_video_title_rejects_thinking_trace():
    # Mô phỏng trường hợp kịch bản bị lọt dòng suy nghĩ như Video 429
    script_with_thought = """### [INTRO]
[MC]: Lời mở đầu...

### [TIÊU ĐỀ]
Calculating and optimizing Vietnamese audio story title length

### [SLUG]
8-nam-giam-cam-lam-vo-5-anh-em-cha-qua-doi

### [METADATA]
TIÊU ĐỀ VIDEO: 8 Năm Bị Giam Cầm Làm Vợ 5 Anh Em Nơi Đất Khách
SLUG: 8-nam-giam-cam-lam-vo-5-anh-em
"""
    # Không được lấy dòng "Calculating and optimizing..." mà phải fallback lấy tiêu đề hợp lệ trong METADATA
    title = extract_generated_video_title(script_with_thought)
    assert title == "8 Năm Bị Giam Cầm Làm Vợ 5 Anh Em Nơi Đất Khách"
    assert "Calculating" not in title


def test_validate_cast_policy_preserves_approved_assignment_on_hash_change():
    # Giả lập phân vai đã được duyệt nhưng manifest_hash thay đổi do ChatGPT vừa refresh manifest
    voices = [
        {
            "id": "mc-1",
            "name": "sam_1",
            "casting_profile": {
                "usage_modes": ["multi_host"],
                "voice_pools": ["sam_mc"],
            },
        },
        {
            "id": "guest-1",
            "name": "nu_trung_nien_1",
            "casting_profile": {
                "gender": "female",
                "age_group": "adult",
                "usage_modes": ["multi_guest"],
                "voice_pools": ["sam_khach"],
            },
        },
    ]

    assignments = [
        {
            "role_key": "MC",
            "voice_id": "mc-1",
            "casting_version": 2,
            "manifest_hash": "old_hash_123",
            "approval_status": "approved",
            "source": "fixed_mc",
        },
        {
            "role_key": "KHACH_1",
            "voice_id": "guest-1",
            "casting_version": 2,
            "manifest_hash": "old_hash_123",  # Hash cũ
            "approval_status": "approved",    # ĐÃ ĐƯỢC DUYỆT
            "source": "manual_override",
        },
    ]

    # Manifest mới được refresh với hash khác
    new_manifest = [
        {"speaker_key": "MC", "role": "mc"},
        {"speaker_key": "KHACH_1", "role": "guest", "gender": "female", "age_group": "adult"},
    ]

    # Không được ném ngoại lệ AutoCastingAttentionRequired vì assignment đã được approved
    validate_cast_policy(
        assignments,
        voices,
        mc_voice_id="mc-1",
        guest_voice_pool="sam_khach",
        manifest=new_manifest,
    )
