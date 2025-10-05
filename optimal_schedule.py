#!/usr/bin/env python3
"""Constraint-driven schedule generator.

Steps:
1. Parse HTML registrar output into course sections grouped by inferred field/level.
2. Build all non-conflicting section combinations that satisfy config selections and constraints (time windows, excluded instructors, etc.).
3. Score schedules by minimizing total idle minutes between classes and tie-break using daily span, daily hours standard deviation, and average end time.
4. Render the top schedules as ASCII calendars with 24-hour times.
"""

from __future__ import annotations

import json
import math
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import zip_longest
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Set
import statistics

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError as exc:  # pragma: no cover - handled at runtime
    raise SystemExit(
        "BeautifulSoup4 is required. Install with 'pip install beautifulsoup4'."
    ) from exc


BASE_DIR = Path(__file__).resolve().parent
COURSE_DIR = BASE_DIR / "course_schedule"
PARSED_DIR = BASE_DIR / "parsed_courses"
CONFIG_PATH = BASE_DIR / "config.json"

DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri"]
TIME_SLOT_MINUTES = 30


# ---------------------------------------------------------------------------
# Data models


@dataclass
class Meeting:
    days: List[str]
    start: int  # minutes from midnight
    end: int  # minutes from midnight
    raw_days: str
    raw_time: str


@dataclass
class Section:
    unique_number: str
    instruction_mode: str
    instructor: str
    status: str
    meetings: List[Meeting] = field(default_factory=list)


@dataclass
class Course:
    field: str
    level: str
    course_code: str
    course_title: str
    sections: List[Section] = field(default_factory=list)


@dataclass
class SectionSelection:
    course_code: str
    course_title: str
    section: Section


@dataclass
class Schedule:
    selections: List[SectionSelection]
    metrics: Dict[str, float]


# ---------------------------------------------------------------------------
# Utility helpers


def normalise_whitespace(text: str) -> str:
    return " ".join(text.split())


def parse_day_tokens(day_str: str) -> List[str]:
    day_str = (day_str or "").strip()
    if not day_str:
        return []
    upper = day_str.upper()
    if upper in {"TBA", "ARR", "ARRANGED"}:
        return []

    days: List[str] = []
    i = 0
    while i < len(upper):
        pair = upper[i : i + 2]
        if pair == "TH":
            days.append("Thu")
            i += 2
            continue
        if pair == "SU":
            days.append("Sun")
            i += 2
            continue
        if pair == "SA":
            days.append("Sat")
            i += 2
            continue

        ch = upper[i]
        mapping = {
            "M": "Mon",
            "T": "Tue",
            "W": "Wed",
            "F": "Fri",
            "S": "Sat",
            "U": "Sun",
            "R": "Thu",
        }
        day = mapping.get(ch)
        if day:
            days.append(day)
        i += 1

    return days


def parse_single_time(text: str) -> Optional[int]:
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip().lower()
    cleaned = cleaned.replace("a.m.", "am").replace("p.m.", "pm")
    cleaned = cleaned.replace("a.m", "am").replace("p.m", "pm")
    cleaned = cleaned.replace(" noon", " 12:00pm")
    cleaned = cleaned.replace("midnight", "12:00am")
    cleaned = cleaned.replace("noon", "12:00pm")
    cleaned = cleaned.replace("--", "-")

    if any(token in cleaned for token in ("tba", "online", "web", "internet")):
        return None

    import re

    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", cleaned)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)

    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    else:
        hour %= 24

    return hour * 60 + minute


def parse_time_range(text: str) -> Optional[Tuple[int, int]]:
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    if not cleaned or cleaned.upper() in {"TBA", "ARR", "ARRANGED"}:
        return None
    if "-" not in cleaned:
        return None

    start_str, end_str = cleaned.split("-", 1)
    start = parse_single_time(start_str)
    end = parse_single_time(end_str)
    if start is None or end is None:
        return None

    if end <= start:
        # Assume wrapped times cross noon/midnight; add 12 hours until end > start.
        while end <= start:
            end += 12 * 60

    return start, end


