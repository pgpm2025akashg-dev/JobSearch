import os
import json
import sqlite3
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Job Agent", page_icon="💼", layout="wide")

# ---------- access control (your API key pays for every click, so always gate) ----------
APP_PASSWORD = st.secrets.get("APP_PASSWORD")
if not APP_PASSWORD:
    st.error("Set APP_PASSWORD in the app's Secrets before using this app.")
    st.stop()
if not st.session_state.get("authed"):
    entered = st.text_input("Password", type="password")
    if entered and entered == APP_PASSWORD:
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()

if "ANTHROPIC_API_KEY" not in st.secrets:
    st.error("Set ANTHROPIC_API_KEY in the app's Secrets.")
    st.stop()
os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]

import job_agent as ja  # noqa: E402  (imported after the key is in the environment)

# ---------- profile ----------
if "profile" not in st.session_state:
    st.session_state["profile"] = st.secrets.get("PROFILE", "") or ja.PROFILE

with st.sidebar:
    st.header("Your profile")
    st.caption("Facts the agent may use. Store it permanently as PROFILE in Secrets, "
               "otherwise edits here reset when the app restarts.")
    st.session_state["profile"] = st.text_area("Profile", st.session_state["profile"], height=420)
    ja.set_profile(st.session_state["profile"])
    if not st.session_state["profile"].strip():
        st.warning("Profile is empty. Scores will be meaningless.")

con = ja.db()
st.title("💼 Job Agent")
tab_paste, tab_fetch, tab_track, tab_backup = st.tabs(
    ["Score a JD", "Fetch from sources", "Tracker", "Backup"])

# ---------- tab 1: paste a JD ----------
with tab_paste:
    c1, c2, c3 = st.columns(3)
    title = c1.text_input("Job title")
    company = c2.text_input("Company")
    url = c3.text_input("Job URL")
    desc = st.text_area("Paste the full job description", height=260)
    if st.button("Score and draft", type="primary", disabled=not (title and desc)):
        job = dict(source="manual", company=company or "unknown", title=title,
                   location="", url=url or f"manual:{title}:{company}", description=desc)
        with st.spinner("Scoring and drafting..."):
            try:
                jid = ja.ingest(con, job)
            except Exception as e:
                jid = None
                st.error(f"Failed: {e}")
        if jid is None:
            st.info("Already scored this one, see the Tracker tab.")
        else:
            st.session_state["last_id"] = jid
    if st.session_state.get("last_id"):
        r = con.execute("SELECT score,label,summary,gaps,draft_path FROM jobs WHERE id=?",
                        (st.session_state["last_id"],)).fetchone()
        if r:
            st.metric("Fit score", r[0], r[1])
            st.write(r[2])
            gaps = json.loads(r[3] or "[]")
            if gaps:
                st.write("**Gaps:** " + "; ".join(gaps))
            if r[4] and os.path.exists(r[4]):
                st.markdown(open(r[4]).read())
            else:
                st.caption(f"Below the draft threshold ({ja.THRESHOLD}), so no draft was written.")

# ---------- tab 2: fetch ----------
with tab_fetch:
    st.write("Pulls from the Greenhouse, Lever and RSS sources in `config.json`, filters by "
             "location and title, then scores new roles.")
    st.caption(f"Sources: {len(ja.CFG.get('greenhouse', []))} Greenhouse, "
               f"{len(ja.CFG.get('lever', []))} Lever, {len(ja.CFG.get('rss', []))} RSS")
    limit = st.number_input("Max new roles to score this run", 1, 100, 20)
    if st.button("Run now"):
        done, seen = 0, 0
        bar = st.progress(0.0)
        for job in ja.collect():
            seen += 1
            if not ja.passes_filter(job):
                continue
            try:
                if ja.ingest(con, job):
                    done += 1
                    bar.progress(min(done / limit, 1.0))
            except Exception as e:
                st.warning(f"{job['title']}: {e}")
            if done >= limit:
                break
        st.success(f"Looked at {seen} listings, scored {done} new ones. See the Tracker tab.")

# ---------- tab 3: tracker ----------
with tab_track:
    df = pd.read_sql_query(
        "SELECT id, score, label, title, company, status, url, found_at FROM jobs "
        "ORDER BY score DESC", con)
    if df.empty:
        st.info("Nothing yet. Score a JD or run a fetch.")
    else:
        status_filter = st.multiselect("Status", sorted(df["status"].unique()),
                                       default=["new"] if "new" in set(df["status"]) else None)
        view = df[df["status"].isin(status_filter)] if status_filter else df
        st.dataframe(view, use_container_width=True, hide_index=True)
        pick = st.selectbox("Open a job", view["id"].tolist(),
                            format_func=lambda i: f"{i}  {view.set_index('id').loc[i, 'title']}")
        if pick:
            r = con.execute("SELECT summary,gaps,draft_path,description,status FROM jobs WHERE id=?",
                            (pick,)).fetchone()
            st.write(r[0])
            gaps = json.loads(r[1] or "[]")
            if gaps:
                st.write("**Gaps:** " + "; ".join(gaps))
            if r[2] and os.path.exists(r[2]):
                with st.expander("Draft materials", expanded=True):
                    st.markdown(open(r[2]).read())
            with st.expander("Job description"):
                st.write(r[3])
            opts = ["new", "applied", "referral_sent", "interview", "offer", "rejected", "skipped"]
            new_status = st.selectbox("Status", opts, index=opts.index(r[4]) if r[4] in opts else 0)
            if st.button("Update status"):
                ja.mark(pick, new_status)
                st.rerun()

# ---------- tab 4: backup ----------
with tab_backup:
    st.warning("Community Cloud storage is temporary. If the app restarts or redeploys, the "
               "database is wiped. Download a backup regularly and restore it here when needed.")
    if ja.DB_PATH.exists():
        st.download_button("Download jobs.db", ja.DB_PATH.read_bytes(), "jobs.db")
    up = st.file_uploader("Restore a backup", type=["db"])
    if up is not None and st.button("Restore"):
        con.close()
        ja.DB_PATH.write_bytes(up.getvalue())
        st.success("Restored. Drafts are not included in the backup; they regenerate if you re-score.")
        st.rerun()
