"""Runs inside .jobspy/ (Python 3.12 + python-jobspy 1.3.0). The main watcher calls it.

Reads a JSON list of searches on stdin and prints a JSON list of jobs. Each search is
{"site": "indeed"|"linkedin", "term": str, "location": str, "remote": bool, "results": int}.
A failed search is reported in "errors" and the rest still run.
"""
import json
import math
import sys

from jobspy import scrape_jobs


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v if isinstance(v, (str, int, float, bool)) else str(v)


def main():
    searches = json.load(sys.stdin)
    jobs, errors = [], []
    for s in searches:
        kw = dict(site_name=[s["site"]], search_term=s["term"], location=s["location"],
                  results_wanted=s.get("results", 25), country_indeed="USA", description_format="markdown")
        if s["site"] == "linkedin":
            kw["linkedin_fetch_description"] = True
            kw["hours_old"] = s.get("hours_old", 168)
        if s.get("remote"):
            kw["is_remote"] = True  # Indeed can't combine this with hours_old; LinkedIn ignores it, so we post-filter
        try:
            df = scrape_jobs(**kw)
        except Exception as e:
            errors.append(f"{s['site']} '{s['term']}' {s['location']}: {type(e).__name__}: {str(e)[:150]}")
            continue
        for rec in df.to_dict("records"):
            jobs.append({k: clean(rec.get(k)) for k in (
                "site", "id", "title", "company", "location", "is_remote", "job_url", "job_url_direct",
                "min_amount", "max_amount", "interval", "currency", "date_posted", "description")})
    json.dump({"jobs": jobs, "errors": errors}, sys.stdout)


if __name__ == "__main__":
    main()
