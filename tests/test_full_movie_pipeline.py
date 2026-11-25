from __future__ import annotations

import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest
from playwright.sync_api import Error as PlaywrightError

from nexus_engine.services import full_movie_pipeline as pipeline
from nexus_engine.services import full_movie_thumbnail_pipeline, full_movie_thumbnail_worker
from nexus_engine.services.chatgpt_runtime import ChatGPTAttentionRequiredError


class _FakeTranslationPage:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.closed = False
        self.goto_calls = 0
        self.url = "about:blank"

    def goto(self, url, **_kwargs):
        self.goto_calls += 1
        if self.error is not None:
            raise self.error
        self.url = url

    def close(self):
        self.closed = True


class _FakeTranslationContext:
    def __init__(self, pages, *, owner_page=None):
        self._job_pages = list(pages)
        self.pages = [owner_page] if owner_page is not None else []
        self.new_page_calls = 0

    def new_page(self):
        page = self._job_pages[self.new_page_calls]
        self.new_page_calls += 1
        return page


def test_translation_navigation_uses_dedicated_page_and_recovers_err_aborted(
    monkeypatch,
):
    owner_page = _FakeTranslationPage()
    first = _FakeTranslationPage(PlaywrightError("Page.goto: net::ERR_ABORTED"))
    second = _FakeTranslationPage(PlaywrightError("Page.goto: net::ERR_ABORTED"))
    third = _FakeTranslationPage()
    context = _FakeTranslationContext(
        [first, second, third],
        owner_page=owner_page,
    )
    monkeypatch.setattr(pipeline, "TRANSLATION_NAVIGATION_BACKOFF_SECONDS", (0, 0))
    monkeypatch.setattr(pipeline, "check_chatgpt_page_attention", lambda _page: None)
    monkeypatch.setattr(pipeline, "wait_for_chatgpt_composer", lambda _page: object())

    page = pipeline._open_translation_page(context)

    assert page is third
    assert context.new_page_calls == 3
    assert first.closed is True
    assert second.closed is True
    assert third.closed is False
    assert owner_page.goto_calls == 0
    assert owner_page.closed is False


def test_translation_navigation_exhaustion_becomes_retry_later(monkeypatch):
    pages = [
        _FakeTranslationPage(PlaywrightError("Page.goto: net::ERR_ABORTED"))
        for _index in range(pipeline.TRANSLATION_NAVIGATION_ATTEMPTS)
    ]
    context = _FakeTranslationContext(pages)
    monkeypatch.setattr(pipeline, "TRANSLATION_NAVIGATION_BACKOFF_SECONDS", (0, 0))

    with pytest.raises(pipeline.FullMovieRetryLater, match="tự thử lại"):
        pipeline._open_translation_page(context)

    assert context.new_page_calls == pipeline.TRANSLATION_NAVIGATION_ATTEMPTS
    assert all(page.closed for page in pages)


def test_translation_attention_is_not_retried(monkeypatch):
    page = _FakeTranslationPage()
    context = _FakeTranslationContext([page])
    monkeypatch.setattr(
        pipeline,
        "check_chatgpt_page_attention",
        lambda _page: (_ for _ in ()).throw(
            ChatGPTAttentionRequiredError("Verification required")
        ),
    )

    with pytest.raises(ChatGPTAttentionRequiredError, match="Verification"):
        pipeline._open_translation_page(context)

    assert context.new_page_calls == 1
    assert page.closed is True


def test_pre_send_failure_is_retryable_but_post_send_failure_is_not(monkeypatch):
    def fail_before_submit(_page, _prompt, **_kwargs):
        raise PlaywrightError("Page closed before prompt submission")

    monkeypatch.setattr(pipeline, "send_prompt", fail_before_submit)
    with pytest.raises(pipeline.FullMovieRetryLater, match="trước khi gửi"):
        pipeline._send_translation_prompt(object(), "prompt")

    def fail_after_submit(_page, _prompt, *, on_prompt_submitted, **_kwargs):
        on_prompt_submitted()
        raise PlaywrightError("Page closed after prompt submission")

    monkeypatch.setattr(pipeline, "send_prompt", fail_after_submit)
    with pytest.raises(PlaywrightError, match="after prompt"):
        pipeline._send_translation_prompt(object(), "prompt")


