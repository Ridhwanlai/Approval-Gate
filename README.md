# Para: Memo Gate on HarnessRouter

Para is an approval gate between a paralegal's AI-drafted research memo and the supervising partner.
It returns APPROVE, HOLD_FOR_REVIEW or BLOCK with a plain-language reason and evidence. The memo is
sent only on APPROVE. This version adds three Hermes agents on HarnessRouter Cloud to the original
list check and Ezio scoring. Decision policy: [POLICY.md](POLICY.md).

The original buildathon submission is unchanged and still runs: see [TEAM_README.md](TEAM_README.md)
(`gate.py`, `wrapper/`). The challenge brief that was this repository's README is now
[CHALLENGE.md](CHALLENGE.md).

## Run it

```bash
python3 harnesses/provision.py        # once: creates or updates the three harnesses, writes harnesses/registry.json
python3 run.py                        # http://127.0.0.1:9000/  (PORT=50412 python3 run.py for another port)
python3 -m unittest discover -s tests # offline: fake HarnessRouter + the real mock Ezio, no key needed
python3 tests/live_eval.py            # live: the 16 practice memos through the real harnesses (uses credits)
```

Python 3.9+, standard library only. The HarnessRouter key is read on the server from `HR_API_KEY` or
the git-ignored, owner-only `.env.harnessrouter`. It is set with the HarnessRouter Skill's secure
key prompt, never pasted into chat or committed. Without a key every memo is held for review, never
sent unread.

## How the work is split

| Layer | Owns | Where |
|---|---|---|
| Product (Para server) | UI, firm citation lists, regex cite-check, Ezio client, **the decision**, review queue, audit log, the send action | `para/`, `ui/`, `firm/` |
| `para-cite-auditor` (Hermes, read-only) | Finds every authority in any format and judges it against the firm lists, including whether a paraphrased holding matches | `harnesses/para-cite-auditor.json` |
| `para-memo-reviewer` (Hermes, read-only) | Analysis or advocacy: overconfidence, emotional language, one-sidedness | `harnesses/para-memo-reviewer.json` |
| `para-revision-drafter` (Hermes, writes files only) | On request, drafts a corrected memo for a held or blocked one. Never sent; it must pass the gate again | `harnesses/para-revision-drafter.json` |

**Why three.** Merging the auditor and reviewer would work, but separate harnesses keep two success
criteria apart, run in parallel and fail independently. The drafter must be separate so no agent
grades its own fix. Ezio stays an API integration. The decision stays in code: a "decider" harness
would hand the safety call to a model.

**How they combine (`para/gate.py`, `para/pipeline.py`).** For each check the server runs the list
check, Ezio, the auditor and the reviewer in parallel. Agent output is untrusted: a finding is
discarded unless its quote is verbatim in the memo and its list entry exists and is named by the
quote. Validated findings can only make the decision stricter:

- fabricated → BLOCK
- misstated or unverified → HOLD
- advocacy → HOLD
- an agent saying "verified" never lowers anything
- an agent failure (outage, timeout, billing, unreadable or off-task reply) → HOLD

The browser sends memo text only. The server maps `feature_key` to `harness_id` from
`harnesses/registry.json`. Keys, harness IDs and session IDs never reach the page. Traces are
served through `/api/trace/<check>/<feature>`.

## Account notes (checked 2026-10-11)

- Workspace `hr_default`. The Free plan runs **2 tasks at a time**; the server queues to
  `HR_MAX_CONCURRENT` (default 2), so a full check takes about one harness run's time per memo in line.
- `claude-haiku-5.5` returned 402 (`billing_error`, needs a provider key) on the Free plan;
  `claude-sonnet-5.5` runs, so all three harnesses use it.
- Hermes tool restrictions are enforced by instruction, not hard limits. That is why the server
  validates every finding.

## Files

| Path | Role |
|---|---|
| `para/gate.py` | The decision: lists + Ezio + validated agent findings → APPROVE/HOLD/BLOCK |
| `para/pipeline.py` | Runs checks and revisions as jobs with per-stage progress; review; trace |
| `para/agents.py` | Feature → harness routing, task inputs, output validation, plan-limit queue |
| `para/hr_client.py` | HarnessRouter UHP client: streaming, recovery without resubmitting, retries, error codes |
| `para/citation_check.py`, `para/ezio_client.py` | From the original Para, unchanged |
| `para/policy.json` | Every threshold and agent rule with its one-sentence why |
| `harnesses/*.json`, `provision.py` | Harness configuration as data, and the script that applies it |
| `data/` | `audit_log.jsonl`, `actions.log` (git-ignored) |
