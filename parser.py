from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import pdfplumber

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIME_RE = re.compile(r"\((\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\)")
SESSION_RE = re.compile(r"\b(\d{4})\s*-\s*(\d{4})\b")
SEMESTER_RE = re.compile(r"Semester#\s*(\d+)")
DEGREE_RE = re.compile(r"^(BS|MS|PhD)\s+(?:in\s+)?", re.IGNORECASE)
SECTION_RE = re.compile(r"(Regular|Self Support)\s*\d+", re.IGNORECASE)

PROGRAM_CODES = {"computer science - specialization in artificial intelligence": "AI"}
PROGRAM_NAMES = {"computer science - specialization in artificial intelligence": "Artificial Intelligence"}
ROOM_RE = re.compile(r"\b(?:smart room|smart lab|committee room|online|room|hall|lab|cr|l)\b", re.IGNORECASE)
ROW_GAP_THRESHOLD = 80


@dataclass
class ParsedSession:
    combined_class: str | None
    course_title: str | None
    course_code: str | None
    course_truncated: bool
    program_line: str | None
    program_truncated: bool
    degree: str | None
    program: str | None
    program_code: str | None
    section: str | None
    session: str | None
    semester: int | None
    teacher_name: str | None
    teacher_truncated: bool
    start_time: str | None
    end_time: str | None
    practical: bool
    raw_lines: list[str]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Candidate:
    page: int
    region: int
    day: str
    top: float
    bottom: float
    lines: list[str]


def parse_timetable(pdf_path: str, *, resolve_truncated: bool = True, keep_truncated: bool = False) -> dict:
    timetable, _ = parse_timetable_with_report(
        pdf_path, resolve_truncated=resolve_truncated, keep_truncated=keep_truncated
    )
    return timetable


def parse_timetable_with_report(
    pdf_path: str, *, resolve_truncated: bool = True, keep_truncated: bool = False
) -> tuple[dict, dict]:
    candidates, report = extract_candidates(pdf_path)
    references = unique_program_lines(line for item in candidates for line in item.lines)
    timetable: dict[str, dict[str, list[dict]]] = {}

    for candidate in candidates:
        room = candidate_room(candidate, report["rooms"])
        if not room:
            report["warnings"].append({
                "type": "unassigned_session", "page": candidate.page,
                "region": candidate.region, "day": candidate.day,
                "top": round(candidate.top, 1), "lines": candidate.lines,
            })
            continue
        sessions = parse_cell(
            "\n".join(candidate.lines), references,
            resolve_truncated=resolve_truncated, keep_truncated=keep_truncated,
        )
        if not sessions:
            report["warnings"].append({
                "type": "malformed_session", "page": candidate.page,
                "region": candidate.region, "day": candidate.day,
                "room": room, "top": round(candidate.top, 1),
                "lines": candidate.lines,
            })
            continue
        timetable.setdefault(candidate.day, {}).setdefault(room, []).extend(
            item.to_dict() for item in sessions
        )

    infer_missing_semesters(timetable)
    report["summary"] = {
        "pages": report.pop("page_count"),
        "regions": len(report["regions"]),
        "rooms": len({item["room"] for item in report["rooms"]}),
        "sessions": sum(len(items) for rooms in timetable.values() for items in rooms.values()),
        "warnings": len(report["warnings"]),
    }
    report["status"] = "failed" if not candidates else ("partial" if report["warnings"] else "clean")
    return {"timestamp": datetime.now().isoformat(), "rooms": report["rooms"], **timetable}, report


