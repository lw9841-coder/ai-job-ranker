"""Reads Applied / Skip button taps from a private ntfy topic (ntfy keeps them 12 hours)."""
import csv
import json
import re
import urllib.request
from datetime import datetime

from ranker import key

MARK = re.compile(r"^\s*(applied|skip)\s+([0-9a-f]{10})\s*$", re.I)


def poll(server, topic, since, timeout=30):
    req = urllib.request.Request(f"{server}/{topic}/json?poll=1&since={since}",
                                 headers={"User-Agent": "job-watch/1.0 (personal job alerts)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        lines = r.read().decode("utf-8", "replace").splitlines()
    msgs = [json.loads(line) for line in lines if line.strip()]
    return [(m.get("time", 0), m.get("message", "")) for m in msgs if m.get("event") == "message"]


def apply_marks(msgs, marks, scores_by_sid):
    """Record each tap; returns {url: 'YYYY-MM-DD' or 'skipped'} for the job log."""
    log_updates = {}
    for t, text in msgs:
        m = MARK.match(text or "")
        marks["since"] = max(marks.get("since", 0), int(t) + 1)
        if not m:
            continue
        kind, s = m.group(1).lower(), m.group(2)
        info = scores_by_sid.get(s, {})
        day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        marks["ids"][s] = {"mark": kind, "date": day, "url": info.get("url", ""),
                           "title": f"{info.get('company', '?')}: {info.get('title', '?')}"}
        if info:
            marks["keys"][key(info["company"], info["title"])] = kind
            log_updates[info.get("url", "")] = day if kind == "applied" else "skipped"
    return log_updates


def mark_csv(csv_path, updates):
    """Fill the empty 'applied' cells for these links."""
    if not csv_path.exists() or not updates:
        return
    with csv_path.open(newline="") as f:
        rows = list(csv.reader(f))
    link, applied = rows[0].index("link"), rows[0].index("applied")
    for r in rows[1:]:
        if len(r) > applied and r[link] in updates and not r[applied]:
            r[applied] = updates[r[link]]
    tmp = csv_path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        csv.writer(f).writerows(rows)
    tmp.replace(csv_path)
