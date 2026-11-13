#!/usr/bin/env python3
"""
Internship Watcher -- starter kit (single file, ~250 lines).
Polls company job boards + community tracker repos every hour (GitHub Actions),
filters for NEW internships you care about, emails you the moment one opens.
Run:   SMTP_USERNAME=... SMTP_PASSWORD=... EMAIL_TO=... python watcher.py
Dry:   DRY_RUN=1 python watcher.py        (prints, writes preview.html, no state)
Probe: python watcher.py probe "Jane Street" stripe figma   (find board tokens)
"""
import json, os, re, smtplib, sys, time
from concurrent.futures import ThreadPoolExecutor
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
import requests

CONFIG_FILE, SEEN_FILE, TIMEOUT = "config.json", "seen_jobs.json", 15
DRY_RUN = os.environ.get("DRY_RUN") == "1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "application/json"}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, json.JSONDecodeError): return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=2, ensure_ascii=False)


# ------------------------------ board fetchers ------------------------------ #
# Each returns normalized dicts: {id, title, location, url, content, company?}.
# Token = the slug in the company's board URL (see `probe` at the bottom).

def fetch_greenhouse(firm):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{firm['token']}/jobs?content=true", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("id")), "title": j.get("title") or "", "location": (j.get("location") or {}).get("name") or "",
             "url": j.get("absolute_url") or "", "content": (j.get("content") or "").lower()} for j in r.json().get("jobs", [])]


def fetch_lever(firm):
    r = requests.get(f"https://api.lever.co/v0/postings/{firm['token']}?mode=json", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("id")), "title": j.get("text") or "", "location": (j.get("categories") or {}).get("location") or "",
             "url": j.get("hostedUrl") or "", "content": (j.get("descriptionPlain") or "").lower()} for j in r.json()]


def fetch_ashby(firm):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{firm['token']}?includeCompensation=false", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("id")), "title": j.get("title") or "", "location": j.get("location") or "",
             "url": j.get("jobUrl") or j.get("applyUrl") or "", "content": (j.get("descriptionPlain") or "").lower()} for j in r.json().get("jobs", [])]


def fetch_workable(firm):
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{firm['token']}?details=true", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("shortcode") or j.get("id")), "title": j.get("title") or "",
             "location": ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x),
             "url": j.get("url") or j.get("application_url") or "", "content": (j.get("description") or "").lower()} for j in r.json().get("jobs", [])]


