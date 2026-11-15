from __future__ import annotations

import pytest

from nexus_engine.services import database as db
from nexus_engine.services import drain_mode
from nexus_engine.services import chatgpt_worker
from nexus_engine.services import script_body_regeneration as body_regen


def test_body_audit_blocks_editorial_language_and_unsupported_numbers():
    transcript = "Năm 1945 sự kiện diễn ra tại Hà Nội với hai đoàn đại biểu."
    candidate = (
        "Trong phần trên của kịch bản, sự kiện năm 1945 diễn ra tại Hà Nội. "
        "Đến năm 1954, 3 đoàn đại biểu tiếp tục xuất hiện."
    )

    report = body_regen.audit_body_candidate(candidate, transcript)

    assert report["can_apply"] is False
    codes = {item["code"] for item in report["errors"]}
    assert "editorial_language" in codes
    assert "unsupported_numeric_facts" in codes
    assert set(report["errors"][1]["values"]) >= {"1954", "3"}


def test_replace_body_preserves_every_character_outside_body():
    script = (
        "### [TITLE]\nTiêu đề\n\n"
        "### [INTRO]\nMở đầu nguyên bản\n\n"
        "### [BODY]\nNội dung cũ\n\n"
        "### [OUTRO]\nKết thúc nguyên bản\n"
    )

    updated = body_regen.replace_script_section(script, "BODY", "Nội dung mới")

    assert body_regen.section_text(updated, "BODY") == "Nội dung mới"
    assert updated.startswith(script.split("Nội dung cũ", 1)[0])
    assert updated.endswith(script.split("Nội dung cũ", 1)[1])
    assert body_regen.section_text(updated, "INTRO") == "Mở đầu nguyên bản"
    assert body_regen.section_text(updated, "OUTRO") == "Kết thúc nguyên bản"


def test_replace_narration_preserves_metadata_and_replaces_all_voice_sections():
    script = _sample_script()
    narration = body_regen.build_narration_sections(
        intro="Mở đầu hoàn toàn mới",
        body="Thân bài hoàn toàn mới",
        outro="Kết thúc hoàn toàn mới",
    )

    updated = body_regen.replace_narration_sections(script, narration)

    assert body_regen.section_text(updated, "INTRO") == "Mở đầu hoàn toàn mới"
    assert body_regen.section_text(updated, "BODY") == "Thân bài hoàn toàn mới"
    assert body_regen.section_text(updated, "OUTRO") == "Kết thúc hoàn toàn mới"
    assert body_regen.section_text(updated, "TITLE") == "Tiêu đề giữ nguyên"


def test_narration_audit_blocks_video_446_style_overlap_and_expansion():
    intro = " ".join(
        [
            "Việt Nam vừa tuyên bố độc lập nhưng chính quyền còn non trẻ và đất nước kiệt quệ",
            "quân nước ngoài đang tiến vào trong khi tương lai dân tộc đứng trước thử thách sinh tử",
        ]
        * 4
    )
    body_core = " ".join(
        [
            "Chính quyền tổ chức cứu đói củng cố bộ máy chuẩn bị tổng tuyển cử và bảo vệ thành quả cách mạng",
            "các cuộc thương lượng diễn ra theo trình tự và mỗi quyết định đều gắn với một sức ép cụ thể",
        ]
        * 8
    )
    body = intro + " " + body_core + " " + body_core
    outro = " ".join(
        [
            "Những lựa chọn ấy để lại bài học về độc lập chủ quyền đoàn kết và trách nhiệm với lịch sử",
        ]
        * 4
    )
    source = " ".join([intro, body_core, outro])
    narration = body_regen.build_narration_sections(
        intro=intro,
        body=body,
        outro=outro,
    )

    report = body_regen.audit_narration_candidate(narration, source)

    assert report["can_apply"] is False
    codes = {item["code"] for item in report["errors"]}
    assert "intro_body_semantic_overlap" in codes
    assert "narration_excessive_expansion" in codes
    assert report["metrics"]["section_word_counts"]["BODY"] > 0


