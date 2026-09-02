from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Iterable

import camelot
import pandas as pd
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
DEGREE_RE = re.compile(r"^(BS|MS|PhD)\s+(?:in\s+)?", re.IGNORECASE)
SECTION_RE = re.compile(r"(Regular|Self Support)\s*\d+", re.IGNORECASE)

PROGRAM_CODES = {
    "computer science - specialization in artificial intelligence": "AI",
}
PROGRAM_NAMES = {
    "computer science - specialization in artificial intelligence": "Artificial Intelligence",
}


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
        return {
            "combined_class": self.combined_class,
            "course_title": self.course_title,
            "course_code": self.course_code,
            "course_truncated": self.course_truncated,
            "program_line": self.program_line,
            "program_truncated": self.program_truncated,
            "degree": self.degree,
            "program": self.program,
            "program_code": self.program_code,
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
    tables = extract_tables(pdf_path)
    # The updated PDF puts the program picker on page 1 and wraps its entries
    # across several visual lines.  The timetable cells themselves contain
    # complete program lines, so use them as the authoritative references for
    # resolving ellipsized values.  Keep page 1 as a supplemental source for
    # PDFs where a program occurs only in the picker.
    reference_programs = (
        collect_program_references(pdf_path, tables) if resolve_truncated else []
    )
    timetable: dict[str, dict[str, list[dict]]] = {}

    last_room: str | None = None
    pending_room_prefix: str | None = None
    pending_stream_blocks: list[tuple[str, str]] = []
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

        stream_parts: dict[str, list[str]] = {}
        is_stream_table = getattr(table, "_parser_flavor", None) == "stream"
        for row_index in range(header_row + 1, len(df)):
            row = [str(value) for value in df.iloc[row_index].tolist()]
            if not row:
                continue
            room = normalize_spacing(row[0].replace("\n", " ")).strip()
            if room.lower() in {"nan", ""}:
                room = ""
            room_label_completed = bool(room)
            if pending_room_prefix and room:
                if is_room_prefix(room):
                    pending_room_prefix = room
                    room = last_room or ""
                    room_label_completed = False
                else:
                    room = normalize_spacing(f"{pending_room_prefix} {room}")
                    pending_room_prefix = (
                        room if is_room_prefix(room) else None
                    )
                    room_label_completed = pending_room_prefix is None

            # Stream extraction may place a department prefix in one row and
            # its room number in a later row. Keep using the previous room
            # until the complete label is available.
            if is_room_prefix(room):
                pending_room_prefix = room
                room = last_room or ""
                room_label_completed = False
            if not room:
                has_content = any(
                    str(cell).strip() and str(cell).strip().lower() != "nan"
                    for cell in row[1:]
                )
                if has_content and last_room:
                    room = last_room
                elif is_stream_table and has_content:
                    # Some stream tables place the room label after the
                    # first block of classes. Keep those cells until the
                    # label arrives instead of dropping the first room.
                    room = ""
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

            if is_stream_table:
                for day, parts in day_values.items():
                    stream_parts.setdefault(day, []).extend(parts)
                if any("" in value for value in row):
                    for day, parts in stream_parts.items():
                        pending_stream_blocks.append((day, "\n".join(parts)))
                    stream_parts.clear()
                if room_label_completed and pending_stream_blocks:
                    for day, cell_value in pending_stream_blocks:
                        add_parsed_sessions(
                            timetable, day, room, cell_value,
                            reference_programs, resolve_truncated, keep_truncated,
                        )
                    pending_stream_blocks.clear()
                continue

            for day, parts in day_values.items():
                add_parsed_sessions(
                    timetable, day, room, "\n".join(parts),
                    reference_programs, resolve_truncated, keep_truncated,
                )

        for day, parts in stream_parts.items():
            pending_stream_blocks.append((day, "\n".join(parts)))

    for day, cell_value in pending_stream_blocks:
        add_parsed_sessions(
            timetable, day, last_room, cell_value,
            reference_programs, resolve_truncated, keep_truncated,
        )

    infer_missing_semesters(timetable)

    # attach a timestamp at top-level so consumers can know when this data was generated
    # use ISO format for easy parsing and ordering
    return {"timestamp": datetime.now().isoformat(), **timetable}


def add_parsed_sessions(
    timetable: dict,
    day: str,
    room: str | None,
    cell_value: str,
    reference_programs: Iterable[str],
    resolve_truncated: bool,
    keep_truncated: bool,
) -> None:
    if not room:
        return
    sessions = parse_cell(
        cell_value,
        reference_programs,
        resolve_truncated=resolve_truncated,
        keep_truncated=keep_truncated,
    )
    if sessions:
        timetable.setdefault(day, {}).setdefault(room, []).extend(
            session.to_dict() for session in sessions
        )


def extract_tables(pdf_path: str) -> list:
    """Extract the best table set for each page.

    Lattice preserves merged room cells well, but may miss entire pages when
    their borders differ. Stream handles those pages, although it can split
    merged room labels. Selecting per page keeps both advantages.
    """
    extracted: dict[str, list] = {}
    for flavor in ("lattice", "stream"):
        try:
            tables = list(camelot.read_pdf(pdf_path, pages="1-end", flavor=flavor))
        except Exception:
            tables = []
        if tables:
            for table in tables:
                table._parser_flavor = flavor
            extracted[flavor] = tables

    if not extracted:
        return []

    tables_by_page: dict[int, list] = {}
    for flavor in ("lattice", "stream"):
        for table in extracted.get(flavor, []):
            if find_header_score(table.df) >= 2:
                tables_by_page.setdefault(table.page, []).append((flavor, table))

    lattice_pages = {
        page for page, page_tables in tables_by_page.items()
        if any(flavor == "lattice" for flavor, _ in page_tables)
    }
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                if page_number in lattice_pages:
                    continue
                for table_data in page.extract_tables():
                    table = SimpleNamespace(
                        df=pd.DataFrame(table_data),
                        page=page_number,
                        _parser_flavor="pdfplumber",
                    )
                    if find_header_score(table.df) >= 2:
                        tables_by_page.setdefault(page_number, []).append(
                            ("pdfplumber", table)
                        )
    except Exception:
        pass

    selected = []
    for page in sorted(tables_by_page):
        page_tables = tables_by_page[page]
        lattice_tables = [table for flavor, table in page_tables if flavor == "lattice"]
        stream_tables = [table for flavor, table in page_tables if flavor == "stream"]
        pdfplumber_tables = [
            table for flavor, table in page_tables if flavor == "pdfplumber"
        ]
        # Stream can recover a timetable that shares a page with non-table
        # metadata (notably the first room on the updated PDF).  In that case
        # the day-header row is below the first row; otherwise pdfplumber is
        # generally more reliable for the regular timetable pages.
        stream_with_metadata = [
            table for table in stream_tables if find_header_row(table.df) > 0
        ]
        selected.extend(
            lattice_tables
            or stream_with_metadata
            or pdfplumber_tables
            or [table for _, table in page_tables]
        )
    return selected


def find_header_score(df) -> int:
    header_row = find_header_row(df)
    return sum(
        1
        for value in df.iloc[header_row].tolist()
        if normalize_day(str(value).strip())
    )


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


def collect_program_references(pdf_path: str, tables: Iterable) -> list[str]:
    """Collect full program lines from page metadata and timetable cells."""
    references: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = normalize_spacing(value)
        if value and looks_like_program_line(value) and value not in seen:
            seen.add(value)
            references.append(value)

    for value in extract_program_lines(pdf_path):
        add(value)

    for table in tables:
        for value in table.df.astype(str).to_numpy().flat:
            for line in str(value).splitlines():
                add(line.strip())

    return references


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
        and line.strip() != ""
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

    if current and any(TIME_RE.search(line) for line in current):
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

    if meaningful_lines and is_combined_header(meaningful_lines[0]):
        combined_class = meaningful_lines.pop(0)

    # PDF extraction can return the course and program lines in either order.
    program_index = next(
        (idx for idx, line in enumerate(meaningful_lines) if looks_like_program_line(line)),
        None,
    )
    if program_index is not None:
        program_line = meaningful_lines.pop(program_index)
    course_line = next((line for line in meaningful_lines if "#" in line), None)
    if course_line is None and meaningful_lines:
        course_line = meaningful_lines[0]

    course_title, course_code = split_course_line(course_line)
    course_truncated = has_ellipsis(course_title)

    raw_program_line = program_line
    program_truncated = has_ellipsis(raw_program_line)
    if resolve_truncated and program_truncated and raw_program_line:
        resolved = resolve_program_line(raw_program_line, reference_programs)
        if not keep_truncated:
            program_line = resolved

    degree, program, section, session, semester = parse_program_fields(program_line)
    program_code = get_program_code(degree, program)
    if program_code and program:
        program = PROGRAM_NAMES.get(normalize_spacing(program).lower(), program)

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
        program_code=program_code,
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
        if not looks_like_program_line(candidate):
            continue
        if session_value and session_value not in candidate:
            continue
        candidate_compact = candidate.replace(" ", "")
        if semester_value and semester_value not in candidate_compact:
            continue
        if prefix and candidate.startswith(prefix):
            return candidate

    for candidate in reference_programs:
        if not looks_like_program_line(candidate):
            continue
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

    degree_match = DEGREE_RE.match(cleaned)
    if not degree_match:
        return None, None, None, None, None

    degree = degree_match.group(1)
    remainder = cleaned[degree_match.end():]
    section_match = SECTION_RE.search(remainder)
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


def looks_like_program_line(value: str) -> bool:
    """Return whether a PDF line contains the identifying program metadata."""
    return bool(DEGREE_RE.match(value.strip()) and SESSION_RE.search(value))


def get_program_code(degree: str | None, program: str | None) -> str | None:
    if not degree or not program:
        return None
    normalized = normalize_spacing(program).lower()
    suffix = PROGRAM_CODES.get(normalized)
    return suffix


def normalize_spacing(value: str) -> str:
    return " ".join(value.split())


def is_room_prefix(value: str) -> bool:
    text = value.lower()
    if "smart lab" in text or "committee room" in text:
        return False
    return (
        text.startswith("department of ")
        and (
        text.endswith("(")
        or text.endswith("(computer")
        or text.endswith("committee")
        or not re.search(
        r"\b(?:cr|l)[-\s]*\w|\b(?:room|lab)\s+\w", text
        )
        )
    ) or text.endswith("committee") or text.endswith("cr-")


def is_combined_header(value: str) -> bool:
    text = value.strip().lower()
    if text.startswith("combined class"):
        return True
    return bool(re.match(r"^combined\s*\(\d+\)", text))


def infer_missing_semesters(timetable: dict) -> None:
    semester_map: dict[tuple[str, str, str, str], int] = {}
    conflicts: set[tuple[str, str, str, str]] = set()
    fallback_map: dict[tuple[str, str, str], set[int]] = {}

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
                fallback_key = (key[0], key[2], key[3])
                fallback_map.setdefault(fallback_key, set()).add(semester)

    for day_rooms in timetable.values():
        for sessions in day_rooms.values():
            for session in sessions:
                if session.get("semester") is not None:
                    continue
                key = build_semester_key(session)
                if not key:
                    continue
                inferred = None if key in conflicts else semester_map.get(key)
                if inferred is None:
                    semesters = fallback_map.get((key[0], key[2], key[3]), set())
                    inferred = next(iter(semesters)) if len(semesters) == 1 else None
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
