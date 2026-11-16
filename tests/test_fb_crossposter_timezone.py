"""Regression tests for Fanpage-local scheduling and UTC persistence."""

import datetime
import sqlite3
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from nexus_engine.services import database as db
from nexus_engine.services import fb_crossposter_service as fb_service
from nexus_engine.services.facebook_schedule_policy import MetaScheduleWindowError
import nexus_engine.main as main


@pytest.fixture
def isolated_fb_database(tmp_path, monkeypatch):
    database_path = tmp_path / "crossposter-timezone.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    conn = sqlite3.connect(database_path)
    conn.executescript(
        """
        CREATE TABLE fb_crossposter_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_fb_page_id TEXT NOT NULL UNIQUE,
            schedule_times_json TEXT DEFAULT '["11:30", "19:30"]',
            daily_quota INTEGER DEFAULT 2,
            lead_time_minutes INTEGER DEFAULT 60,
            publication_timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh'
        );
        CREATE TABLE fb_crossposter_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_page_id TEXT DEFAULT '',
            youtube_id TEXT NOT NULL,
            youtube_url TEXT DEFAULT '',
            original_title TEXT DEFAULT '',
            original_description TEXT DEFAULT '',
            original_tags_json TEXT DEFAULT '[]',
            thumbnail_url TEXT DEFAULT '',
            youtube_upload_date TEXT DEFAULT '',
            fb_title TEXT DEFAULT '',
            fb_description TEXT DEFAULT '',
            fb_description_source TEXT DEFAULT 'auto',
            status TEXT DEFAULT 'pending',
            sort_order INTEGER DEFAULT 0,
            scheduled_publish_time INTEGER DEFAULT 0,
            meta_scheduled_publish_time INTEGER DEFAULT 0,
            fb_post_id TEXT DEFAULT '',
            upload_session_id TEXT DEFAULT '',
            upload_video_id TEXT DEFAULT '',
            checkpoint_phase TEXT DEFAULT '',
            can_resume INTEGER DEFAULT 0,
            meta_state TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            UNIQUE(youtube_id, target_page_id)
        );
        """
    )
    conn.close()
    return database_path


def _insert_queue_item(page_id: str, youtube_id: str, *, status: str = "pending") -> int:
    conn = sqlite3.connect(db.DB_PATH)
    cursor = conn.execute(
        """
        INSERT INTO fb_crossposter_queue (
            target_page_id, youtube_id, status, original_title, sort_order
        ) VALUES (?, ?, ?, ?, 1)
        """,
        (page_id, youtube_id, status, youtube_id),
    )
    conn.commit()
    item_id = int(cursor.lastrowid)
    conn.close()
    return item_id