def fetch_github_json(firm):
    """Community tracker repos that publish listings.json (SimplifyJobs / vanshb03
    family). One source = hundreds of companies. `cycle_year` is injected so
    repo-scoped listings pass the year check even when the title has no year."""
    r = requests.get(firm["url"], headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out, cy, seasons = [], str(firm.get("cycle_year", "")), [s.lower() for s in firm.get("seasons", [])]
    for j in r.json():
        if not isinstance(j, dict) or j.get("active") is False or j.get("is_visible") is False: continue
        terms = j.get("terms") or []
        season_text = ((" ".join(terms) if isinstance(terms, list) else str(terms)) + " " + str(j.get("season") or "")).lower()
        if seasons and not any(s in season_text for s in seasons): continue
        locs = j.get("locations") or j.get("location") or []
        link = j.get("url") or j.get("application_link") or ""
        title = (j.get("title") or "").strip()
        out.append({"id": str(j.get("id") or link or title), "title": title, "company": (j.get("company_name") or j.get("company") or "").strip(),
                    "location": ", ".join(map(str, locs)) if isinstance(locs, list) else str(locs), "url": link, "content": "",
                    "year_text": f"{title} {season_text} {cy}"})
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workable": fetch_workable, "github_json": fetch_github_json}


# --------------------------------- filters ---------------------------------- #
DROPS = {}


def _drop(reason, title):
    DROPS.setdefault(reason, []).append(title); return False


def _title_is_internship(title, keywords):
    # Word-boundary: 'intern' matches Intern/Interns/Internship but NOT
    # Internal / International / Internet. Substring matching floods you with
    # "Internal Audit" directors.
    return any(re.search(r"\b" + re.escape(k.lower()) + r"(s|ship|ships)?\b", title) for k in keywords)


def _has_term(title, term):
    term = term.lower()
    return re.search((r"\b%s\b" if len(term) <= 3 else r"\b%s") % re.escape(term), title) is not None


def is_relevant(job, f):
    title = job["title"].lower()
    if f.get("title_keywords") and not _title_is_internship(title, f["title_keywords"]): return _drop("no-intern-word", title)
    if f.get("title_require_any") and not any(_has_term(title, t) for t in f["title_require_any"]): return _drop("no-domain-match", title)
    for bad in f.get("title_exclude", []):
        if bad.lower() in title: return _drop(f"excluded:{bad}", title)
    # CYCLE CHECK. NEVER require the year in the title -- most companies omit it.
    # If the title names a year, it must be ours; else look in the body; if no
    # year appears anywhere, KEEP IT (a live intern posting is the next cycle).
    years = [str(y) for y in f.get("years", [])]
    ty = set(re.findall(r"\b(20\d{2})\b", title))
    if years and ty and not (ty & set(years)): return _drop("wrong-year-in-title", title)
    if years and not ty:
        hay = f"{job.get('year_text','')} {title} {(job.get('content') or '')[:4000]}".lower()
        if not any(y in hay for y in years) and any(p.lower() in hay for p in f.get("reject_cycle_phrases", [])):
            return _drop("wrong-cycle-phrase", title)
    loc = (job.get("location") or "").lower()
    if loc and f.get("location_exclude") and any(b.lower() in loc for b in f["location_exclude"]):
        if not any(m.lower() in loc for m in f.get("location_keep_markers", [])): return _drop("excluded-location", title)
    return True


# ---------------------------------- email ----------------------------------- #
def build_email(grouped, baseline=False):
    parts = ["<p>Baseline of currently-open roles. Future emails contain only <b>newly opened</b> postings.</p>" if baseline
             else "<p>These internship postings just opened:</p>"]
    for firm in sorted(grouped):
        parts.append(f"<h3 style='margin:14px 0 4px'>{escape(firm)}</h3><ul>")
        for j in grouped[firm]:
            loc = f" &mdash; {escape(j['location'])}" if j.get("location") else ""
            parts.append(f"<li><a href='{escape(j['url'])}'>{escape(j['title'])}</a>{loc}</li>")
        parts.append("</ul>")
    parts.append("<p style='color:#888;font-size:12px'>Sent automatically by your internship watcher.</p>")
    return "\n".join(parts)


def send_email(subject, html):
    if DRY_RUN:
        open("preview.html", "w", encoding="utf-8").write(html); print(f"DRY RUN: {subject!r} -> preview.html"); return
    user, pw, to = os.environ.get("SMTP_USERNAME"), os.environ.get("SMTP_PASSWORD"), os.environ.get("EMAIL_TO")
    if not (user and pw and to): sys.exit("set SMTP_USERNAME, SMTP_PASSWORD (Gmail App Password), EMAIL_TO")
    tos = [a.strip() for a in re.split(r"[,;\s]+", to) if a.strip()]
    msg = MIMEMultipart("alternative"); msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(tos)
    msg.attach(MIMEText(html, "html", "utf-8"))
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465")), timeout=TIMEOUT) as s:
                s.login(user, pw); s.sendmail(user, tos, msg.as_string())
            print(f"Email sent to {tos}: {subject}"); return
        except (smtplib.SMTPException, OSError) as e:
            print(f"  ! SMTP attempt {attempt+1}: {e}"); time.sleep(5 * (attempt + 1))
    raise RuntimeError("SMTP failed 3x")


# ---------------------------------- main ------------------------------------ #
def main():
    cfg = load_json(CONFIG_FILE, None) or sys.exit("config.json missing")
    f, seen = cfg.get("filters", {}), load_json(SEEN_FILE, {})
    first_run, current, new = not seen, {}, {}
    firms = [x for x in cfg["firms"] if x.get("enabled", True)]

    def poll(firm):
        try: return firm, FETCHERS[firm["ats"]](firm)
        except Exception as e:  # one dead board never kills the run
            print(f"  x {firm.get('name')} skipped: {e}"); return firm, []

    with ThreadPoolExecutor(max_workers=12) as pool:   # keep this parallel; 200 boards serially takes an hour
        results = list(pool.map(poll, firms))
    for firm, jobs in results:
        rel = [j for j in jobs if is_relevant(j, f)]
        print(f"  ok {firm['name']}: {len(jobs)} jobs, {len(rel)} relevant")
        for j in rel:
            key = (j.get("url") or f"{firm['name']}:{j['id']}").strip().lower()
            if key in current: continue
            j.setdefault("company", firm["name"]); current[key] = j
            if key not in seen: new.setdefault(j["company"], []).append(j)
    if DROPS:   # log every drop -- silent filtering is the most dangerous bug class
        print("Filter drops: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(DROPS.items(), key=lambda kv: -len(kv[1]))[:10]))
    grouped = {j["company"]: [] for j in current.values()} if first_run else new
    if first_run:
        for j in current.values(): grouped[j["company"]].append(j)
    total = sum(len(v) for v in grouped.values())
    if total:
        send_email(f"[Internship Watcher] {'Baseline: ' if first_run else ''}{total} {'open' if first_run else 'new'} role(s)", build_email(grouped, first_run))
    else:
        print("No new roles this run.")
    if not DRY_RUN:
        for k, j in current.items(): seen[k] = {"title": j["title"], "url": j.get("url", "")}
        save_json(SEEN_FILE, seen)


def probe(names):
    """Guess board tokens from company names and tell you which ATS each uses."""
    for n in names:
        slug = re.sub(r"[^a-z0-9]", "", n.lower())
        for cands in ([slug, slug + "llc", slug + "inc", slug + "careers", slug + "trading", slug + "capital"],):
            for tok in cands:
                for ats in ("greenhouse", "lever", "ashby", "workable"):
                    try:
                        jobs = FETCHERS[ats]({"token": tok})
                        if jobs: print(f"  {n}: {{\"name\": \"{n}\", \"ats\": \"{ats}\", \"token\": \"{tok}\"}}  ({len(jobs)} postings)"); break
                    except Exception: pass
                else: continue
                break


if __name__ == "__main__":
    probe(sys.argv[2:]) if len(sys.argv) > 1 and sys.argv[1] == "probe" else main()
