from __future__ import annotations

import copy
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from nexus_engine.services import database as db
from nexus_engine.services import short_video_api
from nexus_engine.services import tts_registry
from nexus_engine.services import vieneu_tts_worker, vieneu_voice_profiles, voice_config


def _write_wav(path: Path, seconds: float = 4.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(48000 * seconds)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\x00\x00" * frames)


class VieNeuVoiceRegistryTests(unittest.TestCase):
    def test_schema_v2_migration_adds_vieneu_without_changing_default(self):
        current = voice_config.load_voice_config()
        legacy = copy.deepcopy(current)
        legacy["schema_version"] = 2
        legacy["providers"] = [
            provider
            for provider in legacy["providers"]
            if provider["id"] != voice_config.VIENEU_PROVIDER_ID
        ]
        legacy["voices"] = [
            voice
            for voice in legacy["voices"]
            if voice["provider_id"] != voice_config.VIENEU_PROVIDER_ID
        ]
        expected_default = legacy["default_voice_id"]

        migrated = voice_config.validate_voice_config(legacy)

        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["default_voice_id"], expected_default)
        provider = next(
            item for item in migrated["providers"]
            if item["id"] == voice_config.VIENEU_PROVIDER_ID
        )
        self.assertTrue(provider["capabilities"]["voice_clone"])
        self.assertFalse(provider["capabilities"]["parallel_jobs"])

    def test_global_revision_snapshot_is_relative_and_immutable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            _write_wav(source)
            profiles = root / "profiles"
            runtime = {
                "available": True,
                "detail": "ready",
                "runtime_revision": "runtime-test",
                "model_revision": {"backbone": "model-test"},
                "lock_sha256": "b" * 64,
            }
            with (
                patch.object(vieneu_voice_profiles, "VIENEU_TTS_VOICE_PROFILES_DIR", profiles),
                patch.object(vieneu_voice_profiles.vieneu_tts, "runtime_status", return_value=runtime),
            ):
                vieneu_voice_profiles.create_revision(
                    voice_id="test-voice",
                    revision=1,
                    source_path=source,
                    start_seconds=0,
                    end_seconds=4,
                    transcript="Xin chào",
                    name="Test voice",
                )
                fields = vieneu_voice_profiles.build_voice_snapshot_fields(
                    voice_id="test-voice",
                    revision=1,
                )
                voice_snapshot = {
                    "voice_id": "test-voice",
                    "voice_name": "Test voice",
                    "provider_id": voice_config.VIENEU_PROVIDER_ID,
                    "voice_revision": 1,
                    **fields,
                }
                item_dir = root / "item"
                snapshot = vieneu_voice_profiles.snapshot_revision(
                    voice_snapshot=voice_snapshot,
                    item_dir=item_dir,
                )
                copied = item_dir / snapshot["reference"]["path"]
                self.assertTrue(copied.is_file())
                copied_mtime = copied.stat().st_mtime_ns
                repeated = vieneu_voice_profiles.snapshot_revision(
                    voice_snapshot=voice_snapshot,
                    item_dir=item_dir,
                )
                self.assertEqual(repeated["reference"]["sha256"], snapshot["reference"]["sha256"])
                self.assertEqual(copied.stat().st_mtime_ns, copied_mtime)
                self.assertNotIn(str(root), snapshot["reference"]["path"])
                public = short_video_api._public_profile_snapshot(
                    {"resolved_voice_profile": snapshot}
                )
                self.assertNotIn("path", public["resolved_voice_profile"]["reference"])
                self.assertNotIn("runtime_lock_sha256", public["resolved_voice_profile"])

    def test_request_hash_includes_reference_and_model_revision(self):
        base = {
            "voice_id": "test-voice",
            "voice_name": "Test voice",
            "provider_id": voice_config.VIENEU_PROVIDER_ID,
            "provider_voice_id": "test-voice",
            "voice_revision": 1,
            "config": {"sample_rate": 48000},
            "profile": {
                "voice_id": "test-voice",
                "revision": 1,
                "reference_sha256": "a" * 64,
            },
            "model_revision": {"backbone": "model-a"},
        }
        first = tts_registry.get_request_hash("Xin chào", "test-voice", base)
        changed_reference = copy.deepcopy(base)
        changed_reference["profile"]["reference_sha256"] = "b" * 64
        changed_model = copy.deepcopy(base)
        changed_model["model_revision"] = {"backbone": "model-b"}
        self.assertNotEqual(
            first,
            tts_registry.get_request_hash("Xin chào", "test-voice", changed_reference),
        )
        self.assertNotEqual(
            first,
            tts_registry.get_request_hash("Xin chào", "test-voice", changed_model),
        )


class VieNeuGenerationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(db, "DB_PATH", self.root / "nexus.db")
        self.jobs_patch = patch.object(
            vieneu_tts_worker,
            "VIENEU_TTS_JOBS_DIR",
            self.root / "jobs",
        )
        self.db_patch.start()
        self.jobs_patch.start()
        db.init_db()

    def tearDown(self):
        self.jobs_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def test_general_tts_chunks_are_bounded(self):
        text = ("Một câu đủ dài để kiểm tra việc chia đoạn tự nhiên. " * 20).strip()
        chunks = vieneu_tts_worker.split_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 220 for chunk in chunks))

    def test_enqueue_is_idempotent_and_rejects_changed_payload(self):
        snapshot = {
            "voice_id": "test-voice",
            "voice_name": "Test voice",
            "provider_id": voice_config.VIENEU_PROVIDER_ID,
            "voice_revision": 1,
        }
        with patch.object(vieneu_tts_worker, "kick"):
            first = vieneu_tts_worker.enqueue(
                text="Nội dung một.",
                voice_snapshot=snapshot,
                request_hash="hash-one",
                client_request_id="request-123",
            )
            second = vieneu_tts_worker.enqueue(
                text="Nội dung một.",
                voice_snapshot=snapshot,
                request_hash="hash-one",
                client_request_id="request-123",
            )
            with self.assertRaisesRegex(ValueError, "client_request_id"):
                vieneu_tts_worker.enqueue(
                    text="Nội dung khác.",
                    voice_snapshot=snapshot,
                    request_hash="hash-two",
                    client_request_id="request-123",
                )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            len(db.list_system_jobs(job_type=vieneu_tts_worker.JOB_TYPE, limit=None)),
            1,
        )

    def test_restart_pauses_queued_job_without_starting_coordinator(self):
        snapshot = {
            "voice_id": "test-voice",
            "voice_name": "Test voice",
            "provider_id": voice_config.VIENEU_PROVIDER_ID,
            "voice_revision": 1,
        }
        with patch.object(vieneu_tts_worker, "kick"):
            job = vieneu_tts_worker.enqueue(
                text="Nội dung.",
                voice_snapshot=snapshot,
                request_hash="restart-hash",
                client_request_id="restart-request",
            )
        paused = vieneu_tts_worker.pause_after_restart()
        refreshed = db.get_system_job(job["id"])
        self.assertEqual(paused, 1)
        self.assertEqual(refreshed["status"], "paused")

    def test_preview_client_request_id_is_idempotent_and_conflict_safe(self):
        from nexus_engine import main

        voice = {
            "id": "test-voice",
            "name": "Test voice",
            "provider_id": voice_config.VIENEU_PROVIDER_ID,
            "provider_voice_id": "test-voice",
            "status": "draft",
            "revision": 1,
            "active_revision": None,
            "draft_revision": 1,
            "config": {},
        }
        snapshot = {
            "voice_id": "test-voice",
            "voice_name": "Test voice",
            "provider_id": voice_config.VIENEU_PROVIDER_ID,
            "provider_voice_id": "test-voice",
            "voice_revision": 1,
            "config": {},
            "profile": {
                "voice_id": "test-voice",
                "revision": 1,
                "reference_sha256": "a" * 64,
            },
            "model_revision": {"backbone": "model-a"},
        }

        class FakeProvider:
            submit_count = 0

            def validate_voice(self, _voice):
                return None

            def submit(self, _text, _voice, _request_hash):
                self.submit_count += 1
                return {"id": "vieneu:11111111-1111-1111-1111-111111111111", "status": "queued"}

        provider = FakeProvider()
        request = main.TTSPreviewCreateData(
            voice_id="test-voice",
            text="Xin chào.",
            client_request_id="preview-request-123",
        )
        with (
            patch.object(main.tts, "get_voice_context", return_value=(voice, provider)),
            patch.object(main.voice_config, "build_voice_snapshot", return_value=snapshot),
            patch.object(main, "_sync_tts_preview", side_effect=lambda preview_id: db.get_tts_preview(preview_id)),
        ):
            first = main.create_tts_preview(request)
            second = main.create_tts_preview(request)
            with self.assertRaises(HTTPException) as conflict:
                main.create_tts_preview(
                    main.TTSPreviewCreateData(
                        voice_id="test-voice",
                        text="Nội dung khác.",
                        client_request_id="preview-request-123",
                    )
                )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(provider.submit_count, 1)
        self.assertEqual(conflict.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