def test_same_wall_clock_creates_different_epoch_per_fanpage_timezone(
    isolated_fb_database,
):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    start_date = datetime.date(2026, 1, 16)
    vietnam_item = _insert_queue_item("vn", "video-vn")
    new_york_item = _insert_queue_item("ny", "video-ny")

    db.recalculate_fb_queue_schedule(
        1,
        ["19:30"],
        target_page_id="vn",
        start_date=start_date,
        timezone_name="Asia/Ho_Chi_Minh",
        now_utc=now_utc,
    )
    db.recalculate_fb_queue_schedule(
        1,
        ["19:30"],
        target_page_id="ny",
        start_date=start_date,
        timezone_name="America/New_York",
        now_utc=now_utc,
    )

    vietnam_epoch = db.get_fb_crossposter_queue_item(vietnam_item)[
        "scheduled_publish_time"
    ]
    new_york_epoch = db.get_fb_crossposter_queue_item(new_york_item)[
        "scheduled_publish_time"
    ]
    assert datetime.datetime.fromtimestamp(
        vietnam_epoch, ZoneInfo("Asia/Ho_Chi_Minh")
    ).strftime("%Y-%m-%d %H:%M") == "2026-01-16 19:30"
    assert datetime.datetime.fromtimestamp(
        new_york_epoch, ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %H:%M") == "2026-01-16 19:30"
    assert new_york_epoch - vietnam_epoch == 12 * 60 * 60


def test_recalculate_skips_nonexistent_dst_slot(isolated_fb_database):
    item_id = _insert_queue_item("ny", "video-dst")
    db.recalculate_fb_queue_schedule(
        1,
        ["02:30"],
        target_page_id="ny",
        start_date=datetime.date(2026, 3, 8),
        timezone_name="America/New_York",
        now_utc=datetime.datetime(2026, 3, 7, tzinfo=datetime.timezone.utc),
    )
    scheduled_epoch = db.get_fb_crossposter_queue_item(item_id)[
        "scheduled_publish_time"
    ]
    scheduled_local = datetime.datetime.fromtimestamp(
        scheduled_epoch, ZoneInfo("America/New_York")
    )
    assert scheduled_local.strftime("%Y-%m-%d %H:%M %z") == "2026-03-09 02:30 -0400"


def test_reserve_uses_campaign_timezone_and_new_rows_use_utc_iso(
    isolated_fb_database,
):
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        """
        INSERT INTO fb_crossposter_settings (
            target_fb_page_id, schedule_times_json, daily_quota,
            lead_time_minutes, publication_timezone
        ) VALUES ('vn', '["19:30"]', 1, 30, 'Asia/Ho_Chi_Minh')
        """
    )
    conn.commit()
    conn.close()
    item_id = _insert_queue_item("vn", "video-reserve")

    reserved_epoch = db.reserve_next_fb_queue_slot(
        item_id,
        now_utc=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
    )
    assert datetime.datetime.fromtimestamp(
        reserved_epoch, ZoneInfo("Asia/Ho_Chi_Minh")
    ).strftime("%Y-%m-%d %H:%M") == "2026-01-15 19:30"

    db.upsert_fb_crossposter_queue_items(
        [{"youtube_id": "video-new", "original_title": "New video"}],
        target_page_id="vn",
    )
    new_item = db.get_fb_crossposter_queue(target_page_id="vn", search="video-new")[
        "items"
    ][0]
    assert new_item["created_at"].endswith("+00:00")
    assert new_item["updated_at"].endswith("+00:00")


def test_reserve_stops_when_meta_window_is_full(isolated_fb_database):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        """
        INSERT INTO fb_crossposter_settings (
            target_fb_page_id, schedule_times_json, daily_quota,
            lead_time_minutes, publication_timezone
        ) VALUES ('vn', '["19:30"]', 1, 30, 'Asia/Ho_Chi_Minh')
        """
    )
    conn.commit()
    conn.close()
    target_item = _insert_queue_item("vn", "video-target")

    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    for day_offset in range(29):
        occupied_item = _insert_queue_item(
            "vn", f"video-occupied-{day_offset}", status="scheduled"
        )
        local_slot = datetime.datetime.combine(
            now_utc.astimezone(zone).date() + datetime.timedelta(days=day_offset),
            datetime.time(19, 30),
            tzinfo=zone,
        )
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute(
            "UPDATE fb_crossposter_queue SET scheduled_publish_time = ?, fb_post_id = ? WHERE id = ?",
            (int(local_slot.timestamp()), f"meta-{day_offset}", occupied_item),
        )
        conn.commit()
        conn.close()

    with pytest.raises(db.MetaScheduleCapacityError, match="29 ngày"):
        db.reserve_next_fb_queue_slot(target_item, now_utc=now_utc)
    target = db.get_fb_crossposter_queue_item(target_item)
    assert target["status"] == "pending"
    assert target["scheduled_publish_time"] == 0


def test_reserve_displaces_farthest_local_only_slot(isolated_fb_database):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        """
        INSERT INTO fb_crossposter_settings (
            target_fb_page_id, schedule_times_json, daily_quota,
            lead_time_minutes, publication_timezone
        ) VALUES ('vn', '["19:30"]', 1, 30, 'Asia/Ho_Chi_Minh')
        """
    )
    conn.commit()
    conn.close()
    target_item = _insert_queue_item("vn", "video-priority")

    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    occupied_ids = []
    for day_offset in range(29):
        occupied_item = _insert_queue_item(
            "vn", f"video-local-{day_offset}", status="scheduled"
        )
        occupied_ids.append(occupied_item)
        local_slot = datetime.datetime.combine(
            now_utc.astimezone(zone).date() + datetime.timedelta(days=day_offset),
            datetime.time(19, 30),
            tzinfo=zone,
        )
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute(
            "UPDATE fb_crossposter_queue SET scheduled_publish_time = ? WHERE id = ?",
            (int(local_slot.timestamp()), occupied_item),
        )
        conn.commit()
        conn.close()

    reserved = db.reserve_next_fb_queue_slot(target_item, now_utc=now_utc)

    displaced = db.get_fb_crossposter_queue_item(occupied_ids[-1])
    assert displaced["status"] == "pending"
    assert displaced["scheduled_publish_time"] == 0
    assert db.get_fb_crossposter_queue_item(target_item)["scheduled_publish_time"] == reserved


