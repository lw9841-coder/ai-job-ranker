#!/usr/bin/env python3
"""AI job ranker: checks companies' own job boards, three remote job feeds, and
LinkedIn and Indeed searches, keeps the roles that fit one candidate's search,
has Claude score each new posting against a written profile, and sends the
ranked matches to a phone through ntfy. The watcher itself is standard library only.

  python3 job_watch.py            normal run: alert on new matches
  python3 job_watch.py --dry-run  print matches only; send and save nothing
  python3 job_watch.py --seed     first run: mark everything open now as seen,
                                  write open-now.md, send one summary alert
"""
import csv
import fcntl
import gzip
import html
import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG_FILE = HERE / "config.json" if (HERE / "config.json").exists() else HERE / "config.example.json"
CFG = json.loads(CFG_FILE.read_text())
PROFILE_FILE = HERE / "profile.md" if (HERE / "profile.md").exists() else HERE / "profile.example.md"
SEEN_FILE = HERE / "seen.json"
LOG_CSV = HERE / "jobs-log.csv"
UA = "job-watch/1.0 (personal job alerts)"
NOW = datetime.now(timezone.utc)
BYTES = [0]
sys.path.insert(0, str(HERE))  # -I drops the script folder from the path; our own modules live here
import marks as marks_mod  # noqa: E402
import ranker  # noqa: E402
import scorer  # noqa: E402
TODAY = NOW.astimezone().strftime("%Y-%m-%d")


def rx(patterns, flags=re.I):
    return re.compile("|".join(f"(?:{p})" for p in patterns), flags)


TITLE_IN = rx(CFG["title_include"])
GIG_IN = rx(CFG["gig_include"])
TITLE_OUT = rx(CFG["title_exclude"])
NON_US = rx(CFG["non_us_places"])
US_STRONG = rx(CFG["us_strong"])
BARE_US = re.compile(r"^\s*(united states( of america)?|us|usa|u\.s\.a?\.?|north america|americas|(us[- ]?)?nationwide|anywhere in (the )?usa?)\s*$", re.I)
DFW = rx(CFG["dallas_area"])
AI_SIGNAL = rx(CFG["ai_signal"], 0)  # case-sensitive on purpose: "AI", not "ai" inside words
PER_YEAR = {"year": 1, "annual": 1, "yearly": 1, "hour": 2080, "hourly": 2080,
            "month": 12, "monthly": 12, "week": 52, "weekly": 52, "day": 260, "daily": 260}


