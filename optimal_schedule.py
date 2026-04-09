#!/usr/bin/env python3
"""Constraint-driven schedule generator.

Steps:
1. Parse HTML registrar output into course sections grouped by inferred field/level.
2. Build all non-conflicting section combinations that satisfy config selections and hard constraints.
3. Score schedules using a fixed ranking order.
4. Render the best schedules as ASCII calendars with 24-hour times.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError as exc:  # pragma: no cover - handled at runtime
    raise SystemExit(
        "BeautifulSoup4 is required. Install with 'pip install beautifulsoup4'."
    ) from exc


BASE_DIR = Path(__file__).resolve().parent
COURSE_DIR = BASE_DIR / "course_schedule"
PARSED_DIR = BASE_DIR / "parsed_courses"
RESULTS_DIR = BASE_DIR / "results"
CONFIG_PATH = BASE_DIR / "config.json"

DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri"]
TIME_SLOT_MINUTES = 30
DEFAULT_PRIORITY_ORDER = (
    "fewer_gaps",
    "fewer_days",
    "earlier_finish",
    "compact_days",
    "balanced_week",
)


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


@dataclass(frozen=True)
class HardConstraints:
    top_k: int
    allow_friday: bool
    include_closed: bool
    show_reserved: bool
    earliest_start: int
    latest_end: int


@dataclass(frozen=True)
class AppConfig:
    constraints: HardConstraints
    selections: Dict[str, object]


@dataclass(frozen=True)
class ScheduleMetrics:
    total_gap: float
    days_with_classes: float
    earliest_start: float
    latest_end: float
    max_daily_span: float
    daily_hours_std: float
    average_end: float


@dataclass
class Schedule:
    selections: List[SectionSelection]
    metrics: ScheduleMetrics


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
        while end <= start:
            end += 12 * 60

    return start, end


def minutes_to_time_str(minutes: float) -> str:
    total_minutes = int(minutes)
    hour = (total_minutes // 60) % 24
    minute = total_minutes % 60
    return f"{hour:02d}:{minute:02d}"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitise_filename(field: str, level: str) -> str:
    safe = f"{field}_{level}".replace(" ", "_")
    safe = safe.replace("/", "-").replace("\\", "-")
    return safe


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


def parse_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


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

            try:
                numeric_part = int("".join(filter(str.isdigit, course_number))[-2:])
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

            current_course = Course(
                field=" ".join(field_tokens).strip(),
                level=course_level,
                course_code=f"{' '.join(field_tokens)} {course_number}".strip(),
                course_title=" ".join(title_tokens).strip(),
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


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}. Please create config.json based on README guidance.")

    with path.open("r", encoding="utf-8") as handle:
        raw_config = json.load(handle)

    if not isinstance(raw_config, dict):
        raise SystemExit("Config root must be a JSON object.")

    raw_constraints = raw_config.get("constraints")
    if not isinstance(raw_constraints, dict):
        raise SystemExit("Config missing 'constraints' object.")

    raw_selections = raw_config.get("selections")
    if not isinstance(raw_selections, dict):
        raise SystemExit("Config missing 'selections' object.")

    top_k = raw_constraints.get("top_k")
    if not isinstance(top_k, int) or top_k <= 0:
        raise SystemExit("Config constraints.top_k must be a positive integer.")

    earliest_start_time = raw_constraints.get("earliest_start_time")
    latest_end_time = raw_constraints.get("latest_end_time")
    if not isinstance(earliest_start_time, str) or not earliest_start_time.strip():
        raise SystemExit("Config constraints.earliest_start_time must be a non-empty string.")
    if not isinstance(latest_end_time, str) or not latest_end_time.strip():
        raise SystemExit("Config constraints.latest_end_time must be a non-empty string.")

    course_entries = raw_selections.get("course")
    if isinstance(course_entries, str):
        selections = dict(raw_selections)
        selections["course"] = [course_entries]
    elif isinstance(course_entries, list):
        selections = dict(raw_selections)
    else:
        raise SystemExit("Config selections.course must be a list or string.")

    constraints = HardConstraints(
        top_k=top_k,
        allow_friday=parse_bool(raw_constraints.get("allow_friday"), True),
        include_closed=parse_bool(raw_constraints.get("include_closed"), False),
        show_reserved=parse_bool(raw_constraints.get("show_reserved"), True),
        earliest_start=parse_time_to_minutes(earliest_start_time, 8 * 60),
        latest_end=parse_time_to_minutes(latest_end_time, 23 * 60),
    )
    return AppConfig(constraints=constraints, selections=selections)


# ---------------------------------------------------------------------------
# Selection and filtering


def section_passes_filters(section: Section, constraints: HardConstraints) -> bool:
    status_lower = section.status.lower()

    if not constraints.include_closed and ("closed" in status_lower or "cancelled" in status_lower):
        return False
    if not constraints.show_reserved and "reserved" in status_lower:
        return False

    for meeting in section.meetings:
        if not constraints.allow_friday and any(day == "Fri" for day in meeting.days):
            return False
        if meeting.start < constraints.earliest_start:
            return False
        if meeting.end > constraints.latest_end:
            return False

    return True


def build_course_options(
    course: Course,
    constraints: HardConstraints,
    excluded_instructors: Set[str],
) -> List[SectionSelection]:
    options: List[SectionSelection] = []
    for section in course.sections:
        instructor_name = normalise_whitespace(section.instructor).lower()
        instructor_last = instructor_name.split(",", 1)[0]
        if excluded_instructors and (
            instructor_name in excluded_instructors or instructor_last in excluded_instructors
        ):
            continue
        if not section_passes_filters(section, constraints):
            continue
        options.append(
            SectionSelection(
                course_code=course.course_code,
                course_title=course.course_title,
                section=section,
            )
        )
    return options


def selector_is_unique_number(selector: str) -> bool:
    digits_only = normalise_whitespace(selector).replace(" ", "")
    return digits_only.isdigit()


def resolve_selector(
    selector: str,
    courses: List[Course],
    by_code: Dict[str, Course],
    by_unique: Dict[str, Tuple[Course, Section]],
    constraints: HardConstraints,
    taken_courses: Set[str],
    excluded_instructors: Set[str],
) -> List[SectionSelection]:
    value = normalise_whitespace(selector)
    digits_only = value.replace(" ", "")

    if digits_only.isdigit():
        record = by_unique.get(digits_only)
        if not record:
            return []
        course, section = record
        if course.course_code in taken_courses or not section_passes_filters(section, constraints):
            return []
        instructor_name = normalise_whitespace(section.instructor).lower()
        instructor_last = instructor_name.split(",", 1)[0]
        if excluded_instructors and (
            instructor_name in excluded_instructors or instructor_last in excluded_instructors
        ):
            return []
        return [
            SectionSelection(
                course_code=course.course_code,
                course_title=course.course_title,
                section=section,
            )
        ]

    direct_course = by_code.get(value)
    if direct_course and direct_course.course_code not in taken_courses:
        return build_course_options(direct_course, constraints, excluded_instructors)

    if "-" in value:
        field_part, level_part = [part.strip() for part in value.split("-", 1)]
        if field_part and level_part:
            options: List[SectionSelection] = []
            for course in courses:
                if course.field != field_part or course.level.lower() != level_part.lower():
                    continue
                if course.course_code in taken_courses:
                    continue
                options.extend(build_course_options(course, constraints, excluded_instructors))
            return options

    options = []
    for course in courses:
        if course.field != value:
            continue
        if course.course_code in taken_courses:
            continue
        options.extend(build_course_options(course, constraints, excluded_instructors))
    return options


def deduplicate_selections(selections: List[SectionSelection]) -> List[SectionSelection]:
    unique: Dict[Tuple[str, str], SectionSelection] = {}
    for selection in selections:
        key = (selection.course_code, selection.section.unique_number)
        unique[key] = selection
    return sorted(unique.values(), key=lambda item: (item.course_code, item.section.unique_number))


def build_course_groups(
    courses: List[Course],
    by_code: Dict[str, Course],
    by_unique: Dict[str, Tuple[Course, Section]],
    config: AppConfig,
) -> List[List[SectionSelection]]:
    selection_entries = config.selections.get("course", []) or []
    if isinstance(selection_entries, str):
        selection_entries = [selection_entries]
    if not isinstance(selection_entries, list):
        return []

    taken_entries = config.selections.get("taken_courses", []) or []
    if isinstance(taken_entries, str):
        taken_entries = [taken_entries]
    taken_courses = {
        normalise_whitespace(code)
        for code in taken_entries
        if isinstance(code, str) and code.strip()
    }

    excluded_entries = config.selections.get("excluded_instructors", []) or []
    if isinstance(excluded_entries, str):
        excluded_entries = [excluded_entries]
    excluded_instructors = {
        normalise_whitespace(name).lower()
        for name in excluded_entries
        if isinstance(name, str) and name.strip()
    }

    groups: List[List[SectionSelection]] = []
    for entry in selection_entries:
        selectors = entry if isinstance(entry, list) else [entry]
        valid_selectors = [selector for selector in selectors if isinstance(selector, str) and selector.strip()]
        if not valid_selectors:
            continue

        exact_matches: List[SectionSelection] = []
        flexible_matches: List[SectionSelection] = []
        for selector in valid_selectors:
            matches = resolve_selector(
                selector,
                courses,
                by_code,
                by_unique,
                config.constraints,
                taken_courses,
                excluded_instructors,
            )
            if selector_is_unique_number(selector):
                exact_matches.extend(matches)
            else:
                flexible_matches.extend(matches)

        group = deduplicate_selections(exact_matches or flexible_matches)
        if group:
            groups.append(group)

    return groups


# ---------------------------------------------------------------------------
# Schedule generation and scoring


def schedule_conflicts(
    selection: SectionSelection,
    day_map: Dict[str, List[Tuple[int, int, SectionSelection]]],
    chosen_courses: Set[str],
    constraints: HardConstraints,
) -> bool:
    if selection.course_code in chosen_courses:
        return True

    for meeting in selection.section.meetings:
        if meeting.end > constraints.latest_end:
            return True
        for day in meeting.days:
            entries = day_map.setdefault(day, [])
            for start, end, _ in entries:
                if meeting.start < end and meeting.end > start:
                    return True
    return False


def add_selection_to_day_map(selection: SectionSelection, day_map: Dict[str, List[Tuple[int, int, SectionSelection]]]) -> None:
    for meeting in selection.section.meetings:
        for day in meeting.days:
            day_map.setdefault(day, []).append((meeting.start, meeting.end, selection))


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


def evaluate_schedule(selections: List[SectionSelection]) -> ScheduleMetrics:
    total_gap = 0
    days_with_classes = 0
    earliest_start: Optional[int] = None
    latest_end: Optional[int] = None
    max_daily_span = 0
    daily_latest: List[int] = []
    daily_hours: List[float] = []

    for day in DAY_ORDER:
        intervals = [
            (meeting.start, meeting.end)
            for selection in selections
            for meeting in selection.section.meetings
            if day in meeting.days
        ]
        if not intervals:
            daily_hours.append(0.0)
            continue

        days_with_classes += 1
        intervals.sort(key=lambda item: item[0])
        day_start = intervals[0][0]
        day_end = intervals[-1][1]
        daily_latest.append(day_end)
        max_daily_span = max(max_daily_span, day_end - day_start)
        earliest_start = day_start if earliest_start is None else min(earliest_start, day_start)
        latest_end = day_end if latest_end is None else max(latest_end, day_end)

        total_minutes = sum(end - start for start, end in intervals)
        daily_hours.append(total_minutes / 60.0)

        for index in range(1, len(intervals)):
            previous_end = intervals[index - 1][1]
            current_start = intervals[index][0]
            if current_start > previous_end:
                total_gap += current_start - previous_end

    average_end = sum(daily_latest) / len(daily_latest) if daily_latest else 0.0
    daily_hours_std = statistics.pstdev(daily_hours) if daily_hours else 0.0

    return ScheduleMetrics(
        total_gap=float(total_gap),
        days_with_classes=float(days_with_classes),
        earliest_start=float(earliest_start or 0.0),
        latest_end=float(latest_end or 0.0),
        max_daily_span=float(max_daily_span),
        daily_hours_std=float(daily_hours_std),
        average_end=float(average_end),
    )


def schedule_sort_key(metrics: ScheduleMetrics) -> Tuple[float, ...]:
    return (
        metrics.total_gap,
        metrics.days_with_classes,
        metrics.latest_end,
        metrics.max_daily_span,
        metrics.daily_hours_std,
        metrics.average_end,
    )


def generate_schedules(groups: List[List[SectionSelection]], config: AppConfig) -> List[Schedule]:
    sorted_groups = sorted(
        (sorted(group, key=lambda item: (item.course_code, item.section.unique_number)) for group in groups),
        key=len,
    )
    if not sorted_groups:
        return []

    schedules: List[Schedule] = []
    evaluated_count = 0

    def backtrack(
        index: int,
        current: List[SectionSelection],
        day_map: Dict[str, List[Tuple[int, int, SectionSelection]]],
        chosen_courses: Set[str],
    ) -> None:
        nonlocal evaluated_count
        if index == len(sorted_groups):
            schedules.append(Schedule(list(current), evaluate_schedule(current)))
            evaluated_count += 1
            return

        for selection in sorted_groups[index]:
            if schedule_conflicts(selection, day_map, chosen_courses, config.constraints):
                continue
            chosen_courses.add(selection.course_code)
            add_selection_to_day_map(selection, day_map)
            current.append(selection)
            backtrack(index + 1, current, day_map, chosen_courses)
            current.pop()
            remove_selection_from_day_map(selection, day_map)
            chosen_courses.remove(selection.course_code)

    backtrack(0, [], {}, set())

    schedules.sort(key=lambda schedule: schedule_sort_key(schedule.metrics))
    generate_schedules.last_evaluated = evaluated_count
    return schedules[: config.constraints.top_k]


# ---------------------------------------------------------------------------
# Presentation helpers


def format_duration(minutes: float) -> str:
    whole_minutes = int(minutes)
    return f"{whole_minutes // 60:02d}:{whole_minutes % 60:02d}"


def describe_priorities() -> str:
    return " > ".join(DEFAULT_PRIORITY_ORDER)


def render_schedule(schedule: Schedule, index: int) -> None:
    metrics = schedule.metrics
    print(f"\n=== Schedule {index + 1} ===")
    print(f"Ranking priorities: {describe_priorities()}")
    print(
        "Schedule metrics: "
        f"total_gap={metrics.total_gap:.1f} min, "
        f"days_with_classes={int(metrics.days_with_classes)}, "
        f"latest_end={minutes_to_time_str(metrics.latest_end)}, "
        f"max_daily_span={format_duration(metrics.max_daily_span)}, "
        f"daily_hours_std={metrics.daily_hours_std:.2f} h, "
        f"earliest_start={minutes_to_time_str(metrics.earliest_start)}"
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
    for line in calendar_lines(schedule, show_details=True):
        print(line)


def render_results_schedule(schedule: Schedule, index: int) -> str:
    section_numbers = "\n".join(
        f"{selection.section.unique_number} - {selection.course_code}"
        for selection in schedule.selections
    )
    return f"Schedule {index + 1}:\n{section_numbers}"


def write_results_file(schedules: List[Schedule]) -> Path:
    ensure_directory(RESULTS_DIR)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = RESULTS_DIR / f"{timestamp}.txt"
    content = "\n\n".join(
        render_results_schedule(schedule, index)
        for index, schedule in enumerate(schedules)
    ) + "\n"
    output_path.write_text(content, encoding="utf-8")
    return output_path


def calendar_lines(schedule: Schedule, show_details: bool) -> List[str]:
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
        return ["  No scheduled meetings."]

    earliest = (earliest // TIME_SLOT_MINUTES) * TIME_SLOT_MINUTES
    latest = ((latest + TIME_SLOT_MINUTES - 1) // TIME_SLOT_MINUTES) * TIME_SLOT_MINUTES

    column_width = 26
    lines = [
        "".ljust(8) + "".join(day.center(column_width) for day in DAY_ORDER),
        "".ljust(8) + "".join("=" * column_width for _ in DAY_ORDER),
    ]

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
                if show_details:
                    instructor = selection.section.instructor or "Instructor TBA"
                    last_name = instructor.split(",", 1)[0]
                    title = f"{selection.course_code} - {last_name}"
                else:
                    title = selection.course_code
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
        lines.append(time_label + render_boundary(boundary))
        lines.append("".ljust(8) + render_slot(boundary))
        lines.append("".ljust(8) + render_fill(boundary))
        boundary += TIME_SLOT_MINUTES
    lines.append(minutes_to_time_str(latest).ljust(8) + render_boundary(latest))
    return lines


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
    groups = build_course_groups(courses, by_code, by_unique, config)
    if not groups:
        raise SystemExit("No course groups were selected. Update config.json selections.")

    schedules = generate_schedules(groups, config)
    if not schedules:
        raise SystemExit("No valid schedules found with current configuration.")

    write_results_file(schedules)

    evaluated = getattr(generate_schedules, "last_evaluated", "unknown")
    print(
        f"Generated {len(schedules)} schedule(s) using priorities {describe_priorities()}. "
        f"Compared {len(groups)} course group(s) and evaluated {evaluated} combinations."
    )
    for idx, schedule in enumerate(schedules):
        render_schedule(schedule, idx)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