def minutes_to_time_str(minutes: int) -> str:
    minutes = int(minutes)
    hour = (minutes // 60) % 24
    minute = minutes % 60
    return f"{hour:02d}:{minute:02d}"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def level_code_to_name(code: str) -> str:
    mapping = {"U": "Upper", "L": "Lower", "G": "Grad"}
    return mapping.get(code.upper(), code)


def sanitise_filename(field: str, level: str) -> str:
    safe = f"{field}_{level}".replace(" ", "_")
    safe = safe.replace("/", "-").replace("\\", "-")
    return safe


# ---------------------------------------------------------------------------
# Parsing pipeline


def parse_course_files(directory: Path) -> Tuple[List[Course], Dict[str, Course], Dict[str, Tuple[Course, Section]]]:
    courses_map: Dict[Tuple[str, str, str], Course] = {}
    by_unique: Dict[str, Tuple[Course, Section]] = {}

    for html_path in sorted(directory.glob("*.html")):
        with html_path.open("r", encoding="utf-8", errors="ignore") as handle:
            soup = BeautifulSoup(handle, "html.parser")

        for parsed_course in extract_courses_from_soup(soup):
            key = (parsed_course.field, parsed_course.level, parsed_course.course_code)
            course = courses_map.setdefault(
                key,
                Course(
                    field=parsed_course.field,
                    level=parsed_course.level,
                    course_code=parsed_course.course_code,
                    course_title=parsed_course.course_title,
                ),
            )

            existing_unique = {section.unique_number for section in course.sections}
            for section in parsed_course.sections:
                if section.unique_number in existing_unique:
                    continue
                course.sections.append(section)
                by_unique[section.unique_number] = (course, section)

    courses = list(courses_map.values())
    by_code = {course.course_code: course for course in courses}
    return courses, by_code, by_unique


def extract_courses_from_soup(soup: BeautifulSoup) -> Iterable[Course]:
    current_course: Optional[Course] = None

    for tr in soup.find_all("tr"):
        header_cell = tr.find("td", class_="course_header")
        if header_cell:
            if current_course:
                yield current_course
            header_text = normalise_whitespace(header_cell.get_text(" ", strip=True))
            if not header_text:
                current_course = None
                continue

            tokens = header_text.split()
            course_number_index = next((i for i, token in enumerate(tokens) if any(ch.isdigit() for ch in token)), None)
            if course_number_index is None:
                current_course = None
                continue

            field_tokens = tokens[:course_number_index]
            course_number = tokens[course_number_index]
            title_tokens = tokens[course_number_index + 1 :]

            inferred_field = " ".join(field_tokens).strip()
            course_field = inferred_field

            try:
                numeric_part = int(''.join(filter(str.isdigit, course_number))[-2:])
            except (ValueError, IndexError):
                numeric_part = 0
            if 1 <= numeric_part <= 19:
                course_level = "Lower"
            elif 20 <= numeric_part <= 79:
                course_level = "Upper"
            elif 80 <= numeric_part <= 99:
                course_level = "Grad"
            else:
                course_level = "UNKNOWN"

            course_code = f"{' '.join(field_tokens)} {course_number}".strip()
            course_title = " ".join(title_tokens).strip()

            current_course = Course(
                field=course_field,
                level=course_level,
                course_code=course_code,
                course_title=course_title,
            )
            continue

        if not current_course:
            continue

        section = parse_section_row(tr)
        if section:
            current_course.sections.append(section)

    if current_course:
        yield current_course


def parse_section_row(row) -> Optional[Section]:
    unique_cell = row.find("td", {"data-th": "Unique"})
    if not unique_cell:
        return None

    unique_anchor = unique_cell.find("a")
    unique_number = (unique_anchor.get_text(strip=True) if unique_anchor else unique_cell.get_text(strip=True)).strip()
    if not unique_number:
        return None

    instruction_cell = row.find("td", {"data-th": "Instruction Mode"})
    instructor_cell = row.find("td", {"data-th": "Instructor"})
    status_cell = row.find("td", {"data-th": "Status"})
    days_cell = row.find("td", {"data-th": "Days"})
    hour_cell = row.find("td", {"data-th": "Hour"})

    instruction_mode = instruction_cell.get_text(" ", strip=True) if instruction_cell else ""
    instructor = instructor_cell.get_text(" ", strip=True) if instructor_cell else ""
    status = status_cell.get_text(" ", strip=True) if status_cell else ""

    meeting_pairs: List[Tuple[str, str]] = []
    day_spans = [span.get_text(strip=True) for span in days_cell.find_all("span")] if days_cell else []
    hour_spans = [span.get_text(strip=True) for span in hour_cell.find_all("span")] if hour_cell else []

    if not day_spans:
        raw_days = days_cell.get_text(" ", strip=True) if days_cell else ""
        if raw_days:
            day_spans = [raw_days]
    if not hour_spans:
        raw_hours = hour_cell.get_text(" ", strip=True) if hour_cell else ""
        if raw_hours:
            hour_spans = [raw_hours]

    for day_text, hour_text in zip_longest(day_spans, hour_spans, fillvalue=""):
        meeting_pairs.append((day_text or "", hour_text or ""))

    meetings: List[Meeting] = []
    for raw_days, raw_time in meeting_pairs:
        days = parse_day_tokens(raw_days)
        time_range = parse_time_range(raw_time)
        if not days or not time_range:
            continue
        start, end = time_range
        meetings.append(
            Meeting(
                days=days,
                start=start,
                end=end,
                raw_days=raw_days,
                raw_time=raw_time,
            )
        )

    return Section(
        unique_number=unique_number,
        instruction_mode=instruction_mode,
        instructor=instructor,
        status=status,
        meetings=meetings,
    )


# ---------------------------------------------------------------------------
# Persist parsed data


def course_to_dict(course: Course) -> Dict[str, object]:
    return {
        "field": course.field,
        "level": course.level,
        "course_code": course.course_code,
        "course_title": course.course_title,
        "sections": [
            {
                "unique_number": section.unique_number,
                "instruction_mode": section.instruction_mode,
                "instructor": section.instructor,
                "status": section.status,
                "meetings": [
                    {
                        "days": meeting.days,
                        "start_minutes": meeting.start,
                        "end_minutes": meeting.end,
                        "start_time": minutes_to_time_str(meeting.start),
                        "end_time": minutes_to_time_str(meeting.end),
                        "raw_days": meeting.raw_days,
                        "raw_time": meeting.raw_time,
                    }
                    for meeting in section.meetings
                ],
            }
            for section in course.sections
        ],
    }


def write_grouped_json(courses: List[Course]) -> None:
    ensure_directory(PARSED_DIR)
    grouped: Dict[Tuple[str, str], List[Course]] = {}
    for course in courses:
        key = (course.field, course.level)
        grouped.setdefault(key, []).append(course)

    for (field, level), course_list in grouped.items():
        data = {
            "field": field,
            "level": level,
            "courses": [course_to_dict(course) for course in course_list],
        }
        filename = sanitise_filename(field, level) + ".json"
        output_path = PARSED_DIR / filename
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=True)