def extract_candidates(pdf_path: str) -> tuple[list[Candidate], dict]:
    report: dict = {"status": "failed", "regions": [], "rooms": [], "warnings": [], "page_count": 0}
    candidates: list[Candidate] = []
    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as exc:
        report["warnings"].append({"type": "pdf_open_error", "message": str(exc)})
        return candidates, report

    with pdf:
        report["page_count"] = len(pdf.pages)
        for page_number, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=True, keep_blank_chars=False)
            lines = make_lines(words)
            headers = find_day_headers(lines)
            for region, header in enumerate(headers, start=1):
                next_top = headers[region]["top"] if region < len(headers) else page.height
                day_bounds = day_boundaries(header["days"], page.width)
                report["regions"].append({
                    "page": page_number, "region": region,
                    "top": round(header["top"], 1),
                    "days": [day for day, _ in header["days"]],
                })
                region_lines = [
                    line for line in lines
                    if line["top"] > header["top"] + 1 and line["top"] < next_top - 1
                ]
                rooms = find_rooms(region_lines, day_bounds, page_number, region)
                report["rooms"].extend(rooms)
                if not rooms:
                    report["warnings"].append({
                        "type": "missing_room_label", "page": page_number, "region": region
                    })
                for day, (left, right) in day_bounds.items():
                    day_lines = crop_lines(region_lines, left, right)
                    candidates.extend(make_candidates(page_number, region, day, day_lines))
    return candidates, report


def make_lines(words: list[dict], tolerance: float = 1.5) -> list[dict]:
    """Group positioned PDF words into readable visual lines."""
    groups: list[list[dict]] = []
    for word in sorted(words, key=lambda value: (value["top"], value["x0"])):
        if groups and abs(groups[-1][0]["top"] - word["top"]) <= tolerance:
            groups[-1].append(word)
        else:
            groups.append([word])
    return [
        {
            "top": min(word["top"] for word in group),
            "bottom": max(word["bottom"] for word in group),
            "x0": min(word["x0"] for word in group),
            "x1": max(word["x1"] for word in group),
            "words": sorted(group, key=lambda value: value["x0"]),
        }
        for group in groups
    ]


def find_day_headers(lines: list[dict]) -> list[dict]:
    headers: list[dict] = []
    for line in lines:
        days = []
        for word in line["words"]:
            day = normalize_day(word["text"])
            if day:
                days.append((day, (word["x0"] + word["x1"]) / 2))
        if len(days) >= 2:
            headers.append({"top": line["top"], "days": days})
    # A header may be represented by several nearby text lines; retain the
    # line with the most distinct days in each vertical band.
    selected: list[dict] = []
    for header in headers:
        if selected and header["top"] - selected[-1]["top"] < 25:
            if len(header["days"]) > len(selected[-1]["days"]):
                selected[-1] = header
        else:
            selected.append(header)
    return selected


def day_boundaries(days: list[tuple[str, float]], page_width: float) -> dict[str, tuple[float, float]]:
    days = sorted(days, key=lambda item: item[1])
    result: dict[str, tuple[float, float]] = {}
    for index, (day, center) in enumerate(days):
        left = (
            max(0, center - (days[index + 1][1] - center) / 2)
            if index == 0 else (days[index - 1][1] + center) / 2
        )
        right = (
            min(page_width, center + (center - days[index - 1][1]) / 2)
            if index == len(days) - 1 else (center + days[index + 1][1]) / 2
        )
        result[day] = (left, right)
    return result


def crop_lines(lines: list[dict], left: float, right: float) -> list[tuple[float, str]]:
    result = []
    for line in lines:
        words = [word for word in line["words"] if left <= (word["x0"] + word["x1"]) / 2 < right]
        if words:
            result.append((line["top"], " ".join(word["text"] for word in words)))
    return result


def make_candidates(page: int, region: int, day: str, lines: list[tuple[float, str]]) -> list[Candidate]:
    result: list[Candidate] = []
    pending: list[str] = []
    pending_bottom = 0.0
    top = 0.0
    previous_top: float | None = None
    for line_top, text in lines:
        text = normalize_spacing(text)
        if not text:
            continue
        if previous_top is not None and line_top - previous_top > ROW_GAP_THRESHOLD:
            pending = []
        if not pending:
            top = line_top
        pending.append(text)
        pending_bottom = line_top
        previous_top = line_top
        if TIME_RE.search(text):
            result.append(Candidate(page, region, day, top, pending_bottom, pending))
            pending = []
            pending_bottom = 0.0
            previous_top = None
    return result