def test_recalculate_leaves_overflow_pending(isolated_fb_database):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    item_ids = [_insert_queue_item("vn", f"overflow-{index}") for index in range(35)]

    assigned = db.recalculate_fb_queue_schedule(
        1,
        ["19:30"],
        target_page_id="vn",
        timezone_name="Asia/Ho_Chi_Minh",
        now_utc=now_utc,
    )

    items = [db.get_fb_crossposter_queue_item(item_id) for item_id in item_ids]
    scheduled = [item for item in items if item["status"] == "scheduled"]
    pending = [item for item in items if item["status"] == "pending"]
    assert assigned == 29
    assert len(scheduled) == 29
    assert len(pending) == 6
    _, latest_ts = db.meta_schedule_window_epochs(now_utc=now_utc)
    assert max(item["scheduled_publish_time"] for item in scheduled) <= latest_ts
    assert all(item["scheduled_publish_time"] == 0 for item in pending)


def test_recalculate_starts_after_verified_meta_calendar_frontier(isolated_fb_database):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    occupied_time = int(
        datetime.datetime(2026, 1, 18, 19, 30, tzinfo=zone).timestamp()
    )
    item_id = _insert_queue_item("vn", "calendar-blocked")
    db.replace_fb_meta_calendar_occupancy(
        "vn",
        [{"scheduled_publish_time": occupied_time, "title": "Bài ngoài tool"}],
    )

    assigned = db.recalculate_fb_queue_schedule(
        1,
        ["19:30"],
        target_page_id="vn",
        start_date=datetime.date(2026, 1, 16),
        timezone_name="Asia/Ho_Chi_Minh",
        now_utc=now_utc,
    )

    scheduled = db.get_fb_crossposter_queue_item(item_id)["scheduled_publish_time"]
    assert assigned == 1
    assert scheduled > occupied_time
    assert datetime.datetime.fromtimestamp(scheduled, zone).strftime(
        "%Y-%m-%d %H:%M"
    ) == "2026-01-19 19:30"


def test_jit_reservation_starts_after_verified_meta_calendar_frontier(
    isolated_fb_database,
):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    occupied_time = int(
        datetime.datetime(2026, 1, 18, 19, 30, tzinfo=zone).timestamp()
    )
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        """
        INSERT INTO fb_crossposter_settings (
            target_fb_page_id, schedule_times_json, daily_quota,
            lead_time_minutes, publication_timezone
        ) VALUES ('vn', '["19:30"]', 1, 30, 'Asia/Ho_Chi_Minh')
        """
    )
    conn.commit()
    conn.close()
    item_id = _insert_queue_item("vn", "jit-calendar-blocked")
    db.replace_fb_meta_calendar_occupancy(
        "vn",
        [{"scheduled_publish_time": occupied_time, "title": "Bài ngoài tool"}],
    )

    reserved = db.reserve_next_fb_queue_slot(item_id, now_utc=now_utc)

    assert reserved > occupied_time
    assert datetime.datetime.fromtimestamp(reserved, zone).strftime(
        "%Y-%m-%d %H:%M"
    ) == "2026-01-19 19:30"


def test_recalculate_preserves_resumable_item_and_its_slot(isolated_fb_database):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    resumable_item = _insert_queue_item("vn", "resumable", status="scheduled")
    pending_item = _insert_queue_item("vn", "pending")
    first_slot = int(
        datetime.datetime.combine(
            now_utc.astimezone(zone).date(),
            datetime.time(19, 30),
            tzinfo=zone,
        ).timestamp()
    )
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        """
        UPDATE fb_crossposter_queue
        SET scheduled_publish_time = ?, can_resume = 1, checkpoint_phase = 'CP7_SCHEDULE_SET'
        WHERE id = ?
        """,
        (first_slot, resumable_item),
    )
    conn.commit()
    conn.close()

    assigned = db.recalculate_fb_queue_schedule(
        1,
        ["19:30"],
        target_page_id="vn",
        timezone_name="Asia/Ho_Chi_Minh",
        now_utc=now_utc,
    )

    resumable = db.get_fb_crossposter_queue_item(resumable_item)
    pending = db.get_fb_crossposter_queue_item(pending_item)
    assert assigned == 1
    assert resumable["status"] == "scheduled"
    assert resumable["scheduled_publish_time"] == first_slot
    assert pending["scheduled_publish_time"] > first_slot