@pytest.mark.parametrize(
    ("word_count", "expected_error", "expected_warning"),
    [
        (819, False, False),
        (821, False, True),
        (836, False, True),
        (837, True, False),
    ],
)
def test_narration_section_word_budget_boundaries(
    word_count,
    expected_error,
    expected_warning,
):
    assessment = chatgpt_worker._narration_draft_assessment(
        " ".join(["lời"] * word_count),
        transcript=" ".join(["nguồn"] * 1000),
        section_label="BODY phần 3/6",
        minimum_words=600,
        maximum_words=819,
    )

    error_codes = {item["code"] for item in assessment["errors"]}
    warning_codes = {item["code"] for item in assessment["warnings"]}
    assert ("section_above_budget" in error_codes) is expected_error
    assert ("section_above_target_budget" in warning_codes) is expected_warning
    assert assessment["metrics"]["tolerance"] == 17
    assert assessment["metrics"]["hard_maximum"] == 836


def test_narration_section_soft_overflow_does_not_trigger_repair(monkeypatch):
    prompts = []

    def fake_send_prompt(_page, prompt):
        prompts.append(prompt)
        return " ".join(["lời"] * 821)

    monkeypatch.setattr(chatgpt_worker, "send_prompt", fake_send_prompt)

    result = chatgpt_worker._generate_narration_section_with_one_repair(
        object(),
        prompt="Viết BODY",
        source="nguồn",
        transcript=" ".join(["nguồn"] * 1000),
        section_label="BODY phần 3/6",
        minimum_words=600,
        maximum_words=819,
        section_rule="Chỉ viết BODY.",
        cancel_check=None,
    )

    assert len(prompts) == 1
    assert len(result["text"].split()) == 821
    assert result["warnings"][0]["code"] == "section_above_target_budget"


