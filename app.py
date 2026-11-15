import random
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Gradelift Practice", page_icon="🦴", layout="centered")

TZ = ZoneInfo("Africa/Lagos")
LOCAL_FILE = "question_bank.xlsx"      # only used when no SHEET_URL secret is set
LETTERS = ["A", "B", "C", "D"]
REQUIRED = ["ID", "Tag", "Module", "Use", "Status", "Question", "Option A", "Option B",
            "Option C", "Option D", "Correct (letter)", "Explanation"]
SESSION_SIZES = [5, 10, 15, 20]
NEW_DAYS = 7


# ------------------------------------------------------------------ helpers
def secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def today():
    return datetime.now(TZ).date()


def now_utc():
    return datetime.now(timezone.utc)


@st.cache_resource
def get_db():
    from supabase import create_client
    return create_client(secret("SUPABASE_URL"), secret("SUPABASE_KEY"))


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


# ------------------------------------------------------------------ questions
@st.cache_data(ttl=300, show_spinner="Loading questions...")
def load_questions(sheet_url: str):
    if sheet_url:
        m = re.search(r"/d/([a-zA-Z0-9_-]+)", sheet_url)
        if m:
            csv_url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
            g = re.search(r"gid=(\d+)", sheet_url)
            if g:
                csv_url += f"&gid={g.group(1)}"
        else:
            csv_url = sheet_url
        df = pd.read_csv(csv_url, dtype=str)
    else:
        df = pd.read_excel(LOCAL_FILE, sheet_name="Questions", dtype=str)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns in the question bank: {', '.join(missing)}")
    for opt in ["Concept", "Source note", "Difficulty", "Release date", "Access", "Section"]:
        if opt not in df.columns:
            df[opt] = ""
    for c in df.columns:
        df[c] = df[c].fillna("").astype(str).str.strip()

    df = df[(df["Status"].str.lower() == "reviewed") & (df["Use"].str.lower() == "practice")]
    df["Correct (letter)"] = df["Correct (letter)"].str.upper()
    df = df[(df["Question"] != "") & (df["Module"] != "") & (df["Tag"] != "") & (df["ID"] != "")
            & df["Correct (letter)"].isin(LETTERS)]
    df = df[df.apply(lambda r: r[f"Option {r['Correct (letter)']}"] != "", axis=1)]
    df = df.drop_duplicates(subset="ID", keep="first")

    out = []
    for _, r in df.iterrows():
        rel = r["Release date"][:10]
        if rel == "":
            release = ""
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", rel):
            try:
                date.fromisoformat(rel)
                release = rel
            except ValueError:
                release = "BAD"
        else:
            release = "BAD"
        out.append({
            "id": r["ID"], "section": r["Section"], "tag": r["Tag"], "module": r["Module"],
            "question": r["Question"],
            "options": [r[f"Option {l}"] for l in LETTERS if r[f"Option {l}"]],
            "correct": r[f"Option {r['Correct (letter)']}"],
            "explanation": r["Explanation"], "concept": r["Concept"], "source": r["Source note"],
            "release": release,
            "access": "free" if r["Access"].lower() == "free" else "paid",
        })
    return out


def visible_questions(all_q, guest):
    t = today()
    out = []
    for q in all_q:
        if guest and q["access"] != "free":
            continue
        if q["release"] == "BAD":
            continue
        if q["release"] and date.fromisoformat(q["release"]) > t:
            continue
        out.append(q)
    return out


def structure(vis):
    """[(section, [(tag, [(module, [questions])])])] in order of first appearance."""
    sections = {}
    for q in vis:
        sections.setdefault(q["section"], {}).setdefault(q["tag"], {}).setdefault(q["module"], []).append(q)
    out = []
    for sec, tags in sections.items():
        tag_list = []
        for tag, mods in tags.items():
            mod_list = sorted(mods.items(), key=lambda kv: natural_key(kv[0]))
            tag_list.append((tag, mod_list))
        out.append((sec, tag_list))
    return out


# ------------------------------------------------------------------ progress
def last_results(rows, valid_ids):
    last = {}
    for r in sorted(rows, key=lambda r: r.get("id") or 0):
        if r["question_id"] in valid_ids:
            last[r["question_id"]] = bool(r["is_correct"])
    return last