def test_reset_out_of_window_schedules_preserves_meta_and_checkpoint(
    isolated_fb_database,
):
    now_utc = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
    far_epoch = int((now_utc + datetime.timedelta(days=60)).timestamp())
    local_item = _insert_queue_item("vn", "local-far", status="scheduled")
    paused_item = _insert_queue_item("vn", "paused-far", status="checkpoint_paused")
    remote_item = _insert_queue_item("vn", "remote-far", status="meta_scheduled")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        "UPDATE fb_crossposter_queue SET scheduled_publish_time = ? WHERE id = ?",
        (far_epoch, local_item),
    )
    conn.execute(
        """
        UPDATE fb_crossposter_queue
        SET scheduled_publish_time = ?, can_resume = 1, checkpoint_phase = 'CP7_SCHEDULE_SET'
        WHERE id = ?
        """,
        (far_epoch, paused_item),
    )
    conn.execute(
        """
        UPDATE fb_crossposter_queue
        SET scheduled_publish_time = ?, fb_post_id = 'meta-remote', meta_state = 'scheduled'
        WHERE id = ?
        """,
        (far_epoch, remote_item),
    )
    conn.commit()
    conn.close()

    result = db.reset_fb_queue_schedules_outside_meta_window(
        "vn", now_utc=now_utc
    )
    assert result == {"reset_count": 2, "pending_count": 1, "checkpoint_count": 1}
    assert db.get_fb_crossposter_queue_item(local_item)["status"] == "pending"
    paused = db.get_fb_crossposter_queue_item(paused_item)
    assert paused["status"] == "checkpoint_paused"
    assert paused["checkpoint_phase"] == "CP7_SCHEDULE_SET"
    assert paused["scheduled_publish_time"] == 0
    assert db.get_fb_crossposter_queue_item(remote_item)["scheduled_publish_time"] == far_epoch


def test_pause_interrupted_fb_queue_only_touches_pre_meta_checkpoints(
    isolated_fb_database,
):
    pre_meta_item = _insert_queue_item("vn", "pre-meta", status="downloading")
    submitted_item = _insert_queue_item("vn", "submitted", status="verifying")
    session_item = _insert_queue_item("vn", "session", status="uploading")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        """
        UPDATE fb_crossposter_queue
        SET checkpoint_phase = 'CP1_SOURCE_READY', can_resume = 1,
            scheduled_publish_time = 1800000000
        WHERE id = ?
        """,
        (pre_meta_item,),
    )
    conn.execute(
        "UPDATE fb_crossposter_queue SET checkpoint_phase = 'CP8_SUBMITTED' WHERE id = ?",
        (submitted_item,),
    )
    conn.execute(
        """
        UPDATE fb_crossposter_queue
        SET checkpoint_phase = 'CP2_MEDIA_PREPARED', upload_session_id = 'session-remote'
        WHERE id = ?
        """,
        (session_item,),
    )
    conn.commit()
    conn.close()

    assert db.pause_interrupted_fb_queue_checkpoints() == 1
    recovered = db.get_fb_crossposter_queue_item(pre_meta_item)
    assert recovered["status"] == "checkpoint_paused"
    assert recovered["scheduled_publish_time"] == 0
    assert recovered["can_resume"] == 1
    assert db.get_fb_crossposter_queue_item(submitted_item)["status"] == "verifying"
    assert db.get_fb_crossposter_queue_item(session_item)["status"] == "uploading"


def test_recalculate_leaves_meta_submitted_rows_unchanged(isolated_fb_database):
    item_id = _insert_queue_item("vn", "video-meta", status="meta_scheduled")
    original_epoch = 1_800_000_000
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        """
        UPDATE fb_crossposter_queue
        SET scheduled_publish_time = ?, fb_post_id = 'meta-123', meta_state = 'scheduled'
        WHERE id = ?
        """,
        (original_epoch, item_id),
    )
    conn.commit()
    conn.close()

    assert db.recalculate_fb_queue_schedule(
        1,
        ["19:30"],
        target_page_id="vn",
        timezone_name="Asia/Ho_Chi_Minh",
        now_utc=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
    ) == 0
    assert db.get_fb_crossposter_queue_item(item_id)[
        "scheduled_publish_time"
    ] == original_epoch