def test_retry_later_is_scheduled_instead_of_marked_as_terminal_error(monkeypatch):
    scheduled = []
    terminal_updates = []
    monkeypatch.setattr(
        pipeline.security_logging,
        "safe_user_error",
        lambda *_args, **_kwargs: "safe-error-id",
    )
    monkeypatch.setattr(
        pipeline.db,
        "schedule_system_job_recovery",
        lambda job_id, **changes: scheduled.append((job_id, changes)),
    )
    monkeypatch.setattr(
        pipeline.db,
        "update_system_job",
        lambda job_id, **changes: terminal_updates.append((job_id, changes)),
    )

    pipeline.handle_error(
        {"id": "job-1", "resume_from_step": "translate"},
        pipeline.FullMovieRetryLater("ChatGPT chưa sẵn sàng", delay_seconds=20),
    )

    assert len(scheduled) == 1
    assert scheduled[0][0] == "job-1"
    assert scheduled[0][1]["resume_from_step"] == "translate"
    assert scheduled[0][1]["delay_seconds"] == 20
    assert scheduled[0][1]["progress"] == "ChatGPT chưa sẵn sàng"
    assert terminal_updates == []


def test_translation_parser_accepts_fenced_json_and_preserves_index_order():
    response = """```json
    {"movie_title_us":"A Second Chance","episodes":[
      {"index":2,"title_us":"The Return"},
      {"index":1,"title_us":"The Betrayal"}
    ]}
    ```"""

    parsed = pipeline.parse_translation_response(response, 2)

    assert parsed["movie_title_us"] == "A Second Chance"
    assert [item["index"] for item in parsed["episodes"]] == [1, 2]


def test_translation_parser_accepts_chatgpt_escape_wrapper():
    response = (
        '<escape>{"movie_title_us":"Alice and Her Three Fiancés",'
        '"episodes":[{"index":1,"title_us":"Episode 1"}]}</escape>'
    )

    parsed = pipeline.parse_translation_response(response, 1)

    assert parsed == {
        "movie_title_us": "Alice and Her Three Fiancés",
        "episodes": [{"index": 1, "title_us": "Episode 1"}],
    }


def test_translation_parser_recovers_valid_conversation_payload(monkeypatch):
    recovered_response = json.dumps(
        {
            "movie_title_us": "Alice and Her Three Fiancés",
            "episodes": [
                {"index": index, "title_us": f"Episode {index}"}
                for index in range(1, 51)
            ],
        },
        ensure_ascii=False,
    )
    captured = {}

    def recover(_page, expected_user_text=""):
        captured["expected_user_text"] = expected_user_text
        return recovered_response

    monkeypatch.setattr(
        pipeline,
        "recover_assistant_response_from_backend",
        recover,
    )

    parsed = pipeline._parse_translation_response_with_browser_recovery(
        object(),
        "DOM response was incomplete",
        "translation prompt",
        50,
    )

    assert parsed["movie_title_us"] == "Alice and Her Three Fiancés"
    assert len(parsed["episodes"]) == 50
    assert parsed["episodes"][-1] == {"index": 50, "title_us": "Episode 50"}
    assert captured["expected_user_text"] == "translation prompt"


def test_translation_parser_rejects_duplicate_or_missing_indexes():
    response = json.dumps(
        {
            "movie_title_us": "Drama",
            "episodes": [
                {"index": 1, "title_us": "One"},
                {"index": 1, "title_us": "Duplicate"},
            ],
        }
    )

    with pytest.raises(pipeline.FullMovieError, match="Index"):
        pipeline.parse_translation_response(response, 2)


def test_scan_uses_natural_order_and_flags_ambiguous_numbers(tmp_path, monkeypatch):
    for name in ("Episode 10.mp4", "Episode 2.mp4", "A Story 2025.mp4"):
        (tmp_path / name).write_bytes(b"source")

    monkeypatch.setattr(
        pipeline,
        "probe_media",
        lambda _path: {
            "duration": 60.0,
            "width": 720,
            "height": 1280,
            "fps": 25.0,
            "video_codec": "h264",
            "video_profile": "High",
            "sample_aspect_ratio": "1:1",
            "has_audio": True,
            "audio_codec": "aac",
            "audio_sample_rate": 44100,
            "audio_channels": 2,
        },
    )

    scan = pipeline.scan_folder(str(tmp_path))

    assert [item["name"] for item in scan["files"]] == [
        "A Story 2025.mp4",
        "Episode 2.mp4",
        "Episode 10.mp4",
    ]
    assert scan["requires_order_review"] is True
    assert scan["snapshot"]["hash"]


