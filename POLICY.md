# Memo Gate: Decision Policy (Paralegal Persona)

## 1. The moment this gate exists for

It is 6:40 PM. A litigation paralegal has twenty minutes to send the partner a research memo for tomorrow's suppression hearing. The AI draft reads well and Ezio scores it GREEN. One of its cases, *Varghese v. China Southern Airlines*, does not exist.

That case is real history. It is one of the fabricated authorities in *Mata v. Avianca* (S.D.N.Y. 2023), where two attorneys were sanctioned for filing a brief built on cases ChatGPT invented. Ezio cannot catch it, because a fabricated case is not a public claim any fact-checker can look up; the mock returns GREEN with accuracy not scored. Only the firm's own cite-check can.

Memo Gate sits between "send" and the partner. It reads Ezio's signals, checks every authority against the firm's lists, and returns APPROVE, HOLD_FOR_REVIEW or BLOCK with a reason the paralegal can defend.

## 2. Gated action

1. **Action:** `submit_memo_to_partner`.
2. **Runs only on APPROVE.** A held memo runs only after an attorney logs a reason and chooses Proceed. A blocked memo can never be proceeded; it must be fixed and resent.

## 3. Signals

### 3.1. Independent signal: the firm's citation lists

Every authority in the memo (full name, short name such as "Gideon", or a bare reporter cite) is compared with `trusted_citations.json` and `known_fabricated.json`.

1. **Fabricated:** on the fabricated list. BLOCK.
2. **Misstated:** a trusted case with the wrong reporter cite, the wrong year, a misspelled party, or a holding that does not match the firm's one-line summary. The holding check compares the clause attached to each case with its own holding and with every other trusted holding, so a holding moved to the wrong case is caught. Overstated ("all", "in every case") and negated holdings are also caught. BLOCK.
3. **Unverified:** on neither list. HOLD.
4. **Verified:** name, cite, year and holding all match. Logged as evidence.

### 3.2. Rule check: memos are not advocacy

Overconfident or emotional language ("slam dunk", "absolutely", "clearly", repeated exclamation marks) is held even when the citations are right. "Clearly established", a legal term, is exempt.

### 3.3. Ezio's signals

Tier and score; accuracy status and claim counts; each claim's verdict; tone, bias and agreement.

### 3.4. AI auditors on HarnessRouter (evidence only)

Two Hermes harnesses read every memo while the lists and Ezio run. Neither decides, and both are treated as untrusted.

1. **Cite-auditor** (`para-cite-auditor`): finds every authority in any format and judges it against the same firm lists, including whether a paraphrased holding matches.
2. **Memo reviewer** (`para-memo-reviewer`): reports whether the memo reads as analysis or advocacy.

A finding counts only if its quote is verbatim in the memo and any list entry it names exists and is named by the quote. Valid findings can only make the decision stricter:

| Agent finding | Decision | Justification |
|---|---|---|
| Matches a fabricated-list case | BLOCK | Validated against the firm's own list, it is the *Mata* failure. |
| Trusted case misstated | HOLD | A model can misjudge a paraphrase, so it earns a reader, not a block; only the list check blocks misstatements. |
| Authority on neither list that the list check missed | HOLD | It may be real, so a human cite-checks it. |
| Advocacy, quoted verbatim | HOLD | A research memo informs the partner; it does not argue. |
| "Verified" | No effect | An agent never overrides the lists or Ezio, so a manipulated memo cannot talk its way through. |
| Agent unavailable, timed out, unbillable, off-task or unreadable | HOLD | The memo was not fully read, so a person reads it first. |

A third harness, `para-revision-drafter`, runs only when the paralegal asks for a fix to a held or blocked memo. Its draft is never sent; it goes back through the gate like any other draft.

## 4. Decision rules and thresholds

The most severe triggered rule wins (BLOCK over HOLD over APPROVE).

| Trigger | Decision | Justification |
|---|---|---|
| Fabricated case | BLOCK | Citing a case the firm knows is fake is the *Mata* failure, and no score outweighs it. |
| Misstated cite, year, name or holding | BLOCK | Opposing counsel will catch a misquoted authority, so it never reaches the partner. |
| Ezio contradicts more than 0 claims | BLOCK | One fact Ezio can show is false (for example, the wrong amendment) is enough to stop a memo the partner signs. |
| Unresolved drafting placeholder ("[AUTHORITY NEEDED]", "[CITE-CHECK NEEDED]") | HOLD | An unfinished memo goes to an attorney, not the partner, even when everything else passes. |
| Case on neither list | HOLD | It may be real, so it earns a human cite-check rather than a block. |
| Advocacy language | HOLD | A research memo informs the partner; it does not argue to them. |
| Tone or bias below 60 | HOLD | A poor dimension hidden behind a GREEN tier is exactly what a clean score misses. |
| Agreement below 50 | HOLD | Ezio's own checks disagree with each other, which is a reason to look before sending. |
| Tier YELLOW or RED | HOLD | A weak score is a quality concern; provable falsehoods are blocked by their own rule. |
| Accuracy `not_scored` | No effect | No checkable public claim is normal for case memos, and holding them all would be a false block. |
| Ezio confirms a claim | No effect | A supported claim needs no second opinion from the lists. |

APPROVE requires every rule above to pass. A memo with no citations can be approved.

## 5. Failure behaviour

| State | Behaviour | Justification |
|---|---|---|
| Accuracy `unavailable` | HOLD; the lists still BLOCK fabricated or misstated cases | Statements outside the lists are unchecked, so a human reviews them. |
| 429 rate limit | Honour Retry-After (up to 4 s) and retry | The retry succeeds, so the memo gets a real decision. |
| 503 server error | Back off and retry, up to 4 attempts | One-off errors should not punish a good memo. |
| Slow response | Wait up to 15 s per call, 45 s in total | Every decision lands under the 60 s harness limit. |
| Ezio down or malformed | HOLD; the lists still BLOCK | The gate fails closed to review, never open. |
| Internal error in the check | HOLD | A broken check must never approve. |

## 6. Decision record

Every decision is appended to `data/audit_log.jsonl`. Each entry records:
1. The input and context.
2. Ezio's raw response.
3. The per-citation evidence (cited text, status, firm-list entry).
4. Ezio's signals.
5. The decision and its plain-language reason.
6. A UTC timestamp.

Attorney reviews of held memos are logged as separate entries with the reviewer, outcome and reason. `data/actions.log` shows the action only for approved or reviewed-and-proceeded memos.

## 7. Measured on the practice set

Original version (lists and Ezio only): 16 of 16 exact decisions, 8 of 8 bad memos caught, 0 of 8 good memos falsely blocked, and 6 of 6 failure drills passed.

With the HarnessRouter agents (`python3 tests/live_eval.py`, live Hermes harnesses, 2026-10-11): 16 of 16 exact decisions, 8 of 8 bad memos caught, 0 of 8 good memos held or blocked. All 32 agent runs completed and no agent finding failed validation. The memo reviewer quoted the advocacy in pl-12 ("AMAZING news!!!", "slam dunk") on its own. One agent run takes about 15 to 20 s alone; on the Free plan's 2-task limit with four memos in flight it took a median of 76 s (max 110 s).
