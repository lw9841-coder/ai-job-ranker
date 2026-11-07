"""Turns job scores into an apply-in-this-order list."""
import hashlib
import json
import re


def sid(job_id):
    """Short id for the Applied/Skip buttons."""
    return hashlib.sha1(job_id.encode()).hexdigest()[:10]


AGENCY = re.compile(r"staffing|recruit|talent (group|solutions|partners|acquisition)|jobgether|lensa|cybercoders|"
                    r"robert half|insight global|teksystems|kforce|apex systems|randstad|adecco|manpower|"
                    r"kelly services|aerotek|motion recruitment|bright vision", re.I)
SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|plc)\b\.?", re.I)


def key(company, title):
    """Same company + title = same job, even when two boards spell the company differently."""
    company = SUFFIX.sub("", re.sub(r"\bby\b.*$", "", company or "", flags=re.I))
    title = re.sub(r"\(.*?\)|[-|–]\s*(remote|us|usa|united states).*$", "", title or "", flags=re.I)
    return re.sub(r"\W+", " ", f"{company} | {title}").lower().strip()


def is_agency(s):
    return s.get("agency") == "yes" or bool(AGENCY.search(s.get("company") or ""))


def composite(s):
    """Mutual fit counts 70%, job quality 30%; a job loses half a point if either side is weak."""
    a, b, q = s["they_pick_him"], s["he_wants_it"], s["job_quality"]
    penalty = (0.5 if min(a, b) <= 2 else 0) + (0.5 if is_agency(s) else 0)  # agencies hide the real employer
    return round(0.7 * (a + b) / 2 + 0.3 * q - penalty, 2)


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
                + (" · location unclear, check it" if s.get("location_ok") == "unclear" else "")
                + (" · staffing agency" if is_agency(s) else "") + "  ",
                f"   {s.get('why', '')}" + (f" Gaps: {gaps[:160]}" if gaps else ""), ""]
    if waiting:
        out += [f"_{waiting} new jobs are waiting for a score and will be ranked on the next run._", ""]
    for kind, label in (("applied", "Applied"), ("skip", "Skipped")):
        done = [m for m in marks["ids"].values() if m.get("mark") == kind]
        if done:
            out += [f"## {label} ({len(done)})", ""] + [f"- {m['date']}: {m['title']}" for m in done] + [""]
    path.write_text("\n".join(out))
    # The same order as data, for the apply step (apply/apply.py)
    rows = [{"sid": sid(j["id"]), "id": j["id"], "company": j["company"], "title": j["title"], "url": j["url"],
             "pay": pay_text(j, s), "scores": [s["they_pick_him"], s["he_wants_it"], s["job_quality"]],
             "location_ok": s.get("location_ok"), "agency": is_agency(s)} for j, s in ranked]
    path.with_suffix(".json").write_text(json.dumps({"updated": stamp, "jobs": rows}, indent=1))