# ---------------------------------------------------------------------------
# Configuration


def load_config(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}. Please create config.json based on README guidance.")

    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    if not isinstance(config, dict):
        raise SystemExit("Config root must be a JSON object.")

    filters = config.get("constraints")
    if not isinstance(filters, dict):
        raise SystemExit("Config missing 'constraints' object.")

    selections = config.get("selections")
    if not isinstance(selections, dict):
        raise SystemExit("Config missing 'selections' object.")

    if "top_k" not in filters:
        raise SystemExit("Config constraints.top_k is required.")

    if "earliest_start_time" not in filters or "latest_end_time" not in filters:
        raise SystemExit("Config constraints.earliest_start_time and constraints.latest_end_time are required.")

    if "course" not in selections:
        raise SystemExit("Config selections.course is required.")

    top_k = filters.get("top_k")
    if not isinstance(top_k, int) or top_k <= 0:
        raise SystemExit("Config constraints.top_k must be a positive integer.")

    selections_config = dict(selections)
    course_entries = selections_config.get("course")
    if isinstance(course_entries, str):
        selections_config["course"] = [course_entries]
    elif not isinstance(course_entries, list):
        raise SystemExit("Config selections.course must be a list or string.")

    for time_key in ("earliest_start_time", "latest_end_time"):
        value = filters.get(time_key)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"Config constraints.{time_key} must be a non-empty string.")

    config["constraints"] = filters
    config["selections"] = selections_config
    config["constraints"]["top_k"] = top_k
    return config


def parse_time_to_minutes(value: str, default: int) -> int:
    value = value.strip()
    if not value:
        return default
    import re

    match = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value, flags=re.IGNORECASE)
    if not match:
        return default

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    return hour * 60 + minute


def parse_earliest_start(config_filters: Dict[str, object]) -> int:
    return parse_time_to_minutes(str(config_filters.get("earliest_start_time", "8:00 AM")), 8 * 60)


