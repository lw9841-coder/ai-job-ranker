"""Opens a job's application form in Chrome, reads every field, fills it from saved answers, and (only when told)
submits it. Runs under the .apply Python (Playwright). Never solves a CAPTCHA and never signs in or makes accounts."""
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent.parent
PROFILE = HERE / ".apply-profile"

# Pages that need an account or sign-in first. These go back to you as "do this one yourself".
LOGIN_HOSTS = ("myworkdayjobs.com", "taleo.net", "icims.com", "successfactors", "oraclecloud.com",
               "linkedin.com", "indeed.com", "ultipro.com", "adp.com", "brassring.com")
CAPTCHA_SEL = ("iframe[src*='hcaptcha'], iframe[title*='challenge'], iframe[src*='recaptcha/api2/bframe'], "
               "iframe[src*='turnstile'], #px-captcha")


def captcha_showing(page):
    """Invisible reCAPTCHA frames sit on many forms all the time; only a challenge you can see counts."""
    for el in page.query_selector_all(CAPTCHA_SEL):
        box = el.bounding_box()
        if box and box["width"] > 60 and box["height"] > 60 and el.is_visible():
            return True
    return False

APPLY_BUTTON = re.compile(r"^\s*(apply( now| for this job| to this job)?|i'?m interested|submit application)\s*$", re.I)

EXTRACT_JS = r"""
() => {
  const shown = n => { if (!n) return false; const r = n.getBoundingClientRect(); const s = getComputedStyle(n);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none'; };
  const vis = el => { if (el.type === 'file') return true;
    if (shown(el)) return true;
    // custom-drawn radios and checkboxes hide the real input; count it if its label or wrapper shows
    if (el.type === 'radio' || el.type === 'checkbox')
      return shown(el.closest('label')) || (el.id && shown(document.querySelector(`label[for="${CSS.escape(el.id)}"]`))) || shown(el.parentElement);
    return false; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labelFor = el => {
    if (el.getAttribute('aria-labelledby')) {
      const t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => document.getElementById(i))
        .filter(Boolean).map(n => n.innerText).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && clean(l.innerText)) return clean(l.innerText); }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const wrap = el.closest('label'); if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    let p = el.parentElement;
    for (let i = 0; p && i < 6; i++, p = p.parentElement) {
      const l = p.querySelector('label, legend, [class*="label"], [class*="question"]');
      if (l && clean(l.innerText) && !l.contains(el)) return clean(l.innerText);
    }
    return clean(el.placeholder || el.name || el.id);
  };
  const VAGUE = /^(attach|upload|upload file|choose file|browse|drop files? here.*|or drag and drop.*|)$/i;
  const fileLabel = el => {
    let p = el.parentElement;
    for (let i = 0; p && i < 8; i++, p = p.parentElement) {
      for (const l of p.querySelectorAll('label, legend, h3, h4, [class*="label"], [class*="title"]')) {
        const t = clean(l.innerText); if (t && !VAGUE.test(t) && t.length < 80) return t;
      }
    }
    return labelFor(el);
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (fs) { const lg = fs.querySelector('legend, [class*="label"], [class*="question"]');
      if (lg && clean(lg.innerText)) return clean(lg.innerText);
      if (fs.getAttribute('aria-label')) return clean(fs.getAttribute('aria-label')); }
    return labelFor(el);
  };
  const contextOf = el => {
    let p = el.parentElement;
    for (let i = 0; p && i < 7; i++, p = p.parentElement) {
      const t = clean(p.innerText);
      if (t.length > 25 && t.length < 600) return t.slice(0, 300);
      if (t.length >= 600) break;
    }
    return '';
  };
  const out = []; const groups = {}; let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select, [role="combobox"]')) {
    const tag = el.tagName.toLowerCase(); const type = (el.type || tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    // custom radios/checkboxes: the real input is hidden inside a clickable [role=radio] wrapper (Rippling)
    const wrap = (type === 'radio' || type === 'checkbox') ? el.closest('[role="radio"], [role="checkbox"]') : null;
    const target = wrap || el;
    if (!vis(target)) continue;
    if (!wrap && el.closest('[aria-hidden="true"]') && type !== 'file') continue;
    // Ashby-style yes/no: a hidden checkbox with Yes and No buttons beside it
    if (type === 'checkbox' && !wrap) {
      let box = el.parentElement, yn = null;
      for (let i = 0; box && i < 4 && !yn; i++, box = box.parentElement) {
        const b = [...box.querySelectorAll('button')].map(x => clean(x.innerText));
        if (b.includes('Yes') && b.includes('No')) yn = box;
      }
      if (yn) {
        const key = 'f' + (n++); yn.setAttribute('data-apply-key', key);
        out.push({key, kind: 'yesno', label: labelFor(el), options: ['Yes', 'No'],
                  required: el.required || /\*\s*$/.test(labelFor(el)), context: contextOf(el)});
        continue;
      }
    }
    const key = 'f' + (n++); target.setAttribute('data-apply-key', key);
    const req = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio' || type === 'checkbox') {
      const g = groupLabel(el); const gname = (el.name || g) + '|' + type;
      const opt = labelFor(target);
      if (!groups[gname]) { groups[gname] = {key, kind: type, label: g, required: req, options: [], keys: [],
                                             context: contextOf(el.closest('fieldset, [role="radiogroup"], [role="group"]') || el.parentElement)}; out.push(groups[gname]); }
      groups[gname].options.push(opt); groups[gname].keys.push(key); groups[gname].required ||= req;
      continue;
    }
    const f = {key, kind: tag === 'select' ? 'select' : (el.getAttribute('role') === 'combobox' ? 'combobox' : type),
               label: type === 'file' ? fileLabel(el) : labelFor(el), required: req, context: contextOf(el)};
    if (el.id || el.name) f.name = el.id || el.name;
    if (tag === 'select') f.options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^select/i.test(t));
    if (type === 'file') f.accept = el.accept || '';
    out.push(f);
  }
  for (const f of out) if (/\*\s*$/.test(f.label)) { f.required = true; f.label = f.label.replace(/\s*\*\s*$/, ''); }
  return out;
}
"""


