"""Referral leads for a job: people in your exported contacts (a .vcf file) tied to the company, and public LinkedIn
profiles of people on its customer-facing teams (found through web search). Leads only; you reach out yourself."""
import base64
import html
import re
import urllib.parse
import urllib.request
from pathlib import Path

CONTACTS = Path.home() / "Contacts_Vcard.vcf"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|plc|technologies|technology|software|ai|hq)\b\.?", re.I)


def norm_company(name):
    name = re.sub(r"\bby\b.*$", "", name or "", flags=re.I)          # "Fieldwire by Hilti" -> "Fieldwire"
    name = re.sub(r"\(.*?\)", "", name)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", SUFFIX.sub("", name.lower()))).strip()


def load_contacts(path=CONTACTS):
    if not path.exists():
        return []
    text = path.read_text(errors="replace").replace("\r\n ", "").replace("\n ", "")  # unfold vCard lines
    people = []
    for card in text.split("BEGIN:VCARD")[1:]:
        get = lambda key: [m.split(":", 1)[1].strip() for m in re.findall(rf"^{key}[;:][^\n]*", card, re.M) if ":" in m]
        name = (get("FN") or [""])[0]
        people.append({"name": name, "org": " ".join(get("ORG")).replace(";", " "),
                       "title": (get("TITLE") or [""])[0], "emails": get("EMAIL")})
    return people


def contact_matches(company, contacts, limit=5):
    target = norm_company(company)
    if len(target) < 3:
        return []
    squashed = target.replace(" ", "")
    hits = []
    for c in contacts:
        org = norm_company(c["org"])
        domains = [e.split("@")[-1].lower() for e in c["emails"] if "@" in e]
        if (org and (org == target or target in org.split() or squashed == org.replace(" ", ""))) or \
                any(d.split(".")[0] == squashed for d in domains):
            hits.append(c)
    return hits[:limit]


def _search(engine, q, timeout):
    url = {"ddg": "https://html.duckduckgo.com/html/?", "bing": "https://www.bing.com/search?"}[engine] + urllib.parse.urlencode({"q": q})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        page = r.read().decode("utf-8", "replace")
    if engine == "ddg":
        pairs = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        return [(urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(h)).query).get("uddg", [h])[0], t) for h, t in pairs]
    out = []
    for block in page.split('<li class="b_algo"')[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href = html.unescape(m.group(1))
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):  # Bing wraps the real link as base64
            b = u[2:]
            href = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "replace")
        out.append((href, m.group(2)))
    return out


def linkedin_people(company, team="customer success", limit=4, timeout=20):
    """Public LinkedIn profiles of people at the company, via DuckDuckGo's or Bing's plain search pages.
    If both are blocked it returns [] (never solves a CAPTCHA)."""
    q = f'site:linkedin.com/in "{company}" "{team}"'
    target = (norm_company(company).split() or [""])[0]
    for engine in ("ddg", "bing"):
        try:
            results = _search(engine, q, timeout)
        except Exception:
            continue
        people = []
        for url, title in results:
            who = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            if "linkedin.com/in/" in url and target and target in norm_company(who):  # must work there
                people.append({"who": who.replace(" | LinkedIn", "").replace(" - LinkedIn", ""), "url": url.split("?")[0]})
            if len(people) >= limit:
                break
        if people:
            return people
    return []


def leads(job, contacts):
    return {"contacts": contact_matches(job["company"], contacts), "linkedin": linkedin_people(job["company"])}