# ---------- fetching ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
        BYTES[0] += len(data)
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def plain(h):
    t = re.sub(r"(?s)<(style|script).*?</\1>", "", html.unescape(h or ""))
    t = re.sub(r"<br\s*/?>|</(p|li|h\d|div|ul|ol)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def annual(value, period):
    if value in (None, "", 0, "0"):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    p = (period or "year").lower()
    mult = next((m for k, m in PER_YEAR.items() if k in p), 1)
    return v * mult


def money(lo, hi):
    def k(v):
        return f"${v / 1000:.0f}k"
    if lo and hi:
        return f"{k(lo)}-{k(hi)}"
    return k(hi or lo) if (hi or lo) else ""


def job(**kw):
    base = dict(remote=False, onsite=False, non_us_country=False, pay="", pay_max=None, pay_min=None,
                currency="USD", feed=False, ai_text="", loc_text="", where="", text="")
    base.update(kw)
    return base


# ---------- sources: companies' own job boards ----------

def greenhouse(token, company):
    for j in json.loads(fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"))["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        yield job(id=f"gh:{token}:{j['id']}", company=company, title=j["title"].strip(),
                  loc_text=loc, where=loc, remote="remote" in loc.lower() or bool(BARE_US.match(loc)),
                  url=j["absolute_url"], source=f"{company} job board")


def ashby(token, company):
    data = json.loads(fetch(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"))
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        lo = hi = None
        cur = "USD"
        tiers = sorted(comp.get("compensationTiers") or [],
                       key=lambda t: 0 if re.search(r"united states|\bUS\b", t.get("title") or "", re.I) else 1)
        for tier in tiers:
            for c in tier.get("components") or []:
                if c.get("compensationType") == "Salary" and (c.get("minValue") or c.get("maxValue")):
                    cur = c.get("currencyCode") or "USD"
                    lo = annual(c.get("minValue"), c.get("interval"))
                    hi = annual(c.get("maxValue"), c.get("interval"))
                    break
            if lo or hi:
                break
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        secondary = [s.get("location", "") for s in j.get("secondaryLocations") or []]
        tier_titles = [t.get("title") or "" for t in tiers]
        loc_text = " | ".join([j.get("location", ""), addr.get("addressCountry", ""), *secondary, *tier_titles])
        wp = (j.get("workplaceType") or "").lower()
        yield job(id=f"ashby:{token}:{j['id']}", company=company, title=j["title"].strip(),
                  loc_text=loc_text, where=j.get("location", "") + (f" ({j['workplaceType']})" if wp and wp not in j.get("location", "").lower() else ""),
                  remote=(wp == "remote") if wp else bool(j.get("isRemote")), onsite=wp == "onsite",
                  pay=(money(lo, hi) if cur == "USD" else (comp.get("scrapeableCompensationSalarySummary") or "")),
                  pay_max=(hi or lo) if cur == "USD" else None, pay_min=(lo or hi) if cur == "USD" else None,
                  currency=cur, url=j.get("jobUrl"), source=f"{company} job board", text=j.get("descriptionPlain") or "")


def lever(token, company):
    for j in json.loads(fetch(f"https://api.lever.co/v0/postings/{token}?mode=json")):
        cat = j.get("categories") or {}
        loc_text = " | ".join([cat.get("location") or "", *(cat.get("allLocations") or [])])
        sr = j.get("salaryRange") or {}
        cur = sr.get("currency") or "USD"
        lo, hi = annual(sr.get("min"), sr.get("interval")), annual(sr.get("max"), sr.get("interval"))
        wp = (j.get("workplaceType") or "").lower()
        yield job(id=f"lever:{token}:{j['id']}", company=company, title=j["text"].strip(),
                  loc_text=loc_text, where=(cat.get("location") or "") + (f" ({wp})" if wp and wp != "unspecified" else ""),
                  remote=wp == "remote" or (wp in ("", "unspecified") and "remote" in loc_text.lower()),
                  onsite=wp == "onsite",
                  non_us_country=bool(j.get("country")) and j.get("country") != "US",
                  pay=money(lo, hi) if cur == "USD" else "", pay_max=(hi or lo) if cur == "USD" else None,
                  pay_min=(lo or hi) if cur == "USD" else None, currency=cur, url=j.get("hostedUrl"), source=f"{company} job board",
                  text="\n".join([j.get("descriptionPlain") or "", j.get("additionalPlain") or "",
                                   *(f"{x.get('text', '')}:\n{plain(x.get('content'))}" for x in j.get("lists") or [])]))


def greenhouse_text(j):
    _, token, jid = j["id"].split(":", 2)
    return plain(json.loads(fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}")).get("content"))


# ---------- sources: remote job feeds (credited, links go back to them) ----------

def himalayas(query):
    q = urllib.parse.quote(query)
    for j in json.loads(fetch(f"https://himalayas.app/jobs/api/search?q={q}&country=US&sort=recent")).get("jobs", []):
        cur = j.get("currency") or "USD"
        lo, hi = annual(j.get("minSalary"), j.get("salaryPeriod")), annual(j.get("maxSalary"), j.get("salaryPeriod"))
        yield job(id=f"him:{j.get('guid')}", company=j.get("companyName", "").strip(), title=j.get("title", "").strip(),
                  where="Remote (US)", remote=True, feed=True,
                  pay=money(lo, hi) if cur == "USD" else "", pay_max=(hi or lo) if cur == "USD" else None,
                  pay_min=(lo or hi) if cur == "USD" else None, currency=cur,
                  ai_text=" ".join(str(j.get(k, "")) for k in ("excerpt", "description", "categories", "companyName")),
                  url=j.get("applicationLink") or j.get("guid"), source="Himalayas",
                  text=f"Location restrictions: {j.get('locationRestrictions')}\n" + plain(j.get("description")))


def jobicy():
    for j in json.loads(fetch("https://jobicy.com/api/v2/remote-jobs?count=100&geo=usa")).get("jobs", []):
        cur = j.get("salaryCurrency") or "USD"
        lo, hi = annual(j.get("salaryMin"), j.get("salaryPeriod")), annual(j.get("salaryMax"), j.get("salaryPeriod"))
        yield job(id=f"jobicy:{j.get('id')}", company=html.unescape(j.get("companyName", "")).strip(),
                  title=html.unescape(j.get("jobTitle", "")).strip(), where=f"Remote ({j.get('jobGeo', 'USA')})",
                  loc_text=j.get("jobGeo", ""), remote=True, feed=True,
                  pay=money(lo, hi) if cur == "USD" else "", pay_max=(hi or lo) if cur == "USD" else None,
                  pay_min=(lo or hi) if cur == "USD" else None, currency=cur,
                  ai_text=" ".join(str(j.get(k, "")) for k in ("jobExcerpt", "jobDescription", "jobIndustry", "companyName")),
                  url=j.get("url"), source="Jobicy", text=plain(j.get("jobDescription")))


def weworkremotely():
    root = ET.fromstring(fetch("https://weworkremotely.com/remote-jobs.rss"))
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()
        company, _, title = get("title").partition(": ")
        region = " ".join(filter(None, [get("region"), get("country")]))
        if not re.search(r"anywhere|worldwide|usa|united states|north america|americas", region, re.I):
            continue
        yield job(id=f"wwr:{get('guid') or get('link')}", company=company.strip(), title=(title or company).strip(),
                  where=f"Remote ({get('region') or 'see post'})", loc_text=region, remote=True, feed=True,
                  ai_text=" ".join([get("description"), get("category"), get("skills"), company]),
                  url=get("link"), source="We Work Remotely", text=f"Region: {region}\n" + plain(get("description")))


# ---------- sources: LinkedIn and Indeed (big boards, for in-range jobs at regular companies) ----------

LI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def li_get(url, tries=2):
    """LinkedIn throttles fast callers; on a 429, wait a minute and try once more."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": LI_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                BYTES[0] += len(data)
                return data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            time.sleep(60)


def pay_from_text(text):
    nums = [float(n.replace(",", "")) for n in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)", text or "")]
    if not nums:
        return None, None
    period = "hour" if re.search(r"/\s?h(ou)?r|hourly|per hour", text, re.I) else "year"
    lo, hi = min(nums), max(nums)
    return annual(lo, period), annual(hi, period)


def parse_linkedin_cards(page):
    jobs = []
    for card in page.split("<li>")[1:]:
        g = lambda pat: (lambda m: html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else "")(re.search(pat, card, re.S))
        jid = g(r"urn:li:jobPosting:(\d+)")
        if jid:
            jobs.append({"id": jid, "title": g(r'base-search-card__title">(.*?)</h3>'),
                         "company": g(r'base-search-card__subtitle">(.*?)</h4>'),
                         "location": g(r'job-search-card__location">(.*?)</span>'),
                         "salary": g(r'job-search-card__salary-info">(.*?)</span>'),
                         "date": g(r'<time[^>]*datetime="([\d-]+)"')})
    return jobs


def linkedin():
    """Remote (f_WT=2) and Dallas-hybrid (f_WT=3) searches from the past week, paced a few seconds apart."""
    li = CFG["linkedin"]
    for search in li["searches"]:
        for start in range(0, li.get("pages", 2) * 10, 10):
            q = urllib.parse.urlencode({"keywords": search["term"], "location": search["location"],
                                        "f_WT": 2 if search.get("remote") else 3, "f_TPR": "r604800", "start": start})
            cards = parse_linkedin_cards(li_get(f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{q}"))
            time.sleep(4)
            for c in cards:
                lo, hi = pay_from_text(c["salary"])
                remote = bool(search.get("remote"))
                yield job(id=f"li:{c['id']}", company=c["company"], title=c["title"],
                          where=f"{c['location']} ({'Remote' if remote else 'Hybrid'})", loc_text=c["location"],
                          remote=remote, feed=True, floor=CFG.get("board_pay_floor"),
                          pay=money(lo, hi), pay_max=hi, pay_min=lo,
                          url=f"https://www.linkedin.com/jobs/view/{c['id']}", source="LinkedIn")
            if len(cards) < 10:
                break


def linkedin_text(j):
    page = li_get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{j['id'].split(':', 1)[1]}")
    m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
    return plain(m.group(1) if m else page)


def indeed():
    """Indeed through JobSpy, which runs in its own Python 3.12 folder so the watcher itself stays install-free."""
    ind = CFG["indeed"]
    p = subprocess.run([str(Path(ind["python"]).expanduser()), str(HERE / "jobspy_fetch.py")],
                       input=json.dumps([{"site": "indeed", **s} for s in ind["searches"]]),
                       capture_output=True, text=True, timeout=900)
    out = json.loads(p.stdout)
    for e in out.get("errors", []):
        print("  indeed:", e)
    for r in out.get("jobs", []):
        lo = annual(r.get("min_amount"), r.get("interval"))
        hi = annual(r.get("max_amount"), r.get("interval"))
        usd = (r.get("currency") or "USD") == "USD"
        loc = r.get("location") or ""
        yield job(id=f"in:{r['id']}", company=(r.get("company") or "").strip(), title=(r.get("title") or "").strip(),
                  where=f"{loc}{' (Remote)' if r.get('is_remote') else ''}", loc_text=loc,
                  remote=bool(r.get("is_remote")), feed=True, floor=CFG.get("board_pay_floor"),
                  pay=money(lo, hi) if usd else "", pay_max=(hi or lo) if usd else None,
                  pay_min=(lo or hi) if usd else None, currency=r.get("currency") or "USD",
                  ai_text=r.get("description") or "", text=r.get("description") or "",
                  url=r.get("job_url_direct") or r.get("job_url"), source="Indeed")


# ---------- filtering ----------

def us_ok(j):
    text = j["loc_text"] or ""
    strong = bool(US_STRONG.search(text))
    if j["non_us_country"] or NON_US.search(text) or j["currency"] not in ("USD", "", None):
        return strong
    return True


def classify(j):
    """Return 'core', 'faith', 'feed', 'other', 'gig', or None (drop)."""
    t = j["title"]
    company = j["company"].lower()
    if not t or TITLE_OUT.search(t) or any(c.lower() in company for c in CFG["company_exclude"]):
        return None
    gig = bool(GIG_IN.search(t))
    if gig and not any(c.lower() in company for c in CFG["gig_ok_companies"]):
        return None  # unknown gig posters are where the task scams live
    if not gig and not TITLE_IN.search(t):
        return None
    if j["pay_max"] is not None and j["pay_max"] < (j.get("floor") or CFG["salary_floor_usd"]) and not gig:
        return None
    dallas = bool(DFW.search(j["loc_text"] or "")) and not j["onsite"]
    if not ((j["remote"] and us_ok(j)) or dallas):
        return None
    if dallas and not j["remote"] and "hybrid" not in j["where"].lower():
        j["where"] = f"{j['where']} - Dallas area, check if hybrid"
    if gig:
        return "gig"
    if not j["feed"]:
        return "faith" if j.get("group") == "faith" else "core"
    if AI_SIGNAL.search(j["ai_text"]) or company in WATCHLIST_NAMES:
        return "feed"
    if j["pay_min"] is None or j["pay_min"] <= CFG["other_company_pay_min_max"]:
        return "other"  # regular software company, in or near the pay target
    return None


WATCHLIST_NAMES = {c["name"].lower() for c in CFG["companies"]}


def key(j):
    return re.sub(r"\W+", " ", f"{j['company']} {j['title']}").lower().strip()


def collect():
    tasks = []
    for c in CFG["companies"]:
        fn = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever}[c["ats"]]
        tasks.append((f"{c['name']} ({c['ats']})", fn, (c["token"], c["name"]), c.get("group", "ai")))
    for q in CFG["feeds"]["himalayas_queries"]:
        tasks.append((f"Himalayas '{q}'", himalayas, (q,), None))
    if CFG["feeds"].get("jobicy"):
        tasks.append(("Jobicy", jobicy, (), None))
    if CFG["feeds"].get("weworkremotely"):
        tasks.append(("We Work Remotely", weworkremotely, (), None))
    if CFG.get("linkedin", {}).get("enabled"):
        tasks.append(("LinkedIn", linkedin, (), None))
    if CFG.get("indeed", {}).get("enabled"):
        tasks.append(("Indeed", indeed, (), None))

    def run(task):
        name, fn, args, group = task
        try:
            found = list(fn(*args))
            for j in found:
                j["group"] = group
            return name, found, None
        except Exception as e:  # one bad source should never sink the run
            return name, [], f"{name}: {type(e).__name__}: {e}"

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, tasks))
    jobs, errors, seen_keys = [], [], set()
    for _, found, err in results:
        if err:
            errors.append(err)
        for j in found:
            j["bucket"] = classify(j)
            k = key(j)
            if j["bucket"] and k not in seen_keys:
                seen_keys.add(k)
                jobs.append(j)
    # most winnable first: the "faith" group (church and nonprofit software), then in-range roles elsewhere
    order = {"faith": 0, "other": 1, "core": 2, "feed": 3, "gig": 4}

    def pay_rank(j):
        if j["pay_min"] is None:
            return 1
        return 0 if j["pay_min"] <= CFG["other_company_pay_min_max"] else 2

    jobs.sort(key=lambda j: (order[j["bucket"]], pay_rank(j), j["company"].lower(), j["title"].lower()))
    return jobs, errors, len(tasks)


# ---------- output ----------

def ntfy(title, message, click=None, tags=("briefcase",), actions=None):
    body = {"topic": CFG["ntfy_topic"], "title": title[:200], "message": message[:3900], "tags": list(tags)}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions
    req = urllib.request.Request(CFG.get("ntfy_server", "https://ntfy.sh"), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def line(j):
    via = f" · via {j['source']}" if j["feed"] else ""
    return f"{j['where'] or 'Location not listed'} · {j['pay'] or 'pay not listed'}{via}"


def load_seen():
    """Return (seen, ok). A damaged file is set aside so runs rebuild instead of crashing every time."""
    if not SEEN_FILE.exists():
        return {"ids": {}, "keys": {}}, True
    try:
        seen = json.loads(SEEN_FILE.read_text())
        seen["ids"], seen["keys"]
        return seen, True
    except (ValueError, KeyError, TypeError):
        SEEN_FILE.rename(HERE / f"seen.damaged-{datetime.now():%Y%m%d-%H%M%S}.json")
        return {"ids": {}, "keys": {}}, False


def save_seen(seen):
    for part, days in (("ids", 120), ("keys", 45)):  # same title at the same company can alert again after 45 days
        cutoff = (NOW - timedelta(days=days)).strftime("%Y-%m-%d")
        seen[part] = {k: d for k, d in seen[part].items() if d >= cutoff}
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen))
    tmp.replace(SEEN_FILE)  # atomic swap, so a crash mid-write can't damage it


def append_csv(jobs):
    new_file = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date_found", "type", "company", "title", "where", "pay", "source", "link", "applied"])
        for j in jobs:
            kind = {"core": "AI company", "faith": "church/nonprofit software", "feed": "remote board", "other": "other company",
                    "gig": "AI training gig"}[j["bucket"]]
            w.writerow([TODAY, kind, j["company"], j["title"], j["where"], j["pay"], j["source"], j["url"], ""])


def write_md(path, heading, jobs):
    sections = [("core", "At AI companies (their own job boards)"),
                ("faith", "At church and nonprofit software companies"),
                ("feed", "From remote job boards (AI-related posts)"),
                ("other", "From remote job boards, other companies in your pay range"),
                ("gig", "AI training gigs (contract)")]
    out = [f"# {heading}", "", f"Found {TODAY}. {len(jobs)} roles. Tap a title to open the posting.", ""]
    for b, name in sections:
        group = [j for j in jobs if j["bucket"] == b]
        if not group:
            continue
        out += [f"## {name} ({len(group)})", ""]
        for j in group:
            out.append(f"- **{j['company']}**: [{j['title']}]({j['url']}). {line(j)}")
        out.append("")
    path.write_text("\n".join(out))


def deliver(new, suffix=""):
    """Send alerts and return the jobs that actually went out. Stops at the first failure; the rest retry next run."""
    cap = CFG["max_alerts_per_run"]
    singles = new if len(new) <= cap else new[: cap - 1]
    sent = []
    try:
        for j in singles:
            tag = "moneybag" if j["bucket"] == "gig" else "briefcase"
            ntfy(f"{j['company']}: {j['title']}{suffix}", line(j), click=j["url"], tags=(tag,))
            sent.append(j)
        rest = new[len(singles):]
        if rest:
            ntfy(f"+{len(rest)} more new jobs",
                 "\n".join(f"• {j['company']}: {j['title']}" for j in rest)
                 + "\n\nFull list with links: new-jobs.md.", tags=("card_index",))
            sent.extend(rest)
    except Exception as e:
        print(f"  alert failed after {len(sent)} sent; the rest retry next run: {type(e).__name__}: {e}")
    return sent


def wait_for_network(tries=10, pause=30):
    """Right after the Mac wakes, Wi-Fi can lag the scheduled start."""
    for _ in range(tries):
        for host in ("api.ashbyhq.com", "boards-api.greenhouse.io", "ntfy.sh"):
            try:
                socket.create_connection((host, 443), timeout=10).close()
                return True
            except OSError:
                pass
        time.sleep(pause)
    return False


def notify_quietly(*args, **kw):
    try:
        ntfy(*args, **kw)
    except Exception as e:
        print(f"  notice failed: {type(e).__name__}: {e}")


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def buttons(j):
    """Applied / Skip buttons; each tap posts to the private marks topic, read back on the next run."""
    url = f"{CFG.get('ntfy_server', 'https://ntfy.sh')}/{CFG['marks_topic']}"
    s = ranker.sid(j["id"])
    return [{"action": "http", "label": label, "url": url, "method": "POST", "body": f"{word} {s}", "clear": True}
            for label, word in (("Applied", "applied"), ("Skip", "skip"))]


def ranked_body(j, s):
    where = " · location unclear, check it" if s.get("location_ok") == "unclear" else ""
    return (f"Pick {s['they_pick_him']}/5 · Want {s['he_wants_it']}/5 · Job {s['job_quality']}/5 · "
            f"{ranker.pay_text(j, s)}{where}\n{s.get('why', '')}")


def send_morning(ranked, new_ids):
    top = ranked[:5]
    for pos in range(len(top), 0, -1):  # #5 first, so #1 lands on top on the phone
        j, s = top[pos - 1]
        flag = " (new)" if j["id"] in new_ids else ""
        ntfy(f"#{pos} {j['company']}: {j['title']}{flag}", ranked_body(j, s), click=j["url"], actions=buttons(j))
    ntfy(f"Apply today: your top {len(top)} of {len(ranked)}",
         "#1 is right below. Tap a job to open it. Tap Applied or Skip inside the ntfy app so it drops off the list.",
         tags=("trophy",))


def send_new_top(ranked, new_ids, skip_top=0):
    """Alert new jobs that rank in the top 10, skipping any already in the morning top 5."""
    for pos, (j, s) in enumerate(ranked[:10], 1):
        if j["id"] in new_ids and pos > skip_top:
            ntfy(f"New #{pos}: {j['company']}: {j['title']}", ranked_body(j, s), click=j["url"],
                 tags=("sparkles",), actions=buttons(j))


def score_missing(jobs, scores):
    """Score open jobs that have no score yet, new ones first, up to the per-run cap.
    Returns (attempted, scored); attempted > 0 with scored == 0 means the scorer itself is down."""
    todo = [j for j in jobs if j["id"] not in scores][: CFG.get("score_cap_per_run", 36)]
    ready = []
    for j in todo:
        if not j.get("text") and j["id"][:3] in ("gh:", "li:"):
            try:
                j["text"] = greenhouse_text(j) if j["id"].startswith("gh:") else linkedin_text(j)
                if j["id"].startswith("li:"):
                    time.sleep(6)  # stay under LinkedIn's rate limit
            except Exception as e:
                print(f"  no posting text yet for {j['company']}: {j['title']}: {type(e).__name__}")
                continue  # try again next run rather than score blind
        ready.append(j)
    if not ready:
        return 0, 0
    got = scorer.score_jobs(ready, PROFILE_FILE.read_text(), load_json(HERE / "health.json", {}))
    scores.update(got)
    print(f"  scored {len(got)} of {len(ready)} jobs ({len(todo) - len(ready)} still need posting text)")
    return len(ready), len(got)


def pull_marks(scores):
    m = load_json(HERE / "marks.json", {"ids": {}, "keys": {}, "since": 0})
    try:
        msgs = marks_mod.poll(CFG.get("ntfy_server", "https://ntfy.sh"), CFG["marks_topic"], m.get("since") or "12h")
    except Exception as e:
        print(f"  couldn't read Applied/Skip taps: {type(e).__name__}: {e}")
        return m
    updates = marks_mod.apply_marks(msgs, m, {ranker.sid(i): s for i, s in scores.items()})
    save_json(HERE / "marks.json", m)
    marks_mod.mark_csv(LOG_CSV, updates)
    if updates:
        print(f"  recorded {len(updates)} Applied/Skip taps")
    return m


def unranked_plan(unscored_new, attempted, got):
    """If the scorer is down, send unscored new jobs the old way. If they were only past the per-run cap
    (or still need posting text), hold them so they get scored, and alerted by rank, on a later run."""
    if attempted and not got:
        return unscored_new, set()
    return [], {j["id"] for j in unscored_new}


def pick_mode(arg, seen):
    """First run at or after 8 AM does the morning list, first at or after 4 PM the afternoon; others only read taps."""
    if arg in ("--morning", "--afternoon", "--marks"):
        return arg[2:]
    hour = datetime.now().hour
    if hour >= 8 and seen.get("last_morning") != TODAY:
        return "morning"
    if hour >= 16 and seen.get("last_afternoon") != TODAY:
        return "afternoon"
    return "marks"


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    lock = open(HERE / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another run is already going; skipping")
        return
    if arg != "--dry-run" and not wait_for_network():
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] no network for 5 minutes; skipping this run")
        return
    mode = {"--dry-run": "dry-run", "--seed": "seed", "--rank-now": "rank-now"}.get(arg) or pick_mode(arg, load_json(SEEN_FILE, {}))
    scores = load_json(HERE / "scores.json", {})
    if mode == "marks":
        pull_marks(scores)
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] read Applied/Skip taps only")
        return
    jobs, errors, n_sources = collect()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{stamp}] {mode}: {len(jobs)} matching roles from {n_sources} sources, {len(errors)} source errors, "
          f"{BYTES[0] / 1e6:.1f} MB downloaded")
    for e in errors:
        print("  error:", e)

    if mode == "dry-run":
        for j in jobs:
            print(f"  [{j['bucket']}] {j['company']}: {j['title']} | {line(j)}")
        return

    if mode == "rank-now":  # score anything waiting and rewrite ranked.md; sends nothing, changes no schedule state
        marks = pull_marks(scores)
        score_missing(jobs, scores)
        save_json(HERE / "scores.json", scores)
        ranked = ranker.rank(jobs, scores, marks, load_json(SEEN_FILE, {}).get("ids", {}))
        ranker.write_ranked_md(HERE / "ranked.md", ranked, marks, stamp, waiting=sum(1 for j in jobs if j["id"] not in scores))
        for n, (j, s) in enumerate(ranked[:10], 1):
            print(f"  {n:2}. {s['they_pick_him']}/{s['he_wants_it']}/{s['job_quality']} {j['company']}: {j['title']} | {ranker.pay_text(j, s)}")
        return

    seen, seen_ok = load_seen()
    if mode == "seed" or not seen_ok:
        for j in jobs:
            seen["ids"].setdefault(j["id"], TODAY)
            seen["keys"].setdefault(key(j), TODAY)
        if mode == "seed":
            append_csv(jobs)
            write_md(HERE / "open-now.md", "Matching AI jobs open now", jobs)
            companies = len({j["company"] for j in jobs})
            notify_quietly("Job watch is on",
                           f"{len(jobs)} matching roles are open right now at {companies} companies. "
                           "From here on, new ones show up here every morning and afternoon.", tags=("white_check_mark",))
        else:
            notify_quietly("Job watch reset its memory",
                           "Its saved list was damaged, so it rebuilt it. Alerts pick up again with the next new job.",
                           tags=("wrench",))
        save_seen(seen)
        print(f"  {'seeded' if mode == 'seed' else 'rebuilt memory'}: {len(jobs)} roles")
        return

    marks = pull_marks(scores)
    new = [j for j in jobs if j["id"] not in seen["ids"] and key(j) not in seen["keys"]]
    new_ids = {j["id"] for j in new}
    attempted, got = score_missing(sorted(jobs, key=lambda j: j["id"] not in new_ids), scores)
    open_ids, cutoff = {j["id"] for j in jobs}, (NOW - timedelta(days=60)).strftime("%Y-%m-%d")
    scores = {i: s for i, s in scores.items() if i in open_ids or str(s.get("scored_at", ""))[:10] >= cutoff}
    save_json(HERE / "scores.json", scores)
    ranked = ranker.rank(jobs, scores, marks, seen["ids"])
    ranker.write_ranked_md(HERE / "ranked.md", ranked, marks, stamp, waiting=sum(1 for j in jobs if j["id"] not in scores))

    alerts_ok = True
    try:
        if mode == "morning":
            if ranked:
                send_morning(ranked, new_ids)
            send_new_top(ranked, new_ids, skip_top=5)
        else:
            send_new_top(ranked, new_ids)
    except Exception as e:
        alerts_ok = False
        print(f"  ranked alerts failed; they retry next run: {type(e).__name__}: {e}")
    unscored_new = [j for j in new if j["id"] not in scores]
    send_now, wait = unranked_plan(unscored_new, attempted, got)
    sent_unscored = deliver(send_now, suffix=" (not ranked yet)") if send_now else []
    unsent = ({j["id"] for j in send_now} - {j["id"] for j in sent_unscored}) | wait
    if not alerts_ok:
        unsent |= {j["id"] for j in new if j["id"] in scores}
    for j in jobs:
        if j["id"] not in unsent:
            seen["ids"].setdefault(j["id"], TODAY)
            seen["keys"].setdefault(key(j), TODAY)
    handled = [j for j in new if j["id"] not in unsent]
    if handled:
        append_csv(handled)
        write_md(HERE / "new-jobs.md", f"New matching jobs, {stamp}", handled)
    if alerts_ok:
        seen[f"last_{mode}"] = TODAY
    if errors and len(errors) > n_sources * 0.3:
        notify_quietly("Job watch had trouble", f"{len(errors)} of {n_sources} sources failed this run. "
                       "Check logs/run.log.", tags=("warning",))
    now = datetime.now()
    if now.weekday() == 0 and now.hour < 12 and seen.get("last_weekly") != TODAY:
        week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        with LOG_CSV.open() as f:
            n = sum(1 for row in csv.DictReader(f) if row["date_found"] >= week_ago)
        notify_quietly("Job watch weekly check",
                       f"Still running. {n} new matching jobs in the last 7 days. Every one is in jobs-log.csv.",
                       tags=("calendar",))
        seen["last_weekly"] = TODAY
    save_seen(seen)
    print(f"  new: {len(new)}, ranked open jobs: {len(ranked)}, unranked alerts: {len(sent_unscored)}, "
          f"held for retry: {len(unsent)}")


if __name__ == "__main__":
    main()