def parse_latest_end(config_filters: Dict[str, object]) -> int:
    return parse_time_to_minutes(str(config_filters.get("latest_end_time", "11:00 PM")), 23 * 60)


# ---------------------------------------------------------------------------
# Selection and filtering


def section_passes_filters(section: Section, filters: Dict[str, object], earliest_start: int) -> bool:
    status_lower = section.status.lower()

    if not filters.get("include_closed", False) and ("closed" in status_lower or "cancelled" in status_lower):
        return False
    if not filters.get("show_reserved", True) and "reserved" in status_lower:
        return False

    allow_friday = bool(filters.get("allow_friday", True))
    for meeting in section.meetings:
        if not allow_friday and any(day == "Fri" for day in meeting.days):
            return False
        if earliest_start and meeting.start < earliest_start:
            return False

    return True


def build_course_groups(
    courses: List[Course],
    by_code: Dict[str, Course],
    by_unique: Dict[str, Tuple[Course, Section]],
    config: Dict[str, object],
) -> List[SectionSelection]:
    filters = config.get("constraints", {})
    earliest_start = parse_earliest_start(filters)
    latest_end_allowed = parse_latest_end(filters)

    selections_config = config.get("selections", {}) or {}
    selection_entries = selections_config.get("course", []) or []
    if isinstance(selection_entries, (str, list)):
        pass
    else:
        selection_entries = []

    taken_entries = selections_config.get("taken_courses", []) or []
    if isinstance(taken_entries, str):
        taken_entries = [taken_entries]
    taken_courses = {normalise_whitespace(code) for code in taken_entries if isinstance(code, str)}

    excluded_instructors_raw = selections_config.get("excluded_instructors", []) or []
    if isinstance(excluded_instructors_raw, str):
        excluded_instructors_raw = [excluded_instructors_raw]
    excluded_instructors = {
        normalise_whitespace(name).lower()
        for name in excluded_instructors_raw
        if isinstance(name, str) and name.strip()
    }

    groups: Dict[str, List[SectionSelection]] = {}
    locked_courses: Dict[str, str] = {}
    assigned_field_levels: Dict[Tuple[str, str], Set[str]] = {}
    assigned_fields: Dict[str, Set[str]] = {}

    def remove_course_from_groups(course_code: str) -> None:
        for key in list(groups.keys()):
            filtered = [opt for opt in groups[key] if opt.course_code != course_code]
            if not filtered:
                groups.pop(key, None)
            else:
                groups[key] = filtered

    def register_group(key: str, selections: List[SectionSelection], course_code: Optional[str] = None, override: bool = False) -> None:
        if not selections:
            return
        if override or key not in groups:
            groups[key] = selections
        else:
            groups[key].extend(selections)
        if course_code:
            locked_courses[course_code] = key

    def unique_group_key(base: str) -> str:
        if base not in groups:
            return base
        counter = 2
        while f"{base}#{counter}" in groups:
            counter += 1
        return f"{base}#{counter}"

    def add_course_to_groups(course: Course) -> bool:
        if course.course_code in groups or course.course_code in locked_courses:
            return False
        options: List[SectionSelection] = []
        for section in course.sections:
            instructor_name = normalise_whitespace(section.instructor).lower()
            instructor_last = instructor_name.split(",", 1)[0]
            if excluded_instructors and (
                instructor_name in excluded_instructors or instructor_last in excluded_instructors
            ):
                continue
            if not section_passes_filters(section, filters, earliest_start):
                continue
            if any(meeting.end > latest_end_allowed for meeting in section.meetings):
                continue
            options.append(
                SectionSelection(
                    course_code=course.course_code,
                    course_title=course.course_title,
                    section=section,
                )
            )
        if options:
            register_group(course.course_code, options, course.course_code)
            return True
        return False

    def course_is_taken(course_code: str) -> bool:
        return course_code in taken_courses

    for entry in selection_entries:
        if isinstance(entry, list):
            options_raw = entry
        else:
            options_raw = [entry]

        options = [opt for opt in options_raw if isinstance(opt, str) and opt.strip()]
        if not options:
            continue

        fulfilled = False
        for value in options:
            value = value.strip()
            normalised = normalise_whitespace(value)
            digits_only = normalised.replace(" ", "")

        # Unique number selector
        if digits_only.isdigit():
            record = by_unique.get(digits_only)
            if record:
                course, section = record
                if section_passes_filters(section, filters, earliest_start):
                    if course_is_taken(course.course_code):
                        continue
                    remove_course_from_groups(course.course_code)
                    selection = SectionSelection(
                        course_code=course.course_code,
                        course_title=course.course_title,
                        section=section,
                    )
                    register_group(course.course_code, [selection], course.course_code, override=True)
                    fulfilled = True
                    break
        if fulfilled:
            continue

        # Direct course code selector
        for value in options:
            normalised = normalise_whitespace(value)
            course = by_code.get(normalised)
            if course and not course_is_taken(course.course_code):
                remove_course_from_groups(course.course_code)
                if add_course_to_groups(course):
                    fulfilled = True
                    break
        if fulfilled:
            continue

        # Field-level selector (e.g., "C S - Upper")
        for value in options:
            if "-" not in value:
                continue
            field_part, level_part = [part.strip() for part in value.split("-", 1)]
            level_part_lower = level_part.lower()
            assigned = assigned_field_levels.setdefault((field_part, level_part_lower), set())
            for course_candidate in courses:
                if course_candidate.field != field_part:
                    continue
                if level_part and course_candidate.level.lower() != level_part_lower:
                    continue
                if course_is_taken(course_candidate.course_code):
                    continue
                if course_candidate.course_code in assigned:
                    continue
                if add_course_to_groups(course_candidate):
                    assigned.add(course_candidate.course_code)
                    fulfilled = True
                    break
            if fulfilled:
                break
        if fulfilled:
            continue

        # Field-only selector (e.g., "C S")
        for value in options:
            normalised = normalise_whitespace(value)
            assigned_field = assigned_fields.setdefault(normalised, set())
            for course_candidate in courses:
                if course_candidate.field != normalised:
                    continue
                if course_is_taken(course_candidate.course_code):
                    continue
                if course_candidate.course_code in assigned_field:
                    continue
                if add_course_to_groups(course_candidate):
                    assigned_field.add(course_candidate.course_code)
                    fulfilled = True
                    break
            if fulfilled:
                break

    return list(groups.values())