def test_approved_mapping_creates_stable_us_episode_names():
    scan = {
        "files": [
            {"name": "tap 1.mp4"},
            {"name": "tap 2.mkv"},
        ]
    }

    approved = pipeline.build_approved_mapping(
        scan,
        "The CEO: A New Life?",
        [
            {"source_name": "tap 1.mp4", "title_us": "His <Secret>"},
            {"source_name": "tap 2.mkv", "title_us": "A Second Chance"},
        ],
    )

    assert approved["movie_title_us"] == "The CEO A New Life"
    assert [item["target_name"] for item in approved["episodes"]] == [
        "EP001 - His Secret.mp4",
        "EP002 - A Second Chance.mkv",
    ]


def test_automatic_mapping_uses_proposal_indexes_and_source_order():
    scan = {"files": [{"name": "tap 2.mp4"}, {"name": "tap 10.mp4"}]}
    proposal = {
        "movie_title_us": "The Return",
        "episodes": [
            {"index": 2, "title_us": "The Finale"},
            {"index": 1, "title_us": "The Beginning"},
        ],
    }

    approved = pipeline.build_automatic_mapping(scan, proposal)

    assert approved["movie_title_us"] == "The Return"
    assert [item["source_name"] for item in approved["episodes"]] == [
        "tap 2.mp4",
        "tap 10.mp4",
    ]
    assert [item["target_name"] for item in approved["episodes"]] == [
        "EP001 - The Beginning.mp4",
        "EP002 - The Finale.mp4",
    ]


