"""Paralegal source-of-truth check: the firm's citation lists and drafting rules.

check(text, sot) -> list of (level, message, evidence_dict)

Implements personas/paralegal/business_rules.md:
  1. Fabricated case (full or short name)                     -> BLOCK
  2. Trusted case with the wrong reporter, year or holding    -> BLOCK  (short names count)
  3. Case on neither list                                     -> HOLD_FOR_REVIEW
  4. Overconfident, one-sided or emotional language           -> HOLD_FOR_REVIEW
  5. No citations                                             -> no finding
Rule 6 (Ezio's contradicted claims) lives in wrapper.apply_policy.
"""
import difflib
import re

BLOCK, HOLD = "BLOCK", "HOLD_FOR_REVIEW"

# ---------------------------------------------------------------- patterns

_TOK = r"[A-Z0-9][\w'\u2019.&-]*"
_JOIN = r"(?:of|the|and|for|de|del|la|ex|rel\.|&)"
CASE_RE = re.compile(
    r"([A-Z][\w'\u2019.&-]*(?:\s+(?:" + _TOK + r"|" + _JOIN + r"))*)\s+vs?\.?\s+(" + _TOK + r"(?:\s+(?:" + _TOK + r"|" + _JOIN + r"))*)"
)
REPORTER_RE = re.compile(r"\b(\d{1,4})\s+((?:[A-Z][A-Za-z]{0,7}\.?\s?){1,5}(?:\d?(?:d|th|st|nd|rd)\.?)?)\s*(\d{1,7})\b")
PAREN_YEAR_RE = re.compile(r"\((?:[^()]{0,40}?\s)?((?:1[6-9]|20)\d{2})\)")
NEAR_YEAR_RE = re.compile(r"^[\s,]*(?:\(|decided\s+in\s+|in\s+)?((?:1[6-9]|20)\d{2})\b")

LEAD = {"in", "see", "under", "cf", "cf.", "accord", "compare", "following", "citing", "per", "as", "also",
        "but", "and", "the", "then", "unlike", "like", "applying", "our", "moreover", "however", "similarly",
        "notably", "because", "since", "after", "while", "although", "here", "this", "that", "thus", "so",
        "accordingly", "therefore", "further", "furthermore", "amazing", "news", "great", "held", "court"}
GENERIC_PARTY = {"united", "states", "state", "people", "city", "county", "commonwealth", "department", "board",
                 "estate", "matter", "doe", "new", "national", "american", "general", "the", "in", "re"}
SUFFIX = {"co", "co.", "inc", "inc.", "corp", "corp.", "company", "corporation", "railroad", "ltd", "ltd.", "llc"}

STOP = {
    "the", "and", "that", "this", "with", "for", "under", "held", "hold", "holds", "holding", "court", "courts",
    "case", "cases", "was", "were", "are", "is", "been", "be", "has", "have", "had", "its", "their", "which",
    "who", "whom", "our", "we", "us", "your", "you", "from", "into", "by", "on", "in", "of", "to", "as", "at",
    "an", "a", "it", "requires", "require", "required", "through", "also", "see", "per", "here", "decided",
    "decision", "ruled", "ruling", "found", "finds", "said", "says", "supports", "support", "motion", "dismiss",
    "client", "memo", "argument", "accordingly", "therefore", "thus", "so", "then", "there", "these", "those",
    "when", "where", "while", "may", "can", "will", "would", "should", "could", "such", "any", "both", "each",
    "itself", "them", "they", "he", "she", "his", "her", "supreme",
}

ADVOCACY = [
    (r"\bslam[\s-]?dunk\b", "\"slam dunk\""),
    (r"\b(?:absolutely|definitely|certainly|undoubtedly|unquestionably|obviously)\b", "certainty language"),
    (r"\bclearly\b(?!\s+established)", "\"clearly\""),
    (r"\bwithout\s+(?:a|any)\s+doubt\b", "\"without a doubt\""),
    (r"\b(?:can(?:not|'t)\s+lose|sure\s+(?:win|thing)|open[\s-]and[\s-]shut|airtight|no[\s-]brainer)\b", "a guaranteed-win claim"),
    (r"\bguarantee[sd]?\s+(?:a\s+|our\s+|that\s+we\s+)?(?:win|victory|success|we\s+win)", "a guaranteed-win claim"),
    (r"\b(?:outrageous|ridiculous|disgraceful|idiotic|idiots?|pathetic|absurd|laughable|shameful)\b", "emotional language"),
    (r"!{2,}", "repeated exclamation marks"),
    (r"\b[A-Z]{5,}\b(?=[^.]*!)", "shouting"),
]