def stats(qs, last):
    tried = sum(1 for q in qs if q["id"] in last)
    right = sum(1 for q in qs if last.get(q["id"]) is True)
    retry = sum(1 for q in qs if last.get(q["id"]) is False)
    return {"total": len(qs), "tried": tried, "right": right, "retry": retry, "new": len(qs) - tried}


def status_label(s):
    if s["tried"] < 3:
        return "⚪", "Just started"
    pct = 100 * s["right"] / s["tried"]
    if pct >= 75:
        return "🟢", "Strong"
    if pct >= 50:
        return "🟡", "Revise"
    return "🔴", "Needs work"


def fetch_answers(code, bank):
    out, start = [], 0
    while True:
        res = (get_db().table("answers").select("id,question_id,tag,module,is_correct,created_at")
               .eq("code", code).eq("bank", bank).order("id").range(start, start + 999).execute().data)
        out += res
        if len(res) < 1000:
            break
        start += 1000
    return out


def parse_ts(v):
    try:
        t = pd.to_datetime(v, utc=True)
        return t.to_pydatetime()
    except Exception:
        return None


# ------------------------------------------------------------------ state
def init_state():
    defaults = {"stage": "login", "guest": False, "name": "", "code": "", "rows": [],
                "sel": None, "practice": None, "summary": None, "save_error": False,
                "next_id": 1}
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def logout():
    for k in ["stage", "guest", "name", "code", "rows", "sel", "practice", "summary", "save_error"]:
        st.session_state.pop(k, None)


def contact_button(label="Get the full question bank"):
    url = str(secret("CONTACT_URL"))
    if url:
        st.link_button(label, url, type="primary")
    else:
        st.caption("Message your tutor to get your personal code.")


# ------------------------------------------------------------------ login
def check_code(raw):
    code = raw.strip().upper()
    if not code:
        return None, "Please enter your code."
    rows = get_db().table("codes").select("*").eq("code", code).execute().data
    if not rows:
        return None, "That code was not found. Check it and try again."
    row = rows[0]
    if not row.get("active", True):
        return None, "This code has been switched off. Please contact your tutor."
    exp = row.get("expires_on")
    if exp and date.fromisoformat(str(exp)[:10]) < date.today():
        return None, "This code has expired. Please contact your tutor."
    allowed = [a.strip().lower() for a in str(row.get("access", "all")).split(",")]
    if "all" not in allowed and str(secret("BANK_NAME", "lower-limb")).lower() not in allowed:
        return None, "This code does not include this question bank."
    return row, None


def page_login():
    st.title("🦴 Gradelift Practice")
    st.write("Practise anatomy questions, see where you are strong, and know what to study next.")
    code = st.text_input("Your personal code", placeholder="e.g. GL-AB123")
    if st.button("Continue", type="primary"):
        try:
            row, err = check_code(code)
        except Exception:
            st.error("Could not reach the server. Please try again in a moment.")
            return
        if err:
            st.error(err)
            return
        try:
            rows = fetch_answers(row["code"], BANK)
        except Exception:
            st.error("Could not load your progress. Please try again in a moment.")
            return
        st.session_state.update({"stage": "home", "guest": False, "name": row["student_name"],
                                 "code": row["code"], "rows": rows,
                                 "next_id": max([int(r.get("id") or 0) for r in rows], default=0) + 1})
        st.rerun()
    st.divider()
    st.markdown("**No code yet?**")
    gname = st.text_input("Your first name (optional)", key="gname")
    if st.button("Try the free questions"):
        st.session_state.update({"stage": "home", "guest": True, "name": gname.strip(),
                                 "code": "", "rows": []})
        st.rerun()
    with st.expander("Tutor access"):
        acode = st.text_input("Tutor code", type="password", key="acode")
        if st.button("Open tutor panel"):
            if secret("ADMIN_CODE") and acode.strip() == str(secret("ADMIN_CODE")).strip():
                st.session_state.stage = "admin"
                st.rerun()
            else:
                st.error("That tutor code is not correct.")


