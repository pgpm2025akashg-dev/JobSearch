# Job search agent (v1)

Finds roles, scores fit with Claude, drafts tailored materials, writes a daily digest.
It never applies for you; you review and submit.

## Setup
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=...        # Windows: setx ANTHROPIC_API_KEY ...
    # 1. Fill in profile.md with real numbers (this drives scoring and drafts)
    # 2. Edit config.json: add sources (see below)

## Sources (config.json)
- "greenhouse": ["company-board-token"]  -> boards.greenhouse.io/<token>
- "lever": ["company-slug"]              -> jobs.lever.co/<slug>
- "rss": ["https://..."]                 -> any job RSS/Atom feed
- inbox/*.txt: paste any JD (LinkedIn, Naukri, recruiter email). First line optional: Title | Company | URL
Check each target company's careers page URL to see whether it uses Greenhouse or Lever.
Many Indian companies use other systems (Workday, Darwinbox, own sites); use the inbox for those.

## Run
    python job_agent.py run
    python job_agent.py list
    python job_agent.py mark <id> applied

## Daily schedule
cron (Mac/Linux):  0 8 * * *  cd /path/to/job_agent && python job_agent.py run
Windows: Task Scheduler running the same command.
Optional email: set "email.enabled": true and env vars SMTP_HOST, SMTP_USER, SMTP_PASS, MAIL_TO.

## Notes
- Respect each site's terms. This v1 uses public ATS endpoints, RSS and your own pastes; it does not scrape LinkedIn or Naukri.
- Tune "threshold" (draft cutoff) and "title_include/exclude" after the first few runs.

## Deploy on Streamlit Community Cloud
1. Push this folder to a GitHub repo. profile.md, jobs.db and secrets are git-ignored on purpose (personal data).
2. share.streamlit.io > New app > pick the repo, branch, main file `app.py`.
3. App Settings > Secrets: paste ANTHROPIC_API_KEY, APP_PASSWORD and PROFILE (see .streamlit/secrets.toml.example).
4. Open the app, enter the password, test with "Score a JD".
Local test: pip install -r requirements.txt && streamlit run app.py
Limits: storage is temporary (use the Backup tab); the app sleeps when idle and cannot run on a schedule.
