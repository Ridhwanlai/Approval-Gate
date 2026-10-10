# Memo Gate (Paralegal)

Persona: paralegal. Gated action: `submit_memo_to_partner`. Policy: [POLICY.md](POLICY.md).

## 1. Run it

```bash
python3 gate.py try --persona paralegal "Gideon v. Wainwright, 372 U.S. 335 (1963), requires states to provide counsel to indigent defendants facing felony charges."
python3 gate.py practice --persona paralegal          # practice set and six failure drills
python3 gate.py serve --persona paralegal             # persona page with wrapper and mock
python3 gate.py judge --persona paralegal --team "Your Team Name"
```

Python 3.9 or newer, standard library only, fully offline.

## 2. What changed from the starter

| File | Change |
|---|---|
| `wrapper/citation_check.py` | New. Fabricated, misstated (cite, year, name, holding), unverified and advocacy checks |
| `wrapper/wrapper.py` | `check_source_of_truth()` implemented; contradicted-claim rule; source-of-truth check still runs when Ezio fails; plain-language reasons; evidence on every decision |
| `wrapper/policy.json` | Thresholds set; every "why" filled in |
| `wrapper/serve.py` | `POST /review` (log a held memo's review; the action runs only on proceed, never for a blocked memo) and `GET /audit` |
| `wrapper/audit.py` | `record_review()` |
| `wrapper/ui/persona.html` | Persona page: memo editor, decision stamp, evidence table, annotated draft, Ezio signals, sample docket, review queue, audit trail |
