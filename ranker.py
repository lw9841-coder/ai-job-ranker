"""Turns job scores into an apply-in-this-order list."""
import hashlib
import re


def sid(job_id):
    """Short id for the Applied/Skip buttons."""
    return hashlib.sha1(job_id.encode()).hexdigest()[:10]


def key(company, title):
    return re.sub(r"\W+", " ", f"{company} {title}").lower().strip()


def composite(s):
    """Mutual fit counts 70%, job quality 30%; a job loses half a point if either side is weak."""
    a, b, q = s["they_pick_him"], s["he_wants_it"], s["job_quality"]
    return round(0.7 * (a + b) / 2 + 0.3 * q - (0.5 if min(a, b) <= 2 else 0), 2)


def eligible(s):
    return s["they_pick_him"] > 1 and s.get("location_ok") != "no"


def is_marked(job, marks):
    return sid(job["id"]) in marks["ids"] or key(job["company"], job["title"]) in marks["keys"]


def rank(open_jobs, scores, marks, first_seen):
    ranked = [(j, scores[j["id"]]) for j in open_jobs
              if j["id"] in scores and eligible(scores[j["id"]]) and not is_marked(j, marks)]
    ranked.sort(key=lambda js: first_seen.get(js[0]["id"], ""), reverse=True)  # newest first...
    ranked.sort(key=lambda js: (-composite(js[1]), -js[1]["they_pick_him"]))   # ...survives as the tie-break
    return ranked


def pay_text(job, s):
    p = str(s.get("pay") or "")
    if not p or p.lower() == "not listed":
        p = job.get("pay") or "pay not listed"
    p = re.sub(r"^(salary range|pay range|compensation range|base salary|salary)\s*:?\s*", "", p.strip(), flags=re.I)
    hourly = bool(re.search(r"/\s?h(ou)?r\b|hourly|per hour|an hour", p, re.I))
    p = re.split(r"\s\+\s|\(|\bplus\b|\bdepending\b|\bper year\b|\bper hour\b|\bhourly\b|/\s?h(?:ou)?r\b", p, flags=re.I)[0]
    p = p.strip().rstrip(".;,/")
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", p)]
    if nums and (hourly or max(nums) < 500):
        p += "/hr"
    return p[:45]


def write_ranked_md(path, ranked, marks, stamp, waiting=0):
    out = ["# Apply in this order", "",
           f"Updated {stamp}. {len(ranked)} open jobs ranked. Scores out of 5: they'd pick you / you'd want it / good job.",
           "Tap a title to open the posting.", ""]
    for n, (j, s) in enumerate(ranked, 1):
        gaps = s.get("gaps") or []
        gaps = "; ".join(gaps) if isinstance(gaps, list) else str(gaps)
        out += [f"{n}. **[{j['company']}: {j['title']}]({j['url']})** · {pay_text(j, s)} · "
                f"{s['they_pick_him']}/{s['he_wants_it']}/{s['job_quality']}"
                + (" · location unclear, check it" if s.get("location_ok") == "unclear" else "") + "  ",
                f"   {s.get('why', '')}" + (f" Gaps: {gaps[:160]}" if gaps else ""), ""]
    if waiting:
        out += [f"_{waiting} new jobs are waiting for a score and will be ranked on the next run._", ""]
    for kind, label in (("applied", "Applied"), ("skip", "Skipped")):
        done = [m for m in marks["ids"].values() if m.get("mark") == kind]
        if done:
            out += [f"## {label} ({len(done)})", ""] + [f"- {m['date']}: {m['title']}" for m in done] + [""]
    path.write_text("\n".join(out))