def test_queue_update_converts_local_datetime_with_campaign_timezone(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main.db,
        "get_fb_crossposter_queue_item",
        lambda _item_id: {"id": 7, "target_page_id": "ny"},
    )
    monkeypatch.setattr(
        main.db,
        "get_fb_crossposter_settings",
        lambda _page_id: {"publication_timezone": "America/New_York"},
    )
    monkeypatch.setattr(
        main.db,
        "update_fb_crossposter_queue_item",
        lambda _item_id, fields: captured.update(fields) or True,
    )

    target_local = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    ).astimezone(ZoneInfo("America/New_York")).replace(second=0, microsecond=0)
    main.update_fb_crossposter_item(
        7,
        main.FBCrossPosterItemUpdatePayload(
            scheduled_publish_local=target_local.replace(tzinfo=None).isoformat(timespec="minutes")
        ),
    )
    expected = int(target_local.timestamp())
    assert captured["scheduled_publish_time"] == expected


def test_queue_update_keeps_legacy_epoch_contract(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main.db,
        "update_fb_crossposter_queue_item",
        lambda _item_id, fields: captured.update(fields) or True,
    )
    monkeypatch.setattr(
        main.db,
        "get_fb_crossposter_queue_item",
        lambda _item_id: {"id": 7, **captured},
    )

    scheduled_epoch = int(
        (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).timestamp()
    )
    main.update_fb_crossposter_item(
        7,
        main.FBCrossPosterItemUpdatePayload(scheduled_publish_time=scheduled_epoch),
    )
    assert captured["scheduled_publish_time"] == scheduled_epoch


def test_queue_update_rejects_schedule_beyond_meta_window(monkeypatch):
    monkeypatch.setattr(
        main.db,
        "update_fb_crossposter_queue_item",
        lambda _item_id, fields: True,
    )
    far_epoch = int(
        (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).timestamp()
    )
    with pytest.raises(HTTPException) as exc_info:
        main.update_fb_crossposter_item(
            7,
            main.FBCrossPosterItemUpdatePayload(scheduled_publish_time=far_epoch),
        )
    assert exc_info.value.status_code == 422
    assert "29 ngày" in str(exc_info.value.detail)


def test_schedule_ahead_payload_rejects_more_than_meta_window():
    with pytest.raises(ValueError):
        main.FBCrossPosterScheduleAheadPayload(page_id="vn", days_ahead=30)


def test_graph_upload_rejects_far_schedule_before_proxy(tmp_path, monkeypatch):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    monkeypatch.setattr(
        fb_service,
        "_get_proxy_for_gpm_profile",
        lambda _profile_id: pytest.fail("proxy must not be resolved"),
    )
    far_epoch = int(
        (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).timestamp()
    )

    with pytest.raises(MetaScheduleWindowError, match="29 ngày"):
        fb_service.upload_video_to_facebook(
            page_id="page-test",
            access_token="token-test",
            video_path=video_path,
            title="test",
            description="test",
            scheduled_publish_time=far_epoch,
        )


def test_queue_update_rejects_nonexistent_dst_local_time(monkeypatch):
    monkeypatch.setattr(
        main.db,
        "get_fb_crossposter_queue_item",
        lambda _item_id: {"id": 7, "target_page_id": "ny"},
    )
    monkeypatch.setattr(
        main.db,
        "get_fb_crossposter_settings",
        lambda _page_id: {"publication_timezone": "America/New_York"},
    )

    with pytest.raises(HTTPException) as exc_info:
        main.update_fb_crossposter_item(
            7,
            main.FBCrossPosterItemUpdatePayload(
                scheduled_publish_local="2026-03-08T02:30"
            ),
        )
    assert exc_info.value.status_code == 422
    assert "DST" in str(exc_info.value.detail)


def test_settings_reject_invalid_iana_timezone(monkeypatch):
    monkeypatch.setattr(main.db, "get_fb_crossposter_settings", lambda _page_id: {})
    with pytest.raises(HTTPException) as exc_info:
        main.save_fb_crossposter_settings(
            main.FBCrossPosterSettingsPayload(
                target_fb_page_id="page-invalid-tz",
                publication_timezone="Invalid/Timezone",
            )
        )
    assert exc_info.value.status_code == 422
    assert "Invalid/Timezone" in str(exc_info.value.detail)
