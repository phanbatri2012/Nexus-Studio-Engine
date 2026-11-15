from __future__ import annotations

import hashlib
import math
import re
from collections import Counter


BODY_REWRITE_TARGET_SOURCE_WORDS = 950
BODY_REWRITE_MAX_PARTS = 10
BODY_REWRITE_MIN_WORD_RATIO = 0.65
BODY_REWRITE_MAX_WORD_RATIO = 1.25
BODY_REWRITE_HARD_MAX_WORD_RATIO = 1.5

EDITORIAL_PATTERNS = (
    re.compile(
        r"\b(?:"
        r"kịch bản\s+(?:gốc|này|trên|dưới|được cung cấp|đang viết|vừa viết)|"
        r"theo\s+(?:nội dung\s+)?kịch bản|"
        r"(?:tham chiếu|dựa|bám|so với)\s+(?:đến\s+)?(?:nội dung\s+)?kịch bản|"
        r"dàn ý|prompt|đề bài"
        r")\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:nguồn đầu vào|dữ liệu đầu vào|quá trình biên tập)\b", re.IGNORECASE),
    re.compile(r"\b(?:ở|trong)\s+(?:phần|đoạn)\s+(?:trên|dưới|trước|sau)\b", re.IGNORECASE),
)

REPETITIVE_TRANSITIONS = (
    "tuy nhiên",
    "đồng thời",
    "trong hoàn cảnh",
    "chính vì vậy",
    "đây là",
    "tiếp tục",
)

NARRATION_SECTION_TAGS = ("INTRO", "BODY", "OUTRO")
NARRATION_MAX_SOURCE_RATIO = 1.15
NARRATION_HARD_MAX_SOURCE_RATIO = 1.30
NARRATION_BODY_HARD_MAX_SOURCE_RATIO = 1.25
NARRATION_BOUNDARY_WORDS = 350
NARRATION_INTRO_BODY_SIMILARITY_LIMIT = 0.52
NARRATION_BODY_OUTRO_SIMILARITY_LIMIT = 0.55
NARRATION_SECTION_WORD_TOLERANCE_RATIO = 0.02
NARRATION_SECTION_WORD_TOLERANCE_MIN = 10
NARRATION_SECTION_REPAIR_BUFFER_RATIO = 0.02
NARRATION_SECTION_REPAIR_BUFFER_MIN = 5

CONTENT_SIMILARITY_STOPWORDS = {
    "ai",
    "anh",
    "bị",
    "bởi",
    "các",
    "cái",
    "cho",
    "chỉ",
    "chính",
    "chúng",
    "có",
    "còn",
    "cũng",
    "của",
    "do",
    "dưới",
    "giữa",
    "hay",
    "hơn",
    "khi",
    "không",
    "là",
    "lại",
    "mà",
    "một",
    "này",
    "năm",
    "ngay",
    "người",
    "như",
    "nhưng",
    "những",
    "nơi",
    "ra",
    "rất",
    "sau",
    "sự",
    "tại",
    "theo",
    "thì",
    "trên",
    "trong",
    "trước",
    "từ",
    "và",
    "vào",
    "về",
    "việc",
    "vì",
    "với",
    "đang",
    "đã",
    "đó",
    "được",
    "để",
}


def content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def narration_section_word_limits(maximum_words: int) -> tuple[int, int]:
    """Return the allowed overflow and hard maximum for one narration section."""

    target_maximum = max(0, int(maximum_words or 0))
    if target_maximum <= 0:
        return 0, 0
    tolerance = max(
        NARRATION_SECTION_WORD_TOLERANCE_MIN,
        math.ceil(target_maximum * NARRATION_SECTION_WORD_TOLERANCE_RATIO),
    )
    return tolerance, target_maximum + tolerance