# ------------------------------------------------------------------ home / dashboard
def this_week_card(vis, last, rows):
    now = now_utc()
    ids = {q["id"] for q in vis}
    mine = [r for r in rows if r["question_id"] in ids]
    wk, prev = [], []
    for r in mine:
        t = parse_ts(r.get("created_at"))
        if not t:
            continue
        if t >= now - timedelta(days=7):
            wk.append((t, r))
        elif t >= now - timedelta(days=14):
            prev.append((t, r))
    acc = round(100 * sum(1 for _, r in wk if r["is_correct"]) / len(wk)) if wk else None
    days = len({t.astimezone(TZ).date() for t, _ in wk})
    byid = {q["id"]: q for q in vis}
    weak = Counter((byid[qid]["concept"] or byid[qid]["tag"]) for qid, ok in last.items()
                   if ok is False and qid in byid)
    cutoff = today() - timedelta(days=NEW_DAYS - 1)
    new_released = sum(1 for q in vis if q["release"] and date.fromisoformat(q["release"]) >= cutoff
                       and q["id"] not in last)
    with st.container(border=True):
        st.markdown("**📅 This week**")
        c1, c2, c3 = st.columns(3)
        c1.metric("Practised", len(wk), delta=(len(wk) - len(prev)) if prev else None)
        c2.metric("Accuracy", f"{acc}%" if acc is not None else "-")
        c3.metric("Active days", days)
        if weak:
            st.write(f"⚠️ Weakest idea right now: **{weak.most_common(1)[0][0]}**")
        if new_released:
            st.write(f"🆕 **{new_released}** new question(s) released this week")


def page_home(all_q):
    guest = st.session_state.guest
    vis = visible_questions(all_q, guest)
    name = st.session_state.name
    st.title(f"Hi {name} 👋" if name else "Hi 👋")
    if guest:
        st.info("You are using the **free starter questions**. Your progress is not saved.")
        contact_button()
    if not vis:
        st.warning("No questions are available yet. New questions are released every week.")
        footer()
        return
    last = last_results(st.session_state.rows, {q["id"] for q in vis})
    this_week_card(vis, last, st.session_state.rows)

    s = stats(vis, last)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tried", s["tried"])
    c2.metric("Right", s["right"])
    c3.metric("To retry", s["retry"])
    c4.metric("Not yet", s["new"])

    struct = structure(vis)
    rows = []
    for sec, tags in struct:
        for tag, mods in tags:
            qs = [q for _, m in mods for q in m]
            ts = stats(qs, last)
            rows.append({"Topic": tag, "Cleared": ts["right"], "To retry": ts["retry"], "Not yet tried": ts["new"]})
    if rows:
        st.markdown("### Progress by topic")
        st.bar_chart(pd.DataFrame(rows).set_index("Topic"), color=["#2e7d32", "#d32f2f", "#b0b0b0"], horizontal=True)

    cutoff = today() - timedelta(days=NEW_DAYS - 1)
    for sec, tags in struct:
        if sec:
            secq = [q for _, mods in tags for _, m in mods for q in m]
            ss = stats(secq, last)
            st.markdown(f"### {sec}")
            st.progress(ss["right"] / ss["total"] if ss["total"] else 0,
                        text=f"{ss['right']} of {ss['total']} right")
        else:
            st.markdown("### Topics")
        for tag, mods in tags:
            tq = [q for _, m in mods for q in m]
            ts = stats(tq, last)
            has_new = any(q["release"] and date.fromisoformat(q["release"]) >= cutoff and q["id"] not in last for q in tq)
            icon, label = status_label(ts)
            title = f"{icon} {tag}  ·  {ts['right']}/{ts['total']}" + ("  🆕" if has_new else "")
            with st.expander(title, expanded=has_new):
                for mod, qs in mods:
                    ms = stats(qs, last)
                    mi, ml = status_label(ms)
                    st.markdown(f"**{mod}**  {mi} {ml}")
                    st.progress(ms["right"] / ms["total"] if ms["total"] else 0)
                    st.caption(f"Tried {ms['tried']} of {ms['total']} · Right {ms['right']} · "
                               f"To retry {ms['retry']} · Not yet tried {ms['new']}")
                    b1, b2 = st.columns(2)
                    key = f"{tag}|{mod}"
                    if b1.button("Practise", key=f"p_{key}", use_container_width=True):
                        st.session_state.sel = (tag, mod)
                        st.session_state.stage = "setup"
                        st.rerun()
                    if ms["tried"] > 0 and b2.button("Review", key=f"r_{key}", use_container_width=True):
                        st.session_state.sel = (tag, mod)
                        st.session_state.stage = "review"
                        st.rerun()
                    st.divider()
    footer()