# ---------------------------------------------------------------------------
# Schedule generation and scoring


def schedule_conflicts(selection: SectionSelection, day_map: Dict[str, List[Tuple[int, int, SectionSelection]]], latest_end_allowed: int) -> bool:
    for meeting in selection.section.meetings:
        for day in meeting.days:
            entries = day_map.setdefault(day, [])
            for start, end, existing in entries:
                if meeting.start < end and meeting.end > start:
                    return True
        if meeting.end > latest_end_allowed:
            return True
    return False


def add_selection_to_day_map(selection: SectionSelection, day_map: Dict[str, List[Tuple[int, int, SectionSelection]]]) -> None:
    for meeting in selection.section.meetings:
        for day in meeting.days:
            entries = day_map.setdefault(day, [])
            entries.append((meeting.start, meeting.end, selection))


def remove_selection_from_day_map(selection: SectionSelection, day_map: Dict[str, List[Tuple[int, int, SectionSelection]]]) -> None:
    for meeting in selection.section.meetings:
        for day in meeting.days:
            entries = day_map.get(day)
            if not entries:
                continue
            try:
                entries.remove((meeting.start, meeting.end, selection))
            except ValueError:
                continue


def evaluate_schedule(selections: List[SectionSelection]) -> Dict[str, float]:
    daily_latest: List[int] = []
    total_gap = 0
    earliest_start = None
    latest_end = None
    daily_hours: List[float] = []

    for day in DAY_ORDER:
        intervals = [
            (meeting.start, meeting.end, selection)
            for selection in selections
            for meeting in selection.section.meetings
            if day in meeting.days
        ]
        if not intervals:
            daily_hours.append(0.0)
            continue
        intervals.sort(key=lambda item: item[0])
        last_end = intervals[-1][1]
        daily_latest.append(last_end)

        for start, end, _ in intervals:
            earliest_start = start if earliest_start is None else min(earliest_start, start)
            latest_end = end if latest_end is None else max(latest_end, end)

        day_total = 0
        for start, end, _ in intervals:
            day_total += end - start
        daily_hours.append(day_total / 60.0)

        for idx in range(1, len(intervals)):
            prev_end = intervals[idx - 1][1]
            current_start = intervals[idx][0]
            if current_start > prev_end:
                total_gap += current_start - prev_end

    average_end = sum(daily_latest) / len(daily_latest) if daily_latest else 0.0
    span = (latest_end - earliest_start) if (earliest_start is not None and latest_end is not None) else 0.0
    std_dev = statistics.pstdev(daily_hours) if daily_hours else 0.0

    return {
        "average_end": average_end,
        "latest_end": latest_end or 0.0,
        "total_gap": float(total_gap),
        "daily_span": float(span),
        "earliest_start": float(earliest_start or 0.0),
        "daily_std": float(std_dev),
    }


