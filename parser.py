from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import camelot
import pdfplumber
from datetime import datetime

DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

TIME_RE = re.compile(r"\((\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\)")
SESSION_RE = re.compile(r"\b(\d{4})\s*-\s*(\d{4})\b")
SEMESTER_RE = re.compile(r"Semester#\s*(\d+)")


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
        return {
            "combined_class": self.combined_class,
            "course_title": self.course_title,
            "course_code": self.course_code,
            "course_truncated": self.course_truncated,
            "program_line": self.program_line,
            "program_truncated": self.program_truncated,
            "degree": self.degree,
            "program": self.program,
            "section": self.section,
            "session": self.session,
            "semester": self.semester,
            "teacher_name": self.teacher_name,
            "teacher_truncated": self.teacher_truncated,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "practical": self.practical,
            "raw_lines": self.raw_lines,
        }


def parse_timetable(
    pdf_path: str,
    *,
    resolve_truncated: bool = True,
    keep_truncated: bool = False,
) -> dict:
    reference_programs = extract_program_lines(pdf_path) if resolve_truncated else []
    tables = extract_tables(pdf_path)
    timetable: dict[str, dict[str, list[dict]]] = {}

    for table in tables:
        df = table.df
        if df.empty:
            continue
        header_row = find_header_row(df)
        header = [str(value).strip() for value in df.iloc[header_row].tolist()]
        day_columns: dict[int, str] = {}
        last_day: str | None = None
        for idx, value in enumerate(header):
            day = normalize_day(value)
            if day:
                last_day = day
                day_columns[idx] = day
                continue
            if idx > 0 and last_day and not value:
                day_columns[idx] = last_day

        last_room: str | None = None
        for row_index in range(header_row + 1, len(df)):
            row = [str(value) for value in df.iloc[row_index].tolist()]
            if not row:
                continue
            room = normalize_spacing(row[0].replace("\n", " ")).strip()
            if room.lower() == "nan":
                room = ""
            if not room:
                has_content = any(
                    str(cell).strip() and str(cell).strip().lower() != "nan"
                    for cell in row[1:]
                )
                if has_content and last_room:
                    room = last_room
                else:
                    continue
            else:
                last_room = room

            day_values: dict[str, list[str]] = {}
            for col_idx, day in day_columns.items():
                if col_idx >= len(row):
                    continue
                cell_value = row[col_idx]
                if cell_value.lower() == "nan":
                    continue
                cell_value = cell_value.strip()
                if not cell_value:
                    continue
                day_values.setdefault(day, []).append(cell_value)

            for day, parts in day_values.items():
                cell_value = "\n".join(parts)
                sessions = parse_cell(
                    cell_value,
                    reference_programs,
                    resolve_truncated=resolve_truncated,
                    keep_truncated=keep_truncated,
                )
                if not sessions:
                    continue
                timetable.setdefault(day, {}).setdefault(room, []).extend(
                    session.to_dict() for session in sessions
                )

    infer_missing_semesters(timetable)

    # attach a timestamp at top-level so consumers can know when this data was generated
    # use ISO format for easy parsing and ordering
    return {"timestamp": datetime.now().isoformat(), **timetable}


def extract_tables(pdf_path: str) -> list:
    try:
        tables = list(camelot.read_pdf(pdf_path, pages="1-end", flavor="lattice"))
    except Exception:
        tables = []

    if tables and any(not table.df.empty for table in tables):
        return tables

    try:
        return list(camelot.read_pdf(pdf_path, pages="1-end", flavor="stream"))
    except Exception:
        return []


def find_header_row(df) -> int:
    best_row = 0
    best_score = 0
    max_rows = min(len(df), 10)
    for row_index in range(max_rows):
        row = [str(value).strip() for value in df.iloc[row_index].tolist()]
        score = sum(1 for value in row if normalize_day(value))
        if score > best_score:
            best_score = score
            best_row = row_index
    return best_row


def normalize_day(value: str) -> str | None:
    text = value.strip().lower()
    for day in DAY_NAMES:
        if text.startswith(day.lower()):
            return day
    return None


def extract_program_lines(pdf_path: str) -> list[str]:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                return []
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return []

    candidates: list[str] = []
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if SESSION_RE.search(line) and SEMESTER_RE.search(line):
            candidates.append(line)
    return candidates


def parse_cell(
    cell_value: str,
    reference_programs: Iterable[str],
    *,
    resolve_truncated: bool,
    keep_truncated: bool,
) -> list[ParsedSession]:
    text = str(cell_value or "").strip()
    if not text:
        return []

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
        and line.strip().lower() != " swap"
        and not line.strip().lower().startswith("was:")
        # ignore stray delete boxes which can appear in some PDFs
        and "delete" not in line.strip().lower()
    ]
    if not lines:
        return []

    sessions: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if TIME_RE.search(line):
            sessions.append(current)
            current = []

    if current:
        sessions.append(current)

    return [
        parse_session(
            session_lines,
            reference_programs,
            resolve_truncated=resolve_truncated,
            keep_truncated=keep_truncated,
        )
        for session_lines in sessions
    ]