def footer():
    st.divider()
    if st.button("Log out"):
        logout()
        st.rerun()


# ------------------------------------------------------------------ practice
def module_questions(all_q):
    tag, mod = st.session_state.sel
    vis = visible_questions(all_q, st.session_state.guest)
    return vis, [q for q in vis if q["tag"] == tag and q["module"] == mod]


def page_setup(all_q):
    tag, mod = st.session_state.sel
    vis, qs = module_questions(all_q)
    last = last_results(st.session_state.rows, {q["id"] for q in vis})
    s = stats(qs, last)
    st.title(f"{tag}")
    st.subheader(mod)
    st.caption(f"Tried {s['tried']} of {s['total']} · Right {s['right']} · To retry {s['retry']} · Not yet tried {s['new']}")
    pool = s["retry"] + s["new"]
    again = False
    if pool == 0:
        st.success("✅ You have cleared every question released so far in this module.")
        st.write("You can practise them again, or come back when new questions are released.")
        pool = s["total"]
        again = True
    else:
        st.write("Questions you got wrong come first, then new ones.")
    sizes = [n for n in SESSION_SIZES if n < pool] + [pool]
    sizes = sorted(set(sizes))
    n = st.selectbox("How many questions?", sizes, index=min(1, len(sizes) - 1),
                     format_func=lambda x: f"All {x}" if x == pool else str(x))
    if st.button("Start" if not again else "Practise again", type="primary"):
        start_session(qs, last, n, again)
        st.rerun()
    if st.button("← Back"):
        st.session_state.stage = "home"
        st.rerun()


def start_session(qs, last, n, again):
    retry = [q for q in qs if last.get(q["id"]) is False]
    new = [q for q in qs if q["id"] not in last]
    random.shuffle(retry)
    random.shuffle(new)
    pool = retry + new
    if not pool or again:
        pool = qs[:]
        random.shuffle(pool)
    picked = pool[:n]
    items = []
    for q in picked:
        opts = q["options"][:]
        random.shuffle(opts)
        items.append({**q, "options": opts})
    attempt_id = None
    st.session_state.save_error = False
    if not st.session_state.guest:
        try:
            attempt_id = get_db().table("attempts").insert({
                "code": st.session_state.code, "bank": BANK, "mode": "practice",
                "started_at": now_utc().isoformat()}).execute().data[0]["id"]
        except Exception:
            st.session_state.save_error = True
    st.session_state.practice = {"items": items, "idx": 0, "submitted": False, "chosen": None,
                                 "correct": 0, "attempt_id": attempt_id, "answered": []}
    st.session_state.stage = "practice"


def save_answer(item, chosen, ok):
    p = st.session_state.practice
    tag, mod = st.session_state.sel
    row = {"id": st.session_state.next_id, "question_id": item["id"], "tag": item["tag"],
           "module": item["module"], "is_correct": ok, "created_at": now_utc().isoformat()}
    st.session_state.next_id += 1
    st.session_state.rows.append(row)
    if st.session_state.guest or p["attempt_id"] is None:
        return
    try:
        get_db().table("answers").insert({
            "attempt_id": p["attempt_id"], "code": st.session_state.code, "bank": BANK,
            "question_id": item["id"], "tag": item["tag"], "module": item["module"],
            "chosen": chosen, "correct": item["correct"], "is_correct": ok, "mode": "practice"}).execute()
    except Exception:
        st.session_state.save_error = True


def page_practice():
    p = st.session_state.practice
    items, idx = p["items"], p["idx"]
    item = items[idx]
    st.progress((idx + 1) / len(items))
    st.caption(f"Question {idx + 1} of {len(items)}")
    st.markdown(f"**{item['question']}**")
    if not p["submitted"]:
        choice = st.radio("Choose one", item["options"], index=None, key=f"c_{idx}",
                          label_visibility="collapsed")
        if st.button("Submit answer", type="primary", disabled=choice is None):
            ok = choice == item["correct"]
            p["chosen"], p["submitted"] = choice, True
            p["correct"] += int(ok)
            p["answered"].append((item, choice, ok))
            save_answer(item, choice, ok)
            st.rerun()
    else:
        if p["chosen"] == item["correct"]:
            st.success(f"Correct: {item['correct']}")
        else:
            st.error(f"Not quite. You chose: {p['chosen']}")
            st.info(f"Correct answer: {item['correct']}")
        if item["explanation"]:
            st.write(item["explanation"])
        last_q = idx == len(items) - 1
        if st.button("Finish" if last_q else "Next question", type="primary"):
            p["submitted"] = False
            if last_q:
                finish_session()
            else:
                p["idx"] += 1
            st.rerun()
    if st.session_state.save_error:
        st.caption("⚠️ Your last answer could not be saved. Check your connection.")