def generate_schedules(groups: List[List[SectionSelection]], top_k: int, filters: Dict[str, object]) -> List[Schedule]:
    sorted_groups = sorted(groups, key=len)
    total_groups = len(sorted_groups)
    if total_groups == 0:
        return []

    schedules: List[Schedule] = []
    evaluated_count = 0
    latest_end_allowed = parse_latest_end(filters)

    def backtrack(index: int, current: List[SectionSelection], day_map: Dict[str, List[Tuple[int, int, SectionSelection]]]):
        nonlocal evaluated_count
        if index == total_groups:
            metrics = evaluate_schedule(current)
            schedules.append(Schedule(list(current), metrics))
            evaluated_count += 1
            return

        for selection in sorted_groups[index]:
            if selection.section.meetings and schedule_conflicts(selection, day_map, latest_end_allowed):
                continue
            add_selection_to_day_map(selection, day_map)
            current.append(selection)
            backtrack(index + 1, current, day_map)
            current.pop()
            remove_selection_from_day_map(selection, day_map)

    backtrack(0, [], {})

    if not schedules:
        return []

    schedules.sort(
        key=lambda sch: (
            sch.metrics["total_gap"],
            sch.metrics["daily_span"],
            sch.metrics["daily_std"],
            sch.metrics["average_end"],
        )
    )

    generate_schedules.last_evaluated = evaluated_count
    return schedules[:top_k]


# ---------------------------------------------------------------------------
# Presentation helpers


def render_schedule(schedule: Schedule, index: int, strategy: str) -> None:
    print(f"\n=== Schedule {index + 1} ===")
    avg_end = minutes_to_time_str(schedule.metrics['average_end'])
    latest_end = minutes_to_time_str(schedule.metrics['latest_end'])
    span_minutes = schedule.metrics.get('daily_span', 0.0)
    span_str = f"{int(span_minutes // 60):02d}:{int(span_minutes % 60):02d}"
    std_val = schedule.metrics.get('daily_std', 0.0)
    print(
        f"Schedule metrics: average_end={avg_end}, latest_end={latest_end}, "
        f"span={span_str}, daily_std={std_val:.2f} h, total_gap={schedule.metrics['total_gap']:.1f} min"
    )

    print("\nSelected Sections:")
    for selection in schedule.selections:
        status = selection.section.status or "(status n/a)"
        print(
            f"  - {selection.course_code} {selection.course_title} | "
            f"#{selection.section.unique_number} | {status} | "
            f"{selection.section.instructor or 'Instructor TBA'}"
        )

    print("\nWeekly Calendar:")
    render_calendar(schedule)


