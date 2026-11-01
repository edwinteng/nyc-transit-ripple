"""Explanation consistency checker (M6, part A5).

Every number in every generated sentence (event explanations and recommendation
texts) must be one of the metrics stored with that sentence. Violations are
listed; ``ripple evaluate --check-explanations`` exits with an error when there is
any, and the test suite runs the same check over every template variant so CI
fails if a template can produce an uncited number.
"""

from __future__ import annotations

import json

import pandas as pd

from ..explain import sentences_of, uncited_numbers
from .common import STATUS_INSUFFICIENT, STATUS_OK, Metric, Section, day


def check_sentences(sentences: list[dict]) -> list[dict]:
    """Violations in a list of {"text", "metrics"} sentences."""
    out = []
    for i, s in enumerate(sentences):
        bad = uncited_numbers(s.get("text", ""), s.get("metrics") or {})
        if bad:
            out.append({"sentence": i, "text": s.get("text", ""), "uncited": bad})
    return out


def find_violations(con, start, end) -> tuple[list[dict], int, int]:
    """(violations, n_event_sentences, n_recommendation_texts) in the period."""
    violations = []
    events = con.execute(
        "SELECT * FROM events_all WHERE computed_utc >= $s AND computed_utc < $e",
        {"s": start, "e": end},
    ).df()
    n_sentences = 0
    for ev in events.to_dict("records"):
        sentences = sentences_of(ev)
        n_sentences += len(sentences)
        for v in check_sentences(sentences):
            violations.append({"kind": "event", "id": ev["event_id"], **v})
    recs = con.execute(
        "SELECT event_id, station_id, option_id, text, metrics FROM recommendations "
        "WHERE computed_utc >= $s AND computed_utc < $e",
        {"s": start, "e": end},
    ).df()
    for r in recs.to_dict("records"):
        metrics = json.loads(r["metrics"]) if isinstance(r["metrics"], str) else {}
        for v in check_sentences([{"text": r["text"], "metrics": metrics}]):
            violations.append(
                {"kind": "recommendation", "id": f"{r['event_id']}/{r['option_id']}", **v}
            )
    return violations, n_sentences, len(recs)


def evaluate_explanations(con, settings, start, end) -> Section:
    violations, n_sent, n_rec = find_violations(con, start, end)
    n = n_sent + n_rec
    section = Section(
        "explanations",
        "Explanation consistency",
        STATUS_OK if n else STATUS_INSUFFICIENT,
        day(start),
        day(end - pd.Timedelta(seconds=1)),
        "",
        metrics=[
            Metric(
                "violations",
                "Sentences with a number not in their metrics",
                len(violations),
                n,
                "sentences",
            ),
        ],
        notes=[
            "Every stored event sentence and recommendation text is re-checked: each number "
            "in the text must equal one of the metrics stored with it (quoted alert text "
            "excepted). Any violation fails the evaluation workflow; the test suite runs "
            "the same check over every template variant."
        ],
    )
    section.tables["violations"] = [
        {**v, "uncited": ", ".join(v["uncited"])} for v in violations[:100]
    ]
    section.summary = (
        f"{len(violations)} violation(s) in {n_sent} event sentence(s) and {n_rec} "
        "recommendation text(s)."
    )
    return section