def test_execute_auto_approves_existing_translation_without_pausing(tmp_path, monkeypatch):
    source = tmp_path / "Episode 1.mp4"
    source.write_bytes(b"source")
    stat = source.stat()
    scan = {
        "source_path": str(tmp_path),
        "folder_name": "Demo",
        "files": [
            {
                "name": source.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        ],
        "required_free_bytes": 0,
    }
    scan["snapshot"] = pipeline._source_snapshot(scan["files"])
    job_id = "e" * 32
    job = {
        "id": job_id,
        "payload": {
            "source_path": str(tmp_path),
            "scan": scan,
            "source_snapshot": scan["snapshot"],
            "translation_proposal": {
                "movie_title_us": "Demo Drama",
                "episodes": [{"index": 1, "title_us": "Episode One"}],
            },
        },
    }
    updates = []

    monkeypatch.setattr(
        pipeline.db,
        "get_system_job",
        lambda _job_id: {"id": job_id, "status": "running", "cancel_requested": False},
    )
    monkeypatch.setattr(
        pipeline.db,
        "update_system_job",
        lambda _job_id, **changes: updates.append(changes) or {"id": job_id, **changes},
    )
    monkeypatch.setattr(
        pipeline,
        "generate_translation_proposal",
        lambda _scan: pytest.fail("existing proposal must be reused"),
    )

    class _StopAfterApproval(Exception):
        pass

    monkeypatch.setattr(
        pipeline,
        "_job_temp_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_StopAfterApproval()),
    )

    with pytest.raises(_StopAfterApproval):
        pipeline.execute(job)

    assert updates[0]["progress"] == "Đã tự động dịch tên; đang chuẩn bị ghép phim"
    assert updates[0]["payload_json"]["stage"] == "render"
    assert updates[0]["payload_json"]["approved"]["movie_title_us"] == "Demo Drama"
    assert updates[0].get("status") is None


def test_two_phase_rename_rolls_back_every_source_on_failure(tmp_path, monkeypatch):
    first = tmp_path / "tap 1.mp4"
    second = tmp_path / "tap 2.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    temp_dir = tmp_path / ".nexus_full_movie" / ("a" * 32)
    temp_dir.mkdir(parents=True)
    approved = {
        "episodes": [
            {"order": 1, "source_name": first.name, "target_name": "EP001 - One.mp4", "title_us": "One"},
            {"order": 2, "source_name": second.name, "target_name": "EP002 - Two.mp4", "title_us": "Two"},
        ]
    }
    real_replace = pipeline.os.replace
    injected = {"raised": False}

    def failing_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not injected["raised"]
            and source_path.name.startswith(".__nexus_rename_")
            and destination_path.name == "EP002 - Two.mp4"
        ):
            injected["raised"] = True
            raise OSError("injected rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(pipeline.os, "replace", failing_replace)

    with pytest.raises(pipeline.FullMovieAttentionRequired, match="rollback"):
        pipeline._rename_sources(source_dir=tmp_path, approved=approved, temp_dir=temp_dir)

    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"
    assert not (tmp_path / "EP001 - One.mp4").exists()
    assert not (tmp_path / "EP002 - Two.mp4").exists()


def test_locked_filters_preserve_aspect_ratio_and_never_use_async_audio():
    assert "force_original_aspect_ratio=decrease" in pipeline.VIDEO_FILTER
    assert "setsar=1" in pipeline.VIDEO_FILTER
    assert "pad=1920:1080" in pipeline.VIDEO_FILTER
    assert pipeline.VIDEO_FILTER.endswith("fps=25,format=yuv420p")
    assert pipeline.AUDIO_FILTER == "aresample=44100,asetpts=N/SR/TB"
    assert "async" not in pipeline.AUDIO_FILTER


def test_real_ffmpeg_keeps_1282_source_centered_on_black_16x9_canvas(tmp_path):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "source-720x1282.mp4"
    output = tmp_path / "part-00001.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x1282:rate=25:duration=0.8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=0.8",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    pipeline._encode_part(
        ffmpeg_exe=ffmpeg,
        encoder="libx264",
        source=source,
        destination=output,
        duration=0.8,
        job_id="test-full-movie-render",
    )

    media = pipeline.probe_media(output)
    assert (media["width"], media["height"]) == (1920, 1080)
    assert media["fps"] == pytest.approx(25.0, abs=0.05)
    assert media["audio_sample_rate"] == 44100
    assert media["audio_channels"] == 2

    av = pipeline._load_av()
    with av.open(str(output)) as container:
        frame = next(container.decode(video=0)).to_ndarray(format="rgb24")
    assert frame[:, :200].max() == 0
    assert frame[:, -200:].max() == 0
    assert frame[:, 800:1120].mean() > 5


def test_end_to_end_two_episode_merge_qc_rename_and_cleanup(tmp_path, monkeypatch):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    for index, frequency in ((1, 440), (2, 660)):
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=360x640:rate=25:duration=0.7",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=44100:duration=0.7",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(tmp_path / f"Episode {index}.mp4"),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )

    scan = pipeline.scan_folder(str(tmp_path))
    approved = pipeline.build_approved_mapping(
        scan,
        "Love Returns",
        [
            {"source_name": "Episode 1.mp4", "title_us": "The Beginning"},
            {"source_name": "Episode 2.mp4", "title_us": "The Return"},
        ],
    )
    job_id = "b" * 32
    job = {
        "id": job_id,
        "payload": {
            "source_path": str(tmp_path),
            "scan": scan,
            "source_snapshot": scan["snapshot"],
            "approved": approved,
            "stage": "render",
        },
    }
    updates = []
    monkeypatch.setattr(pipeline, "_nvenc_available", lambda _ffmpeg: False)
    monkeypatch.setattr(
        pipeline.db,
        "get_system_job",
        lambda _job_id: {"id": job_id, "status": "running", "cancel_requested": False},
    )
    monkeypatch.setattr(
        pipeline.db,
        "update_system_job",
        lambda _job_id, **changes: updates.append(changes) or {"id": job_id, **changes},
    )
    thumbnail_calls = []
    thumbnail_kicks = []

    def create_thumbnail(title, movie_path, **kwargs):
        thumbnail_calls.append((title, movie_path, kwargs))
        return {"id": "c" * 32}, True

    monkeypatch.setattr(full_movie_thumbnail_pipeline, "create_job", create_thumbnail)
    monkeypatch.setattr(full_movie_thumbnail_worker, "kick", lambda: thumbnail_kicks.append(True))

    pipeline.execute(job)

    output = tmp_path / "Full Movie - Love Returns.mp4"
    assert output.is_file()
    assert (tmp_path / "EP001 - The Beginning.mp4").is_file()
    assert (tmp_path / "EP002 - The Return.mp4").is_file()
    assert not (tmp_path / ".nexus_full_movie").exists()
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result_json"]["episode_count"] == 2
    assert updates[-1]["result_json"]["thumbnail_job_id"] == "c" * 32
    assert thumbnail_calls == [
        (
            "Love Returns",
            str(output),
            {"origin": "full_movie_merge", "parent_job_id": job_id},
        )
    ]
    assert thumbnail_kicks == [True]
    assert pipeline.probe_media(output)["duration"] == pytest.approx(1.4, abs=0.3)