def find_rooms(lines: list[dict], day_bounds: dict[str, tuple[float, float]], page: int, region: int) -> list[dict]:
    first_day_left = min(left for left, _ in day_bounds.values())
    left_lines = []
    for line in lines:
        words = [word for word in line["words"] if (word["x0"] + word["x1"]) / 2 < first_day_left]
        if words:
            left_lines.append((line["top"], " ".join(word["text"] for word in words)))

    blocks: list[list[tuple[float, str]]] = []
    for item in left_lines:
        if blocks and item[0] - blocks[-1][-1][0] <= 14:
            blocks[-1].append(item)
        else:
            blocks.append([item])
    rooms = []
    for block in blocks:
        text = normalize_spacing(" ".join(value for _, value in block))
        if looks_like_room(text):
            rooms.append({
                "page": page, "region": region, "room": normalize_room_label(text),
                "top": sum(value for value, _ in block) / len(block),
            })
    return rooms


def looks_like_room(value: str) -> bool:
    text = value.lower()
    if re.match(r"^room\s*/\s*lab\b", text):
        return False
    return bool(ROOM_RE.search(text))


def normalize_room_label(value: str) -> str:
    """Restore labels whose PDF words are emitted in column, not reading, order."""
    value = normalize_spacing(value)
    department = re.search(
        r"\bDepartment of [A-Za-z ]+?(?=\s+(?:CR-|L-|Lab|Room|Hall)|\s+\d+$|$)",
        value,
    )
    if not department or department.start() == 0:
        return value
    prefix = value[:department.start()].strip()
    suffix = value[department.end():].strip()
    return normalize_spacing(f"{department.group(0)} {prefix} {suffix}")


def candidate_room(candidate: Candidate, rooms: list[dict]) -> str | None:
    matches = [item for item in rooms if item["page"] == candidate.page and item["region"] == candidate.region]
    if not matches:
        return None
    anchor = candidate.bottom or candidate.top
    return min(matches, key=lambda item: (abs(item["top"] - anchor), item["top"]))["room"]


def unique_program_lines(lines: Iterable[str]) -> list[str]:
    result, seen = [], set()
    for line in lines:
        line = normalize_spacing(line)
        if looks_like_program_line(line) and line not in seen:
            seen.add(line)
            result.append(line)
    return result


def parse_cell(cell_value: str, reference_programs: Iterable[str], *, resolve_truncated: bool, keep_truncated: bool) -> list[ParsedSession]:
    lines = [
        line.strip() for line in str(cell_value or "").split("\n")
        if line.strip() and line.strip() != "" and "delete" not in line.lower()
    ]
    sessions, current = [], []
    for line in lines:
        current.append(line)
        if TIME_RE.search(line):
            sessions.append(current)
            current = []
    return [parse_session(item, reference_programs, resolve_truncated=resolve_truncated, keep_truncated=keep_truncated) for item in sessions]