def narration_section_repair_maximum(
    minimum_words: int,
    maximum_words: int,
) -> int:
    """Choose a buffered repair target without collapsing a valid word range."""

    safe_minimum = max(0, int(minimum_words or 0))
    target_maximum = max(safe_minimum, int(maximum_words or 0))
    desired_buffer = max(
        NARRATION_SECTION_REPAIR_BUFFER_MIN,
        math.ceil(target_maximum * NARRATION_SECTION_REPAIR_BUFFER_RATIO),
    )
    available_buffer = max(0, (target_maximum - safe_minimum) // 2)
    return target_maximum - min(desired_buffer, available_buffer)


def split_transcript_source_segments(
    transcript: str,
    *,
    target_words: int = BODY_REWRITE_TARGET_SOURCE_WORDS,
    max_parts: int = BODY_REWRITE_MAX_PARTS,
) -> list[str]:
    """Split the exact source into bounded, deterministic word spans."""

    words = normalize_whitespace(transcript).split()
    if not words:
        return []

    safe_target = max(300, int(target_words or BODY_REWRITE_TARGET_SOURCE_WORDS))
    safe_max_parts = max(1, min(int(max_parts or BODY_REWRITE_MAX_PARTS), 20))
    part_count = max(1, min(safe_max_parts, math.ceil(len(words) / safe_target)))
    words_per_part = math.ceil(len(words) / part_count)
    return [
        " ".join(words[start : start + words_per_part])
        for start in range(0, len(words), words_per_part)
    ]


def part_word_budget(source_segment: str) -> tuple[int, int]:
    source_words = len(normalize_whitespace(source_segment).split())
    if source_words <= 0:
        return 0, 0
    minimum = max(180, round(source_words * 0.75))
    maximum = max(minimum + 80, round(source_words * 1.15))
    return minimum, maximum


def allocate_outline_part_word_budgets(
    transcript: str,
    outline_parts: list[str],
) -> list[tuple[int, int]]:
    """Allocate one bounded BODY budget while keeping total expansion near source size."""

    if not outline_parts:
        return []
    source_word_count = len(normalize_whitespace(transcript).split())
    weights = [max(1, len(normalize_whitespace(part).split())) for part in outline_parts]
    total_weight = max(1, sum(weights))

    def allocate(total: int, floors: list[int]) -> list[int]:
        values = list(floors)
        remaining = max(0, total - sum(values))
        raw_shares = [remaining * (weight / total_weight) for weight in weights]
        whole_shares = [math.floor(value) for value in raw_shares]
        values = [value + extra for value, extra in zip(values, whole_shares)]
        leftover = remaining - sum(whole_shares)
        order = sorted(
            range(len(weights)),
            key=lambda index: raw_shares[index] - whole_shares[index],
            reverse=True,
        )
        for index in order[:leftover]:
            values[index] += 1
        return values

    part_count = len(outline_parts)
    per_part_floor = max(15, min(100, source_word_count // max(1, part_count * 3)))
    outline_word_count = sum(weights)
    minimum_total = max(per_part_floor * part_count, round(outline_word_count * 0.80))
    minimums = allocate(minimum_total, [per_part_floor] * part_count)
    maximum_floor = [value + 10 for value in minimums]
    maximum_total = max(
        sum(maximum_floor),
        min(
            round(source_word_count * 1.05),
            round(outline_word_count * 1.35),
        ),
    )
    maximums = allocate(maximum_total, maximum_floor)
    return list(zip(minimums, maximums))


def build_body_part_prompt(
    source_segment: str,
    *,
    part_index: int,
    part_count: int,
    previous_context: str = "",
    next_context: str = "",
) -> str:
    minimum_words, maximum_words = part_word_budget(source_segment)
    previous_context = normalize_whitespace(previous_context)
    next_context = normalize_whitespace(next_context)
    return f"""Bạn đang viết phần {part_index + 1}/{part_count} của lời dẫn phim tài liệu lịch sử cho kênh \"Góc Khuất Việt Sử\".

NGUỒN DUY NHẤT ĐƯỢC PHÉP DÙNG:
<SOURCE>
{normalize_whitespace(source_segment)}
</SOURCE>

NGỮ CẢNH NỐI MẠCH, CHỈ ĐỂ TRÁNH LẶP, KHÔNG ĐƯỢC KỂ LẠI:
<PREVIOUS_CONTEXT>{previous_context}</PREVIOUS_CONTEXT>
<NEXT_CONTEXT>{next_context}</NEXT_CONTEXT>

YÊU CẦU BẮT BUỘC:
1. Chuyển thể đúng phần SOURCE thành lời kể tự nhiên, mạch lạc, truyền cảm và giàu nhịp điệu.
2. Mọi ngày tháng, con số, tên người, địa danh, trích dẫn và sự kiện phải xuất hiện trong SOURCE. Không tự bổ sung kiến thức bên ngoài dù cho rằng kiến thức đó đúng.
3. Nếu SOURCE có câu nhận dạng giọng nói không rõ hoặc mâu thuẫn, hãy diễn đạt trung tính hay lược bỏ chi tiết không chắc chắn; tuyệt đối không tự đoán dữ kiện thay thế.
4. Bỏ lời chào kênh cũ, quảng cáo, lời kêu gọi xem phần khác, nhạc hiệu và câu chuyển tập. Không bỏ sự kiện lịch sử.
5. Mỗi câu phải bổ sung một diễn biến, dữ kiện hoặc quan hệ nhân quả có ích. Không lặp lại bối cảnh, mức độ nguy hiểm, ý nghĩa hoặc kết luận đã nói.
6. Không nhắc đến kịch bản, transcript, nguồn, dàn ý, prompt, phần trên, phần dưới hay quá trình biên tập.
7. Viết thành một khối văn xuôi duy nhất, không tiêu đề, không gạch đầu dòng, không ghi chú và không dùng dấu gạch ngang.
8. Mục tiêu từ {minimum_words} đến {maximum_words} từ. Đây là giới hạn bảo vệ mật độ thông tin, không được kéo dài bằng câu đệm.
9. Chỉ trả về lời dẫn hoàn chỉnh của phần này, không giải thích."""


def build_body_part_repair_prompt(
    source_segment: str,
    draft: str,
    issues: list[dict],
    *,
    part_index: int,
    part_count: int,
) -> str:
    issue_text = "; ".join(str(item.get("message") or item.get("code") or "") for item in issues)
    minimum_words, maximum_words = part_word_budget(source_segment)
    return f"""Hãy sửa lại phần {part_index + 1}/{part_count} vì bản nháp vi phạm: {issue_text}.

NGUỒN DUY NHẤT:
<SOURCE>{normalize_whitespace(source_segment)}</SOURCE>

BẢN NHÁP CẦN SỬA:
<DRAFT>{normalize_whitespace(draft)}</DRAFT>

Chỉ giữ dữ kiện có trong SOURCE, xóa mọi dữ kiện tự bổ sung, câu lặp và ngôn ngữ biên tập. Viết từ {minimum_words} đến {maximum_words} từ thành một khối văn xuôi. Chỉ trả về bản đã sửa."""


def _numeric_tokens(text: str) -> set[str]:
    return {
        token.replace(".", "").replace(",", "")
        for token in re.findall(r"(?<!\w)\d[\d.,]*(?!\w)", str(text or ""))
        if token.strip(".,")
    }


def _editorial_hits(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in EDITORIAL_PATTERNS:
        for match in pattern.finditer(str(text or "")):
            value = match.group(0).strip()
            if value and value.lower() not in {item.lower() for item in hits}:
                hits.append(value)
    return hits


def audit_body_part(
    body_part: str,
    *,
    source_segment: str,
    full_transcript: str,
) -> dict:
    body_part = normalize_whitespace(body_part)
    source_segment = normalize_whitespace(source_segment)
    word_count = len(body_part.split())
    source_word_count = len(source_segment.split())
    ratio = word_count / source_word_count if source_word_count else 0.0
    errors: list[dict] = []
    warnings: list[dict] = []

    if word_count < 15:
        errors.append({"code": "body_part_too_short", "message": "Phần BODY không có đủ nội dung."})

    editorial_hits = _editorial_hits(body_part)
    if editorial_hits:
        errors.append(
            {
                "code": "editorial_language",
                "message": "Còn ngôn ngữ biên tập: " + ", ".join(editorial_hits[:5]),
                "values": editorial_hits,
            }
        )

    unsupported_numbers = sorted(_numeric_tokens(body_part) - _numeric_tokens(full_transcript))
    if unsupported_numbers:
        errors.append(
            {
                "code": "unsupported_numeric_facts",
                "message": "Có số liệu không xuất hiện trong transcript: " + ", ".join(unsupported_numbers[:12]),
                "values": unsupported_numbers,
            }
        )

    if ratio > BODY_REWRITE_HARD_MAX_WORD_RATIO:
        errors.append(
            {
                "code": "body_part_excessive_expansion",
                "message": f"Phần viết dài gấp {ratio:.2f} lần đoạn nguồn.",
            }
        )
    elif ratio > BODY_REWRITE_MAX_WORD_RATIO:
        warnings.append(
            {
                "code": "body_part_expansion_warning",
                "message": f"Phần viết dài gấp {ratio:.2f} lần đoạn nguồn.",
            }
        )
    elif ratio < BODY_REWRITE_MIN_WORD_RATIO:
        warnings.append(
            {
                "code": "body_part_compression_warning",
                "message": f"Phần viết chỉ còn {ratio:.2f} lần đoạn nguồn; cần kiểm tra mất ý.",
            }
        )

    return {
        "can_apply": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "word_count": word_count,
            "source_word_count": source_word_count,
            "source_expansion_ratio": round(ratio, 4),
        },
    }


def audit_body_candidate(body: str, transcript: str) -> dict:
    body = str(body or "").strip()
    transcript = normalize_whitespace(transcript)
    body_word_count = len(normalize_whitespace(body).split())
    source_word_count = len(transcript.split())
    ratio = body_word_count / source_word_count if source_word_count else 0.0
    paragraphs = [normalize_whitespace(item) for item in re.split(r"\n\s*\n", body) if item.strip()]
    paragraph_counts = Counter(paragraphs)
    exact_duplicates = [item for item, count in paragraph_counts.items() if count > 1]
    editorial_hits = _editorial_hits(body)
    unsupported_numbers = sorted(_numeric_tokens(body) - _numeric_tokens(transcript))
    errors: list[dict] = []
    warnings: list[dict] = []

    if not body:
        errors.append({"code": "empty_body", "message": "BODY ứng viên đang trống."})
    if editorial_hits:
        errors.append(
            {
                "code": "editorial_language",
                "message": "BODY còn ngôn ngữ biên tập.",
                "values": editorial_hits,
            }
        )
    if unsupported_numbers:
        errors.append(
            {
                "code": "unsupported_numeric_facts",
                "message": "BODY có số liệu không xuất hiện trong transcript.",
                "values": unsupported_numbers,
            }
        )
    if exact_duplicates:
        errors.append(
            {
                "code": "duplicate_body_paragraphs",
                "message": "BODY có đoạn bị lặp nguyên văn.",
                "count": len(exact_duplicates),
            }
        )
    if ratio > BODY_REWRITE_HARD_MAX_WORD_RATIO:
        errors.append(
            {
                "code": "excessive_source_expansion",
                "message": f"BODY dài gấp {ratio:.2f} lần transcript.",
            }
        )
    elif ratio > BODY_REWRITE_MAX_WORD_RATIO:
        warnings.append(
            {
                "code": "source_expansion_warning",
                "message": f"BODY dài gấp {ratio:.2f} lần transcript.",
            }
        )
    elif source_word_count and ratio < BODY_REWRITE_MIN_WORD_RATIO:
        warnings.append(
            {
                "code": "source_compression_warning",
                "message": f"BODY chỉ còn {ratio:.2f} lần transcript; cần kiểm tra mất ý.",
            }
        )

    normalized_lower = normalize_whitespace(body).lower()
    transition_counts = {
        phrase: len(re.findall(rf"\b{re.escape(phrase)}\b", normalized_lower))
        for phrase in REPETITIVE_TRANSITIONS
    }
    dense_transitions = {
        phrase: count
        for phrase, count in transition_counts.items()
        if count > max(8, math.ceil(body_word_count / 500))
    }
    if dense_transitions:
        warnings.append(
            {
                "code": "repetitive_transitions",
                "message": "Một số từ nối bị lặp dày: "
                + ", ".join(f"{key} ({value})" for key, value in dense_transitions.items()),
                "values": dense_transitions,
            }
        )

    return {
        "can_apply": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "word_count": body_word_count,
            "source_word_count": source_word_count,
            "source_expansion_ratio": round(ratio, 4),
            "paragraph_count": len(paragraphs),
            "unsupported_numeric_count": len(unsupported_numbers),
            "editorial_hit_count": len(editorial_hits),
            "transition_counts": transition_counts,
        },
        "body_hash": content_hash(body),
    }


def section_text(script: str, section_tag: str) -> str:
    match = re.search(
        rf"### \[{re.escape(section_tag)}\]\s*(.*?)(?=\n### \[|\Z)",
        str(script or ""),
        flags=re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def replace_script_section(script: str, section_tag: str, replacement: str) -> str:
    """Replace exactly one tagged section while preserving all surrounding text."""

    script_text = str(script or "")
    header_pattern = re.compile(
        rf"^### \[{re.escape(section_tag)}\][^\S\r\n]*(?:\r?\n|$)",
        flags=re.MULTILINE,
    )
    matches = list(header_pattern.finditer(script_text))
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one [{section_tag}] section, found {len(matches)}."
        )
    clean_replacement = str(replacement or "").strip()
    if not clean_replacement:
        raise ValueError(f"Replacement for [{section_tag}] cannot be empty.")
    header = matches[0]
    next_header = re.search(r"^### \[", script_text[header.end() :], flags=re.MULTILINE)
    content_end = header.end() + next_header.start() if next_header else len(script_text)
    current_content = script_text[header.end() : content_end]
    trailing_match = re.search(r"\s*\Z", current_content)
    trailing = trailing_match.group(0) if trailing_match else ""
    return (
        script_text[: header.end()]
        + clean_replacement
        + trailing
        + script_text[content_end:]
    )


def build_narration_sections(*, intro: str, body: str, outro: str) -> str:
    """Build the portable three-section payload stored by narration revisions."""

    sections = {
        "INTRO": str(intro or "").strip(),
        "BODY": str(body or "").strip(),
        "OUTRO": str(outro or "").strip(),
    }
    missing = [tag for tag, value in sections.items() if not value]
    if missing:
        raise ValueError("Narration is missing sections: " + ", ".join(missing))
    return "\n\n".join(f"### [{tag}]\n{sections[tag]}" for tag in NARRATION_SECTION_TAGS)


def replace_narration_sections(script: str, narration: str) -> str:
    """Replace INTRO, BODY and OUTRO while preserving every other script section."""

    candidate = str(narration or "")
    replacements: dict[str, str] = {}
    for tag in NARRATION_SECTION_TAGS:
        header_count = len(
            re.findall(
                rf"^### \[{re.escape(tag)}\][^\S\r\n]*(?:\r?\n|$)",
                candidate,
                flags=re.MULTILINE,
            )
        )
        if header_count != 1:
            raise ValueError(
                f"Expected exactly one [{tag}] section in narration, found {header_count}."
            )
        replacements[tag] = section_text(candidate, tag)
        if not replacements[tag]:
            raise ValueError(f"Narration section [{tag}] cannot be empty.")

    updated = str(script or "")
    for tag in NARRATION_SECTION_TAGS:
        updated = replace_script_section(updated, tag, replacements[tag])
    return updated


def _content_similarity_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-zà-ỹđ]+", str(text or "").lower())
        if len(token) > 2 and token not in CONTENT_SIMILARITY_STOPWORDS
    ]