def test_narration_section_repair_uses_buffer_and_accepts_soft_overflow(monkeypatch):
    prompts = []
    responses = iter(
        [
            " ".join(["lời"] * 837),
            " ".join(["lời"] * 821),
        ]
    )

    def fake_send_prompt(_page, prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(chatgpt_worker, "send_prompt", fake_send_prompt)

    result = chatgpt_worker._generate_narration_section_with_one_repair(
        object(),
        prompt="Viết BODY",
        source="nguồn",
        transcript=" ".join(["nguồn"] * 1000),
        section_label="BODY phần 3/6",
        minimum_words=600,
        maximum_words=819,
        section_rule="Chỉ viết BODY.",
        cancel_check=None,
    )

    assert len(prompts) == 2
    assert "tối đa 802 từ" in prompts[1]
    assert len(result["text"].split()) == 821
    assert result["warnings"][0]["hard_maximum"] == 836


def test_narration_section_repair_still_fails_above_hard_limit(monkeypatch):
    prompts = []

    def fake_send_prompt(_page, prompt):
        prompts.append(prompt)
        return " ".join(["lời"] * 837)

    monkeypatch.setattr(chatgpt_worker, "send_prompt", fake_send_prompt)

    with pytest.raises(RuntimeError, match="failed bounded quality repair"):
        chatgpt_worker._generate_narration_section_with_one_repair(
            object(),
            prompt="Viết BODY",
            source="nguồn",
            transcript=" ".join(["nguồn"] * 1000),
            section_label="BODY phần 3/6",
            minimum_words=600,
            maximum_words=819,
            section_rule="Chỉ viết BODY.",
            cancel_check=None,
        )

    assert len(prompts) == 2


def test_narration_section_tolerance_does_not_hide_content_errors():
    draft = " ".join(["lời"] * 820 + ["1945"])

    assessment = chatgpt_worker._narration_draft_assessment(
        draft,
        transcript=" ".join(["nguồn"] * 1000),
        section_label="BODY phần 3/6",
        minimum_words=600,
        maximum_words=819,
    )

    assert "unsupported_numeric_facts" in {
        item["code"] for item in assessment["errors"]
    }
    assert "section_above_target_budget" in {
        item["code"] for item in assessment["warnings"]
    }


def test_narration_regeneration_uses_version_prompts_and_section_rules(
    tmp_path,
    monkeypatch,
):
    class FakePage:
        url = "https://chatgpt.com/g/g-p-test/project"

    class FakeContext:
        pages = [FakePage()]

        def close(self):
            return None

    class FakePlaywright:
        chromium = object()

    class FakePlaywrightManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, exc_type, exc, traceback):
            return False

    intro_words = "bí ẩn vận mệnh thử thách quốc gia".split() * 36
    body_one_words = "chiến dịch lực lượng tiến công phòng tuyến".split() * 13
    body_two_words = "đàm phán hiệp định đại biểu chủ quyền".split() * 13
    outro_words = "tri ân đoàn kết trách nhiệm hòa bình".split() * 24
    filler_words = "nguồn tư liệu diễn biến nhân vật địa danh".split() * 76
    transcript = " ".join(
        intro_words + body_one_words + body_two_words + outro_words + filler_words
    )
    responses = iter(
        [
            "[PHAN] chiến dịch lực lượng tiến công phòng tuyến thay đổi cục diện bảo vệ địa bàn quan trọng\n"
            "[PHAN] đàm phán hiệp định đại biểu chủ quyền xác lập lựa chọn và kết quả cuối cùng",
            " ".join(intro_words),
            " ".join(body_one_words),
            " ".join(body_two_words),
            " ".join(outro_words),
        ]
    )
    sent_prompts = []

    monkeypatch.setattr(chatgpt_worker, "get_content_mode", lambda _version: "monologue")
    monkeypatch.setattr(
        chatgpt_worker,
        "get_active_prompts",
        lambda _version: {
            "outline": "OUTLINE PROMPT {transcript}",
            "intro": "INTRO PROMPT",
            "body": "BODY PROMPT {part}",
            "outro": "OUTRO PROMPT",
            "_narration_rules": {
                "outline": "OUTLINE_ONLY_BOUNDARY",
                "intro": "INTRO_ONLY_BOUNDARY",
                "body": "BODY_ONLY_BOUNDARY",
                "outro": "OUTRO_ONLY_BOUNDARY",
            },
        },
    )
    monkeypatch.setattr(chatgpt_worker, "gpt_profile_dir", lambda _profile: tmp_path)
    monkeypatch.setattr(chatgpt_worker, "sync_playwright", FakePlaywrightManager)
    monkeypatch.setattr(
        chatgpt_worker,
        "launch_chatgpt_context",
        lambda _chromium, _profile: FakeContext(),
    )
    monkeypatch.setattr(
        chatgpt_worker,
        "navigate_to_chatgpt_project",
        lambda _page, _url: None,
    )
    monkeypatch.setattr(
        chatgpt_worker,
        "get_chatgpt_project_url",
        lambda _version: "https://chatgpt.com/g/g-p-test/project",
    )

    def fake_send_prompt(_page, prompt):
        sent_prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(chatgpt_worker, "send_prompt", fake_send_prompt)

    result = chatgpt_worker.regenerate_narration_from_transcript(
        transcript,
        "gkvs-test",
    )

    assert result["report"]["can_apply"] is True
    assert result["part_count"] == 2
    assert body_regen.section_text(result["narration"], "INTRO")
    assert body_regen.section_text(result["narration"], "BODY")
    assert body_regen.section_text(result["narration"], "OUTRO")
    assert "OUTLINE_ONLY_BOUNDARY" in sent_prompts[0]
    assert "INTRO_ONLY_BOUNDARY" in sent_prompts[1]
    assert "BODY_ONLY_BOUNDARY" in sent_prompts[2]
    assert "BODY_ONLY_BOUNDARY" in sent_prompts[3]
    assert "OUTRO_ONLY_BOUNDARY" in sent_prompts[4]