def render_calendar(schedule: Schedule) -> None:
    events_by_day: Dict[str, List[Tuple[int, int, SectionSelection]]] = {
        day: [] for day in DAY_ORDER
    }
    earliest = None
    latest = None

    for selection in schedule.selections:
        for meeting in selection.section.meetings:
            for day in meeting.days:
                if day not in events_by_day:
                    continue
                events_by_day[day].append((meeting.start, meeting.end, selection))
                earliest = meeting.start if earliest is None else min(earliest, meeting.start)
                latest = meeting.end if latest is None else max(latest, meeting.end)

    if earliest is None or latest is None:
        print("  No scheduled meetings.")
        return

    earliest = (earliest // TIME_SLOT_MINUTES) * TIME_SLOT_MINUTES
    latest = ((latest + TIME_SLOT_MINUTES - 1) // TIME_SLOT_MINUTES) * TIME_SLOT_MINUTES

    column_width = 26
    header = "".ljust(8) + "".join(day.center(column_width) for day in DAY_ORDER)
    print(header)
    print("".ljust(8) + "".join("=" * column_width for _ in DAY_ORDER))

    slot_map: Dict[str, Dict[int, Dict[str, object]]] = {day: {} for day in DAY_ORDER}
    for day, entries in events_by_day.items():
        entries.sort(key=lambda item: item[0])
        for start, end, selection in entries:
            start_slot = (start // TIME_SLOT_MINUTES) * TIME_SLOT_MINUTES
            end_slot = math.ceil(end / TIME_SLOT_MINUTES) * TIME_SLOT_MINUTES
            duration_slots = max(1, (end_slot - start_slot) // TIME_SLOT_MINUTES)
            for idx in range(duration_slots):
                slot = start_slot + idx * TIME_SLOT_MINUTES
                if slot < earliest or slot >= latest:
                    continue
                slot_map[day][slot] = {
                    "selection": selection,
                    "offset": idx,
                    "span": duration_slots,
                }

    def render_boundary(boundary: int) -> str:
        cells: List[str] = []
        inner_width = column_width - 2
        for day in DAY_ORDER:
            event_current = slot_map[day].get(boundary)
            event_prev = slot_map[day].get(boundary - TIME_SLOT_MINUTES)

            if event_current and event_current["offset"] == 0:
                cells.append("+" + "-" * inner_width + "+")
            elif event_prev and event_prev["offset"] == event_prev["span"] - 1:
                cells.append("+" + "-" * inner_width + "+")
            elif event_prev:
                cells.append("|" + " " * inner_width + "|")
            else:
                cells.append(" " * column_width)
        return "".join(cells)

    def render_slot(slot_start: int) -> str:
        cells: List[str] = []
        inner_width = column_width - 2
        for day in DAY_ORDER:
            event = slot_map[day].get(slot_start)
            if not event:
                cells.append(" " * column_width)
                continue

            selection: SectionSelection = event["selection"]  # type: ignore[index]
            offset: int = event["offset"]  # type: ignore[index]
            span: int = event["span"]  # type: ignore[index]

            if offset == 0:
                instructor = selection.section.instructor or "Instructor TBA"
                last_name = instructor.split(",", 1)[0]
                title = f"{selection.course_code} - {last_name}"
                cells.append("|" + title.center(inner_width)[:inner_width].ljust(inner_width) + "|")
            elif offset == span - 1:
                unique = selection.section.unique_number
                cells.append("|" + unique.center(inner_width)[:inner_width].ljust(inner_width) + "|")
            else:
                cells.append("|" + " " * inner_width + "|")
        return "".join(cells)

    def render_fill(slot_start: int) -> str:
        cells: List[str] = []
        inner_width = column_width - 2
        for day in DAY_ORDER:
            if slot_map[day].get(slot_start):
                cells.append("|" + " " * inner_width + "|")
            else:
                cells.append(" " * column_width)
        return "".join(cells)

    boundary = earliest
    while boundary < latest:
        time_label = minutes_to_time_str(boundary).ljust(8)
        print(time_label + render_boundary(boundary))
        print("".ljust(8) + render_slot(boundary))
        print("".ljust(8) + render_fill(boundary))
        boundary += TIME_SLOT_MINUTES
    print(minutes_to_time_str(latest).ljust(8) + render_boundary(latest))


# ---------------------------------------------------------------------------
# Entry point


def main() -> None:
    if not COURSE_DIR.exists():
        raise SystemExit(f"Course schedule directory not found: {COURSE_DIR}")

    courses, by_code, by_unique = parse_course_files(COURSE_DIR)
    if not courses:
        raise SystemExit("No courses parsed. Ensure HTML files are present in course_schedule/.")

    write_grouped_json(courses)

    config = load_config(CONFIG_PATH)
    constraints = config.get("constraints", {})
    top_k = int(constraints.get("top_k", 5) or 5)

    groups = build_course_groups(courses, by_code, by_unique, config)

    if not groups:
        raise SystemExit("No course groups were selected. Update config.json selections.")

    schedules = generate_schedules(groups, top_k, constraints)
    if not schedules:
        raise SystemExit("No valid schedules found with current configuration.")

    evaluated = getattr(generate_schedules, "last_evaluated", "unknown")
    print(
        f"Generated {len(schedules)} schedule(s) prioritizing minimal idle time between classes. "
        f"Compared {len(groups)} course group(s) and evaluated {evaluated} combinations."
    )
    for idx, schedule in enumerate(schedules):
        render_schedule(schedule, idx, "minimal_idle")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

