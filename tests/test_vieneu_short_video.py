import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nexus_engine.services import database as db
from nexus_engine.services import short_video_api
from nexus_engine.services import short_video_pipeline
from nexus_engine.services import short_video_renderer
from nexus_engine.services import short_video_worker
from nexus_engine.services import vieneu_tts
from nexus_engine.services.short_video_api import AudioPreviewRequest
from nexus_engine.services.short_video_registry import ShortVideoProjectRegistry


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "short_video" / "poetry-demo.v1.json"


class VieNeuPoemAudioTests(unittest.TestCase):
    def test_profile_validation_uses_remote_voice_snapshot_without_reference(self):
        voice_snapshot = {
            "voice_id": "catalog-bh-1",
            "voice_name": "BH_1",
            "provider_id": "vieneu_v3_turbo",
            "provider_voice_id": "BH_1",
            "provider_voice_revision": 3,
        }
        manifest = {
            "pipeline_profile": {"id": "poem_to_short_v1"},
            "voice_profile": {
                "provider": "vieneu_v3_turbo",
                "voice_id": "catalog-bh-1",
                "voice_name": "BH_1",
            },
            "resolved_voice_snapshot": voice_snapshot,
            "resolved_voice_profile": {
                "provider": "vieneu_v3_turbo",
                "provider_voice_id": "BH_1",
                "provider_voice_revision": 3,
            },
            "visual_profile": {"provider": "muse", "available_providers": ["muse"]},
            "render_profile": {
                "subtitles": False,
                "title_card": False,
                "credits": False,
                "music": False,
                "ambience": False,
            },
        }

        _, _, validated_voice = short_video_pipeline._validate_profile({"manifest": manifest})
        self.assertEqual(validated_voice["provider_voice_id"], "BH_1")
        self.assertNotIn("reference", manifest["resolved_voice_profile"])

        legacy = dict(manifest)
        legacy["audio_profile"] = {"provider": "vieneu_v3_turbo"}
        with self.assertRaisesRegex(short_video_pipeline.ShortVideoPipelineError, "legacy"):
            short_video_pipeline._validate_profile({"manifest": legacy})

    def test_nexus_sends_whole_poem_once_without_title_or_local_delivery(self):
        item = {
            "id": "item-1",
            "title": "TÊN BÀI THƠ KHÔNG ĐƯỢC ĐỌC",
            "source_text": "Dòng thơ thứ nhất\nDòng thơ thứ hai",
        }
        profile = {
            "resolved_voice_snapshot": {
                "provider_voice_id": "BH_1",
                "provider_voice_revision": 3,
            },
            "resolved_voice_profile": {
                "provider_voice_id": "BH_1",
                "provider_voice_revision": 3,
            },
        }
        captured = {"calls": 0}

        class FakeSession:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def generate(self, *, text, seed, output_path):
                captured.update({"calls": captured["calls"] + 1, "text": text, "seed": seed})
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(output_path), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(48000)
                    target.writeframes(b"\x00\x00" * 48000)
                output_path.with_suffix(".receipt.json").write_text("{}", encoding="utf-8")
                return {"duration_seconds": 1.0, "sha256": "a" * 64}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            with (
                patch.object(vieneu_tts, "api_health", return_value={"available": True}),
                patch.object(vieneu_tts, "api_voice", return_value={"id": "BH_1", "revision": 3}),
                patch.object(vieneu_tts, "VieNeuGenerationSession", FakeSession),
                patch.object(short_video_pipeline, "_save_checkpoint"),
            ):
                duration = short_video_pipeline._ensure_vieneu_audio(
                    checkpoint={"audio_revision": 0, "audio": {}},
                    item_dir=Path(temp_dir),
                    job={"id": "job-1"},
                    item=item,
                    profile=profile,
                    max_duration=180,
                )

        self.assertEqual(captured["calls"], 1)
        self.assertEqual(captured["text"], item["source_text"])
        self.assertNotIn(item["title"], captured["text"])
        self.assertEqual(duration, 1.0)

    def test_renderer_probe_falls_back_to_ffmpeg_metadata(self):
        ffmpeg_output = """
Duration: 00:00:12.34, start: 0.000000, bitrate: 1500 kb/s
Stream #0:0: Video: h264 (High), yuv420p, 1080x1920, 30 fps
Stream #0:1: Audio: aac (LC), 48000 Hz, stereo
Stream mapping:
Stream #0:0 -> #0:0 (h264 (native) -> wrapped_avframe (native))
Stream #0:1 -> #0:1 (aac (native) -> pcm_s16le (native))
"""
        with (
            patch.object(short_video_renderer, "_ffprobe_exe", return_value=None),
            patch.object(short_video_renderer, "_ffmpeg_exe", return_value="ffmpeg"),
            patch.object(
                short_video_renderer.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=ffmpeg_output),
            ),
        ):
            metadata = short_video_renderer.probe(Path("draft.mp4"))
        self.assertAlmostEqual(metadata["duration_seconds"], 12.34)
        self.assertEqual(metadata["streams"][0]["codec_name"], "h264")
        self.assertEqual(metadata["streams"][0]["width"], 1080)
        self.assertEqual(metadata["streams"][0]["height"], 1920)
        self.assertEqual(metadata["streams"][0]["r_frame_rate"], "30/1")
        self.assertEqual(metadata["streams"][1]["codec_name"], "aac")
        self.assertEqual(len(metadata["streams"]), 2)


