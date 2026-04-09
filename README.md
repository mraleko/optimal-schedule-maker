# Optimal Schedule Maker

Generate the top course schedules from UT Austin HTML exports using a constraint-driven search. The algorithm:

1. Parses registrar HTML results and groups sections by field/level selection rules in `config.json`.
2. Builds all non-conflicting combinations that satisfy required courses and hard constraints (earliest start, latest end, excluded instructors, etc.).
3. Scores valid schedules using a fixed priority order that favors practical schedules for most students: fewer class days, avoiding overly long daily spans, more compact days, earlier finishes, fewer gaps, and a more balanced week.
4. Renders the best schedules as ASCII calendars with 24-hour times.

## Inputs

- Place registrar HTML results under `course_schedule/`.
- First run creates/updates `parsed_courses/` (json snapshots) and `results/` (schedule text files).
- Provide configuration via `config.json` (no defaults are generated).

## Running

```bash
python3 optimal_schedule.py
```

Output shows the top schedules that satisfy the hard constraints and rank best under the fixed ranking order.

## `config.json`

### Constraints

- `top_k`: number of schedules to print.
- `allow_friday`: allow Friday classes.
- `include_closed`: include closed/cancelled sections.
- `show_reserved`: include reserved sections.
- `earliest_start_time`: earliest allowed start (e.g. `09:00`).
- `latest_end_time`: latest end time allowed (e.g. `17:00`).

These are hard constraints. Schedules that violate them are excluded before ranking.

### Ranking

The script uses a fixed ranking order:

1. `fewer_days`
2. `avoid_long_spans`
3. `compact_days`
4. `earlier_finish`
5. `fewer_gaps`
6. `balanced_week`

`avoid_long_spans` adds a soft penalty when a day's first class to last class span exceeds 4 hours. This makes a small gap acceptable if it avoids impractical 4-6 hour marathon days.

The script compares schedules lexicographically using this order.

### Selections (`selections.course`)

Each entry chooses at least one course. Examples:

- Field + level: `"C S - Upper"`
- Course code: `"M 427J"`
- Unique number: `"54770"`

Selectors can be combined; unique numbers override broader selectors.

### Taken courses (`selections.taken_courses`)

List course codes you have already taken (e.g. `"C S 429"`). Those courses are excluded from schedule generation.

### Excluded instructors (`selections.excluded_instructors`)

List instructor surnames or full names to avoid (case-insensitive match). Any section taught by a matching instructor is filtered out (e.g. `"MITRA"`, `"GOODMAN"`).

## Notes

- Requires `beautifulsoup4` (`pip install beautifulsoup4`).
- ASCII calendar shows schedule from earliest start to latest end.
- Schedule output includes the ranking priorities and the metrics used to compare results.