def content_cosine_similarity(left: str, right: str) -> float:
    """Conservative lexical-semantic proxy used only at narration boundaries."""

    left_counts = Counter(_content_similarity_tokens(left))
    right_counts = Counter(_content_similarity_tokens(right))
    if not left_counts or not right_counts:
        return 0.0
    dot_product = sum(value * right_counts[token] for token, value in left_counts.items())
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot_product / (left_norm * right_norm)


def audit_narration_candidate(narration: str, transcript: str) -> dict:
    """Audit one complete INTRO/BODY/OUTRO candidate before it can be applied."""

    narration = str(narration or "").strip()
    transcript = normalize_whitespace(transcript)
    errors: list[dict] = []
    warnings: list[dict] = []
    sections: dict[str, str] = {}

    for tag in NARRATION_SECTION_TAGS:
        header_count = len(
            re.findall(
                rf"^### \[{re.escape(tag)}\][^\S\r\n]*(?:\r?\n|$)",
                narration,
                flags=re.MULTILINE,
            )
        )
        if header_count != 1:
            errors.append(
                {
                    "code": "invalid_narration_structure",
                    "section": tag,
                    "message": f"Cần đúng một section {tag}, hiện có {header_count}.",
                }
            )
        sections[tag] = section_text(narration, tag) if header_count else ""
        if header_count == 1 and not sections[tag]:
            errors.append(
                {
                    "code": "empty_narration_section",
                    "section": tag,
                    "message": f"Section {tag} đang trống.",
                }
            )

    section_word_counts = {
        tag: len(normalize_whitespace(value).split())
        for tag, value in sections.items()
    }
    body = sections.get("BODY", "")
    body_words = normalize_whitespace(body).split()
    source_word_count = len(transcript.split())
    narration_word_count = sum(section_word_counts.values())
    body_word_count = section_word_counts.get("BODY", 0)
    narration_ratio = narration_word_count / source_word_count if source_word_count else 0.0
    body_ratio = body_word_count / source_word_count if source_word_count else 0.0

    if section_word_counts.get("INTRO", 0) < 15:
        errors.append(
            {"code": "intro_too_short", "section": "INTRO", "message": "INTRO quá ngắn."}
        )
    elif section_word_counts.get("INTRO", 0) > 600:
        errors.append(
            {
                "code": "intro_too_long",
                "section": "INTRO",
                "message": "INTRO vượt 600 từ và đang lấn sang phần thân bài.",
            }
        )

    if body_word_count < 15:
        errors.append(
            {"code": "body_too_short", "section": "BODY", "message": "BODY quá ngắn."}
        )
    if section_word_counts.get("OUTRO", 0) < 15:
        errors.append(
            {"code": "outro_too_short", "section": "OUTRO", "message": "OUTRO quá ngắn."}
        )
    elif section_word_counts.get("OUTRO", 0) > 900:
        errors.append(
            {
                "code": "outro_too_long",
                "section": "OUTRO",
                "message": "OUTRO vượt 900 từ và đang kể lại quá nhiều nội dung.",
            }
        )

    generic_report = audit_body_candidate(narration, transcript)
    accepted_generic_codes = {
        "editorial_language",
        "unsupported_numeric_facts",
        "duplicate_body_paragraphs",
    }
    errors.extend(
        item
        for item in generic_report.get("errors", [])
        if item.get("code") in accepted_generic_codes
    )
    warnings.extend(
        item
        for item in generic_report.get("warnings", [])
        if item.get("code") == "repetitive_transitions"
    )

    if source_word_count:
        if narration_ratio > NARRATION_HARD_MAX_SOURCE_RATIO:
            errors.append(
                {
                    "code": "narration_excessive_expansion",
                    "message": (
                        f"Toàn bộ lời đọc dài gấp {narration_ratio:.2f} lần transcript; "
                        "cần rút gọn trước khi áp dụng."
                    ),
                }
            )
        elif narration_ratio > NARRATION_MAX_SOURCE_RATIO:
            warnings.append(
                {
                    "code": "narration_expansion_warning",
                    "message": f"Toàn bộ lời đọc dài gấp {narration_ratio:.2f} lần transcript.",
                }
            )
        if body_ratio > NARRATION_BODY_HARD_MAX_SOURCE_RATIO:
            errors.append(
                {
                    "code": "body_excessive_expansion",
                    "section": "BODY",
                    "message": f"Riêng BODY dài gấp {body_ratio:.2f} lần transcript.",
                }
            )

    intro_body_similarity = 0.0
    body_outro_similarity = 0.0
    if sections.get("INTRO") and body_words:
        intro_body_similarity = content_cosine_similarity(
            sections["INTRO"],
            " ".join(body_words[:NARRATION_BOUNDARY_WORDS]),
        )
        if intro_body_similarity >= NARRATION_INTRO_BODY_SIMILARITY_LIMIT:
            errors.append(
                {
                    "code": "intro_body_semantic_overlap",
                    "section": "INTRO/BODY",
                    "message": (
                        "Đầu BODY đang kể lại quá nhiều nội dung đã có trong INTRO "
                        f"(độ tương đồng {intro_body_similarity:.2f})."
                    ),
                }
            )
    if sections.get("OUTRO") and body_words:
        body_outro_similarity = content_cosine_similarity(
            " ".join(body_words[-NARRATION_BOUNDARY_WORDS:]),
            sections["OUTRO"],
        )
        if body_outro_similarity >= NARRATION_BODY_OUTRO_SIMILARITY_LIMIT:
            errors.append(
                {
                    "code": "body_outro_semantic_overlap",
                    "section": "BODY/OUTRO",
                    "message": (
                        "Cuối BODY đang kể lại quá nhiều nội dung dành cho OUTRO "
                        f"(độ tương đồng {body_outro_similarity:.2f})."
                    ),
                }
            )

    return {
        "can_apply": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "word_count": narration_word_count,
            "source_word_count": source_word_count,
            "source_expansion_ratio": round(narration_ratio, 4),
            "body_source_expansion_ratio": round(body_ratio, 4),
            "section_word_counts": section_word_counts,
            "intro_body_similarity": round(intro_body_similarity, 4),
            "body_outro_similarity": round(body_outro_similarity, 4),
            "unsupported_numeric_count": len(
                _numeric_tokens(narration) - _numeric_tokens(transcript)
            ),
            "editorial_hit_count": len(_editorial_hits(narration)),
        },
        "narration_hash": content_hash(narration),
    }
