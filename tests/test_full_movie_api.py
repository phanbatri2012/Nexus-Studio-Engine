from __future__ import annotations

from nexus_engine import main


def test_full_movie_routes_are_registered():
    paths = {route.path for route in main.app.routes if hasattr(route, "path")}

    assert "/api/full-movie/select-folder" in paths
    assert "/api/full-movie/scan" in paths
    assert "/api/full-movie/jobs" in paths
    assert "/api/full-movie/jobs/latest" in paths
    assert "/api/full-movie/jobs/{job_id}" in paths
    assert "/api/full-movie/jobs/{job_id}/approve" in paths
    assert "/api/full-movie/thumbnail/select-video" in paths
    assert "/api/full-movie/thumbnail/jobs" in paths
    assert "/api/full-movie/thumbnail/jobs/latest" in paths
    assert "/api/full-movie/thumbnail/jobs/{job_id}" in paths
    assert "/api/full-movie/thumbnail/jobs/{job_id}/images/{slot}" in paths


def test_create_full_movie_job_kicks_only_the_full_movie_worker(monkeypatch):
    fake_job = {
        "id": "a" * 32,
        "status": "queued",
        "title": "Ghép phim Full · Demo",
        "progress": "Đang chờ",
        "payload": {"stage": "translate", "scan": {}},
        "result": {},
    }
    kicked = []
    monkeypatch.setattr(
        main.full_movie_pipeline,
        "create_job",
        lambda source_path, ordered: (fake_job, True),
    )
    monkeypatch.setattr(main.full_movie_worker, "kick", lambda: kicked.append(True))

    result = main.create_full_movie_job(
        main.FullMovieCreateRequest(
            source_path=r"E:\Movies\Demo",
            ordered_source_names=["Episode 1.mp4"],
        )
    )

    assert result["success"] is True
    assert result["created"] is True
    assert result["job"]["id"] == fake_job["id"]
    assert kicked == [True]


def test_job_center_exposes_safe_full_movie_controls():
    base = {
        "id": "c" * 32,
        "job_type": "full_movie_merge",
        "status": "running",
        "title": "Ghép phim Full · Demo",
        "progress": "Đang mã hóa",
        "payload": {"source_path": r"E:\Movies\Demo"},
        "result": {},
        "error": "",
        "cancel_requested": False,
        "resume_from_step": "encoding",
    }

    running = main._system_job_center_item(base, None)
    review = main._system_job_center_item(
        {
            **base,
            "status": "paused",
            "result": {"attention_required": "filename_review"},
        },
        None,
    )
    restart_paused = main._system_job_center_item(
        {
            **base,
            "status": "paused",
            "result": {"attention_required": "full_movie_resume_after_restart"},
        },
        None,
    )

    assert running["type_label"] == "Ghép phim Full"
    assert running["can_pause"] is True
    assert running["can_cancel"] is True
    assert review["can_resume"] is True
    assert restart_paused["can_resume"] is True


def test_latest_full_movie_job_returns_most_recent_job(monkeypatch):
    fake_job = {
        "id": "d" * 32,
        "status": "running",
        "title": "Ghép phim Full · Latest",
        "progress": "Đang ghép",
        "payload": {"stage": "render", "scan": {"source_path": r"E:\Movies\Latest"}},
        "result": {},
    }
    monkeypatch.setattr(
        main.db,
        "list_system_jobs",
        lambda **kwargs: [fake_job],
    )

    result = main.get_latest_full_movie_job()

    assert result["success"] is True
    assert result["job"]["id"] == fake_job["id"]
    assert result["job"]["scan"]["source_path"] == r"E:\Movies\Latest"


def test_create_full_movie_thumbnail_job_kicks_thumbnail_worker(monkeypatch):
    fake_job = {
        "id": "e" * 32,
        "job_type": "full_movie_thumbnail",
        "status": "queued",
        "title": "Tạo thumbnail phim · Demo",
        "progress": "Đang chờ",
        "payload": {
            "stage": "scan_frames",
            "input_title": "Demo",
            "input_path": r"E:\Movies\Demo.mp4",
            "resolved_video_path": r"E:\Movies\Demo.mp4",
        },
        "result": {},
    }
    kicked = []
    monkeypatch.setattr(
        main.full_movie_thumbnail_pipeline,
        "create_job",
        lambda title, movie_path: (fake_job, True),
    )
    monkeypatch.setattr(
        main.full_movie_thumbnail_worker,
        "kick",
        lambda: kicked.append(True),
    )

    result = main.create_full_movie_thumbnail_job(
        main.FullMovieThumbnailCreateRequest(
            title="Demo",
            movie_path=r"E:\Movies\Demo.mp4",
        )
    )

    assert result["success"] is True
    assert result["job"]["id"] == fake_job["id"]
    assert kicked == [True]


def test_job_center_exposes_full_movie_thumbnail_controls():
    base = {
        "id": "f" * 32,
        "job_type": "full_movie_thumbnail",
        "status": "running",
        "title": "Tạo thumbnail phim · Demo",
        "progress": "Đang tạo ảnh",
        "payload": {"resolved_video_path": r"E:\Movies\Demo.mp4"},
        "result": {},
        "error": "",
        "cancel_requested": False,
        "resume_from_step": "generate_images",
    }

    running = main._system_job_center_item(base, None)
    paused = main._system_job_center_item(
        {
            **base,
            "status": "paused",
            "result": {"attention_required": "chatgpt_verification"},
        },
        None,
    )

    assert running["type_label"] == "Tạo thumbnail phim"
    assert running["can_pause"] is True
    assert running["can_cancel"] is True
    assert paused["can_resume"] is True