def finish_session():
    p = st.session_state.practice
    if p["attempt_id"] is not None and not st.session_state.guest:
        try:
            get_db().table("attempts").update({
                "submitted_at": now_utc().isoformat(), "score": p["correct"],
                "total": len(p["items"])}).eq("id", p["attempt_id"]).execute()
        except Exception:
            pass
    st.session_state.summary = {"correct": p["correct"], "total": len(p["items"]),
                                "answered": p["answered"]}
    st.session_state.stage = "summary"


def page_summary(all_q):
    sm = st.session_state.summary
    tag, mod = st.session_state.sel
    vis, qs = module_questions(all_q)
    last = last_results(st.session_state.rows, {q["id"] for q in vis})
    s = stats(qs, last)
    st.title("Session complete")
    st.subheader(f"{sm['correct']} out of {sm['total']} right")
    if st.session_state.save_error:
        st.warning("Some answers could not be saved. Please check your connection.")
    elif not st.session_state.guest:
        st.caption("✅ Your progress has been saved.")
    st.caption(f"{tag} · {mod}: right {s['right']} of {s['total']} · to retry {s['retry']} · not yet tried {s['new']}")
    wrong = [(it, ch) for it, ch, ok in sm["answered"] if not ok]
    if wrong:
        st.write(f"**{len(wrong)} question(s) will come back** next time you practise this module.")
    if s["new"] == 0 and s["tried"] > 0:
        st.success("🎉 You have tried every question released so far in this module. Here is your review.")
        render_review(qs, last, tag, mod)
    else:
        st.info(f"Your module review unlocks when you have tried every released question "
                f"({s['new']} left). You can also see it now with the Review button on your dashboard.")
    if st.session_state.guest:
        contact_button("Unlock the full question bank")
    c1, c2 = st.columns(2)
    if c1.button("Practise more", use_container_width=True):
        st.session_state.stage = "setup"
        st.rerun()
    if c2.button("Dashboard", use_container_width=True):
        st.session_state.stage = "home"
        st.rerun()


# ------------------------------------------------------------------ review
def render_review(qs, last, tag, mod):
    s = stats(qs, last)
    icon, label = status_label(s)
    st.markdown(f"### {icon} {tag}: {mod}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tried", f"{s['tried']}/{s['total']}")
    c2.metric("Right", s["right"])
    c3.metric("To retry", s["retry"])
    c4.metric("Not yet", s["new"])
    if s["tried"]:
        st.write(f"**{label}**: {round(100 * s['right'] / s['tried'])}% of the questions you have tried are cleared.")
    retry = [q for q in qs if last.get(q["id"]) is False]
    cleared = [q for q in qs if last.get(q["id"]) is True]
    label_of = lambda q: q["concept"] or q["tag"]
    weak = Counter(label_of(q) for q in retry)
    seen = Counter(label_of(q) for q in qs if q["id"] in last)
    done = Counter(label_of(q) for q in cleared)
    strong = [c for c in seen if done[c] == seen[c]]
    if weak:
        st.markdown("**📚 What to study**")
        for c, n in weak.most_common(8):
            st.write(f"- {c} ({n} to retry)")
        notes = Counter(q["source"] for q in retry if q["source"])
        if notes:
            st.write("Revise from: " + "; ".join(n for n, _ in notes.most_common(3)))
    else:
        st.success("Nothing to retry right now. Well done.")
    if strong:
        st.markdown("**✅ Going well (you can skip these for now)**")
        st.write(", ".join(strong[:8]))
    if s["new"]:
        st.caption(f"{s['new']} released question(s) not yet tried, so this review is partial.")