DOCTRINE_NOUN = re.compile(r"\s+(?:warnings?|rights|rules?|stops?|frisks?|hearings?|doctrine|standard|test|analysis|violations?|claims?|issues?|motions?|line)\b", re.I)

# ---------------------------------------------------------------- helpers


def _words(s):
    return re.findall(r"[A-Za-z][A-Za-z'.]*", s)


def _content(s):
    out = []
    for w in re.findall(r"[a-z]+", s.lower()):
        if len(w) < 3 or w in STOP:
            continue
        out.append(w[:4])
    return out


def _coverage(segment_tokens, holding):
    if not segment_tokens:
        return 0.0
    h = set(_content(holding))
    return sum(t in h for t in segment_tokens) / len(segment_tokens)


def _norm_rep(r):
    return re.sub(r"[\s.]", "", r).lower()


def _party_tokens(p):
    return [w for w in re.findall(r"[a-z0-9']+", p.lower()) if w not in SUFFIX and w not in ("the", "of", "and", "&")]


def _entries(sot):
    """Yield (kind, entry) from every list in the persona data; kind is trusted or fabricated."""
    for stem, data in sot.items():
        kind = "fabricated" if any(w in stem.lower() for w in ("fabricat", "fake", "bogus")) else "trusted"
        cases = data.get("cases", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        for c in cases:
            if isinstance(c, dict) and c.get("case"):
                yield kind, c


def _prepare(sot):
    out = []
    for kind, c in _entries(sot):
        name = c["case"]
        parts = re.split(r"\s+vs?\.?\s+", name, maxsplit=1)
        p1, p2 = (parts[0], parts[1]) if len(parts) == 2 else (name, "")
        t1, t2 = _party_tokens(p1), _party_tokens(p2)
        shorts = []
        dist1 = [w for w in t1 if w not in GENERIC_PARTY]
        if t1 and t1[0] not in GENERIC_PARTY:
            shorts.append(" ".join(w.capitalize() for w in t1))   # "New York Times", "Erie", "Gideon"
            shorts.append(t1[0].capitalize())
        elif dist1:
            shorts.append(dist1[0].capitalize())
        if (len(t1) >= 3 or not dist1) and t2 and t2[0] not in GENERIC_PARTY:
            shorts.append(t2[0].capitalize())                     # "Sullivan"
        rep = None
        m = REPORTER_RE.search(str(c.get("citation", "")))
        if m:
            rep = (m.group(1), _norm_rep(m.group(2)), m.group(3))
        full_re = None
        if t1 and t2:
            full_re = re.compile(r"\b" + re.escape(p1.split()[0]) + r"\b[^;\n]{0,45}?\s+vs?\.?\s+(?:the\s+)?" + re.escape(p2.split()[0]) + r"\b(?:\s+(?:[A-Z][\w.&'-]*|of|the|and|&))*", 0)
        out.append({"kind": kind, "case": name, "t1": t1, "t2": t2, "shorts": list(dict.fromkeys(s for s in shorts if len(s) >= 4)),
                    "full_re": full_re, "rep": rep, "citation": c.get("citation"), "year": str(c.get("year") or "") or None,
                    "holding": c.get("holding", ""), "note": c.get("note", "")})
    return out


def _sentence_bounds(text, pos):
    start = max(text.rfind(". ", 0, pos), text.rfind("! ", 0, pos), text.rfind("? ", 0, pos), text.rfind("\n", 0, pos))
    start = 0 if start < 0 else start + 1
    ends = [i for i in (text.find(". ", pos), text.find("! ", pos), text.find("? ", pos), text.find("\n", pos)) if i >= 0]
    end = min(ends) + 1 if ends else len(text)
    return start, end


def _abbrev_safe_sentence(text, pos):
    """Sentence bounds that ignore the periods inside 'v.', 'U.S.', 'Co.', 'F.3d'."""
    masked = re.sub(r"\b(v|vs|U\.S|Co|Inc|Corp|Ltd|No|Cir|App|Ct|F|Supp|Cal|N\.Y|Ill|Mr|Ms|Dr|St|Jr)\.", lambda m: m.group(0).replace(".", "\x00"), text)
    masked = re.sub(r"(\d)\.(\d)", "\\1\x00\\2", masked)
    masked = re.sub(r"\.(?=\d|[a-z]{1,2}\b)", "\x00", masked)
    return _sentence_bounds(masked, pos)


# ---------------------------------------------------------------- main check


def check(text, sot):
    entries = _prepare(sot)
    findings = []
    mentions = []  # dicts with start, end, entry or None, label
    taken = []

    def overlaps(a, b):
        return any(a < e and s < b for s, e in taken)

    # 1. full names of listed cases
    for e in entries:
        if e["full_re"]:
            for m in e["full_re"].finditer(text):
                if not overlaps(m.start(), m.end()):
                    taken.append((m.start(), m.end()))
                    mentions.append({"start": m.start(), "end": m.end(), "entry": e, "as": m.group(0)})

    # 2. any other "X v. Y" is either a near-miss of a listed case or unknown
    for m in CASE_RE.finditer(text):
        p1w = [w for w in m.group(1).split() if w.lower().strip(",") not in LEAD]
        if not p1w:
            continue
        p1 = " ".join(p1w[-8:])
        start = text.find(p1w[0], m.start())
        p2w = []
        for w in m.group(2).split():
            if p2w and (w.lower().strip(",.") in LEAD or w.lower().strip(",.") in ("court", "held", "holds")):
                break
            p2w.append(w)
            if w.endswith((",", ";")):
                break
        p2 = " ".join(p2w).rstrip(",;")
        end = text.find(p2, start) + len(p2) if p2 and text.find(p2, start) >= 0 else m.end()
        if overlaps(start, end):
            continue
        a, b = _party_tokens(p1), _party_tokens(p2)
        if not a or not b:
            continue
        near = None
        for e in entries:
            if not e["t1"] or not e["t2"]:
                continue
            s1 = difflib.SequenceMatcher(None, a[0], e["t1"][0]).ratio()
            s2 = difflib.SequenceMatcher(None, b[0], e["t2"][0]).ratio()
            if (a[0] == e["t1"][0] and a[0] not in GENERIC_PARTY) or (s1 >= 0.8 and s2 >= 0.8) or (b[0] == e["t2"][0] and s1 >= 0.75):
                near = e
                break
        taken.append((start, end))
        mentions.append({"start": start, "end": end, "entry": near, "as": text[start:end], "near_name": bool(near)})

    # 3. short names ("Gideon", "In Varghese, ...")
    for e in entries:
        for s in e["shorts"]:
            for m in re.finditer(r"\b" + re.escape(s) + r"\b", text):
                # "Miranda warnings", "Terry stop": a trusted case's name used as a doctrine label is not a
                # citation, so it gets no holding check. Fabricated names are always treated as citations.
                if e["kind"] == "trusted" and DOCTRINE_NOUN.match(text, m.end()):
                    continue
                if not overlaps(m.start(), m.end()):
                    taken.append((m.start(), m.end()))
                    mentions.append({"start": m.start(), "end": m.end(), "entry": e, "as": m.group(0), "short": True})

    # 4. bare reporter cites that identify a trusted case ("372 U.S. 335" with no name)
    reps = [r for r in REPORTER_RE.finditer(text) if "." in r.group(2) or r.group(2).strip().isupper()]
    mentions.sort(key=lambda x: x["start"])
    for r in reps:
        owner = None
        for mt in mentions:
            if mt["end"] <= r.start() and r.start() - mt["end"] <= 60 and not any(o["start"] > mt["end"] and o["start"] < r.start() for o in mentions):
                owner = mt
        if owner is not None:
            owner.setdefault("reps", []).append(r)
        else:
            key = (r.group(1), _norm_rep(r.group(2)), r.group(3))
            e = next((x for x in entries if x["rep"] == key), None)
            mentions.append({"start": r.start(), "end": r.end(), "entry": e, "as": r.group(0), "reps": [r], "bare": True})
    mentions.sort(key=lambda x: x["start"])

    # 5. judge each mention
    for i, mt in enumerate(mentions):
        e, cited = mt["entry"], mt["as"].strip()
        nxt = mentions[i + 1]["start"] if i + 1 < len(mentions) else len(text)
        tail_start = mt["reps"][-1].end() if mt.get("reps") else mt["end"]
        yr = None
        pm = PAREN_YEAR_RE.match(text, tail_start, min(nxt, tail_start + 50)) or PAREN_YEAR_RE.search(text, tail_start, min(nxt, tail_start + 50))
        if pm and pm.start() - tail_start <= 3:
            yr, tail_start = pm.group(1), pm.end()
        else:
            ny = NEAR_YEAR_RE.match(text[tail_start:min(nxt, tail_start + 30)])
            if ny:
                yr, tail_start = ny.group(1), tail_start + ny.end()
        if mt.get("reps") or yr:
            cited = text[mt["start"]:tail_start].strip().rstrip(",")
        ev = {"cited": cited, "status": None, "firm_list_entry": None, "note": ""}

        if e is None:
            ev["status"] = "unverified"
            findings.append((HOLD, f"{cited} is on neither the trusted nor the fabricated list, so it needs a human cite-check", ev))
            continue
        ev["firm_list_entry"] = f"{e['case']}" + (f", {e['citation']} ({e['year']})" if e.get("citation") else "")
        ev["note"] = e["note"] or e["holding"]
        if e["kind"] == "fabricated":
            ev["status"] = "fabricated"
            findings.append((BLOCK, f"{cited} is on the firm's fabricated-case list ({e['note'] or 'flagged as fabricated'})", ev))
            continue
        if mt.get("near_name"):
            ev["status"] = "near_miss"
            findings.append((BLOCK, f"\"{cited}\" misnames the trusted case {e['case']}", ev))
            continue
        problems = []
        for r in mt.get("reps", []):
            key = (r.group(1), _norm_rep(r.group(2)), r.group(3))
            if e["rep"] and key != e["rep"]:
                problems.append(f"the memo cites {r.group(0).strip()} but the firm's list has {e['citation']}")
        if yr and e["year"] and yr != e["year"]:
            problems.append(f"the memo dates it {yr} but the firm's list has {e['year']}")
        # holding: the clause attached to this mention
        seg = text[tail_start:nxt]
        s0, s1 = _abbrev_safe_sentence(text, mt["start"])
        seg = seg[: max(0, s1 - tail_start)] if s1 > tail_start else ""
        seg = re.sub(r"^[\s,;:]+", "", seg)
        cut = None
        for cm in re.finditer(r",", seg):
            if len(_content(seg[: cm.start()])) >= 3:
                cut = cm.start()
                break
        seg = seg[:cut] if cut else seg
        toks = _content(seg)
        if len(toks) < 2:
            prev_end = mentions[i - 1]["end"] if i > 0 else 0
            before = text[max(s0, prev_end):mt["start"]]
            toks = _content(before)
            seg = before.strip()
        if len(toks) >= 2 and e["holding"]:
            own = _coverage(toks, e["holding"])
            others = [(x, _coverage(toks, x["holding"])) for x in entries if x is not e and x["kind"] == "trusted" and x["holding"]]
            best, bcov = max(others, key=lambda o: o[1]) if others else (None, 0)
            claims_holding = bool(re.search(r"\b(held|holds|holding|establish\w*|requir\w*|appl\w+|means|protect\w*|allow\w*|permit\w*|found|ruled|recogni\w*|extend\w*|says|provid\w*|must|guarantee\w*|bars?|prohibit\w*|forbid\w*|mandat\w*|stands for|inadmissible|admissible|violat\w*)\b", seg, re.I)) or mt["start"] > s0 + 1 and re.match(r"\s*(under|per|following|applying)\b", text[s0:mt["start"]], re.I)
            if own < 0.5 and (claims_holding or (best and bcov >= 0.6)):
                problems.append(f"the memo says it held \"{seg.strip(' .,')}\", but the firm's list says: {e['holding']}"
                                + (f" (that statement belongs to {best['case']})" if best and bcov >= 0.6 else ""))
            elif best and bcov >= 0.6 and bcov > own + 0.2:
                problems.append(f"the stated holding matches {best['case']}, not {e['case']}")
            elif own < 0.8 and claims_holding and re.search(r"\b(every|all|always|any\s+case|without\s+exception|in\s+all|categorically|absolute)\b", seg, re.I) \
                    and not re.search(r"\b(every|all|always|any)\b", e["holding"], re.I):
                problems.append(f"the memo overstates the holding as \"{seg.strip(' .,')}\"; the firm's list says: {e['holding']}")
            elif claims_holding and re.search(r"\b(not|never|no|cannot)\b|n't\b", seg, re.I) \
                    and not re.search(r"\b(not|never|no|unless|only|inadmissible)\b", e["holding"], re.I):
                problems.append(f"the memo negates the holding (\"{seg.strip(' .,')}\"); the firm's list says: {e['holding']}")
        if problems:
            ev["status"] = "near_miss"
            findings.append((BLOCK, f"{cited}: " + "; ".join(problems), ev))
        else:
            ev["status"] = "verified"
            findings.append((None, f"{cited} matches the firm's trusted list", ev))

    # 6. advocacy language (rule 4)
    hits = []
    for pat, label in ADVOCACY:
        m = re.search(pat, text, flags=0 if label == "shouting" else re.I)
        if m:
            hits.append(f"{label} (\"{m.group(0)}\")")
    if hits:
        findings.append((HOLD, "the memo reads as advocacy, not analysis: " + ", ".join(hits[:3]),
                         {"signal": "rule_check", "rule": "Memos are not advocacy (business rule 4)", "matches": hits}))
    return findings