def parse_session(
    raw_lines: list[str],
    reference_programs: Iterable[str],
    *,
    resolve_truncated: bool,
    keep_truncated: bool,
) -> ParsedSession:
    raw_lines_clean = [line.strip() for line in raw_lines if line.strip()]

    content_lines = [line for line in raw_lines_clean]
    teacher_line = None
    teacher_index = None
    for idx, line in enumerate(content_lines):
        if TIME_RE.search(line):
            teacher_line = line
            teacher_index = idx

    if teacher_index is not None:
        content_lines = [line for idx, line in enumerate(content_lines) if idx != teacher_index]

    practical = any(line.strip().lower() == "practical" for line in content_lines)
    content_lines = [
        line for line in content_lines if line.strip().lower() != "practical"
    ]

    meaningful_lines = [line for line in content_lines if line != ""]

    combined_class = None
    course_line = None
    program_line = None

    if meaningful_lines:
        if is_combined_header(meaningful_lines[0]):
            combined_class = meaningful_lines[0]
            course_line = meaningful_lines[1] if len(meaningful_lines) > 1 else None
            program_line = meaningful_lines[2] if len(meaningful_lines) > 2 else None
        else:
            course_line = meaningful_lines[0]
            program_line = meaningful_lines[1] if len(meaningful_lines) > 1 else None

    course_title, course_code = split_course_line(course_line)
    course_truncated = has_ellipsis(course_title)

    raw_program_line = program_line
    program_truncated = has_ellipsis(raw_program_line)
    if resolve_truncated and program_truncated and raw_program_line:
        resolved = resolve_program_line(raw_program_line, reference_programs)
        if not keep_truncated:
            program_line = resolved

    degree, program, section, session, semester = parse_program_fields(program_line)

    teacher_name = None
    start_time = None
    end_time = None
    if teacher_line:
        time_match = TIME_RE.search(teacher_line)
        if time_match:
            start_time, end_time = time_match.groups()
        teacher_name = teacher_line.split("(")[0].strip() or None

    teacher_truncated = has_ellipsis(teacher_name)

    return ParsedSession(
        combined_class=combined_class,
        course_title=course_title,
        course_code=course_code,
        course_truncated=course_truncated,
        program_line=program_line,
        program_truncated=program_truncated,
        degree=degree,
        program=program,
        section=section,
        session=session,
        semester=semester,
        teacher_name=teacher_name,
        teacher_truncated=teacher_truncated,
        start_time=start_time,
        end_time=end_time,
        practical=practical,
        raw_lines=raw_lines_clean,
    )


def split_course_line(course_line: str | None) -> tuple[str | None, str | None]:
    if not course_line:
        return None, None
    if "#" not in course_line:
        return course_line.strip(), None
    left, right = course_line.rsplit("#", 1)
    return left.strip() or None, right.strip() or None


def has_ellipsis(text: str | None) -> bool:
    if not text:
        return False
    return "…" in text or text.endswith("...")


def resolve_program_line(program_line: str, reference_programs: Iterable[str]) -> str:
    prefix = program_line.replace("…", "").strip()
    session_match = SESSION_RE.search(program_line)
    semester_match = SEMESTER_RE.search(program_line)
    session_value = session_match.group(0) if session_match else None
    semester_value = semester_match.group(0).replace(" ", "") if semester_match else None

    for candidate in reference_programs:
        if session_value and session_value not in candidate:
            continue
        candidate_compact = candidate.replace(" ", "")
        if semester_value and semester_value not in candidate_compact:
            continue
        if prefix and candidate.startswith(prefix):
            return candidate

    for candidate in reference_programs:
        if session_value and session_value in candidate:
            if not semester_value or semester_value in candidate.replace(" ", ""):
                return candidate

    return program_line


def parse_program_fields(program_line: str | None) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
]:
    if not program_line:
        return None, None, None, None, None

    cleaned = " ".join(program_line.replace("(", " ( ").replace(")", " ) ").split())

    degree_match = re.match(r"^(BS|MS|PhD)\s+(?:in\s+)?", cleaned, re.IGNORECASE)
    if not degree_match:
        return None, None, None, None, None

    degree = degree_match.group(1)
    remainder = cleaned[degree_match.end():]
    section_match = re.search(r"(Regular|Self Support)\s*\d+", remainder, re.IGNORECASE)
    program = None
    section = None
    if section_match:
        program = remainder[: section_match.start()].strip() or None
        section = normalize_spacing(section_match.group(0))

    session_match = SESSION_RE.search(cleaned)
    session = session_match.group(0).replace(" ", "") if session_match else None

    semester_match = SEMESTER_RE.search(cleaned)
    semester = int(semester_match.group(1)) if semester_match else None

    return degree, program, section, session, semester


def normalize_spacing(value: str) -> str:
    return " ".join(value.split())


def is_combined_header(value: str) -> bool:
    text = value.strip().lower()
    if text.startswith("combined class"):
        return True
    return bool(re.match(r"^combined\s*\(\d+\)", text))


def infer_missing_semesters(timetable: dict) -> None:
    semester_map: dict[tuple[str, str, str, str], int] = {}
    conflicts: set[tuple[str, str, str, str]] = set()

    for day_rooms in timetable.values():
        for sessions in day_rooms.values():
            for session in sessions:
                key = build_semester_key(session)
                semester = session.get("semester")
                if not key or semester is None:
                    continue
                if key in semester_map and semester_map[key] != semester:
                    conflicts.add(key)
                else:
                    semester_map[key] = semester

    for day_rooms in timetable.values():
        for sessions in day_rooms.values():
            for session in sessions:
                if session.get("semester") is not None:
                    continue
                key = build_semester_key(session)
                if not key or key in conflicts:
                    continue
                inferred = semester_map.get(key)
                if inferred is not None:
                    session["semester"] = inferred


def build_semester_key(session: dict) -> tuple[str, str, str, str] | None:
    degree = session.get("degree")
    program = session.get("program")
    section = session.get("section")
    years = session.get("session")
    if not degree or not program or not section or not years:
        return None
    return (
        normalize_spacing(str(degree)),
        normalize_spacing(str(program)),
        normalize_spacing(str(section)),
        str(years).replace(" ", ""),
    )