def parse_session(raw_lines: list[str], reference_programs: Iterable[str], *, resolve_truncated: bool, keep_truncated: bool) -> ParsedSession:
    lines = [line.strip() for line in raw_lines if line.strip()]
    teacher_index = next((i for i in range(len(lines) - 1, -1, -1) if TIME_RE.search(lines[i])), None)
    teacher_line = lines.pop(teacher_index) if teacher_index is not None else None
    practical = any("practical" in line.lower() for line in lines)
    lines = [line for line in lines if line.lower() != "practical"]
    combined = lines.pop(0) if lines and is_combined_header(lines[0]) else None
    program_index = next((i for i, line in enumerate(lines) if looks_like_program_line(line)), None)
    program_line = lines.pop(program_index) if program_index is not None else None
    course_line = next((line for line in lines if "#" in line), lines[0] if lines else None)
    course_title, course_code = split_course_line(course_line)
    raw_program_line = program_line
    program_truncated = has_ellipsis(raw_program_line)
    if resolve_truncated and raw_program_line and program_truncated and not keep_truncated:
        program_line = resolve_program_line(raw_program_line, reference_programs)
    degree, program, section, session, semester = parse_program_fields(program_line)
    program_code = get_program_code(degree, program)
    if program_code and program:
        program = PROGRAM_NAMES.get(normalize_spacing(program).lower(), program)
    match = TIME_RE.search(teacher_line or "")
    teacher_name = (teacher_line or "").split("(")[0].strip() or None
    return ParsedSession(combined, course_title, course_code, has_ellipsis(course_title), program_line,
        program_truncated, degree, program, program_code, section, session, semester, teacher_name,
        has_ellipsis(teacher_name), match.group(1) if match else None, match.group(2) if match else None,
        practical, raw_lines)


def split_course_line(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if "#" not in value:
        return value.strip(), None
    title, code = value.rsplit("#", 1)
    return title.strip() or None, code.strip() or None


def has_ellipsis(value: str | None) -> bool:
    return bool(value and ("…" in value or value.endswith("...")))


def resolve_program_line(program_line: str, references: Iterable[str]) -> str:
    prefix = program_line.replace("…", "").strip()
    session = SESSION_RE.search(program_line)
    semester = SEMESTER_RE.search(program_line)
    for candidate in references:
        if prefix and candidate.startswith(prefix):
            return candidate
        if session and session.group(0) in candidate and semester and semester.group(1) in candidate:
            return candidate
    return program_line


def parse_program_fields(value: str | None) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    if not value:
        return None, None, None, None, None
    cleaned = normalize_spacing(value.replace("(", " ( ").replace(")", " ) "))
    degree_match = DEGREE_RE.match(cleaned)
    if not degree_match:
        return None, None, None, None, None
    degree = degree_match.group(1)
    remainder = cleaned[degree_match.end():]
    section_match = SECTION_RE.search(remainder)
    program = remainder[:section_match.start()].strip() if section_match else None
    section = normalize_spacing(section_match.group(0)) if section_match else None
    session_match, semester_match = SESSION_RE.search(cleaned), SEMESTER_RE.search(cleaned)
    return degree, program or None, section, session_match.group(0).replace(" ", "") if session_match else None, int(semester_match.group(1)) if semester_match else None


def looks_like_program_line(value: str) -> bool:
    return bool(DEGREE_RE.match(value.strip()) and SESSION_RE.search(value))


def get_program_code(degree: str | None, program: str | None) -> str | None:
    return PROGRAM_CODES.get(normalize_spacing(program).lower()) if degree and program else None


def is_combined_header(value: str) -> bool:
    return value.lower().startswith("combined class") or bool(re.match(r"^combined\s*\(\d+\)", value.lower()))


def normalize_day(value: str) -> str | None:
    return next((day for day in DAY_NAMES if value.strip().lower().startswith(day.lower())), None)


def normalize_spacing(value: str) -> str:
    return " ".join(value.split())


def infer_missing_semesters(timetable: dict) -> None:
    known: dict[tuple[str, str, str, str], set[int]] = defaultdict(set)
    for rooms in timetable.values():
        for sessions in rooms.values():
            for session in sessions:
                key = semester_key(session)
                if key and session["semester"] is not None:
                    known[key].add(session["semester"])
    for rooms in timetable.values():
        for sessions in rooms.values():
            for session in sessions:
                key = semester_key(session)
                if key and session["semester"] is None and len(known[key]) == 1:
                    session["semester"] = next(iter(known[key]))


def semester_key(session: dict) -> tuple[str, str, str, str] | None:
    values = (session.get("degree"), session.get("program"), session.get("section"), session.get("session"))
    return tuple(normalize_spacing(str(value)) for value in values) if all(values) else None