class Blocked(Exception):
    """The page needs a person: a sign-in wall, a CAPTCHA, or a form this can't read."""


def browser(p, headless=False):
    # A real Chrome window with its own profile. Headless Chrome gets flagged as a bot far more often.
    return p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless, viewport={"width": 1280, "height": 1000},
        args=["--window-position=2400,0"])


def check_host(url):
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    for h in LOGIN_HOSTS:
        if h in host:
            raise Blocked(f"{h} needs a sign-in or account")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _norm(t):
    t = re.sub(r"\(.*?\)|[-|–,].*?(remote|united states|usa|us)\b.*$", "", t or "", flags=re.I)
    return re.sub(r"\W+", " ", t).lower().strip()


def find_on_ats(company, title):
    """Look for the same job on the company's own Greenhouse, Lever, or Ashby board. Returns its link or None."""
    base = re.sub(r"\b(inc|llc|ltd|corp|co|company|technologies|software)\b\.?", "", company, flags=re.I)
    words = re.findall(r"[a-z0-9]+", base.lower())
    slugs = list(dict.fromkeys(["".join(words), "-".join(words)]))
    want = _norm(title)
    for slug in slugs:
        for kind, api in (("gh", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
                          ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json"),
                          ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}")):
            try:
                data = json.loads(_get(api))
            except Exception:
                continue
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
            for j in jobs:
                name = j.get("title") or j.get("text") or ""
                if _norm(name) == want:
                    return j.get("absolute_url") or j.get("hostedUrl") or j.get("jobUrl")
    return None


def linkedin_apply_link(url, company, title):
    """LinkedIn jobs that apply on the company's site: find that posting on the company's own board."""
    page = _get(url)
    if "offsite-apply" not in page:
        raise Blocked("LinkedIn Easy Apply (needs your LinkedIn sign-in)")
    found = find_on_ats(company, title)
    if not found:
        raise Blocked("applies on the company's own site, but couldn't find the posting there. Tap to open it on LinkedIn")
    return found


def open_form(page, url):
    """Go to the posting, keep its text, then get to the form. Returns the posting text."""
    check_host(url)
    page.goto(url, wait_until="commit", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    posting = page.inner_text("body")[:15000]
    if not page.query_selector("input[type=file]"):
        for el in page.query_selector_all("a, button"):
            try:
                if not (APPLY_BUTTON.match(el.inner_text() or "") and el.is_visible()):
                    continue
                pages_before = len(page.context.pages)
                el.click()
                page.wait_for_timeout(3500)
                if len(page.context.pages) > pages_before:  # the apply link opened a new tab
                    new = page.context.pages[-1]
                    new.wait_for_load_state("domcontentloaded", timeout=45000)
                    new.wait_for_timeout(2500)
                    page.goto(new.url, wait_until="domcontentloaded", timeout=45000)
                    new.close()
                    page.wait_for_timeout(2500)
                break
            except Blocked:
                raise
            except Exception:
                continue
    check_host(page.url)
    if not page.query_selector("input[type=file]"):
        raise Blocked("couldn't find an application form with a resume upload on this page")
    if captcha_showing(page):
        raise Blocked("the page shows a CAPTCHA")
    return posting


def read_fields(page):
    fields = page.evaluate(EXTRACT_JS)
    for f in fields:  # dropdowns built from divs only show their choices once opened
        if f["kind"] != "combobox" or f.get("options"):
            continue
        try:
            sel = f"[data-apply-key={f['key']}]"
            page.click(sel, timeout=3000)
            page.wait_for_timeout(600)
            f["options"] = [t for t in (o.inner_text().strip() for o in _options(page, sel)) if t][:60]
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
    return fields


def _options(page, sel):
    """The open choices for this dropdown only: its own listbox if it names one, else whatever is showing."""
    owner = page.eval_on_selector(sel, "e => e.getAttribute('aria-controls') || e.getAttribute('aria-owns') || ''")
    if owner:
        opts = page.query_selector_all(f"[id='{owner}'] [role=option]")
        if opts:
            return opts
    return [o for o in page.query_selector_all("[role=option]") if o.is_visible()]


def _type_into(page, el, text):
    el.click()
    try:
        el.fill("")
        el.type(text, delay=30)
    except Exception:  # a div acting as a dropdown: type into whatever has focus after the click
        page.keyboard.type(text, delay=30)


def _pick_combobox(page, el, value, options=()):
    sel = f"[data-apply-key={el.get_attribute('data-apply-key')}]"
    if value in options:  # a known choice: open the list and click it without typing (search boxes filter oddly)
        try:
            el.click()
            page.wait_for_timeout(700)
            for o in _options(page, sel):
                if (o.inner_text() or "").strip() == value:
                    o.scroll_into_view_if_needed()
                    o.click()
                    return
            page.keyboard.press("Escape")
        except Exception:
            pass
    city, _, state = value.partition(",")
    tries = [value] + ([city.strip()] if state else [])
    for typed in tries:
        _type_into(page, el, typed)
        page.wait_for_timeout(1200)
        opts = _options(page, sel)
        texts = [(o, (o.inner_text() or "").strip().lower()) for o in opts]
        exact = [o for o, t in texts if t == value.strip().lower()]
        if exact:
            exact[0].click()
            return
        if typed != value:  # typed just the city: take the option for that city in the right state
            st = state.strip().split(",")[0].strip().lower()
            st_alt = STATES.get(st) or {v: k for k, v in STATES.items()}.get(st, st)
            for o, t in texts:
                if t.startswith(city.strip().lower()) and (re.search(rf"\b{st}\b", t) or re.search(rf"\b{st_alt}\b", t)):
                    o.click()
                    return
        close = [o for o, t in texts if value.strip().lower() in t]
        if len(close) == 1 or (close and len(value) > 3):
            close[0].click()
            return
    page.keyboard.press("Escape")
    raise Blocked(f"couldn't pick '{value}' in a dropdown")


def _tick(page, sel):
    el = page.query_selector(sel)
    if el.get_attribute("role") in ("radio", "checkbox"):
        if el.get_attribute("aria-checked") != "true":
            el.click()
    else:
        try:
            page.check(sel, force=True, timeout=5000)
        except Exception:  # a hidden input: click the visible label instead
            if el.is_checked():
                return
            label = el.evaluate_handle("e => (e.id && document.querySelector(`label[for='${CSS.escape(e.id)}']`))"
                                       " || e.closest('label') || e.parentElement").as_element()
            label.click()
            if not el.is_checked():
                raise Blocked("couldn't tick a checkbox")


def fill(page, fields, answers, files):
    """answers: {key: value}. files: {key: path}. Returns the labels of required fields left empty."""
    file_fields = [f for f in fields if f["kind"] == "file"]
    for key, path in sorted(files.items(), key=lambda kv: kv[1].endswith("Cover_Letter.pdf")):  # resume first
        n = next(i for i, f in enumerate(file_fields) if f["key"] == key)
        try:  # by position among the upload boxes: some forms redraw after reading the resume and drop our tags
            page.locator("input[type=file]").nth(n).set_input_files(str(path), timeout=10000)
        except Exception:
            if file_fields[n].get("required"):
                raise
        page.wait_for_timeout(3000)
    if files:  # some forms (Rippling) redraw themselves after reading the resume, so find each field again by its label
        fields, answers = _rekey(page, fields, answers)
    by_key = {f["key"]: f for f in fields}
    for key, value in answers.items():
        f = by_key.get(key)
        if not f or value in (None, "", []):
            continue
        sel = f"[data-apply-key={key}]"
        kind = f["kind"]
        if kind == "yesno":
            page.click(f"{sel} button:text-is('{'Yes' if str(value).lower().startswith('y') else 'No'}')")
            continue
        if kind in ("radio", "checkbox"):
            wanted = value if isinstance(value, list) else [value]
            for opt, k in zip(f["options"], f["keys"]):
                if any(opt.strip().lower() == w.strip().lower() for w in wanted):
                    _tick(page, f"[data-apply-key={k}]")
        elif kind == "select":
            page.select_option(sel, label=value)
        elif kind == "combobox":
            _pick_combobox(page, page.query_selector(sel), str(value), f.get("options") or ())
        else:
            page.fill(sel, str(value))
            if re.search(r"location|city", f["label"], re.I):
                _pick_place(page, sel, str(value))
    page.wait_for_timeout(500)
    _sign_disability_form(page, answers)
    missing = [f["label"] for f in fields if f.get("required") and f["key"] not in answers and f["key"] not in files]
    return missing + _late_fields(page, {f["label"] for f in fields})


def _rekey(page, old_fields, answers):
    new_fields = page.evaluate(EXTRACT_JS)
    slots = {}
    for f in new_fields:
        slots.setdefault((f["kind"], f["label"]), []).append(f)
    seen, out = {}, {}
    for f in old_fields:
        n = seen.get((f["kind"], f["label"]), 0)
        seen[(f["kind"], f["label"])] = n + 1
        match = slots.get((f["kind"], f["label"]), [])
        if f["key"] in answers and n < len(match):
            nf = match[n]
            if f.get("options") and not nf.get("options"):
                nf["options"] = f["options"]
            out[nf["key"]] = answers[f["key"]]
    return new_fields, out


# Questions some forms only show after an earlier answer (race after "Hispanic/Latino: No", for example).
# Only self-identification has a fixed answer; anything else that shows up late and is required stops the send.
LATE = [(re.compile(r"\brace\b|ethnicit", re.I), "White"),
        (re.compile(r"hispanic|latino", re.I), "No"),
        (re.compile(r"gender|\bsex\b", re.I), "Male"),
        (re.compile(r"veteran", re.I), "not a protected veteran"),
        (re.compile(r"disabilit", re.I), "No, I do not have a disability")]


def _late_fields(page, known_labels):
    left = []
    for f in read_fields(page):
        if f["label"] in known_labels or f["kind"] == "file":
            continue
        sel = f"[data-apply-key={f['key']}]"
        value = next((v for rx, v in LATE if rx.search(f["label"])), None)
        try:
            if value is None:
                raise Blocked("")
            opts = f.get("options") or []
            pick = next((o for o in opts if o.lower() == value.lower()), None) or \
                next((o for o in opts if value.lower() in o.lower()), None)
            if f["kind"] == "select" and pick:
                page.select_option(sel, label=pick)
            elif f["kind"] in ("radio", "checkbox") and pick:
                _tick(page, f"[data-apply-key={f['keys'][opts.index(pick)]}]")
            elif f["kind"] == "combobox":
                _pick_combobox(page, page.query_selector(sel), pick or value)
            else:
                raise Blocked("")
        except Blocked:
            if f.get("required"):
                left.append(f["label"])
    return left


SUGGEST = ".dropdown-location, .pac-item, [role=listbox] [role=option]"
STATES = dict(s.rsplit(" ", 1) for s in (
    "alabama al,alaska ak,arizona az,arkansas ar,california ca,colorado co,connecticut ct,delaware de,florida fl,"
    "georgia ga,hawaii hi,idaho id,illinois il,indiana in,iowa ia,kansas ks,kentucky ky,louisiana la,maine me,"
    "maryland md,massachusetts ma,michigan mi,minnesota mn,mississippi ms,missouri mo,montana mt,nebraska ne,"
    "nevada nv,new hampshire nh,new jersey nj,new mexico nm,new york ny,north carolina nc,north dakota nd,ohio oh,"
    "oklahoma ok,oregon or,pennsylvania pa,rhode island ri,south carolina sc,south dakota sd,tennessee tn,texas tx,"
    "utah ut,vermont vt,virginia va,washington wa,west virginia wv,wisconsin wi,wyoming wy").split(","))


def _pick_place(page, sel, value):
    """Location boxes that only keep a value picked from their suggestions (Lever, Google Places)."""
    city, _, state = value.partition(",")
    page.click(sel)
    page.fill(sel, "")
    page.type(sel, value[: len(city) + 3], delay=50)
    page.wait_for_timeout(1500)
    opts = [o for o in page.query_selector_all(SUGGEST) if o.is_visible()]
    if not opts:
        page.fill(sel, value)
        return
    st = state.strip().lower()
    st = STATES.get(st, st)
    for o in opts:
        t = (o.inner_text() or "").lower()
        if t.startswith(city.strip().lower()) and (not st or re.search(rf"\b({st}|{state.strip().lower()})\b", t)):
            o.click()
            return
    page.fill(sel, value)


def _sign_disability_form(page, answers):
    """The federal disability form asks for a name and date once it's answered (Lever shows these late)."""
    name = page.query_selector("input[name*='disabilitySignature']:not([name*='Date'])")
    date = page.query_selector("input[name*='disabilitySignatureDate']")
    if name and name.is_visible() and not name.input_value():
        name.fill(SIGNATURE_NAME)
    if date and date.is_visible() and not date.input_value():
        date.fill(time.strftime("%m/%d/%Y"))


SIGNATURE_NAME = ""  # apply.py sets this from facts


SUBMIT = re.compile(r"^\s*(submit( application| your application)?|apply|send application)\s*$", re.I)
DONE = re.compile(r"thank you|thanks for applying|application (has been )?(received|submitted)|we('ve| have) received", re.I)


def submit_button(page):
    for el in page.query_selector_all("button, input[type=submit]"):
        try:
            text = el.inner_text() if el.evaluate("e => e.tagName") == "BUTTON" else el.get_attribute("value")
            if SUBMIT.match(text or "") and el.is_visible():
                return el
        except Exception:
            continue
    return None


def check_ready(page):
    """Before anything goes to your phone: the submit button must exist and be clickable (forms grey it out
    while a required answer is missing)."""
    page.keyboard.press("Escape")
    page.wait_for_timeout(4000)  # let resume uploads finish parsing
    el = submit_button(page)
    if not el:
        raise Blocked("couldn't find the submit button")
    if not el.is_enabled() or el.get_attribute("aria-disabled") == "true":
        raise Blocked("the submit button is greyed out, so a required question is still blank")
    return el


def submit(page):
    check_ready(page).click()
    for _ in range(20):
        page.wait_for_timeout(1000)
        if captcha_showing(page):
            raise Blocked("a CAPTCHA came up after submit")
        if DONE.search(page.inner_text("body")[:5000]):
            return True
    raise Blocked("submitted, but no confirmation page showed up. Check your email for a confirmation")


def run(url, plan=None, do_submit=False, shot=None, headless=False):
    """plan=None: just read the form and return its fields.
    plan={"answers":..., "files":...}: fill it; screenshot to `shot`; submit if do_submit."""
    with sync_playwright() as p:
        ctx = browser(p, headless)
        page = ctx.new_page()
        try:
            posting = open_form(page, url)
            fields = read_fields(page)
            if plan is None:
                return {"fields": fields, "url": page.url, "posting": posting}
            labels = {f["key"]: f["label"] for f in fields}
            if [labels.get(k) for k in sorted(plan["labels"])] != [plan["labels"][k] for k in sorted(plan["labels"])]:
                raise Blocked("the form changed since the answers were written")
            missing = fill(page, fields, plan["answers"], plan["files"])
            if shot:
                page.screenshot(path=str(shot), full_page=True)
            if missing:
                raise Blocked("required questions left blank: " + "; ".join(missing)[:300])
            if not do_submit:
                check_ready(page)
            if do_submit:
                submit(page)
                time.sleep(2)
            return {"fields": fields, "url": page.url, "submitted": do_submit}
        finally:
            ctx.close()
