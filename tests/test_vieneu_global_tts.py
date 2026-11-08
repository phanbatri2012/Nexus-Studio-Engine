from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from nexus_engine.services import database as db
from nexus_engine.services import tts_registry
from nexus_engine.services import vieneu_tts, vieneu_tts_worker, voice_config


class VieNeuVoiceRegistryTests(unittest.TestCase):
    def test_api_health_distinguishes_missing_configuration(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(vieneu_tts, "VIENEU_TTS_SETTINGS_PATH", Path("missing-settings.json")),
        ):
            health = vieneu_tts.api_health()

        self.assertFalse(health["available"])
        self.assertEqual(health["code"], "api_unconfigured")

    def test_api_generation_sends_only_synthesis_contract(self):
        captured = {}

        class FakeResponse:
            headers = {"X-Request-Id": "provider-request-1"}

            def __init__(self):
                self._chunks = iter((b"\x00\x00" * 480, b""))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, _size):
                return next(self._chunks)

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            output = Path(temp_dir) / "audio.wav"
            with (
                patch.object(vieneu_tts, "api_base_url", return_value="http://127.0.0.1:8000"),
                patch.object(vieneu_tts.urllib.request, "urlopen", side_effect=fake_urlopen),
            ):
                receipt = vieneu_tts.generate_api_audio(
                    text="  Xin chào.  ",
                    provider_voice_id="BH_1",
                    seed=7,
                    output_path=output,
                )

            payload = __import__("json").loads(captured["request"].data.decode("utf-8"))
            self.assertEqual(
                set(payload),
                {"model", "input", "voice", "response_format", "stream_format", "sample_rate", "seed"},
            )
            self.assertEqual(payload["input"], "Xin chào.")
            self.assertEqual(payload["voice"], "BH_1")
            self.assertNotIn("reference", payload)
            self.assertNotIn("temperature", payload)
            self.assertTrue(output.is_file())
            self.assertEqual(receipt["provider_request_id"], "provider-request-1")

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
        self.assertEqual(provider["type"], "local_api")
        self.assertTrue(provider["capabilities"]["remote_voices"])
        self.assertFalse(provider["capabilities"]["voice_clone"])
        self.assertFalse(provider["capabilities"]["parallel_jobs"])

    def test_request_hash_uses_remote_voice_revision_not_local_profile(self):
        base = {
            "voice_id": "test-voice",
            "voice_name": "Test voice",
            "provider_id": voice_config.VIENEU_PROVIDER_ID,
            "provider_voice_id": "test-voice",
            "voice_revision": 1,
            "provider_voice_revision": 3,
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
        self.assertEqual(
            first,
            tts_registry.get_request_hash("Xin chào", "test-voice", changed_reference),
        )
        self.assertEqual(
            first,
            tts_registry.get_request_hash("Xin chào", "test-voice", changed_model),
        )
        changed_revision = copy.deepcopy(base)
        changed_revision["provider_voice_revision"] = 4
        self.assertNotEqual(
            first,
            tts_registry.get_request_hash("Xin chào", "test-voice", changed_revision),
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

    def test_general_tts_preserves_text_for_provider_owned_chunking(self):
        text = ("Một câu đủ dài để kiểm tra việc chia đoạn tự nhiên. " * 20).strip()
        self.assertEqual(vieneu_tts_worker.validate_text(text), text)
        self.assertIn("\n", vieneu_tts_worker.validate_text("Dòng một\nDòng hai"))

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
