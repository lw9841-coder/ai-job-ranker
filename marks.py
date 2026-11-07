"""Reads button taps from a private ntfy topic (ntfy keeps them 12 hours): Applied / Skip on job alerts, and
Interview / Rejected / No reply on the Monday "heard back?" check. Also handles marks made through Claude."""
import csv
import json
import re
import urllib.request
from datetime import datetime

from ranker import key

KINDS = ("applied", "skip", "interview", "rejected", "noreply", "offer")
OUTCOMES = ("interview", "rejected", "noreply", "offer")
MARK = re.compile(rf"^\s*({'|'.join(KINDS)})\s+([0-9a-f]{{10}})\s*$", re.I)


def poll(server, topic, since, timeout=30):
    req = urllib.request.Request(f"{server}/{topic}/json?poll=1&since={since}",
                                 headers={"User-Agent": "job-watch/1.0 (personal job alerts)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        lines = r.read().decode("utf-8", "replace").splitlines()
    msgs = [json.loads(line) for line in lines if line.strip()]
    return [(m.get("time", 0), m.get("message", "")) for m in msgs if m.get("event") == "message"]


def record(marks, s, kind, day, info):
    """Record one mark. Outcomes keep the job's 'applied' status and add the outcome on top."""
    entry = marks["ids"].setdefault(s, {"mark": "applied" if kind in OUTCOMES else kind, "date": day,
                                        "url": info.get("url", ""),
                                        "title": f"{info.get('company', '?')}: {info.get('title', '?')}"})
    if kind in OUTCOMES:
        entry["outcome"], entry["outcome_date"] = kind, day
        entry.setdefault("mark", "applied")
    else:
        entry["mark"], entry["date"] = kind, day
    if info:
        marks["keys"][key(info["company"], info["title"])] = entry["mark"]
    url = info.get("url", "")
    if kind == "applied":
        return url, {"applied": day}
    if kind == "skip":
        return url, {"applied": "skipped"}
    return url, {"outcome": f"{kind} {day}"}


def apply_marks(msgs, marks, scores_by_sid):
    """Record each tap; returns {url: {column: value}} for the job log."""
    log_updates = {}
    for t, text in msgs:
        m = MARK.match(text or "")
        marks["since"] = max(marks.get("since", 0), int(t) + 1)
        if not m:
            continue
        url, cols = record(marks, m.group(2), m.group(1).lower(),
                           datetime.fromtimestamp(t).strftime("%Y-%m-%d"), scores_by_sid.get(m.group(2), {}))
        if url:
            log_updates.setdefault(url, {}).update(cols)
    return log_updates


def mark_csv(csv_path, updates):
    """Fill the job log's 'applied' and 'outcome' columns for these links (adds 'outcome' if missing)."""
    if not csv_path.exists() or not updates:
        return
    with csv_path.open(newline="") as f:
        rows = list(csv.reader(f))
    if "outcome" not in rows[0]:
        rows[0].append("outcome")
        for r in rows[1:]:
            r.append("")
    link = rows[0].index("link")
    cols = {name: rows[0].index(name) for name in ("applied", "outcome")}
    for r in rows[1:]:
        for name, value in updates.get(r[link], {}).items():
            i = cols[name]
            if name == "outcome" or not r[i]:
                r[i] = value
    tmp = csv_path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        csv.writer(f).writerows(rows)
    tmp.replace(csv_path)


def outcome_notes(marks, scores_by_sid, minimum=5):
    """Once there are enough real outcomes, tell the scorer which kinds of jobs answered the candidate."""
    done = [(s, m) for s, m in marks["ids"].items() if m.get("outcome") in ("interview", "offer", "rejected")]
    if len(done) < minimum:
        return ""
    lines = ["", "# What has happened so far (use it to calibrate they_pick_him)"]
    for s, m in done:
        sc = scores_by_sid.get(s, {})
        scores = f" (scored {sc.get('they_pick_him')}/{sc.get('he_wants_it')}/{sc.get('job_quality')})" if sc else ""
        lines.append(f"- {m['outcome']}: {m['title']}{scores}")
    return "\n".join(lines)
