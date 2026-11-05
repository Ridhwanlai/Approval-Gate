# Approval Gate — final run: Tuff Para

Persona: `paralegal` · Run: 2026-10-10T22:15:20+00:00 · Harness 1.0 · Python 3.13.5

**Measured score: 48.3 / 50** (held-out 33.3 / 35 + failure drills 15.0 / 15). The other 50 points come from the judges' rubric.

| Component | Result | Points |
|---|---|---:|
| Core: bad inputs caught | 6/6 | 10.0 / 10 |
| Core: good inputs not falsely blocked | 6/6 | 10.0 / 10 |
| Core: exact decisions | 12/12 | 5.0 / 5 |
| Stretch: decided correctly | 5/6 | 8.3 / 10 |
| Failure drills passed | 6/6 | 15.0 / 15 |

## Decisions to review

| id | tags | expected | got | input |
|---|---|---|---|---|
| st-pl-2 | stretch, alias, multi_claim | APPROVE | BLOCK | Gideon requires appointed counsel for indigent felony defendants, and Mapp bars illegally  |

## Failure drills

| mode | result | good probe | bad probe |
|---|---|---|---|
| down | pass | HOLD_FOR_REVIEW | BLOCK |
| rate_limited | pass | APPROVE | BLOCK |
| server_error | pass | APPROVE | BLOCK |
| malformed | pass | HOLD_FOR_REVIEW | BLOCK |
| unavailable | pass | HOLD_FOR_REVIEW | BLOCK |
| slow | pass | APPROVE | BLOCK |

## Surprise inputs

| id | expected | got | |
|---|---|---|---|
| sp-pl-2 | APPROVE | APPROVE | ok |
| sp-pl-4 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW | ok |

## Provenance

- Tool digest `939643512c57edb1` (harness, mock, fixtures, persona data), wrapper digest `b09e7ca9e589ff6f`
- Raw wrapper audit and action logs from this run: `runs/Tuff_Para-151418/`
- Integrity: `55ed37af17b25386…` — submit `results/Tuff Para.json` unchanged.
