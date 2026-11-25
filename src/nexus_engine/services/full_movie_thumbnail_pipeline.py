"""Durable thumbnail generation for local full-movie files."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from nexus_engine.paths import THUMBNAILS_DIR, gpt_profile_dir
from nexus_engine.services import (
    chatgpt_runtime,
    database as db,
    security_logging,
)
from nexus_engine.services.chatgpt_projects import DEFAULT_CHATGPT_BOOTSTRAP_URL
from nexus_engine.services.chatgpt_runtime import (
    ChatGPTAttentionRequiredError,
    ChatGPTPreSendTransientError,
)
from nexus_engine.services.chatgpt_worker import (
    CHATGPT_NAVIGATION_TIMEOUT_MS,
    DEFAULT_GPT_PROFILE,
    check_chatgpt_page_attention,
    download_chatgpt_image_via_page,
    launch_chatgpt_context,
    recover_assistant_response_from_backend,
    send_prompt,
    send_thumbnail_prompt,
    wait_for_chatgpt_composer,
)
from nexus_engine.services.full_movie_pipeline import (
    VIDEO_EXTENSIONS,
    probe_media,
    sanitize_windows_component,
)


logger = logging.getLogger(__name__)

JOB_TYPE = "full_movie_thumbnail"
PROFILE_VERSION = 1
CHECKPOINT_VERSION = 1
ACTIVE_STATUSES = {"queued", "running", "retry_wait", "paused"}
MAX_FRAME_SAMPLES = 36
MAX_REFERENCE_FRAMES = 12
MAX_FRAME_DECODE_STEPS = 120
MAX_CHATGPT_RECOVERIES = 5
CHATGPT_RETRY_DELAYS_SECONDS = (20, 40, 80, 160, 300)
NAVIGATION_ATTEMPTS = 3
NAVIGATION_BACKOFF_SECONDS = (0.5, 1.0)
MAX_OUTPUT_BYTES = 2_000_000
OUTPUT_SIZE = (1280, 720)
TEXT_ZONES = ("left", "right", "bottom")
_CREATE_LOCK = threading.Lock()


class FullMovieThumbnailError(RuntimeError):
    pass


class FullMovieThumbnailPaused(FullMovieThumbnailError):
    pass


class FullMovieThumbnailCanceled(FullMovieThumbnailError):
    pass


class FullMovieThumbnailRetryLater(FullMovieThumbnailError):
    def __init__(self, message: str, *, delay_seconds: float = 20.0):
        self.delay_seconds = max(1.0, min(float(delay_seconds), 300.0))
        super().__init__(message)


class FullMovieThumbnailAttentionRequired(FullMovieThumbnailError):
    def __init__(self, message: str, *, reason: str, details: dict | None = None):
        self.reason = str(reason or "full_movie_thumbnail_attention")
        self.details = dict(details or {})
        super().__init__(message)


def select_video_file() -> dict:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise FullMovieThumbnailError(
            "Máy hiện tại không hỗ trợ hộp chọn video Windows."
        ) from exc
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(
            parent=root,
            title="Chọn video full để tạo thumbnail",
            filetypes=[
                ("Video", "*.mp4 *.mkv *.mov *.m4v *.webm *.avi"),
                ("Tất cả file", "*.*"),
            ],
        )
    finally:
        root.destroy()
    return {
        "path": str(Path(selected).resolve()) if selected else "",
        "cancelled": not bool(selected),
    }


def resolve_movie_path(raw_path: str) -> Path:
    value = str(raw_path or "").strip().strip('"')
    if not value:
        raise FullMovieThumbnailError("Hãy chọn file video full hoặc thư mục phim.")
    path = Path(value).expanduser().resolve(strict=False)
    if path.is_dir():
        candidates = sorted(
            (
                item
                for item in path.iterdir()
                if item.is_file()
                and item.suffix.casefold() in VIDEO_EXTENSIONS
                and item.stem.casefold().startswith("full movie - ")
                and ".partial" not in item.name.casefold()
            ),
            key=lambda item: item.name.casefold(),
        )
        if not candidates:
            raise FullMovieThumbnailError(
                "Thư mục không có file bắt đầu bằng 'Full Movie - '. "
                "Hãy chọn trực tiếp video full."
            )
        if len(candidates) > 1:
            raise FullMovieThumbnailError(
                "Thư mục có nhiều video full; hãy chọn trực tiếp đúng file cần tạo thumbnail."
            )
        path = candidates[0].resolve()
    if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
        raise FullMovieThumbnailError("Đường dẫn không phải file video được hỗ trợ.")
    if path.parent == path.parent.parent:
        raise FullMovieThumbnailError("Không lưu thumbnail trực tiếp tại thư mục gốc ổ đĩa.")
    if not os.access(path, os.R_OK) or not os.access(path.parent, os.W_OK):
        raise FullMovieThumbnailError("Nexus cần quyền đọc video và ghi cạnh file phim.")
    return path


def _source_snapshot(path: Path, media: dict | None = None) -> dict:
    stat = path.stat()
    media = media or probe_media(path)
    return {
        "path": str(path.resolve()),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "duration": round(float(media.get("duration") or 0.0), 3),
        "width": int(media.get("width") or 0),
        "height": int(media.get("height") or 0),
    }


def _snapshot_matches(path: Path, expected: dict) -> bool:
    try:
        current = _source_snapshot(path)
    except (OSError, RuntimeError):
        return False
    return all(
        current.get(key) == expected.get(key)
        for key in ("path", "size", "mtime_ns", "duration", "width", "height")
    )


def _active_job_for_video(video_path: Path) -> dict | None:
    canonical = str(video_path.resolve()).casefold()
    jobs = db.list_system_jobs(limit=None, job_type=JOB_TYPE, statuses=ACTIVE_STATUSES)
    return next(
        (
            job
            for job in jobs
            if str((job.get("payload") or {}).get("resolved_video_path") or "").casefold()
            == canonical
        ),
        None,
    )


def _job_for_parent(parent_job_id: str) -> dict | None:
    if not parent_job_id:
        return None
    return next(
        (
            job
            for job in db.list_system_jobs(limit=None, job_type=JOB_TYPE)
            if str((job.get("payload") or {}).get("parent_job_id") or "")
            == parent_job_id
        ),
        None,
    )


def create_job(
    title: str,
    raw_path: str,
    *,
    origin: str = "standalone",
    parent_job_id: str = "",
) -> tuple[dict, bool]:
    normalized_title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not normalized_title:
        raise FullMovieThumbnailError("Hãy nhập tiêu đề phim hoặc tiêu đề video.")
    if len(normalized_title) > 500:
        raise FullMovieThumbnailError("Tiêu đề không được dài quá 500 ký tự.")
    video_path = resolve_movie_path(raw_path)
    media = probe_media(video_path)
    if float(media.get("duration") or 0.0) <= 0:
        raise FullMovieThumbnailError("Video không có thời lượng hợp lệ.")
    snapshot = _source_snapshot(video_path, media)
    with _CREATE_LOCK:
        parent_job = _job_for_parent(parent_job_id)
        if parent_job:
            return parent_job, False
        existing = _active_job_for_video(video_path)
        if existing:
            return existing, False
        job_id = uuid.uuid4().hex
        payload = {
            "profile_version": PROFILE_VERSION,
            "input_title": normalized_title,
            "input_path": str(raw_path),
            "resolved_video_path": str(video_path),
            "source_snapshot": snapshot,
            "origin": str(origin or "standalone"),
            "parent_job_id": str(parent_job_id or ""),
            "stage": "scan_frames",
        }
        created = db.create_system_job(
            job_id,
            JOB_TYPE,
            f"Tạo thumbnail phim · {normalized_title[:80]}",
            payload,
        )
        return created, True


def _job_temp_dir(video_path: Path, job_id: str, *, create: bool = True) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "").casefold()):
        raise FullMovieThumbnailError("Job ID thumbnail không hợp lệ.")
    root = (video_path.parent / ".nexus_thumbnail").resolve()
    target = (root / job_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise FullMovieThumbnailError("Namespace tạm thumbnail không hợp lệ.") from exc
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _load_checkpoint(temp_dir: Path, job_id: str) -> dict:
    path = temp_dir / "checkpoint.json"
    if not path.is_file():
        return {"version": CHECKPOINT_VERSION, "job_id": job_id, "stage": "scan_frames"}
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FullMovieThumbnailAttentionRequired(
            "Checkpoint thumbnail bị hỏng; cần xóa job và chạy lại.",
            reason="checkpoint_invalid",
        ) from exc
    if (
        checkpoint.get("version") != CHECKPOINT_VERSION
        or checkpoint.get("job_id") != job_id
    ):
        raise FullMovieThumbnailAttentionRequired(
            "Checkpoint thumbnail không tương thích.",
            reason="checkpoint_invalid",
        )
    return checkpoint


def _save_checkpoint(
    checkpoint: dict,
    *,
    temp_dir: Path,
    job_id: str,
    payload: dict,
    stage: str,
    progress: str,
) -> None:
    checkpoint["stage"] = stage
    _atomic_json(temp_dir / "checkpoint.json", checkpoint)
    payload["stage"] = stage
    db.update_system_job(
        job_id,
        progress=progress,
        payload_json=payload,
        resume_from_step=stage,
    )


def _check_job_control(job_id: str) -> None:
    current = db.get_system_job(job_id)
    if not current:
        raise FullMovieThumbnailCanceled("Job thumbnail không còn tồn tại.")
    if current.get("status") == "paused":
        raise FullMovieThumbnailPaused("Job thumbnail đã tạm dừng.")
    if current.get("status") == "canceled" or current.get("cancel_requested"):
        raise FullMovieThumbnailCanceled("Job thumbnail đã bị hủy.")


def _crop_pillarbox(image: Image.Image) -> Image.Image:
    rgb = image.convert("RGB")
    gray = ImageOps.grayscale(rgb)
    preview = gray.copy()
    preview.thumbnail((480, 270))
    threshold = preview.point(lambda value: 255 if value > 14 else 0)
    bbox = threshold.getbbox()
    if not bbox:
        return rgb
    left_ratio = bbox[0] / preview.width
    right_ratio = (preview.width - bbox[2]) / preview.width
    remaining_ratio = (bbox[2] - bbox[0]) / preview.width
    if (
        min(left_ratio, right_ratio) < 0.08
        or abs(left_ratio - right_ratio) > 0.06
        or remaining_ratio < 0.22
    ):
        return rgb
    left = max(0, int(left_ratio * rgb.width))
    right = min(rgb.width, int((1.0 - right_ratio) * rgb.width))
    return rgb.crop((left, 0, right, rgb.height)) if right > left else rgb


def _difference_hash(image: Image.Image) -> int:
    gray = ImageOps.grayscale(image).resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(gray.get_flattened_data())
    value = 0
    for row in range(8):
        for column in range(8):
            value = (value << 1) | int(
                pixels[row * 9 + column] > pixels[row * 9 + column + 1]
            )
    return value


def _hash_distance(left: int, right: int) -> int:
    return int(left ^ right).bit_count()


def _frame_quality(image: Image.Image) -> tuple[float, dict]:
    sample = ImageOps.grayscale(image)
    sample.thumbnail((640, 360))
    stats = ImageStat.Stat(sample)
    brightness = float(stats.mean[0])
    contrast = float(stats.stddev[0])
    edge = sample.filter(ImageFilter.FIND_EDGES)
    edge_strength = float(ImageStat.Stat(edge).mean[0])
    brightness_score = max(0.0, 100.0 - abs(brightness - 128.0) * 0.9)
    contrast_score = min(100.0, contrast * 2.0)
    edge_score = min(100.0, edge_strength * 4.0)
    quality = 0.35 * brightness_score + 0.4 * contrast_score + 0.25 * edge_score
    return round(quality, 3), {
        "brightness": round(brightness, 3),
        "contrast": round(contrast, 3),
        "edge_strength": round(edge_strength, 3),
    }


def _sample_positions(duration: float, count: int = MAX_FRAME_SAMPLES) -> list[float]:
    usable_count = max(1, min(int(count), MAX_FRAME_SAMPLES))
    start = duration * 0.05
    end = duration * 0.95
    if usable_count == 1 or end <= start:
        return [max(0.0, duration / 2.0)]
    return [
        start + (end - start) * index / (usable_count - 1)
        for index in range(usable_count)
    ]


def _decode_frame_at(container, video_stream, timestamp: float):
    container.seek(max(0, int(timestamp * 1_000_000)), backward=True, any_frame=False)
    selected = None
    for index, frame in enumerate(container.decode(video_stream)):
        selected = frame
        if frame.time is None or float(frame.time) >= timestamp - 0.1:
            break
        if index + 1 >= MAX_FRAME_DECODE_STEPS:
            break
    return selected


def _save_reference_frame(image: Image.Image, path: Path) -> None:
    output = image.convert("RGB")
    output.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    output.save(path, format="JPEG", quality=90, optimize=True)


def _build_frame_contact_sheet(frames: list[dict], destination: Path) -> None:
    canvas = Image.new("RGB", OUTPUT_SIZE, "#0b0b0f")
    draw = ImageDraw.Draw(canvas)
    font = _load_font(28, bold=True)
    columns, rows = 4, 3
    cell_width = OUTPUT_SIZE[0] // columns
    cell_height = OUTPUT_SIZE[1] // rows
    for index, item in enumerate(frames[: columns * rows]):
        with Image.open(item["path"]) as source:
            tile = ImageOps.fit(
                source.convert("RGB"),
                (cell_width, cell_height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.38),
            )
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        canvas.paste(tile, (x, y))
        draw.rectangle((x + 8, y + 8, x + 76, y + 44), fill=(0, 0, 0, 190))
        draw.text((x + 15, y + 10), item["id"], font=font, fill="white")
    canvas.save(destination, format="JPEG", quality=90, optimize=True)


def extract_reference_frames(video_path: Path, temp_dir: Path, duration: float) -> dict:
    frames_dir = temp_dir / "frames"
    contact_sheet = temp_dir / "frame_contact_sheet.jpg"
    manifest_path = temp_dir / "frames.json"
    if manifest_path.is_file() and contact_sheet.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("frames") and all(
                Path(item.get("path") or "").is_file() for item in manifest["frames"]
            ):
                return manifest
        except (OSError, json.JSONDecodeError):
            pass

    try:
        import av
    except ImportError as exc:
        raise FullMovieThumbnailError("Thiếu PyAV để lấy cảnh từ video.") from exc

    frames_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[dict] = []
    hashes: list[int] = []
    try:
        with av.open(str(video_path), mode="r") as container:
            video_stream = next(iter(container.streams.video), None)
            if video_stream is None:
                raise FullMovieThumbnailError("Video không có hình ảnh để tạo thumbnail.")
            for timestamp in _sample_positions(duration):
                frame = _decode_frame_at(container, video_stream, timestamp)
                if frame is None:
                    continue
                image = _crop_pillarbox(frame.to_image())
                quality, metrics = _frame_quality(image)
                if metrics["brightness"] < 18 or metrics["brightness"] > 238:
                    continue
                frame_hash = _difference_hash(image)
                if any(_hash_distance(frame_hash, known) < 7 for known in hashes):
                    continue
                hashes.append(frame_hash)
                candidates.append(
                    {
                        "timestamp": round(timestamp, 3),
                        "quality": quality,
                        "metrics": metrics,
                        "image": image.copy(),
                    }
                )
    except FullMovieThumbnailError:
        raise
    except Exception as exc:
        raise FullMovieThumbnailError(f"Không thể lấy frame từ video: {exc}") from exc

    if not candidates:
        raise FullMovieThumbnailError("Không tìm được cảnh đủ sáng và rõ để tạo thumbnail.")

    candidates.sort(key=lambda item: item["quality"], reverse=True)
    selected: list[dict] = []
    minimum_spacing = max(1.0, duration / 30.0)
    for candidate in candidates:
        if all(
            abs(candidate["timestamp"] - item["timestamp"]) >= minimum_spacing
            for item in selected
        ):
            selected.append(candidate)
        if len(selected) >= MAX_REFERENCE_FRAMES:
            break
    if len(selected) < min(MAX_REFERENCE_FRAMES, len(candidates)):
        for candidate in candidates:
            if all(candidate is not item for item in selected):
                selected.append(candidate)
            if len(selected) >= MAX_REFERENCE_FRAMES:
                break
    selected.sort(key=lambda item: item["timestamp"])

    frame_records: list[dict] = []
    for index, item in enumerate(selected, start=1):
        frame_id = f"F{index:02d}"
        frame_path = frames_dir / f"{frame_id}.jpg"
        _save_reference_frame(item.pop("image"), frame_path)
        frame_records.append({"id": frame_id, "path": str(frame_path), **item})
    _build_frame_contact_sheet(frame_records, contact_sheet)
    manifest = {"frames": frame_records, "contact_sheet": str(contact_sheet)}
    _atomic_json(manifest_path, manifest)
    return manifest


def _extract_json_object(text: str) -> dict:
    cleaned = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else cleaned[cleaned.find("{") : cleaned.rfind("}") + 1]
    if not candidate or not candidate.startswith("{"):
        raise FullMovieThumbnailError("ChatGPT không trả về JSON thumbnail hợp lệ.")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise FullMovieThumbnailError("JSON thumbnail từ ChatGPT không hợp lệ.") from exc
    if not isinstance(payload, dict):
        raise FullMovieThumbnailError("JSON thumbnail phải là một object.")
    return payload


def _normalize_hook(value: str, fallback: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?", str(value or ""))
    if not words:
        words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?", fallback)
    return " ".join(words[:5]).upper() or "FULL DRAMA"


def _validate_concepts(payload: dict, input_title: str, valid_frame_ids: set[str]) -> dict:
    normalized_title = re.sub(
        r"\s+", " ", str(payload.get("normalized_title_us") or input_title)
    ).strip()[:500]
    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list) or len(raw_variants) != 3:
        raise FullMovieThumbnailError("ChatGPT phải trả đúng ba concept thumbnail.")
    variants: list[dict] = []
    fallback_refs = sorted(valid_frame_ids)[:2]
    for index, raw in enumerate(raw_variants, start=1):
        if not isinstance(raw, dict):
            raise FullMovieThumbnailError("Concept thumbnail không hợp lệ.")
        reference_ids = [
            str(item).upper()
            for item in (raw.get("reference_ids") or [])
            if str(item).upper() in valid_frame_ids
        ][:3]
        if not reference_ids:
            reference_ids = fallback_refs
        text_zone = str(raw.get("text_zone") or TEXT_ZONES[index - 1]).casefold()
        if text_zone not in TEXT_ZONES:
            text_zone = TEXT_ZONES[index - 1]
        variants.append(
            {
                "index": index,
                "hook": _normalize_hook(str(raw.get("hook") or ""), normalized_title),
                "reference_ids": reference_ids,
                "concept": re.sub(r"\s+", " ", str(raw.get("concept") or "")).strip()[:800],
                "text_zone": text_zone,
            }
        )
    return {"normalized_title_us": normalized_title or input_title, "variants": variants}


def _concept_prompt(input_title: str, frames: list[dict]) -> str:
    frame_times = ", ".join(f"{item['id']}={item['timestamp']:.1f}s" for item in frames)
    return (
        "You are designing high-click YouTube thumbnails for an English-language short-drama "
        "full movie. Analyze the attached contact sheet made only from the supplied movie. "
        "Use the real characters and story mood; do not invent celebrities, logos, watermarks, "
        "or brand identity. Return ONLY valid JSON.\n"
        "Create exactly 3 distinct concepts. Each hook must be natural US English, uppercase, "
        "2-5 words, and suitable for at most two lines. Select 1-3 frame IDs per concept. "
        "Use varied emotional compositions and text zones.\n"
        "Schema: {\"normalized_title_us\":\"...\",\"variants\":["
        "{\"index\":1,\"hook\":\"...\",\"reference_ids\":[\"F01\"],"
        "\"concept\":\"...\",\"text_zone\":\"left|right|bottom\"}]}\n"
        f"Input title: {input_title}\nAvailable frames: {frame_times}"
    )


def _generation_prompt(title: str, variant: dict) -> str:
    zone = variant["text_zone"]
    return (
        "Create exactly ONE polished 16:9 YouTube thumbnail background for an English-language "
        "romantic/revenge short-drama full movie. Use the attached real movie stills as visual "
        "references and keep the lead characters recognizable. Improve lighting, emotional "
        "intensity, separation, cinematic color, and composition. Use close expressive faces "
        "and a clear conflict or relationship dynamic. Premium warm skin tones, deep shadows, "
        "gold/red accents, crisp mobile-readable focal point. Do not add any text, letters, "
        "logos, watermarks, UI, or channel branding. Do not create a collage border. Leave a "
        f"clean low-detail safe area in the {zone} zone for text that will be added later. "
        f"Movie title context: {title}. Concept: {variant['concept'] or 'dramatic confrontation'}."
    )


def _is_transient_browser_error(exc: BaseException) -> bool:
    if isinstance(exc, (PlaywrightError, ChatGPTPreSendTransientError)):
        return True
    message = str(exc or "").casefold()
    return any(
        marker in message
        for marker in (
            "net::err_aborted",
            "net::err_internet_disconnected",
            "target page, context or browser has been closed",
            "connection closed",
            "no prompt was sent",
            "page did not become ready",
        )
    )


def _close_page(page) -> None:
    if page is None:
        return
    try:
        page.close()
    except Exception:
        logger.debug("Could not close the dedicated full-movie thumbnail page.")


def _open_chatgpt_page(context):
    last_error: Exception | None = None
    for attempt in range(NAVIGATION_ATTEMPTS):
        page = None
        try:
            page = context.new_page()
            page.goto(
                DEFAULT_CHATGPT_BOOTSTRAP_URL,
                wait_until="domcontentloaded",
                timeout=CHATGPT_NAVIGATION_TIMEOUT_MS,
            )
            check_chatgpt_page_attention(page)
            wait_for_chatgpt_composer(page)
            return page
        except ChatGPTAttentionRequiredError:
            _close_page(page)
            raise
        except Exception as exc:
            _close_page(page)
            if not _is_transient_browser_error(exc):
                raise
            last_error = exc
            if attempt < NAVIGATION_ATTEMPTS - 1:
                time.sleep(NAVIGATION_BACKOFF_SECONDS[min(attempt, 1)])
    raise FullMovieThumbnailRetryLater(
        "ChatGPT chưa sẵn sàng; job thumbnail sẽ tự thử lại.",
        delay_seconds=20,
    ) from last_error


def _send_json_prompt(page, prompt: str, reference_files: list[str], validator) -> dict:
    submitted = False

    def mark_submitted() -> None:
        nonlocal submitted
        submitted = True

    try:
        response = send_prompt(
            page,
            prompt,
            reference_files=reference_files,
            on_prompt_submitted=mark_submitted,
        ).strip()
    except ChatGPTAttentionRequiredError:
        raise
    except Exception as exc:
        if not submitted and _is_transient_browser_error(exc):
            raise FullMovieThumbnailRetryLater(
                "Kết nối ChatGPT gián đoạn trước khi gửi; job sẽ tự thử lại.",
                delay_seconds=20,
            ) from exc
        raise
    try:
        return validator(_extract_json_object(response))
    except FullMovieThumbnailError:
        pass
    candidates: list[str] = []
    try:
        recovered = recover_assistant_response_from_backend(
            page, expected_user_text=prompt
        ).strip()
        if recovered and recovered != response:
            candidates.append(recovered)
    except ChatGPTAttentionRequiredError:
        raise
    except Exception:
        pass
    for candidate in candidates:
        try:
            return validator(_extract_json_object(candidate))
        except FullMovieThumbnailError:
            continue
    repair_prompt = (
        "Repair your previous response. Return ONLY valid JSON matching the requested schema. "
        "Keep exactly three variants and do not add commentary."
    )
    repaired = send_prompt(page, repair_prompt).strip()
    return validator(_extract_json_object(repaired))


def _local_thumbnail_file(url: str) -> Path:
    prefix = "/api/thumbnails/"
    if not str(url or "").startswith(prefix):
        raise FullMovieThumbnailError("ChatGPT không trả về đường dẫn ảnh local hợp lệ.")
    filename = Path(str(url).split("?", 1)[0]).name
    path = (THUMBNAILS_DIR / filename).resolve()
    try:
        path.relative_to(THUMBNAILS_DIR.resolve())
    except ValueError as exc:
        raise FullMovieThumbnailError("Đường dẫn ảnh ChatGPT nằm ngoài kho tạm.") from exc
    if not path.is_file():
        raise FullMovieThumbnailError("Không tìm thấy ảnh ChatGPT vừa tải.")
    return path


def _generate_raw_variant(page, variant: dict, title: str, references: list[str], destination: Path) -> None:
    prompt = _generation_prompt(title, variant)
    for attempt in range(2):
        _response, image_urls = send_thumbnail_prompt(
            page,
            prompt if attempt == 0 else "Retry the same concept. Generate exactly one image and no text.",
            lambda image_url: download_chatgpt_image_via_page(page, image_url),
            reference_files=references,
        )
        if image_urls:
            source = _local_thumbnail_file(image_urls[0])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            for unused_url in image_urls[1:]:
                try:
                    _local_thumbnail_file(unused_url).unlink(missing_ok=True)
                except OSError:
                    pass
            return
        if attempt == 0:
            continue
    raise FullMovieThumbnailError(
        "ChatGPT không tạo được ảnh cho một phương án thumbnail."
    )


def _font_candidates(*, italic: bool, bold: bool) -> list[Path]:
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    names = ["georgiaz.ttf", "georgiab.ttf", "arialbi.ttf", "arialbd.ttf"]
    if not italic:
        names = ["georgiab.ttf", "arialbd.ttf", "georgiaz.ttf", "arialbi.ttf"]
    if not bold:
        names.extend(["georgia.ttf", "arial.ttf"])
    return [windows_fonts / name for name in names]


def _load_font(size: int, *, italic: bool = False, bold: bool = True):
    for candidate in _font_candidates(italic=italic, bold=bold):
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _wrap_hook(hook: str) -> list[str]:
    words = hook.split()
    if len(words) <= 3:
        return [hook]
    midpoint = math.ceil(len(words) / 2)
    return [" ".join(words[:midpoint]), " ".join(words[midpoint:])]


def _fit_text(draw: ImageDraw.ImageDraw, lines: list[str], max_width: int):
    for size in range(92, 47, -2):
        font = _load_font(size, italic=True, bold=True)
        widths = [draw.textbbox((0, 0), line, font=font, stroke_width=4)[2] for line in lines]
        if max(widths, default=0) <= max_width:
            return font
    return _load_font(48, italic=True, bold=True)


def render_text_variant(raw_path: Path, destination: Path, hook: str, zone: str, index: int) -> float:
    with Image.open(raw_path) as source:
        canvas = ImageOps.fit(
            source.convert("RGB"),
            OUTPUT_SIZE,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.45),
        )
    overlay = Image.new("RGBA", OUTPUT_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    lines = _wrap_hook(_normalize_hook(hook, "FULL DRAMA"))
    max_width = 650 if zone != "bottom" else 1050
    font = _fit_text(draw, lines, max_width)
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=4) for line in lines]
    widths = [box[2] - box[0] for box in boxes]
    heights = [box[3] - box[1] for box in boxes]
    line_gap = 12
    total_height = sum(heights) + line_gap * max(0, len(lines) - 1)
    if zone == "right":
        start_y = max(80, OUTPUT_SIZE[1] - total_height - 100)
    elif zone == "bottom":
        start_y = OUTPUT_SIZE[1] - total_height - 70
    else:
        start_y = max(80, OUTPUT_SIZE[1] - total_height - 105)
    accent_colors = ("#F4D06F", "#FFDB75", "#EF665F")
    y = start_y
    for line_index, line in enumerate(lines):
        width = widths[line_index]
        if zone == "right":
            x = OUTPUT_SIZE[0] - 60 - width
        elif zone == "bottom":
            x = (OUTPUT_SIZE[0] - width) // 2
        else:
            x = 60
        color = accent_colors[(index - 1) % len(accent_colors)] if line_index == len(lines) - 1 else "#FFF7E8"
        draw.text(
            (x + 4, y + 6),
            line,
            font=font,
            fill=(0, 0, 0, 170),
            stroke_width=5,
            stroke_fill=(0, 0, 0, 190),
        )
        draw.text(
            (x, y),
            line,
            font=font,
            fill=color,
            stroke_width=4,
            stroke_fill="#171014",
        )
        y += heights[line_index] + line_gap
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for quality in (92, 88, 84, 80, 76, 72, 68):
        canvas.save(destination, format="JPEG", quality=quality, optimize=True, progressive=True)
        if destination.stat().st_size <= MAX_OUTPUT_BYTES:
            break
    if destination.stat().st_size > MAX_OUTPUT_BYTES:
        raise FullMovieThumbnailError("Không thể nén thumbnail xuống dưới 2 MB.")
    return technical_score(destination)


def technical_score(path: Path) -> float:
    with Image.open(path) as image:
        if image.size != OUTPUT_SIZE:
            return 0.0
        gray = ImageOps.grayscale(image)
        stats = ImageStat.Stat(gray)
        brightness = float(stats.mean[0])
        contrast = float(stats.stddev[0])
        edge_strength = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0])
    brightness_score = max(0.0, 100.0 - abs(brightness - 128.0) * 0.8)
    contrast_score = min(100.0, contrast * 1.8)
    sharpness_score = min(100.0, edge_strength * 3.5)
    return round(0.35 * brightness_score + 0.4 * contrast_score + 0.25 * sharpness_score, 1)


def _build_variant_contact_sheet(variants: list[dict], destination: Path) -> None:
    canvas = Image.new("RGB", (1280, 720), "#111118")
    draw = ImageDraw.Draw(canvas)
    label_font = _load_font(32, bold=True)
    cell_width = 1280 // 3
    for index, item in enumerate(variants):
        with Image.open(item["temp_path"]) as source:
            tile = ImageOps.fit(source.convert("RGB"), (cell_width, 720), Image.Resampling.LANCZOS)
        x = index * cell_width
        canvas.paste(tile, (x, 0))
        draw.rectangle((x + 12, 12, x + 64, 58), fill="#000000")
        draw.text((x + 27, 17), str(index + 1), font=label_font, fill="white")
    canvas.save(destination, format="JPEG", quality=88, optimize=True)


def _validate_scores(payload: dict, variants: list[dict]) -> dict:
    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, list):
        raise FullMovieThumbnailError("ChatGPT không trả về bảng điểm hợp lệ.")
    creative_by_index: dict[int, float] = {}
    reasons: dict[int, str] = {}
    for item in raw_scores:
        if not isinstance(item, dict):
            continue
        index = int(item.get("index") or 0)
        if index not in (1, 2, 3):
            continue
        creative_by_index[index] = max(0.0, min(100.0, float(item.get("creative_score") or 0.0)))
        reasons[index] = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:300]
    if len(creative_by_index) != 3:
        raise FullMovieThumbnailError("ChatGPT chưa chấm đủ ba thumbnail.")
    return {"creative": creative_by_index, "reasons": reasons}


def _score_prompt(title: str) -> str:
    return (
        "Compare the three numbered YouTube thumbnails in the attached image for this drama: "
        f"{title}. Score each from 0-100 for title relevance, recognizable emotional faces, "
        "clear story conflict, composition, and mobile text readability. Return ONLY JSON: "
        "{\"scores\":[{\"index\":1,\"creative_score\":0,\"reason\":\"...\"},"
        "{\"index\":2,\"creative_score\":0,\"reason\":\"...\"},"
        "{\"index\":3,\"creative_score\":0,\"reason\":\"...\"}]}"
    )


def _unique_output_dir(video_path: Path, title: str) -> Path:
    root = video_path.parent / "Thumbnails"
    root.mkdir(parents=True, exist_ok=True)
    base = sanitize_windows_component(title, fallback="Untitled Drama", max_length=100)
    candidate = root / base
    suffix = 2
    while candidate.exists() and suffix <= 999:
        candidate = root / f"{base} ({suffix})"
        suffix += 1
    if candidate.exists():
        raise FullMovieThumbnailError("Có quá nhiều thư mục thumbnail trùng tên.")
    candidate.mkdir(parents=False, exist_ok=False)
    return candidate


def _publish_outputs(video_path: Path, title: str, variants: list[dict], primary_index: int) -> dict:
    output_dir = _unique_output_dir(video_path, title)
    published: list[dict] = []
    try:
        for item in variants:
            safe_hook = sanitize_windows_component(
                item["hook"], fallback=f"Variant {item['index']}", max_length=60
            )
            target = output_dir / f"{item['index']:02d} - {safe_hook}.jpg"
            shutil.copy2(item["temp_path"], target)
            published.append({**item, "path": str(target)})
        primary_source = next(item for item in published if item["index"] == primary_index)
        primary_path = output_dir / "PRIMARY.jpg"
        shutil.copy2(primary_source["path"], primary_path)
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    return {
        "output_dir": str(output_dir),
        "variants": [
            {key: value for key, value in item.items() if key != "temp_path"}
            for item in published
        ],
        "primary_index": primary_index,
        "primary_path": str(primary_path),
    }


def _cleanup_job_temp(temp_dir: Path) -> None:
    root = temp_dir.parent.resolve()
    target = temp_dir.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise FullMovieThumbnailError("Từ chối xóa temp thumbnail ngoài namespace.") from exc
    if root.name != ".nexus_thumbnail":
        raise FullMovieThumbnailError("Namespace temp thumbnail không hợp lệ.")
    shutil.rmtree(target, ignore_errors=False)
    try:
        root.rmdir()
    except OSError:
        pass


def cleanup_job_artifacts(job: dict) -> None:
    if str(job.get("job_type") or "") != JOB_TYPE:
        return
    video_path = Path(str((job.get("payload") or {}).get("resolved_video_path") or ""))
    if not video_path.name:
        return
    target = _job_temp_dir(video_path, str(job.get("id") or ""), create=False)
    if target.is_dir():
        _cleanup_job_temp(target)


def _ensure_final_variants(temp_dir: Path, concepts: dict, checkpoint: dict) -> list[dict]:
    existing = checkpoint.get("final_variants") or []
    if len(existing) == 3 and all(Path(item.get("temp_path") or "").is_file() for item in existing):
        return existing
    final_dir = temp_dir / "final"
    variants: list[dict] = []
    raw_images = checkpoint.get("raw_images") or {}
    for variant in concepts["variants"]:
        raw_path = Path(str(raw_images.get(str(variant["index"])) or ""))
        if not raw_path.is_file():
            raise FullMovieThumbnailError("Thiếu ảnh thô để hoàn thiện thumbnail.")
        final_path = final_dir / f"variant_{variant['index']}.jpg"
        score = render_text_variant(
            raw_path,
            final_path,
            variant["hook"],
            variant["text_zone"],
            variant["index"],
        )
        variants.append(
            {
                "index": variant["index"],
                "hook": variant["hook"],
                "text_zone": variant["text_zone"],
                "technical_score": score,
                "temp_path": str(final_path),
            }
        )
    checkpoint["final_variants"] = variants
    return variants


def _generate_with_chatgpt(
    job_id: str,
    payload: dict,
    checkpoint: dict,
    temp_dir: Path,
    frame_manifest: dict,
) -> tuple[dict, list[dict]]:
    if not chatgpt_runtime.try_start_operation("full_movie_thumbnail"):
        raise FullMovieThumbnailRetryLater(
            "ChatGPT đang bận; job thumbnail sẽ thử lại.", delay_seconds=20
        )
    page = None
    try:
        profile_dir = gpt_profile_dir(DEFAULT_GPT_PROFILE)
        if not profile_dir.exists():
            raise ChatGPTAttentionRequiredError(
                "Chưa có profile ChatGPT. Hãy chạy Auto Login rồi tiếp tục job."
            )
        with sync_playwright() as playwright, closing(
            launch_chatgpt_context(playwright.chromium, profile_dir)
        ) as context:
            page = _open_chatgpt_page(context)
            frames = frame_manifest["frames"]
            valid_ids = {item["id"] for item in frames}
            concepts = checkpoint.get("concepts")
            if not concepts:
                concepts = _send_json_prompt(
                    page,
                    _concept_prompt(payload["input_title"], frames),
                    [frame_manifest["contact_sheet"]],
                    lambda value: _validate_concepts(value, payload["input_title"], valid_ids),
                )
                checkpoint["concepts"] = concepts
                _save_checkpoint(
                    checkpoint,
                    temp_dir=temp_dir,
                    job_id=job_id,
                    payload=payload,
                    stage="generate_images",
                    progress="Đã chọn cảnh; đang tạo 3 phương án thumbnail",
                )

            raw_images = dict(checkpoint.get("raw_images") or {})
            frames_by_id = {item["id"]: item for item in frames}
            for variant in concepts["variants"]:
                _check_job_control(job_id)
                key = str(variant["index"])
                existing = Path(str(raw_images.get(key) or ""))
                if existing.is_file():
                    continue
                destination = temp_dir / "raw" / f"variant_{variant['index']}.png"
                references = [
                    frames_by_id[frame_id]["path"]
                    for frame_id in variant["reference_ids"]
                    if frame_id in frames_by_id
                ]
                _generate_raw_variant(
                    page,
                    variant,
                    concepts["normalized_title_us"],
                    references,
                    destination,
                )
                raw_images[key] = str(destination)
                checkpoint["raw_images"] = raw_images
                _save_checkpoint(
                    checkpoint,
                    temp_dir=temp_dir,
                    job_id=job_id,
                    payload=payload,
                    stage="generate_images",
                    progress=f"Đã tạo {len(raw_images)}/3 phương án thumbnail",
                )

            variants = _ensure_final_variants(temp_dir, concepts, checkpoint)
            _save_checkpoint(
                checkpoint,
                temp_dir=temp_dir,
                job_id=job_id,
                payload=payload,
                stage="score",
                progress="Đang chấm điểm và chọn thumbnail chính",
            )
            score_sheet = temp_dir / "variant_contact_sheet.jpg"
            _build_variant_contact_sheet(variants, score_sheet)
            try:
                scores = _send_json_prompt(
                    page,
                    _score_prompt(concepts["normalized_title_us"]),
                    [str(score_sheet)],
                    lambda value: _validate_scores(value, variants),
                )
            except FullMovieThumbnailError:
                logger.warning("Creative thumbnail scoring failed; using technical scores.")
                scores = {
                    "creative": {item["index"]: item["technical_score"] for item in variants},
                    "reasons": {item["index"]: "Technical fallback" for item in variants},
                }
            for item in variants:
                creative = float(scores["creative"][item["index"]])
                item["creative_score"] = round(creative, 1)
                item["score"] = round(creative * 0.8 + item["technical_score"] * 0.2, 1)
                item["reason"] = scores["reasons"].get(item["index"], "")
            checkpoint["final_variants"] = variants
            return concepts, variants
    finally:
        _close_page(page)
        chatgpt_runtime.finish_operation()


def execute(job: dict) -> None:
    job_id = str(job["id"])
    payload = dict(job.get("payload") or {})
    video_path = resolve_movie_path(str(payload.get("resolved_video_path") or ""))
    expected_snapshot = dict(payload.get("source_snapshot") or {})
    if not _snapshot_matches(video_path, expected_snapshot):
        raise FullMovieThumbnailAttentionRequired(
            "Video nguồn đã thay đổi sau khi tạo job; hãy tạo job thumbnail mới.",
            reason="source_changed",
        )
    temp_dir = _job_temp_dir(video_path, job_id)
    checkpoint = _load_checkpoint(temp_dir, job_id)
    published = checkpoint.get("published_result") or {}
    if published and Path(str(published.get("primary_path") or "")).is_file():
        result = published
    else:
        _check_job_control(job_id)
        frame_manifest = extract_reference_frames(
            video_path,
            temp_dir,
            float(expected_snapshot.get("duration") or 0.0),
        )
        checkpoint["frame_manifest"] = frame_manifest
        _save_checkpoint(
            checkpoint,
            temp_dir=temp_dir,
            job_id=job_id,
            payload=payload,
            stage="concepts",
            progress=f"Đã chọn {len(frame_manifest['frames'])} cảnh; đang phân tích nhân vật",
        )
        concepts, variants = _generate_with_chatgpt(
            job_id, payload, checkpoint, temp_dir, frame_manifest
        )
        _check_job_control(job_id)
        if not _snapshot_matches(video_path, expected_snapshot):
            raise FullMovieThumbnailAttentionRequired(
                "Video nguồn đã thay đổi trong lúc tạo thumbnail.",
                reason="source_changed",
            )
        primary = max(variants, key=lambda item: (item["score"], -item["index"]))
        result = {
            "input_title": payload["input_title"],
            "normalized_title_us": concepts["normalized_title_us"],
            "video_path": str(video_path),
            **_publish_outputs(
                video_path,
                concepts["normalized_title_us"],
                variants,
                int(primary["index"]),
            ),
        }
        checkpoint["published_result"] = result
        _save_checkpoint(
            checkpoint,
            temp_dir=temp_dir,
            job_id=job_id,
            payload=payload,
            stage="completed",
            progress="Đã lưu 3 thumbnail và chọn ảnh chính",
        )
    db.update_system_job(
        job_id,
        status="completed",
        progress="Đã tạo xong 3 thumbnail phim",
        result_json=result,
        error="",
        cancel_requested=0,
        finished_at=db.utc_now(),
        resume_from_step="",
    )
    try:
        _cleanup_job_temp(temp_dir)
    except OSError as exc:
        logger.warning("Could not remove thumbnail temp for job %s: %s", job_id, exc)


def handle_error(job: dict, exc: Exception) -> None:
    job_id = str(job["id"])
    if isinstance(exc, FullMovieThumbnailPaused):
        current = db.get_system_job(job_id)
        if current and current.get("status") == "paused":
            return
    if isinstance(exc, FullMovieThumbnailCanceled):
        db.update_system_job(
            job_id,
            status="canceled",
            progress="Đã hủy thumbnail; checkpoint được giữ để chạy lại",
            error="",
            cancel_requested=0,
            finished_at=db.utc_now(),
        )
        return
    safe_error = security_logging.safe_user_error(
        JOB_TYPE, exc, "Tạo thumbnail phim thất bại."
    )
    if isinstance(exc, FullMovieThumbnailRetryLater):
        recovery_count = int((db.get_system_job(job_id) or {}).get("recovery_count") or 0)
        if recovery_count < MAX_CHATGPT_RECOVERIES:
            delay = CHATGPT_RETRY_DELAYS_SECONDS[min(recovery_count, len(CHATGPT_RETRY_DELAYS_SECONDS) - 1)]
            db.schedule_system_job_recovery(
                job_id,
                resume_from_step=str(job.get("resume_from_step") or "concepts"),
                delay_seconds=max(delay, exc.delay_seconds),
                error=safe_error,
                progress=str(exc),
            )
            return
        db.update_system_job(
            job_id,
            status="paused",
            progress="ChatGPT vẫn bận sau 5 lần thử; bấm Tiếp tục khi sẵn sàng",
            result_json={"attention_required": "chatgpt_busy"},
            error=safe_error,
            next_retry_at="",
            cancel_requested=0,
            finished_at="",
        )
        return
    if isinstance(exc, (FullMovieThumbnailAttentionRequired, ChatGPTAttentionRequiredError)):
        reason = (
            exc.reason
            if isinstance(exc, FullMovieThumbnailAttentionRequired)
            else "chatgpt_verification"
        )
        details = exc.details if isinstance(exc, FullMovieThumbnailAttentionRequired) else {}
        db.update_system_job(
            job_id,
            status="paused",
            progress=str(exc),
            result_json={"attention_required": reason, "details": details},
            error=safe_error,
            next_retry_at="",
            cancel_requested=0,
            finished_at="",
        )
        return
    db.update_system_job(
        job_id,
        status="error",
        progress="Tạo thumbnail phim thất bại",
        error=safe_error,
        cancel_requested=0,
        finished_at=db.utc_now(),
    )


def public_job(job: dict) -> dict:
    payload = dict(job.get("payload") or {})
    result = dict(job.get("result") or {})
    variants = []
    for item in result.get("variants") or []:
        index = int(item.get("index") or 0)
        variants.append({**item, "url": f"/api/full-movie/thumbnail/jobs/{job['id']}/images/{index}"})
    if variants:
        result["variants"] = variants
        result["primary_url"] = f"/api/full-movie/thumbnail/jobs/{job['id']}/images/primary"
    return {
        "id": str(job.get("id") or ""),
        "status": str(job.get("status") or ""),
        "title": str(job.get("title") or ""),
        "progress": str(job.get("progress") or ""),
        "error": str(job.get("error") or ""),
        "stage": str(payload.get("stage") or job.get("resume_from_step") or ""),
        "input_title": str(payload.get("input_title") or ""),
        "input_path": str(payload.get("input_path") or ""),
        "resolved_video_path": str(payload.get("resolved_video_path") or ""),
        "origin": str(payload.get("origin") or ""),
        "parent_job_id": str(payload.get("parent_job_id") or ""),
        "result": result,
        "created_at": job.get("created_at") or "",
        "updated_at": job.get("updated_at") or "",
    }


def image_path_for_slot(job: dict, slot: str) -> Path:
    if str(job.get("job_type") or "") != JOB_TYPE:
        raise FullMovieThumbnailError("Không tìm thấy job thumbnail phim.")
    result = dict(job.get("result") or {})
    output_dir = Path(str(result.get("output_dir") or "")).resolve()
    if slot == "primary":
        raw_path = str(result.get("primary_path") or "")
    elif slot in {"1", "2", "3"}:
        raw_path = next(
            (
                str(item.get("path") or "")
                for item in result.get("variants") or []
                if int(item.get("index") or 0) == int(slot)
            ),
            "",
        )
    else:
        raise FullMovieThumbnailError("Vị trí thumbnail không hợp lệ.")
    path = Path(raw_path).resolve()
    try:
        path.relative_to(output_dir)
    except ValueError as exc:
        raise FullMovieThumbnailError("Đường dẫn thumbnail không hợp lệ.") from exc
    if not path.is_file():
        raise FullMovieThumbnailError("File thumbnail không còn tồn tại.")
    return path
