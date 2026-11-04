import pytest
from nexus_engine.services.chatgpt_projects import (
    normalize_image_generation_settings,
    validate_image_generation_settings,
    DEFAULT_IMAGE_GENERATION_SETTINGS,
)
from nexus_engine.services.video_production import build_scene_windows


def test_normalize_preserves_custom_scene_duration():
    """Verify custom scene duration settings are properly preserved in normalized dict."""
    payload = {
        "model": "nano_banana_pro",
        "scene_duration_min_seconds": 12,
        "scene_duration_target_seconds": 18,
        "scene_duration_max_seconds": 24,
    }
    normalized = normalize_image_generation_settings(payload)
    assert normalized["scene_duration_min_seconds"] == 12
    assert normalized["scene_duration_target_seconds"] == 18
    assert normalized["scene_duration_max_seconds"] == 24


def test_normalize_clamps_to_8_and_90_bounds():
    """Verify scene durations are strictly clamped to [8, 90] seconds."""
    payload = {
        "scene_duration_min_seconds": 3,   # below minimum 8
        "scene_duration_target_seconds": 5, # below minimum 8
        "scene_duration_max_seconds": 120, # above maximum 90
    }
    normalized = normalize_image_generation_settings(payload)
    assert normalized["scene_duration_min_seconds"] == 8
    assert normalized["scene_duration_target_seconds"] == 8
    assert normalized["scene_duration_max_seconds"] == 90


def test_normalize_auto_adjusts_inconsistent_order():
    """Verify that if min > target or target > max, values are adjusted to maintain min <= target <= max."""
    payload = {
        "scene_duration_min_seconds": 40,
        "scene_duration_target_seconds": 20,
        "scene_duration_max_seconds": 15,
    }
    normalized = normalize_image_generation_settings(payload)
    assert normalized["scene_duration_min_seconds"] == 40
    assert normalized["scene_duration_target_seconds"] >= normalized["scene_duration_min_seconds"]
    assert normalized["scene_duration_max_seconds"] >= normalized["scene_duration_target_seconds"]
    assert normalized["scene_duration_min_seconds"] == 40
    assert normalized["scene_duration_target_seconds"] == 40
    assert normalized["scene_duration_max_seconds"] == 40


def test_normalize_handles_string_and_invalid_inputs():
    """Verify string numbers are parsed and invalid inputs fallback cleanly."""
    payload = {
        "scene_duration_min_seconds": "15",
        "scene_duration_target_seconds": "22",
        "scene_duration_max_seconds": "invalid",
    }
    normalized = normalize_image_generation_settings(payload)
    assert normalized["scene_duration_min_seconds"] == 15
    assert normalized["scene_duration_target_seconds"] == 22
    assert normalized["scene_duration_max_seconds"] == 35  # fallback to default 35


def test_validate_accepts_duration_settings():
    """Verify validate_image_generation_settings accepts the fields without error."""
    payload = {
        "scene_duration_min_seconds": 10,
        "scene_duration_target_seconds": 15,
        "scene_duration_max_seconds": 20,
    }
    validated = validate_image_generation_settings(payload)
    assert validated["scene_duration_min_seconds"] == 10
    assert validated["scene_duration_target_seconds"] == 15
    assert validated["scene_duration_max_seconds"] == 20


def test_build_scene_windows_with_custom_durations():
    """Verify build_scene_windows works properly with custom duration bounds."""
    # Dummy captions spanning 60 seconds (1 caption every 3s)
    captions = [
        {"start": float(i * 3), "end": float(i * 3 + 2.5), "text": f"Caption {i}"}
        for i in range(20)
    ]
    windows = build_scene_windows(
        captions,
        duration_seconds=60.0,
        minimum_seconds=10.0,
        target_seconds=15.0,
        maximum_seconds=20.0,
        enable_intro_video=False,
    )
    assert len(windows) > 0
    for w in windows:
        window_duration = w["end"] - w["start"]
        # Allow slight boundary tolerance on last window
        if w != windows[-1]:
            assert window_duration >= 8.0, f"Window too short: {window_duration}"
