# Optimal Schedule Maker

Generate the top course schedules from UT Austin HTML exports using a constraint-driven search. The algorithm:

1. Parses registrar HTML results and groups sections by field/level selection rules in `config.json`.
2. Builds all non-conflicting combinations that satisfy required courses and constraints (earliest start, latest end, excluded instructors, etc.).
3. Scores schedules based on the total idle minutes between classes. Ties are broken by daily span (latest end − earliest start), then by daily hours standard deviation, then by average end time.
4. Renders the best schedules as ASCII calendars with 24-hour times.

## Inputs

- Place registrar HTML results under `course_schedule/`.
- First run creates/updates `parsed_courses/` (json snapshots).
- Provide configuration via `config.json` (no defaults are generated).

## Running

```bash
python3 optimal_schedule.py
```

Output shows the top schedules based on the constraints.

## `config.json`

### Constraints

- `top_k`: number of schedules to print.
- `allow_friday`: allow Friday classes.
- `include_closed`: include closed/cancelled sections.
- `show_reserved`: include reserved sections.
- `earliest_start_time`: earliest allowed start (e.g. `09:00`).
- `latest_end_time` : latest end time allowed (e.g. `17:00`)

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
