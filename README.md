# Para: stop fabricated cases before the partner reads them

Para is an approval gate between a paralegal's AI-drafted research memo and the supervising partner.
Every memo is cite-checked against the firm's own lists, scored by Ezio, and read by AI auditors
running on HarnessRouter. Para then returns APPROVE, HOLD_FOR_REVIEW or BLOCK with a plain-language
reason and evidence. The memo is sent only on APPROVE.

**Website:** https://ridhwanlai.github.io/Approval-Gate/ · **Download:** [`docs/download/para.pyz`](docs/download/para.pyz) ·
**Decision policy:** [POLICY.md](POLICY.md)

On the 16-memo practice set, run live through the HarnessRouter agents: 16/16 exact decisions,
8/8 bad memos caught, 0/8 good memos stopped.

## Use it: one file

```bash
python3 para.pyz              # first run asks for your HarnessRouter key (hidden), sets up the agents, opens the browser
python3 para.pyz setup-email  # optional: email approved memos to the partner (Gmail, Outlook or any SMTP server)
python3 para.pyz status       # what is configured (never prints a key or password)
python3 para.pyz setup        # replace the HarnessRouter key
```

In the app: type a research question and choose **Draft with AI**, edit the draft, then **Check and
send to partner**. Under **AI models**, pick the model each agent runs on from everything your
HarnessRouter plan offers.

Python 3.9+, nothing else to install. Everything Para stores lives in `~/.para` (override with
`PARA_HOME`): the HarnessRouter key and SMTP password (each owner-only), the harness map, settings
(model choices, email details) and the audit log. Without a key, every memo is
held for review, never sent unread, and the firm's lists still block fabricated cases.

**Ezio is simulated in this release.** Para scores memos with the buildathon's Ezio simulation, run
locally. Ezio's production API (checked 2026-10-13) accepts only signed-in web-app sessions, not a
developer API key, so a server cannot call it yet. When Ezio provides an API key and endpoint, set
`EZIO_URL` and `EZIO_KEY` before starting and Para uses it instead.

## Develop it: from source

```bash
python3 run.py                        # http://127.0.0.1:9000/  (setup|provision|status also work)
python3 -m unittest discover -s tests # offline: fake HarnessRouter + the mock Ezio, no key needed
python3 tests/live_eval.py            # live: the 16 practice memos through the real harnesses (uses credits)
python3 tools/build_pyz.py            # build dist/para.pyz
python3 tools/record_demo.py --audit data/live_eval/audit_log.jsonl --revise pl-07 pl-12   # refresh the site demo
```

From source, the key is read from `HR_API_KEY` or the git-ignored, owner-only `.env.harnessrouter`.
The website is the static `docs/` folder, served by GitHub Pages.

The original buildathon submission is unchanged and still runs: see [TEAM_README.md](TEAM_README.md)
(`gate.py`, `wrapper/`). The challenge brief that was this repository's README is now
[CHALLENGE.md](CHALLENGE.md).

## How the work is split

| Layer | Owns | Where |
|---|---|---|
| Product (Para server) | UI, firm citation lists, regex cite-check, Ezio client, **the decision**, review queue, audit log, the send action | `para/`, `ui/`, `firm/` |
| `para-cite-auditor` (Hermes, read-only) | Finds every authority in any format and judges it against the firm lists, including whether a paraphrased holding matches | `harnesses/para-cite-auditor.json` |
| `para-memo-reviewer` (Hermes, read-only) | Analysis or advocacy: overconfidence, emotional language, one-sidedness | `harnesses/para-memo-reviewer.json` |
| `para-revision-drafter` (Hermes, writes files only) | On request, drafts a corrected memo for a held or blocked one. Never sent; it must pass the gate again | `harnesses/para-revision-drafter.json` |
| `para-memo-drafter` (Hermes, read-only) | Writes a first draft from the paralegal's question, citing only trusted cases and marking "[AUTHORITY NEEDED]" gaps. Never sent; it must pass the gate | `harnesses/para-memo-drafter.json` |

**Why four.** Merging the auditor and reviewer would work, but separate harnesses keep two success
criteria apart, run in parallel and fail independently. The two writers are separate from the two
judges so no agent grades its own work, and from each other because drafting from a question and
fixing a held memo have different inputs and success criteria. Ezio stays an API integration. The decision stays in code: a "decider" harness
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

The browser sends memo text only. The server maps `feature_key` to `harness_id` from the
registry provisioning writes (`~/.para/registry.json`, or `harnesses/registry.json` from source). Keys, harness IDs and session IDs never reach the page. Traces are
served through `/api/trace/<check>/<feature>`.

## HarnessRouter notes (checked 2026-10-11)

- The Free plan runs **2 tasks at a time**; the server queues to
  `HR_MAX_CONCURRENT` (default 2), so a full check takes about one harness run's time per memo in line.
- `claude-haiku-5.5` returned 402 (`billing_error`, needs a provider key) on the Free plan;
  `claude-sonnet-5.5` runs, so it is every harness's default. Each agent's model can be changed per
  install in the app (sent as the Responses API `model` field; checked live with Claude, GPT and
  Gemini models on 2026-10-13). A model the plan cannot bill is reported and the memo is held.
- Hermes tool restrictions are enforced by instruction, not hard limits. That is why the server
  validates every finding.

## Files

| Path | Role |
|---|---|
| `para/gate.py` | The decision: lists + Ezio + validated agent findings → APPROVE/HOLD/BLOCK |
| `para/pipeline.py` | Runs checks, drafts and revisions as jobs with per-stage progress; review; trace |
| `para/agents.py` | Feature → harness routing, task inputs, output validation, plan-limit queue |
| `para/hr_client.py` | HarnessRouter UHP client: streaming, recovery without resubmitting, retries, error codes |
| `para/citation_check.py`, `para/ezio_client.py` | From the original Para; the cite-check now ignores doctrine labels like "Miranda warnings" |
| `para/policy.json` | Every threshold and agent rule with its one-sentence why |
| `para/cli.py`, `para/provisioning.py` | `run`, `setup`, `setup-email`, `provision`, `status`; creating or updating the harnesses |
| `para/mailer.py`, `para/settings.py` | Emailing approved memos over SMTP; non-secret settings (models, email) |
| `harnesses/*.json`, `provision.py` | Harness configuration as data, and the script that applies it |
| `tools/build_pyz.py`, `tools/record_demo.py` | Build the one-file download; record the site's replay demo from live runs |
| `docs/` | The website, the replay data and the published `para.pyz` |
| `data/` | `audit_log.jsonl`, `actions.log` (git-ignored) |