def test_narration_regeneration_keeps_repaired_soft_overflow_as_warning(
    tmp_path,
    monkeypatch,
):
    class FakePage:
        url = "https://chatgpt.com/g/g-p-test/project"

    class FakeContext:
        pages = [FakePage()]

        def close(self):
            return None

    class FakePlaywright:
        chromium = object()

    class FakePlaywrightManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, exc_type, exc, traceback):
            return False

    outline = " ".join(["diễn biến"] * 20)
    transcript = " ".join(
        ["mở"] * 600
        + ["thân"] * 1800
        + ["kết"] * 600
        + outline.split()
    )
    responses = iter(
        [
            outline,
            " ".join(["mở"] * 180),
            " ".join(["thân"] * 837),
            " ".join(["thân"] * 821),
            " ".join(["kết"] * 120),
        ]
    )
    sent_prompts = []

    monkeypatch.setattr(chatgpt_worker, "get_content_mode", lambda _version: "monologue")
    monkeypatch.setattr(
        chatgpt_worker,
        "get_active_prompts",
        lambda _version: {
            "outline": "OUTLINE {transcript}",
            "intro": "INTRO",
            "body": "BODY {part}",
            "outro": "OUTRO",
            "_narration_rules": chatgpt_worker.DEFAULT_NARRATION_RULES,
        },
    )
    monkeypatch.setattr(chatgpt_worker, "gpt_profile_dir", lambda _profile: tmp_path)
    monkeypatch.setattr(chatgpt_worker, "sync_playwright", FakePlaywrightManager)
    monkeypatch.setattr(
        chatgpt_worker,
        "launch_chatgpt_context",
        lambda _chromium, _profile: FakeContext(),
    )
    monkeypatch.setattr(
        chatgpt_worker,
        "navigate_to_chatgpt_project",
        lambda _page, _url: None,
    )
    monkeypatch.setattr(
        chatgpt_worker,
        "get_chatgpt_project_url",
        lambda _version: "https://chatgpt.com/g/g-p-test/project",
    )
    monkeypatch.setattr(
        body_regen,
        "allocate_outline_part_word_budgets",
        lambda _transcript, _parts: [(600, 819)],
    )

    def fake_send_prompt(_page, prompt):
        sent_prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(chatgpt_worker, "send_prompt", fake_send_prompt)

    result = chatgpt_worker.regenerate_narration_from_transcript(
        transcript,
        "gkvs-test",
    )

    assert len(sent_prompts) == 5
    assert result["report"]["can_apply"] is True
    overflow_warnings = [
        item
        for item in result["report"]["warnings"]
        if item.get("code") == "section_above_target_budget"
    ]
    assert len(overflow_warnings) == 1
    assert overflow_warnings[0]["word_count"] == 821
    assert overflow_warnings[0]["target_maximum"] == 819
    assert overflow_warnings[0]["hard_maximum"] == 836


def test_outline_budgets_bound_total_body_expansion():
    transcript = " ".join(f"nguồn{i}" for i in range(1000))
    parts = ["phần ngắn", "phần dài " * 5, "phần vừa " * 3]

    budgets = body_regen.allocate_outline_part_word_budgets(transcript, parts)

    assert len(budgets) == len(parts)
    assert all(0 < minimum < maximum for minimum, maximum in budgets)
    assert sum(maximum for _, maximum in budgets) <= 1052


@pytest.fixture()
def isolated_database(tmp_path, monkeypatch):
    database_path = tmp_path / "body-revision.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    drain_mode.reset_drain_mode()
    db.init_db()
    return database_path


def _sample_script(body: str = "Nội dung BODY cũ") -> str:
    return (
        "### [TITLE]\nTiêu đề giữ nguyên\n\n"
        "### [INTRO]\nMở đầu giữ nguyên\n\n"
        f"### [BODY]\n{body}\n\n"
        "### [OUTRO]\nKết thúc giữ nguyên\n"
    )