def page_review(all_q):
    tag, mod = st.session_state.sel
    vis, qs = module_questions(all_q)
    last = last_results(st.session_state.rows, {q["id"] for q in vis})
    st.title("Module review")
    render_review(qs, last, tag, mod)
    c1, c2 = st.columns(2)
    if c1.button("Practise this module", use_container_width=True):
        st.session_state.stage = "setup"
        st.rerun()
    if c2.button("Dashboard", use_container_width=True):
        st.session_state.stage = "home"
        st.rerun()


# ------------------------------------------------------------------ tutor panel
def make_code():
    return "GL-" + "".join(random.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(5))


def page_admin(all_q):
    st.title("Tutor panel")
    t1, t2, t3 = st.tabs(["New code", "Codes", "Content"])
    with t1:
        name = st.text_input("Student name", key="adm_name")
        access = st.selectbox("Access", ["all", "lower-limb", "upper-limb"], key="adm_access")
        exp = st.date_input("Expires on", value=today() + timedelta(days=150), key="adm_exp")
        if st.button("Create code", type="primary"):
            if not name.strip():
                st.error("Please enter the student's name.")
            else:
                try:
                    db = get_db()
                    code = make_code()
                    for _ in range(10):
                        if not db.table("codes").select("code").eq("code", code).execute().data:
                            break
                        code = make_code()
                    db.table("codes").insert({"code": code, "student_name": name.strip(), "access": access,
                                              "expires_on": exp.isoformat(), "active": True}).execute()
                    st.success("Code created. Send it to the student:")
                    st.code(code)
                except Exception as e:
                    st.error(f"Could not create the code: {e}")
    with t2:
        try:
            rows = get_db().table("codes").select("*").order("created_at", desc=True).limit(60).execute().data
        except Exception as e:
            rows = []
            st.error(f"Could not load codes: {e}")
        if rows:
            st.dataframe(pd.DataFrame(rows)[["code", "student_name", "access", "expires_on", "active"]],
                         hide_index=True)
            pick = st.selectbox("Switch a code on or off", [r["code"] for r in rows], key="adm_pick")
            cur = next(r for r in rows if r["code"] == pick)
            label = "Switch OFF" if cur.get("active", True) else "Switch ON"
            if st.button(label):
                get_db().table("codes").update({"active": not cur.get("active", True)}).eq("code", pick).execute()
                st.rerun()
    with t3:
        vis = visible_questions(all_q, False)
        bad = [q["id"] for q in all_q if q["release"] == "BAD"]
        free = sum(1 for q in vis if q["access"] == "free")
        scheduled = sum(1 for q in all_q if q["release"] not in ("", "BAD") and date.fromisoformat(q["release"]) > today())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Live now", len(vis))
        c2.metric("Free", free)
        c3.metric("Scheduled", scheduled)
        c4.metric("Bad dates", len(bad))
        if bad:
            st.warning("These questions have a release date I cannot read, so they are hidden. "
                       "Use the format YYYY-MM-DD: " + ", ".join(bad[:20]))
        tab = Counter((q["tag"], q["module"]) for q in vis)
        if tab:
            df = pd.DataFrame([{"Topic": t, "Module": m, "Live questions": n} for (t, m), n in tab.items()])
            st.dataframe(df, hide_index=True)
        st.caption("Only questions with Status = Reviewed and Use = Practice are served.")
    if st.button("Log out"):
        logout()
        st.rerun()


# ------------------------------------------------------------------ main
BANK = str(secret("BANK_NAME", "lower-limb"))
init_state()
stage = st.session_state.stage
if stage == "login":
    page_login()
else:
    try:
        ALL_Q = load_questions(str(secret("SHEET_URL")))
    except Exception as e:
        st.error("Could not load the questions.")
        st.caption(str(e))
        st.stop()
    if stage == "admin":
        page_admin(ALL_Q)
    elif stage == "home":
        page_home(ALL_Q)
    elif stage == "setup" and st.session_state.sel:
        page_setup(ALL_Q)
    elif stage == "practice" and st.session_state.practice:
        page_practice()
    elif stage == "summary" and st.session_state.summary:
        page_summary(ALL_Q)
    elif stage == "review" and st.session_state.sel:
        page_review(ALL_Q)
    else:
        st.session_state.stage = "home"
        st.rerun()
