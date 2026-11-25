import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from nexus_engine.services import source_voice_analyzer as analyzer
from nexus_engine.services.proxy_utils import ProxyConfigurationError


class _FakeYoutubeDL:
    last_options = None

    def __init__(self, options):
        self.options = options
        _FakeYoutubeDL.last_options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def download(self, _urls):
        target = Path(self.options["outtmpl"].replace("%(ext)s", "webm"))
        target.write_bytes(b"audio")
        for hook in self.options.get("progress_hooks") or []:
            hook({"downloaded_bytes": target.stat().st_size})


class _FailingYoutubeDL(_FakeYoutubeDL):
    def download(self, _urls):
        raise RuntimeError("http://private-user:private-pass@127.0.0.1:8080")


class _TimeoutYoutubeDL(_FakeYoutubeDL):
    def download(self, _urls):
        raise RuntimeError("socket operation timed out")


def _write_wav(path: Path):
    samples = (np.zeros(analyzer.SAMPLE_RATE, dtype=np.int16)).tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(analyzer.SAMPLE_RATE)
        handle.writeframes(samples)


class TestSourceVoiceAnalyzer(unittest.TestCase):
    def test_malformed_benchmark_never_enables_auto_mode(self):
        with tempfile.TemporaryDirectory() as temp_name:
            benchmark = Path(temp_name) / "benchmark.json"
            benchmark.write_text(
                json.dumps({"male_count": 10, "female_count": 10, "error_rate": -1}),
                encoding="utf-8",
            )
            with patch.object(analyzer, "BENCHMARK_PATH", benchmark):
                status = analyzer.benchmark_gate_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["mode"], "advisory")

    def test_proxy_is_required_before_subprocess_or_network_access(self):
        with patch.object(analyzer, "_run_analysis_subprocess") as runner:
            with self.assertRaises(ProxyConfigurationError):
                analyzer.analyze_source_voice(
                    "https://www.youtube.com/watch?v=abcdefghijk",
                    duration_seconds=600,
                    proxy="",
                )
        runner.assert_not_called()

    def test_ytdlp_source_download_is_bounded_and_receives_proxy(self):
        with tempfile.TemporaryDirectory() as temp_name, patch.object(
            analyzer, "YoutubeDL", _FakeYoutubeDL
        ):
            output = analyzer._download_source(
                "https://www.youtube.com/watch?v=abcdefghijk",
                proxy_url="http://user:pass@127.0.0.1:8080",
                output_stem=Path(temp_name) / "source",
            )
        self.assertTrue(output.name.endswith(".webm"))
        options = _FakeYoutubeDL.last_options
        self.assertEqual(options["proxy"], "http://user:pass@127.0.0.1:8080")
        self.assertEqual(options["max_filesize"], analyzer.MAX_SOURCE_AUDIO_BYTES)
        self.assertEqual(options["socket_timeout"], analyzer.SOURCE_SOCKET_TIMEOUT_SECONDS)
        self.assertEqual(options["retries"], analyzer.SOURCE_DOWNLOAD_RETRIES)
        self.assertNotIn("download_sections", options)

    def test_ytdlp_socket_timeout_has_transient_failure_code(self):
        with tempfile.TemporaryDirectory() as temp_name, patch.object(
            analyzer, "YoutubeDL", _TimeoutYoutubeDL
        ), self.assertRaises(analyzer.SourceVoiceAnalysisError) as raised:
            analyzer._download_source(
                "https://www.youtube.com/watch?v=abcdefghijk",
                proxy_url="http://user:pass@127.0.0.1:8080",
                output_stem=Path(temp_name) / "source",
            )
        self.assertEqual(raised.exception.failure_code, "source_media_timeout")
        self.assertTrue(raised.exception.retryable)

    def test_invalid_cached_model_checksum_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_name:
            model_path = Path(temp_name) / "model.onnx"
            model_path.write_bytes(b"invalid")
            with patch.object(analyzer, "MODEL_PATH", model_path), self.assertRaises(
                analyzer.SourceVoiceModelError
            ):
                analyzer.ensure_model()

    def _run_with_windows(
        self,
        voiced_results,
        classifier_results,
        benchmark_enabled=True,
    ):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        temp_root = Path(temp_dir.name) / "source-temp"
        model_path = Path(temp_dir.name) / "model.onnx"
        model_path.write_bytes(b"model")
        download_calls = []
        extract_calls = []

        def download(_url, *, output_stem, **_kwargs):
            download_calls.append(output_stem)
            source = output_stem.with_suffix(".webm")
            source.write_bytes(b"audio")
            return source

        def extract(_source, target, *, start, end):
            extract_calls.append((start, end))
            _write_wav(target)

        patches = [
            patch.object(analyzer, "TEMP_ROOT", temp_root),
            patch.object(analyzer, "ensure_model", return_value=model_path),
            patch.object(analyzer.ort, "InferenceSession", return_value=MagicMock()),
            patch.object(analyzer, "_download_source", side_effect=download),
            patch.object(analyzer, "_extract_window_to_mono_wav", side_effect=extract),
            patch.object(analyzer, "_voiced_audio", side_effect=voiced_results),
            patch.object(
                analyzer, "_classify_chunks", side_effect=classifier_results
            ),
            patch.object(
                analyzer,
                "benchmark_gate_status",
                return_value={
                    "enabled": benchmark_enabled,
                    "mode": "auto" if benchmark_enabled else "advisory",
                },
            ),
        ]
        for active_patch in patches:
            active_patch.start()
        try:
            result = analyzer._analyze_source_voice_impl(
                "https://www.youtube.com/watch?v=abcdefghijk",
                duration_seconds=18_000,
                proxy="http://127.0.0.1:8080",
            )
            remaining = list(temp_root.iterdir()) if temp_root.exists() else []
            return result, remaining, download_calls, extract_calls
        finally:
            for active_patch in reversed(patches):
                active_patch.stop()
            temp_dir.cleanup()

    def test_five_hour_source_downloads_once_and_extracts_three_windows(self):
        voiced = np.ones(analyzer.SAMPLE_RATE * 4, dtype=np.float32)
        result, remaining, download_calls, extract_calls = self._run_with_windows(
            [(voiced, 4.0), (voiced, 4.0), (voiced, 4.0)],
            [(0.97, 0.03, 4.0), (0.96, 0.04, 4.0), (0.98, 0.02, 4.0)],
        )
        self.assertEqual(result["gender"], "male")
        self.assertEqual(result["analyzer_revision"], analyzer.ANALYZER_REVISION)
        self.assertEqual(len(download_calls), 1)
        self.assertEqual(len(extract_calls), 3)
        self.assertEqual(remaining, [])

    def test_vad_insufficient_speech_returns_any(self):
        empty = np.empty(0, dtype=np.float32)
        result, remaining, _downloads, _extracts = self._run_with_windows(
            [(empty, 0.0), (empty, 0.0), (empty, 0.0)],
            [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)],
        )
        self.assertEqual(result["gender"], "any")
        self.assertEqual(result["status"], "insufficient_speech")
        self.assertEqual(remaining, [])

    def test_disagreeing_windows_do_not_auto_classify(self):
        voiced = np.ones(analyzer.SAMPLE_RATE * 4, dtype=np.float32)
        result, _remaining, _downloads, _extracts = self._run_with_windows(
            [(voiced, 4.0), (voiced, 4.0), (voiced, 4.0)],
            [(0.99, 0.01, 4.0), (0.01, 0.99, 4.0), (0.99, 0.01, 4.0)],
        )
        self.assertEqual(result["gender"], "any")
        self.assertFalse(result["threshold_passed"])
        self.assertLess(result["agreement"], analyzer.MIN_DURATION_AGREEMENT)

    def test_benchmark_gate_keeps_high_confidence_result_advisory(self):
        voiced = np.ones(analyzer.SAMPLE_RATE * 4, dtype=np.float32)
        result, _remaining, _downloads, _extracts = self._run_with_windows(
            [(voiced, 4.0), (voiced, 4.0), (voiced, 4.0)],
            [(0.97, 0.03, 4.0), (0.96, 0.04, 4.0), (0.98, 0.02, 4.0)],
            benchmark_enabled=False,
        )
        self.assertEqual(result["gender"], "any")
        self.assertEqual(result["predicted_timbre"], "male")
        self.assertEqual(result["status"], "advisory")
        self.assertTrue(result["threshold_passed"])

    def test_twelve_seconds_are_classified_in_bounded_chunks(self):
        audio = np.ones(analyzer.SAMPLE_RATE * 12, dtype=np.float32)
        seen_lengths = []

        def classify(_session, chunk):
            seen_lengths.append(len(chunk))
            return 0.9, 0.1

        with patch.object(analyzer, "_classify", side_effect=classify):
            male, female, seconds = analyzer._classify_chunks(MagicMock(), audio)
        self.assertEqual(len(seen_lengths), 6)
        self.assertTrue(all(size <= analyzer.MAX_CLASSIFY_SAMPLES for size in seen_lengths))
        self.assertEqual(seconds, 12.0)
        self.assertAlmostEqual(male, 0.9)
        self.assertAlmostEqual(female, 0.1)

    def test_short_tail_is_dropped_from_classification(self):
        audio = np.ones(int(analyzer.SAMPLE_RATE * 2.25), dtype=np.float32)
        with patch.object(analyzer, "_classify", return_value=(0.8, 0.2)) as classify:
            _male, _female, seconds = analyzer._classify_chunks(MagicMock(), audio)
        self.assertEqual(classify.call_count, 1)
        self.assertEqual(seconds, 2.0)

    def test_subprocess_timeout_maps_to_retryable_failure(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp_root = Path(temp_name) / "source-voice"
            leftover = temp_root / "radar-run123-orphan"
            leftover.mkdir(parents=True)
            (leftover / "partial.webm").write_bytes(b"partial")
            with patch.object(
                analyzer, "TEMP_ROOT", temp_root
            ), patch.object(
                analyzer.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["python"], 240),
            ), self.assertRaises(analyzer.SourceVoiceAnalysisError) as error:
                analyzer._run_analysis_subprocess(
                    {
                        "url": "https://youtu.be/abcdefghijk",
                        "duration_seconds": 600,
                        "proxy": "secret",
                        "run_id": "run123",
                    }
                )
            self.assertFalse(leftover.exists())
        self.assertEqual(error.exception.failure_code, "source_media_timeout")
        self.assertTrue(error.exception.retryable)

    def test_proxy_is_sent_over_stdin_not_command_line(self):
        secret_proxy = "http://private-user:private-pass@127.0.0.1:8080"
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "result": {"status": "advisory"}}),
            stderr="",
        )
        with patch.object(analyzer.subprocess, "run", return_value=response) as runner:
            result = analyzer._run_analysis_subprocess(
                {"url": "https://youtu.be/abcdefghijk", "duration_seconds": 600, "proxy": secret_proxy}
            )
        command = runner.call_args.args[0]
        self.assertNotIn("private-user", " ".join(command))
        self.assertNotIn("private-pass", " ".join(command))
        self.assertIn(secret_proxy, runner.call_args.kwargs["input"])
        self.assertEqual(result["status"], "advisory")

    def test_network_error_does_not_expose_proxy_credentials(self):
        with tempfile.TemporaryDirectory() as temp_name, patch.object(
            analyzer, "YoutubeDL", _FailingYoutubeDL
        ):
            with self.assertRaises(analyzer.SourceVoiceDownloadError) as error:
                analyzer._download_source(
                    "https://www.youtube.com/watch?v=abcdefghijk",
                    proxy_url="http://private-user:private-pass@127.0.0.1:8080",
                    output_stem=Path(temp_name) / "source",
                )
        combined = str(error.exception) + error.exception.diagnostic
        self.assertNotIn("private-user", combined)
        self.assertNotIn("private-pass", combined)


if __name__ == "__main__":
    unittest.main()
