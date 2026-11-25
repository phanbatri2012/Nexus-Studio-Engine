# -*- coding: utf-8 -*-
import hashlib
import pytest
from pathlib import Path
from unittest.mock import patch

from nexus_engine.services import subtitle_layout
from nexus_engine.services.video_production import (
    _extract_script_context_for_whisper,
    _restore_script_entities,
    _segment_filter,
    _write_segment_srt,
    _format_srt_time,
    create_srt,
    parse_srt_segments,
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


def test_video_466_long_caption_is_split_to_readable_two_line_cues():
    source_text = (
        "Điều gì đã khiến một siêu cường quân sự như Hoa Kỳ, với sức mạnh hỏa lực "
        "áp đảo và nguồn lực chiến tranh khổng lồ, phải đối mặt với một cuộc khủng "
        "hoảng niềm tin ngay giữa lúc tuyên bố chiến thắng đã ở rất gần."
    )

    cues = subtitle_layout.build_readable_cues(
        [{"start": 0.56, "end": 13.12, "text": source_text}]
    )
    stats = subtitle_layout.validate_cues(cues)

    assert len(cues) > 1
    assert stats.max_lines <= 2
    assert stats.max_line_chars <= 42
    assert stats.max_words <= 14
    assert stats.max_duration_seconds <= 6.0
    rendered_words = " ".join(cue["text"].replace("\n", " ") for cue in cues)
    assert rendered_words == source_text


def test_word_timestamps_prefer_punctuation_and_pause_breaks():
    tokens = ["Một", "câu", "mở", "đầu", "rõ", "ràng,", "sau", "đó", "tiếp", "tục."]
    words = []
    cursor = 0.0
    for index, token in enumerate(tokens):
        start = cursor + (0.5 if index == 6 else 0.0)
        end = start + 0.35
        words.append({"start": start, "end": end, "text": token})
        cursor = end

    cues = subtitle_layout.build_readable_cues(
        [{"start": 0.0, "end": cursor, "text": " ".join(tokens), "words": words}]
    )

    assert cues[0]["text"].replace("\n", " ").endswith("ràng,")
    assert " ".join(cue["text"].replace("\n", " ") for cue in cues) == " ".join(tokens)


def test_sparse_long_segment_keeps_each_cue_within_six_seconds():
    cues = subtitle_layout.build_readable_cues(
        [{"start": 0.0, "end": 30.0, "text": "Ba từ thôi"}]
    )

    assert " ".join(cue["text"].replace("\n", " ") for cue in cues) == "Ba từ thôi"
    assert subtitle_layout.validate_cues(cues).max_duration_seconds <= 6.0


def test_word_timestamp_text_mismatch_falls_back_without_losing_content():
    source = "Tướng Nguyễn Văn An xuất hiện."
    cues = subtitle_layout.build_readable_cues(
        [
            {
                "start": 0.0,
                "end": 4.0,
                "text": source,
                "words": [
                    {"start": 0.0, "end": 0.5, "text": "Tướng"},
                    {"start": 0.5, "end": 1.0, "text": "An"},
                    {"start": 1.0, "end": 1.5, "text": "xuất"},
                    {"start": 1.5, "end": 2.0, "text": "hiện."},
                ],
            }
        ]
    )

    assert " ".join(cue["text"].replace("\n", " ") for cue in cues) == source


def test_content_validator_rejects_missing_or_duplicated_words():
    source = [{"start": 0.0, "end": 2.0, "text": "Không mất nội dung"}]

    with pytest.raises(subtitle_layout.SubtitleLayoutError, match="mất hoặc lặp"):
        subtitle_layout.validate_content_preserved(
            source,
            [{"start": 0.0, "end": 2.0, "text": "Không nội dung nội dung"}],
        )


def test_long_single_word_fails_closed_with_clear_error():
    with pytest.raises(subtitle_layout.SubtitleLayoutError, match="Từ quá dài"):
        subtitle_layout.build_readable_cues(
            [{"start": 0.0, "end": 2.0, "text": "x" * 43}]
        )


def test_very_short_vietnamese_sentence_remains_one_cue():
    cues = subtitle_layout.build_readable_cues(
        [{"start": 1.0, "end": 1.8, "text": "Đã rõ."}]
    )

    assert cues == [{"start": 1.0, "end": 1.8, "text": "Đã rõ."}]


def test_parse_and_segment_srt_preserve_explicit_two_line_layout(tmp_path: Path):
    master = tmp_path / "master.srt"
    master.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n"
        "Dòng phụ đề thứ nhất\nDòng phụ đề thứ hai\n",
        encoding="utf-8",
    )

    parsed = parse_srt_segments(master)
    assert parsed[0]["text"] == "Dòng phụ đề thứ nhất\nDòng phụ đề thứ hai"

    segment = tmp_path / "segment.srt"
    assert _write_segment_srt(
        parsed,
        {"start": 0.0, "end": 5.0},
        segment,
    )
    assert "Dòng phụ đề thứ nhất\nDòng phụ đề thứ hai" in segment.read_text(
        encoding="utf-8"
    )


