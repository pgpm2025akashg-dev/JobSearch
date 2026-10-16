#!/usr/bin/env python3
"""
Job search agent: collect -> filter -> score (Claude) -> draft -> digest -> track.
It never submits applications. You review the digest and apply yourself.

Usage:
  python job_agent.py run            # fetch, score, draft, write digest (and email if configured)
  python job_agent.py list [N]       # show top N scored jobs not yet actioned
  python job_agent.py mark ID STATUS # e.g. mark a1b2c3d4 applied | skipped | interview
  Paste a JD manually: save it as inbox/anything.txt (optional first line: Title | Company | URL)
"""
import os, sys, re, json, sqlite3, hashlib, smtplib, pathlib, datetime as dt
from email.mime.text import MIMEText

import requests
import feedparser
import anthropic

BASE = pathlib.Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())
PROFILE = (BASE / "profile.md").read_text() if (BASE / "profile.md").exists() else ""
DB_PATH = BASE / "jobs.db"
MODEL = CFG.get("model", "claude-sonnet-5-5")
THRESHOLD = CFG.get("threshold", 70)
UA = {"User-Agent": "personal-job-agent/1.0"}

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def set_profile(text):
    global PROFILE
    PROFILE = text


# ---------- storage ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT,
        url TEXT, description TEXT, found_at TEXT, score INTEGER, label TEXT,
        summary TEXT, gaps TEXT, status TEXT DEFAULT 'new', draft_path TEXT)""")
    return con


def job_id(url, title, company):
    return hashlib.sha1(f"{url}|{title}|{company}".encode()).hexdigest()[:8]


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", s).strip()


# ---------- sources ----------
def from_greenhouse(token):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
                     headers=UA, timeout=30)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield dict(source=f"greenhouse:{token}", company=token, title=j.get("title", ""),
                   location=(j.get("location") or {}).get("name", ""),
                   url=j.get("absolute_url", ""), description=strip_html(j.get("content", "")))


def from_lever(company):
    r = requests.get(f"https://api.lever.co/v0/postings/{company}?mode=json", headers=UA, timeout=30)
    r.raise_for_status()
    for j in r.json():
        yield dict(source=f"lever:{company}", company=company, title=j.get("text", ""),
                   location=(j.get("categories") or {}).get("location", ""),
                   url=j.get("hostedUrl", ""), description=j.get("descriptionPlain", ""))


def from_rss(url):
    feed = feedparser.parse(url)
    for e in feed.entries:
        yield dict(source=f"rss:{url}", company=feed.feed.get("title", "rss"),
                   title=e.get("title", ""), location="", url=e.get("link", ""),
                   description=strip_html(e.get("summary", "")))


def from_inbox():
    """Paste any JD into inbox/*.txt. Optional first line: Title | Company | URL"""
    folder = BASE / "inbox"
    folder.mkdir(exist_ok=True)
    for f in folder.glob("*.txt"):
        text = f.read_text(errors="ignore")
        first, _, rest = text.partition("\n")
        parts = [p.strip() for p in first.split("|")]
        if len(parts) == 3:
            title, company, url = parts
            body = rest
        else:
            title, company, url, body = f.stem, "manual", f"file://{f.name}", text
        yield dict(source="inbox", company=company, title=title, location="",
                   url=url, description=body)


def collect():
    for t in CFG.get("greenhouse", []):
        try: yield from from_greenhouse(t)
        except Exception as e: print(f"[warn] greenhouse {t}: {e}")
    for c in CFG.get("lever", []):
        try: yield from from_lever(c)
        except Exception as e: print(f"[warn] lever {c}: {e}")
    for u in CFG.get("rss", []):
        try: yield from from_rss(u)
        except Exception as e: print(f"[warn] rss {u}: {e}")
    yield from from_inbox()


# ---------- filtering ----------
def passes_filter(j):
    title = j["title"].lower()
    blob = f"{j['title']} {j['location']} {j['description'][:800]}".lower()
    if j["source"] != "inbox":  # manual pastes always go through
        if CFG.get("title_include") and not any(k in title for k in CFG["title_include"]):
            return False
        if any(k in title for k in CFG.get("title_exclude", [])):
            return False
        locs = CFG.get("locations", [])
        if locs and not any(l in blob for l in locs):
            return False
    return True


# ---------- Claude ----------
def ask(system, user, max_tokens=1500):
    r = get_client().messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                               messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if b.type == "text")


def score_sys():
    return f"""You assess job fit for one candidate. Be honest and strict; do not flatter.
CANDIDATE PROFILE:
{PROFILE}

Return ONLY JSON, no prose, no code fences:
{{"score": 0-100, "label": "fit|stretch|step_down|no",
  "summary": "2 sentences on why", "gaps": ["specific gaps vs the JD"]}}
Scoring guide: 80+ strong match at the right level; 60-79 plausible but a stretch or partial
function match; below 60 poor match, wrong function, wrong level, or wrong location.
The candidate wants a leadership-track role with good pay in Mumbai or nearby."""


def score(j):
    raw = ask(score_sys(), f"Title: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n\n"
                         f"JD:\n{j['description'][:6000]}", 600)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    d = json.loads(raw)
    return int(d["score"]), d.get("label", ""), d.get("summary", ""), json.dumps(d.get("gaps", []))


def draft_sys():
    return f"""You write job-application material for one candidate.
CANDIDATE PROFILE (the ONLY source of facts):
{PROFILE}

Rules: never invent employers, numbers, tools or achievements. If the JD wants something the
profile lacks, do not claim it; list it under 'Gaps to address'. Plain, confident, specific writing.

Output markdown with exactly these sections:
## Tailored summary (3 lines)
## Résumé bullets to emphasise (5-7, reordered for this JD, only facts from the profile)
## Referral / hiring-manager message (under 110 words, LinkedIn style)
## Gaps to address"""


def draft(j):
    return ask(draft_sys(), f"Title: {j['title']}\nCompany: {j['company']}\nURL: {j['url']}\n\n"
                          f"JD:\n{j['description'][:6000]}", 1800)


# ---------- pipeline ----------
def ingest(con, j):
    """Score (and draft if above threshold) one job and store it. Returns id, or None if already known."""
    (BASE / "drafts").mkdir(exist_ok=True)
    jid = job_id(j["url"], j["title"], j["company"])
    if con.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
        return None
    s, label, summary, gaps = score(j)
    draft_path = ""
    if s >= THRESHOLD:
        try:
            p = BASE / "drafts" / f"{jid}.md"
            p.write_text(f"# {j['title']} - {j['company']}\n{j['url']}\n\n{draft(j)}")
            draft_path = str(p)
        except Exception as e:
            print(f"[warn] drafting failed for {j['title']}: {e}")
    con.execute("INSERT INTO jobs(id,source,company,title,location,url,description,found_at,"
                "score,label,summary,gaps,draft_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, j["source"], j["company"], j["title"], j["location"], j["url"],
                 j["description"], dt.datetime.now().isoformat(), s, label, summary, gaps,
                 draft_path))
    con.commit()
    return jid


def run():
    con = db()
    (BASE / "digest").mkdir(exist_ok=True)
    new_ids = []
    seen = 0
    for j in collect():
        seen += 1
        if not passes_filter(j):
            continue
        try:
            jid = ingest(con, j)
        except Exception as e:
            print(f"[warn] scoring failed for {j['title']}: {e}")
            continue
        if jid:
            new_ids.append(jid)
    print(f"Seen {seen}, newly scored {len(new_ids)}.")
    write_digest(con, new_ids)


def write_digest(con, new_ids):
    if not new_ids:
        print("No new matches today.")
        return
    q = ",".join("?" * len(new_ids))
    rows = con.execute(f"SELECT id,title,company,location,url,score,label,summary,gaps,draft_path "
                       f"FROM jobs WHERE id IN ({q}) ORDER BY score DESC", new_ids).fetchall()
    lines = [f"# Job digest {dt.date.today()}\n"]
    for id_, t, c, loc, url, s, lab, summ, gaps, dp in rows:
        if s < CFG.get("digest_min_score", 55):
            continue
        lines.append(f"## [{s}] {t} - {c} ({lab})\n{loc or ''}  \n{url}  \nID: `{id_}`\n\n{summ}\n")
        g = json.loads(gaps or "[]")
        if g:
            lines.append("Gaps: " + "; ".join(g) + "\n")
        if dp:
            lines.append(f"Draft ready: {dp}\n")
    text = "\n".join(lines)
    out = BASE / "digest" / f"{dt.date.today()}.md"
    out.write_text(text)
    print(f"Digest written: {out}")
    send_email(text)


def send_email(body):
    e = CFG.get("email", {})
    if not e.get("enabled"):
        return
    msg = MIMEText(body)
    msg["Subject"] = f"Job digest {dt.date.today()}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ.get("MAIL_TO", os.environ["SMTP_USER"])
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    print("Digest emailed.")


def list_jobs(n=15):
    con = db()
    for r in con.execute("SELECT id,score,title,company,status,url FROM jobs "
                         "WHERE status='new' ORDER BY score DESC LIMIT ?", (n,)):
        print(f"{r[0]}  [{r[1]:>3}]  {r[2]} - {r[3]}  ({r[4]})\n          {r[5]}")


def mark(jid, status):
    con = db()
    con.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    con.commit()
    print(f"{jid} -> {status}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run": run()
    elif cmd == "list": list_jobs(int(sys.argv[2]) if len(sys.argv) > 2 else 15)
    elif cmd == "mark" and len(sys.argv) == 4: mark(sys.argv[2], sys.argv[3])
    else: print(__doc__)