class VieNeuRuntimeTests(unittest.TestCase):
    def test_session_calls_vieneu_api_by_voice_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "candidate.wav"
            receipt = {"sha256": "a" * 64, "provider_request_id": "spk-test"}

            def fake_generate(**kwargs):
                kwargs["output_path"].write_bytes(b"wav")
                return dict(receipt)

            with (
                patch.object(vieneu_tts, "api_health", return_value={"available": True}),
                patch.object(
                    vieneu_tts,
                    "api_voice",
                    return_value={"id": "BH_1", "revision": 3},
                ),
                patch.object(vieneu_tts, "generate_api_audio", side_effect=fake_generate) as generate,
            ):
                with vieneu_tts.VieNeuGenerationSession(
                    job_id="test-session",
                    provider_voice_id="BH_1",
                    work_dir=Path(temp_dir),
                ) as session:
                    result = session.generate(text="Dòng thơ", seed=7, output_path=output)

            self.assertEqual(result["provider_request_id"], "spk-test")
            self.assertTrue(output.is_file())
            self.assertEqual(generate.call_args.kwargs["provider_voice_id"], "BH_1")
            self.assertNotIn("reference_path", generate.call_args.kwargs)

    def test_nexus_does_not_own_a_vieneu_inference_runner(self):
        self.assertFalse((ROOT / "scripts" / "vieneu_tts_runner.py").exists())
        source = (ROOT / "src" / "nexus_engine" / "services" / "vieneu_tts.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"provider_voice_id"', source)
        self.assertIn('/v1/audio/speech', source)
        self.assertNotIn('tts.infer(', source)


class VieNeuRegistryAndPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(db, "DB_PATH", self.root / "preview.db")
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_global_draft_voice_blocks_create_until_activation(self):
        config = self.root / "projects"
        config.mkdir()
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest.pop("character_profile", None)
        (config / "project.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        draft_voice = {
            "id": manifest["voice_profile"]["voice_id"],
            "name": manifest["voice_profile"]["voice_name"],
            "provider_id": "vieneu_v3_turbo",
            "provider_voice_id": manifest["voice_profile"]["voice_name"],
            "status": "draft",
            "revision": 3,
            "active_revision": None,
            "draft_revision": 3,
            "config": {},
            "casting_profile": {"usage_modes": ["single_narrator"]},
        }
        with (
            patch(
                "nexus_engine.services.short_video_registry.voice_config.get_voice",
                side_effect=ValueError("Giọng đọc đã chọn hiện không hoạt động."),
            ),
            patch(
                "nexus_engine.services.short_video_registry.voice_config.load_voice_config",
                return_value={"voices": [draft_voice]},
            ),
        ):
            registry = ShortVideoProjectRegistry(config)
            project = registry.get("poetry-demo")
        self.assertFalse(project["available"])
        voice_dependency = next(
            item for item in project["dependencies"] if item["id"] == "voice"
        )
        self.assertFalse(voice_dependency["available"])
        self.assertIsNone(project["audio_profile_summary"])
        with self.assertRaisesRegex(ValueError, "Dependency"):
            registry.snapshot_for_create("poetry-demo")

    def test_preview_double_click_is_idempotent_and_uses_only_poem(self):
        profile = {
            "provider": "vieneu_v3_turbo",
            "profile_id": "poetry-demo-v1",
            "display_name": "Poetry Demo · VieNeu v1",
            "profile_version": 1,
            "status": "draft",
            "backend": "onnx",
            "precision": "fp32",
            "sample_rate": 48000,
            "runtime_lock_sha256": "b" * 64,
            "reference_sha256": "a" * 64,
        }
        request = AudioPreviewRequest(
            poem="Dòng một\nDòng hai\nDòng ba\nDòng bốn\nDòng năm",
            audio_profile_id=profile["profile_id"],
            client_request_id="preview-request-1",
        )
        with (
            patch.object(
                short_video_api.registry,
                "audio_profile_for_preview",
                return_value=profile,
            ),
            patch.object(short_video_api.short_video_worker, "kick") as kick,
        ):
            first = short_video_api.create_short_video_audio_preview(
                "poetry-demo", request
            )
            second = short_video_api.create_short_video_audio_preview(
                "poetry-demo", request
            )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["preview"]["id"], second["preview"]["id"])
        self.assertEqual(kick.call_count, 2)
        jobs = db.list_system_jobs(limit=None, job_type="short_video_audio_preview")
        self.assertEqual(len(jobs), 1)
        preview = db.get_short_video_audio_preview(first["preview"]["id"])
        self.assertEqual(preview["poem_excerpt"].splitlines(), [
            "Dòng một", "Dòng hai", "Dòng ba", "Dòng bốn"
        ])
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("runtime_lock_sha256", serialized)

        db.update_system_job(first["job"]["id"], status="paused")
        db.update_short_video_audio_preview(
            first["preview"]["id"], status="paused"
        )
        with (
            patch.object(
                short_video_api.registry,
                "audio_profile_for_preview",
                return_value=profile,
            ),
            patch.object(short_video_api.short_video_worker, "kick") as resume_kick,
        ):
            resumed = short_video_api.create_short_video_audio_preview(
                "poetry-demo", request
            )
        self.assertFalse(resumed["created"])
        self.assertEqual(resumed["preview"]["id"], first["preview"]["id"])
        self.assertEqual(resumed["preview"]["status"], "queued")
        self.assertEqual(db.get_system_job(first["job"]["id"])["status"], "queued")
        resume_kick.assert_called_once_with()

    def test_restart_pauses_preview_without_starting_worker(self):
        preview_id = "11111111-1111-4111-8111-111111111111"
        job_id = f"short-video-audio-preview-{preview_id}"
        _preview, _job, created = db.create_short_video_audio_preview_with_job(
            preview_id=preview_id,
            project_key="poetry-demo",
            profile_id="poetry-demo-v1",
            request_hash="c" * 64,
            poem_excerpt="Dòng thơ",
            job_id=job_id,
            payload={"preview_id": preview_id, "audio_profile": {}},
            expires_at="2999-01-01T00:00:00+00:00",
            voice_snapshot={
                "voice_id": "poetry-demo-v1",
                "voice_name": "Poetry Demo · VieNeu v1",
                "provider_id": "vieneu_v3_turbo",
                "voice_revision": 1,
            },
        )
        self.assertTrue(created)
        with patch.object(short_video_worker, "_COORDINATOR", None):
            self.assertEqual(short_video_worker.pause_after_restart(), 1)
            self.assertFalse(short_video_worker.status()["running"])
        self.assertEqual(db.get_system_job(job_id)["status"], "paused")
        self.assertEqual(
            db.get_short_video_audio_preview(preview_id)["status"], "paused"
        )

    def test_public_item_hides_audio_snapshot_path(self):
        manifest = {
            "resolved_audio_profile": {
                "profile_id": "poetry-demo-v1",
                "runtime_lock_sha256": "b" * 64,
                "reference": {
                    "path": "references/audio_profile/reference.wav",
                    "sha256": "a" * 64,
                },
            }
        }
        item = {
            "id": "11111111-1111-4111-8111-111111111111",
            "project_key": "poetry-demo",
            "profile_version": "1.1.0",
            "title": "Cảnh khuya",
            "status": "queued",
            "manifest": manifest,
        }
        public = short_video_api._public_item(item, detail=True)
        serialized = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("references/audio_profile", serialized)
        self.assertNotIn("runtime_lock_sha256", serialized)


if __name__ == "__main__":
    unittest.main()