def test_revision_apply_is_atomic_and_rejects_stale_script(isolated_database):
    original_script = _sample_script()
    video_id = db.save_video(
        "https://example.test/video",
        "Video test",
        "Transcript nguồn năm 1945",
        original_script,
        prompt_version="gkvs",
    )
    revision = db.create_script_body_revision(
        video_id=video_id,
        job_id="body-job-1",
        base_script_hash=body_regen.content_hash(original_script),
        original_body="Nội dung BODY cũ",
        prompt_version="gkvs",
    )
    candidate = "Nội dung BODY mới năm 1945"
    db.update_script_body_revision(
        revision["id"],
        status="draft",
        candidate_body=candidate,
        candidate_body_hash=body_regen.content_hash(candidate),
        report_json={"can_apply": True, "errors": []},
    )
    updated_script = body_regen.replace_script_section(original_script, "BODY", candidate)

    applied = db.apply_script_body_revision(
        revision["id"],
        expected_script=original_script,
        new_script=updated_script,
    )

    assert applied["status"] == "applied"
    stored_script = db.get_video(video_id)["generated_script"]
    assert body_regen.section_text(stored_script, "BODY") == candidate
    assert body_regen.section_text(stored_script, "INTRO") == "Mở đầu giữ nguyên"
    assert body_regen.section_text(stored_script, "OUTRO") == "Kết thúc giữ nguyên"

    stale_revision = db.create_script_body_revision(
        video_id=video_id,
        job_id="body-job-2",
        base_script_hash=body_regen.content_hash(stored_script),
        original_body=candidate,
        prompt_version="gkvs",
    )
    db.update_script_body_revision(
        stale_revision["id"],
        status="draft",
        candidate_body="BODY lần ba",
        candidate_body_hash=body_regen.content_hash("BODY lần ba"),
        report_json={"can_apply": True},
    )
    db.update_script(video_id, _sample_script("Người dùng đã sửa thủ công"))

    with pytest.raises(ValueError, match="Kịch bản đã thay đổi"):
        db.apply_script_body_revision(
            stale_revision["id"],
            expected_script=stored_script,
            new_script=_sample_script("BODY lần ba"),
        )


def test_narration_revision_apply_is_atomic_and_preserves_non_voice_sections(
    isolated_database,
):
    original_script = _sample_script()
    video_id = db.save_video(
        "https://example.test/narration",
        "Narration test",
        "Transcript nguồn",
        original_script,
        prompt_version="gkvs",
    )
    original_narration = body_regen.build_narration_sections(
        intro="Mở đầu giữ nguyên",
        body="Nội dung BODY cũ",
        outro="Kết thúc giữ nguyên",
    )
    candidate = body_regen.build_narration_sections(
        intro="Mở đầu mới đủ nội dung để kiểm tra revision nguyên tử",
        body="Thân bài mới đủ nội dung để kiểm tra revision nguyên tử",
        outro="Kết thúc mới đủ nội dung để kiểm tra revision nguyên tử",
    )
    revision = db.create_script_body_revision(
        video_id=video_id,
        job_id="narration-job-1",
        base_script_hash=body_regen.content_hash(original_script),
        original_body="Nội dung BODY cũ",
        original_narration=original_narration,
        scope="narration",
        prompt_version="gkvs",
    )
    db.update_script_body_revision(
        revision["id"],
        status="draft",
        candidate_narration=candidate,
        candidate_narration_hash=body_regen.content_hash(candidate),
        report_json={"can_apply": True, "errors": []},
    )
    updated_script = body_regen.replace_narration_sections(original_script, candidate)

    applied = db.apply_script_body_revision(
        revision["id"],
        expected_script=original_script,
        new_script=updated_script,
    )

    stored_script = db.get_video(video_id)["generated_script"]
    assert applied["scope"] == "narration"
    assert body_regen.section_text(stored_script, "INTRO").startswith("Mở đầu mới")
    assert body_regen.section_text(stored_script, "BODY").startswith("Thân bài mới")
    assert body_regen.section_text(stored_script, "OUTRO").startswith("Kết thúc mới")
    assert body_regen.section_text(stored_script, "TITLE") == "Tiêu đề giữ nguyên"


def test_shared_chatgpt_queue_claims_oldest_job_type(isolated_database):
    video_id = db.save_video(
        "https://example.test/queue",
        "Queue test",
        "Transcript",
        _sample_script(),
    )
    db.create_system_job(
        "body-first",
        "script_body_regeneration",
        "BODY first",
        {"video_id": video_id},
        video_id=video_id,
    )
    db.create_system_job(
        "video-second",
        "video_generation",
        "Video second",
        {"video_id": video_id},
        video_id=video_id,
    )

    claimed = db.claim_next_system_job_types(
        ("video_generation", "script_body_regeneration")
    )

    assert claimed["id"] == "body-first"
    assert claimed["status"] == "running"