def test_shift_srt_preserves_explicit_line_breaks(tmp_path: Path):
    source = tmp_path / "source_2line.srt"
    shifted = tmp_path / "shifted_2line.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nDòng một\nDòng hai\n",
        encoding="utf-8",
    )

    assert _create_time_shifted_srt(source, shifted, shift_seconds=3.0)
    result = shifted.read_text(encoding="utf-8")
    assert "00:00:04,000 --> 00:00:07,000" in result
    assert "Dòng một\nDòng hai" in result


def test_segment_filter_uses_shared_no_wrap_style():
    graph, output = _segment_filter(
        scene={"duration": 3.0},
        scene_index=0,
        subtitle_path=Path("caption.srt"),
    )

    assert output == "out"
    assert subtitle_layout.SUBTITLE_FORCE_STYLE in graph
    assert "WrapStyle=2" in graph
    assert "MarginL=120,MarginR=120" in graph


def test_create_srt_upgrades_legacy_caption_without_retranscribing(tmp_path: Path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"legacy audio")
    audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    legacy = tmp_path / "legacy.srt"
    legacy.write_text(
        "1\n00:00:00,560 --> 00:00:13,120\n"
        "Điều gì đã khiến một siêu cường quân sự như Hoa Kỳ, với sức mạnh hỏa lực "
        "áp đảo và nguồn lực chiến tranh khổng lồ, phải đối mặt với một cuộc khủng "
        "hoảng niềm tin ngay giữa lúc tuyên bố chiến thắng đã ở rất gần.\n",
        encoding="utf-8",
    )
    legacy_artifact = {
        "artifact_type": "captions",
        "path": str(legacy),
        "content_hash": audio_hash,
        "status": "ready",
        "metadata": {},
    }
    upserts = []

    def latest(_video_id, artifact_type, status=None):
        del status
        return legacy_artifact if artifact_type == "captions" else None

    with (
        patch("nexus_engine.services.video_production.CAPTIONS_DIR", tmp_path),
        patch(
            "nexus_engine.services.video_production.db.get_latest_video_artifact",
            side_effect=latest,
        ),
        patch(
            "nexus_engine.services.video_production.db.upsert_video_artifact",
            side_effect=lambda **kwargs: upserts.append(kwargs) or kwargs,
        ),
        patch(
            "nexus_engine.services.video_production._extract_script_context_for_whisper",
            side_effect=AssertionError("Legacy caption must not run Whisper"),
        ),
    ):
        target, returned_audio_hash = create_srt(
            audio, 466, lambda _message, _stage: None
        )

    assert returned_audio_hash == audio_hash
    assert subtitle_layout.SUBTITLE_PROFILE_VERSION in target.name
    stats = subtitle_layout.validate_srt(target)
    assert stats["max_lines"] <= 2
    assert stats["max_line_chars"] <= 42
    assert {item["artifact_type"] for item in upserts} == {
        "caption_timeline",
        "captions",
    }
    caption_upsert = next(item for item in upserts if item["artifact_type"] == "captions")
    assert caption_upsert["content_hash"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert (
        caption_upsert["metadata"]["subtitle_profile_version"]
        == subtitle_layout.SUBTITLE_PROFILE_VERSION
    )


def test_create_srt_reuses_current_profile_cache(tmp_path: Path):
    audio = tmp_path / "audio-current.mp3"
    audio.write_bytes(b"current audio")
    audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    caption = tmp_path / "current.srt"
    caption.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\nDòng hiện hành hợp lệ.\n",
        encoding="utf-8",
    )
    timeline = tmp_path / "timeline.json"
    timeline.write_text(
        '{"source_audio_sha256":"%s","segments":['
        '{"start":0.0,"end":3.0,"text":"Dòng hiện hành hợp lệ.","words":[]}]}'
        % audio_hash,
        encoding="utf-8",
    )
    caption_artifact = {
        "path": str(caption),
        "content_hash": hashlib.sha256(caption.read_bytes()).hexdigest(),
        "status": "ready",
        "metadata": {
            "source_audio_sha256": audio_hash,
            "subtitle_profile_version": subtitle_layout.SUBTITLE_PROFILE_VERSION,
        },
    }
    timeline_artifact = {
        "path": str(timeline),
        "content_hash": hashlib.sha256(timeline.read_bytes()).hexdigest(),
        "status": "ready",
        "metadata": {"source_audio_sha256": audio_hash},
    }

    def latest(_video_id, artifact_type, status=None):
        del status
        return caption_artifact if artifact_type == "captions" else timeline_artifact

    with (
        patch(
            "nexus_engine.services.video_production.db.get_latest_video_artifact",
            side_effect=latest,
        ),
        patch(
            "nexus_engine.services.video_production.db.upsert_video_artifact",
            side_effect=AssertionError("Current caption cache must be reused"),
        ),
    ):
        returned_path, returned_hash = create_srt(
            audio, 999, lambda _message, _stage: None
        )

    assert returned_path == caption
    assert returned_hash == audio_hash
