# tests/test_dialogue_katex_hardening.py
"""
Unit tests kiểm chứng khả năng kháng lỗi định dạng KaTeX / LaTeX / Unicode Math từ ChatGPT
cho bộ phân tích kịch bản đối thoại (Dialogue Parser) và Auto-Casting.
"""
import pytest
from nexus_engine.dialogue_parser import (
    is_dialogue_script,
    parse_dialogue_turns,
    clean_turn_text_for_tts,
    sanitize_dialogue_script,
)
from nexus_engine.services.auto_casting import script_speaker_keys
from nexus_engine.services.chatgpt_worker import clean_text


def test_standard_bracketed_dialogue():
    script = """### [INTRO]
[MC]: Xin chào quý vị khán giả.
[KHACH_1]: Chào anh Sâm và chương trình.
[MC]: Hôm nay chúng ta có một câu chuyện đặc biệt."""
    assert is_dialogue_script(script) is True
    turns = parse_dialogue_turns(script)
    assert len(turns) == 3
    assert turns[0]["role"] == "MC"
    assert turns[1]["role"] == "KHACH_1"
    assert turns[2]["role"] == "MC"
    assert script_speaker_keys(script) == ["MC", "KHACH_1"]


def test_latex_escaped_brackets_dialogue():
    script = """### [INTRO]
\\[MC\\]: Thưa quý vị khán giả.
\\[KHACH_1\\]: Cháu chào chú Sâm.
\\[MC\\]: Cháu có thể chia sẻ về câu chuyện của mình không?"""
    assert is_dialogue_script(script) is True
    turns = parse_dialogue_turns(script)
    assert len(turns) == 3
    assert turns[0]["role"] == "MC"
    assert turns[1]["role"] == "KHACH_1"
    assert turns[2]["role"] == "MC"
    assert script_speaker_keys(script) == ["MC", "KHACH_1"]
    # Đảm bảo không còn ký tự escaped trong turn text
    assert "\\[" not in turns[0]["text"]
    assert "\\]" not in turns[0]["text"]


def test_unbracketed_line_start_dialogue():
    script = """### [INTRO]
MC: Chào mừng quý vị đến với buổi trò chuyện.
KHACH_1: Dạ thưa bác, cháu là người trong cuộc.
MC: Biến cố bắt đầu từ khi nào?
KHACH_1: Từ năm năm trước khi gia đình cháu gặp nạn."""
    assert is_dialogue_script(script) is True
    turns = parse_dialogue_turns(script)
    assert len(turns) == 4
    assert [t["role"] for t in turns] == ["MC", "KHACH_1", "MC", "KHACH_1"]
    assert script_speaker_keys(script) == ["MC", "KHACH_1"]
    # Kiểm tra clean text không bị dính nhãn MC: hay KHACH_1:
    assert not turns[0]["text"].startswith("MC:")
    assert not turns[1]["text"].startswith("KHACH_1:")


def test_katex_mathematical_italic_script():
    # Mô phỏng chính xác định dạng rác KaTeX mà ChatGPT đã sinh ra
    script = """### [INTRO]
\U0001D440
\U0001D436
MC: Một cô gái bị trói trong cốp xe đã gọi đúng tên người cứu mình.

\U0001D43E
\U0001D43B
\U0001D434
\U0001D436
\U0001D43B
_
1
KHACH_1: Vừa mở cốp xe, cháu thấy cô ấy kiệt sức.

\U0001D440
\U0001D436
MC: Quý vị đang lắng nghe chương trình Sâm Audio."""

    # 1. Test clean_text trong chatgpt_worker xử lý KaTeX fragments
    cleaned = clean_text(script)
    assert "\U0001D440" not in cleaned
    assert "[MC]:" in cleaned
    assert "[KHACH_1]:" in cleaned

    # 2. Test dialogue_parser bóc tách thành công
    assert is_dialogue_script(cleaned) is True
    turns = parse_dialogue_turns(cleaned)
    assert len(turns) == 3
    assert turns[0]["role"] == "MC"
    assert turns[1]["role"] == "KHACH_1"
    assert turns[2]["role"] == "MC"
    assert script_speaker_keys(cleaned) == ["MC", "KHACH_1"]


def test_clean_turn_text_for_tts_strips_all_role_variants():
    # Đảm bảo TTS không bao giờ đọc to nhãn vai của MC hay Khách
    assert clean_turn_text_for_tts("[MC]: Xin chào.") == "Xin chào."
    assert clean_turn_text_for_tts("\\[MC\\]: Xin chào.") == "Xin chào."
    assert clean_turn_text_for_tts("MC: Xin chào.") == "Xin chào."
    assert clean_turn_text_for_tts("[KHACH_1]: Thưa bác.") == "Thưa bác."
    assert clean_turn_text_for_tts("\\[KHACH_1\\]: Thưa bác.") == "Thưa bác."
    assert clean_turn_text_for_tts("KHACH_1: Thưa bác.") == "Thưa bác."


def test_monologue_script_not_detected_as_dialogue():
    monologue = """Đêm muộn, người đàn ông ngồi lặng lẽ bên cửa sổ.
Ông nhớ lại những năm tháng đã qua của cuộc đời mình.
Tất cả đã trôi đi như một giấc mơ."""
    assert is_dialogue_script(monologue) is False
    turns = parse_dialogue_turns(monologue)
    assert len(turns) == 1
    assert turns[0]["role"] == "MC"
