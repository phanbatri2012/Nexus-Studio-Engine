# -*- coding: utf-8 -*-
import pytest
from pathlib import Path
from unittest.mock import patch

from nexus_engine.services.video_production import (
    _extract_script_context_for_whisper,
    _restore_script_entities,
    _write_segment_srt,
    _format_srt_time,
)
from nexus_engine.services.stock_video_renderer import (
    _shift_srt_timestamp,
    _create_time_shifted_srt,
)


def test_restore_script_entities():
    script_text = (
        "Vì sao Chủ tịch Hồ Chí Minh không chọn Đại tướng Võ Nguyên Giáp, "
        "mà lại đặt niềm tin vào Lê Duẩn? Chiến thắng Điện Biên Phủ vang dội."
    )
    with patch("nexus_engine.services.video_production.db.get_video") as mock_get:
        mock_get.return_value = {
            "generated_script": script_text,
            "title": "Bác Hồ và Lê Duẩn",
        }
        prompt, entity_map = _extract_script_context_for_whisper(101)

    assert "Lê Duẩn" in entity_map.values()
    assert "Điện Biên Phủ" in entity_map.values()
    assert "Lê Duẩn" in prompt or "Hồ Chí Minh" in prompt

    # Whisper heard lowercase or misaccented "lê duận" (dấu nặng)
    whisper_output = "Ai cũng biết điện biên phủ lừng lẫy, và Bác đặt niềm tin vào lê duận."
    restored = _restore_script_entities(whisper_output, entity_map)

    # Should restore proper capitalization and fix tone-mark
    assert "Điện Biên Phủ" in restored
    assert "Lê Duẩn" in restored
    assert "lê duận" not in restored


def test_write_segment_srt_no_boundary_duplication(tmp_path: Path):
    """Ensure captions straddling scene boundary are assigned cleanly to 1 scene without duplicate."""
    captions = [
        {"start": 0.0, "end": 15.0, "text": "Đoạn mở đầu câu chuyện"},
        {"start": 28.0, "end": 32.0, "text": "Câu vắt qua ranh giới giữa hai cảnh"},
        {"start": 45.0, "end": 55.0, "text": "Đoạn kết thúc cảnh hai"},
    ]

    scene_1 = {"start": 0.0, "end": 30.0}
    scene_2 = {"start": 30.0, "end": 60.0}

    srt_file_1 = tmp_path / "scene_1.srt"
    srt_file_2 = tmp_path / "scene_2.srt"

    has_sub_1 = _write_segment_srt(captions, scene_1, srt_file_1)
    has_sub_2 = _write_segment_srt(captions, scene_2, srt_file_2)

    assert has_sub_1 is True
    assert has_sub_2 is True

    content_1 = srt_file_1.read_text(encoding="utf-8")
    content_2 = srt_file_2.read_text(encoding="utf-8")

    # Scene 1 should have Caption 1, but NOT Caption 2 (midpoint 30.0s belongs to Scene 2)
    assert "Đoạn mở đầu câu chuyện" in content_1
    assert "Câu vắt qua ranh giới giữa hai cảnh" not in content_1

    # Scene 2 should have Caption 2 and Caption 3
    assert "Câu vắt qua ranh giới giữa hai cảnh" in content_2
    assert "Đoạn kết thúc cảnh hai" in content_2


def test_shift_srt_timestamp():
    assert _shift_srt_timestamp("00:00:01,230", 3.0) == "00:00:04,230"
    assert _shift_srt_timestamp("00:01:58,500", 3.0) == "00:02:01,500"
    assert _shift_srt_timestamp("00:59:59,000", 2.5) == "01:00:01,500"


def test_create_time_shifted_srt(tmp_path: Path):
    src_srt = tmp_path / "source.srt"
    dst_srt = tmp_path / "shifted.srt"

    src_srt.write_text(
        "1\n00:00:01,000 --> 00:00:05,000\nLời thoại một\n\n"
        "2\n00:00:06,500 --> 00:00:10,250\nLời thoại hai\n",
        encoding="utf-8",
    )

    success = _create_time_shifted_srt(src_srt, dst_srt, shift_seconds=3.0)
    assert success is True

    result_text = dst_srt.read_text(encoding="utf-8")
    assert "00:00:04,000 --> 00:00:08,000" in result_text
    assert "00:00:09,500 --> 00:00:13,250" in result_text
    assert "Lời thoại một" in result_text
    assert "Lời thoại hai" in result_text
