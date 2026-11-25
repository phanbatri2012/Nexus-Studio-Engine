"""Shared readable subtitle layout, SRT parsing, and validation."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SUBTITLE_PROFILE_VERSION = "readable_2line_v1"
SUBTITLE_RENDER_STYLE_VERSION = "readable_2line_style_v1"
SUBTITLE_MAX_LINES = 2
SUBTITLE_MAX_CHARS_PER_LINE = 42
SUBTITLE_MAX_WORDS_PER_CUE = 14
SUBTITLE_MAX_CUE_SECONDS = 6.0
SUBTITLE_PAUSE_BREAK_SECONDS = 0.35
SUBTITLE_FORCE_STYLE = (
    "FontName=Arial,FontSize=20,Bold=1,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1,"
    "Alignment=2,MarginL=120,MarginR=120,MarginV=45,WrapStyle=2"
)

_TIMESTAMP_PATTERN = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)
_TERMINAL_PUNCTUATION = re.compile(r"[.!?…][\"')\]]?$")
_SOFT_PUNCTUATION = re.compile(r"[,;:][\"')\]]?$")


class SubtitleLayoutError(ValueError):
    """Raised when subtitle input cannot satisfy the system layout profile."""


@dataclass(frozen=True)
class SubtitleStats:
    cue_count: int
    max_lines: int
    max_line_chars: int
    max_words: int
    max_duration_seconds: float

    def as_dict(self) -> dict:
        return {
            "cue_count": self.cue_count,
            "max_lines": self.max_lines,
            "max_line_chars": self.max_line_chars,
            "max_words": self.max_words,
            "max_duration_seconds": round(self.max_duration_seconds, 3),
        }


def parse_srt_time(value: str) -> float:
    hours, minutes, remainder = value.strip().replace(".", ",").split(":")
    seconds, milliseconds = remainder.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds.ljust(3, "0")[:3]) / 1000
    )


def format_srt_time(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt(path: Path) -> list[dict]:
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig"))
    cues: list[dict] = []
    for block in blocks:
        raw_lines = block.splitlines()
        timestamp_index = next(
            (
                index
                for index, line in enumerate(raw_lines)
                if _TIMESTAMP_PATTERN.search(line.strip())
            ),
            None,
        )
        if timestamp_index is None:
            continue
        match = _TIMESTAMP_PATTERN.search(raw_lines[timestamp_index].strip())
        if match is None:
            continue
        text_lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in raw_lines[timestamp_index + 1 :]
            if line.strip()
        ]
        text = "\n".join(text_lines).strip()
        if not text:
            continue
        cues.append(
            {
                "start": parse_srt_time(match.group("start")),
                "end": parse_srt_time(match.group("end")),
                "text": text,
            }
        )
    return cues


def render_srt(cues: Iterable[dict]) -> str:
    rendered: list[str] = []
    for index, cue in enumerate(cues, start=1):
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        rendered.append(
            f"{index}\n{format_srt_time(float(cue['start']))} --> "
            f"{format_srt_time(float(cue['end']))}\n{text}\n"
        )
    return "\n".join(rendered)


def _flatten_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _content_fingerprint(text: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", str(text or "")))


def validate_content_preserved(
    source_segments: Iterable[dict], cues: Iterable[dict]
) -> None:
    expected_text = " ".join(
        _flatten_text(segment.get("text")) for segment in source_segments
    ).strip()
    actual_text = " ".join(_flatten_text(cue.get("text")) for cue in cues).strip()
    if _content_fingerprint(actual_text) != _content_fingerprint(expected_text):
        raise SubtitleLayoutError(
            "Nội dung subtitle không khớp timeline nguồn; có thể bị mất hoặc lặp chữ."
        )


def _balanced_lines(text: str) -> str | None:
    clean = _flatten_text(text)
    if not clean:
        return None
    if len(clean) <= SUBTITLE_MAX_CHARS_PER_LINE:
        return clean
    words = clean.split()
    candidates: list[tuple[int, int, str, str]] = []
    for split_at in range(1, len(words)):
        left = " ".join(words[:split_at])
        right = " ".join(words[split_at:])
        if (
            len(left) <= SUBTITLE_MAX_CHARS_PER_LINE
            and len(right) <= SUBTITLE_MAX_CHARS_PER_LINE
        ):
            punctuation_penalty = 0 if re.search(r"[,;:.!?…]$", left) else 1
            candidates.append(
                (abs(len(left) - len(right)), punctuation_penalty, left, right)
            )
    if not candidates:
        return None
    _, _, left, right = min(candidates, key=lambda item: (item[0], item[1]))
    return f"{left}\n{right}"


def _fallback_words(segment: dict) -> list[dict]:
    text = _flatten_text(segment.get("text"))
    tokens = text.split()
    if not tokens:
        return []
    start = max(0.0, float(segment.get("start") or 0.0))
    end = max(start + 0.001, float(segment.get("end") or start))
    weights = [max(1, len(token)) for token in tokens]
    total_weight = float(sum(weights))
    duration = end - start
    cursor_weight = 0.0
    words: list[dict] = []
    for token, weight in zip(tokens, weights):
        word_start = start + duration * cursor_weight / total_weight
        cursor_weight += weight
        word_end = min(
            start + duration * cursor_weight / total_weight,
            word_start + SUBTITLE_MAX_CUE_SECONDS,
        )
        words.append({"start": word_start, "end": word_end, "text": token})
    return words


def _normalized_words(segment: dict) -> list[dict]:
    raw_words = segment.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        return _fallback_words(segment)
    segment_start = max(0.0, float(segment.get("start") or 0.0))
    segment_end = max(segment_start + 0.001, float(segment.get("end") or segment_start))
    words: list[dict] = []
    cursor = segment_start
    for raw in raw_words:
        if not isinstance(raw, dict):
            continue
        text = _flatten_text(raw.get("text") or raw.get("word"))
        if not text:
            continue
        try:
            start = max(cursor, segment_start, float(raw.get("start")))
            end = min(
                segment_end,
                start + SUBTITLE_MAX_CUE_SECONDS,
                max(start + 0.001, float(raw.get("end"))),
            )
        except (TypeError, ValueError):
            return _fallback_words(segment)
        if end <= start:
            return _fallback_words(segment)
        words.append({"start": start, "end": end, "text": text})
        cursor = end
    if not words:
        return _fallback_words(segment)
    source_text = _content_fingerprint(segment.get("text"))
    timed_text = _content_fingerprint(" ".join(str(word["text"]) for word in words))
    if source_text and source_text != timed_text:
        return _fallback_words(segment)
    return words


def _candidate_fits(words: list[dict]) -> bool:
    if not words or len(words) > SUBTITLE_MAX_WORDS_PER_CUE:
        return False
    if float(words[-1]["end"]) - float(words[0]["start"]) > SUBTITLE_MAX_CUE_SECONDS + 0.001:
        return False
    return _balanced_lines(" ".join(str(word["text"]) for word in words)) is not None


def _choose_break(words: list[dict], start_index: int, maximum_end: int) -> int:
    candidates = list(range(start_index + 1, maximum_end + 1))
    terminal = [
        end
        for end in candidates
        if _TERMINAL_PUNCTUATION.search(str(words[end - 1]["text"]))
    ]
    internal_terminal = [end for end in terminal if end < maximum_end]
    if internal_terminal:
        return internal_terminal[0]
    soft = [
        end
        for end in candidates
        if end - start_index >= 6
        and _SOFT_PUNCTUATION.search(str(words[end - 1]["text"]))
    ]
    pauses = [
        end
        for end in candidates
        if end < len(words)
        and end - start_index >= 6
        and float(words[end]["start"]) - float(words[end - 1]["end"])
        >= SUBTITLE_PAUSE_BREAK_SECONDS
    ]
    preferred = soft + pauses
    if preferred:
        return max(preferred)
    return terminal[0] if terminal else maximum_end


def build_readable_cues(segments: Iterable[dict]) -> list[dict]:
    source_segments = list(segments)
    cues: list[dict] = []
    for segment_index, segment in enumerate(source_segments, start=1):
        words = _normalized_words(segment)
        cursor = 0
        while cursor < len(words):
            maximum_end = cursor
            while maximum_end < len(words) and _candidate_fits(
                words[cursor : maximum_end + 1]
            ):
                maximum_end += 1
            if maximum_end == cursor:
                token = str(words[cursor]["text"])
                raise SubtitleLayoutError(
                    f"Từ quá dài không thể dàn trong 2 dòng ở segment {segment_index}: {token!r}"
                )
            end_index = _choose_break(words, cursor, maximum_end)
            chunk = words[cursor:end_index]
            laid_out = _balanced_lines(" ".join(str(word["text"]) for word in chunk))
            if laid_out is None:
                raise SubtitleLayoutError(
                    f"Không thể dàn subtitle segment {segment_index} trong 2 dòng."
                )
            cue_start = float(chunk[0]["start"])
            cue_end = float(chunk[-1]["end"])
            if cues and cue_start < float(cues[-1]["end"]):
                cue_start = float(cues[-1]["end"])
            if cue_end <= cue_start:
                raise SubtitleLayoutError(
                    f"Timestamp subtitle không tăng tại segment {segment_index}."
                )
            cues.append({"start": cue_start, "end": cue_end, "text": laid_out})
            cursor = end_index
    validate_cues(cues)
    validate_content_preserved(source_segments, cues)
    return cues


def validate_cues(cues: Iterable[dict]) -> SubtitleStats:
    normalized = list(cues)
    if not normalized:
        raise SubtitleLayoutError("Subtitle không có cue thoại.")
    previous_end = -math.inf
    max_lines = 0
    max_line_chars = 0
    max_words = 0
    max_duration = 0.0
    for index, cue in enumerate(normalized, start=1):
        try:
            start = float(cue.get("start"))
            end = float(cue.get("end"))
        except (TypeError, ValueError) as exc:
            raise SubtitleLayoutError(f"Cue {index} có timestamp không hợp lệ.") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise SubtitleLayoutError(f"Cue {index} có timestamp không hợp lệ.")
        if start < previous_end - 0.001:
            raise SubtitleLayoutError(f"Cue {index} chồng lấn cue trước đó.")
        lines = [line.strip() for line in str(cue.get("text") or "").splitlines() if line.strip()]
        if not lines:
            raise SubtitleLayoutError(f"Cue {index} không có nội dung.")
        if len(lines) > SUBTITLE_MAX_LINES:
            raise SubtitleLayoutError(
                f"Cue {index} có {len(lines)} dòng, vượt giới hạn {SUBTITLE_MAX_LINES}."
            )
        longest_line = max(len(line) for line in lines)
        if longest_line > SUBTITLE_MAX_CHARS_PER_LINE:
            raise SubtitleLayoutError(
                f"Cue {index} có dòng dài {longest_line} ký tự, vượt giới hạn "
                f"{SUBTITLE_MAX_CHARS_PER_LINE}."
            )
        word_count = len(_flatten_text(" ".join(lines)).split())
        if word_count > SUBTITLE_MAX_WORDS_PER_CUE:
            raise SubtitleLayoutError(
                f"Cue {index} có {word_count} từ, vượt giới hạn "
                f"{SUBTITLE_MAX_WORDS_PER_CUE}."
            )
        duration = end - start
        if duration > SUBTITLE_MAX_CUE_SECONDS + 0.001:
            raise SubtitleLayoutError(
                f"Cue {index} dài {duration:.3f}s, vượt giới hạn "
                f"{SUBTITLE_MAX_CUE_SECONDS:.1f}s."
            )
        previous_end = end
        max_lines = max(max_lines, len(lines))
        max_line_chars = max(max_line_chars, longest_line)
        max_words = max(max_words, word_count)
        max_duration = max(max_duration, duration)
    return SubtitleStats(
        cue_count=len(normalized),
        max_lines=max_lines,
        max_line_chars=max_line_chars,
        max_words=max_words,
        max_duration_seconds=max_duration,
    )


def validate_srt(path: Path) -> dict:
    if not path.is_file() or path.suffix.casefold() != ".srt" or path.stat().st_size <= 0:
        raise SubtitleLayoutError("Caption SRT không tồn tại hoặc rỗng.")
    return {"path": str(path), **validate_cues(parse_srt(path)).as_dict()}
