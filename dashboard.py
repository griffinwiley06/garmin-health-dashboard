"""
Streamlit health dashboard. Reads from health.db (populated by pull_garmin.py).

Run with: streamlit run dashboard.py
"""

import os
import re
import html
import sqlite3
import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import recurring_ical_events
import streamlit as st
import streamlit.components.v1 as components
from icalendar import Calendar as ICalendar
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "health.db"
STEP_GOAL = 12000

# Name used in the dashboard greeting.
USER_NAME = os.getenv("USER_NAME", "there")

# Google Calendar "Secret address in iCal format" for each calendar to show
# (Settings -> your calendar -> Integrate calendar). Comma- or newline-separated
# to pull from several calendars at once. Local .ics file paths also work.
GCAL_ICS_SOURCES = tuple(
    s.strip() for s in re.split(r"[,\n]", os.getenv("GCAL_ICS_URL", "")) if s.strip()
)
# Per-calendar accent colors, cycled in source order.
CAL_PALETTE = ["#4285F4", "#1D9E75", "#E8A33D", "#A879E6", "#E05C5C", "#24A0A0"]
# Week runs Sunday -> Saturday.
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

GREEN = "#1D9E75"
AMBER = "#E8A33D"
RED = "#E05C5C"
GRAY = "#9AA0A6"
CARD_BG = "#1e1e1e"

# Used only to estimate BMR (Mifflin-St Jeor) for the Calories card subtext,
# since garmin_daily only stores a single total-calories figure. Override via
# .env (BIO_AGE, BIO_SEX, BIO_WEIGHT_LB, BIO_HEIGHT_IN) for an accurate split.
BIO_AGE = int(os.getenv("BIO_AGE", 30))
BIO_SEX = os.getenv("BIO_SEX", "male").lower()
BIO_WEIGHT_LB = float(os.getenv("BIO_WEIGHT_LB", 170))
BIO_HEIGHT_IN = float(os.getenv("BIO_HEIGHT_IN", 69))


st.set_page_config(page_title="Health Dashboard", layout="wide")


@st.cache_resource
def get_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@st.cache_data(ttl=30)
def get_today_row(_conn, today):
    df = pd.read_sql_query(
        "SELECT * FROM garmin_daily WHERE date = ?", _conn, params=(today,)
    )
    if df.empty:
        df = pd.read_sql_query(
            "SELECT * FROM garmin_daily ORDER BY date DESC LIMIT 1", _conn
        )
    return df.iloc[0] if not df.empty else None


@st.cache_data(ttl=30)
def get_week_avg(_conn, column):
    query = f"SELECT AVG({column}) AS avg_val FROM garmin_daily WHERE date >= date('now', '-7 days')"
    df = pd.read_sql_query(query, _conn)
    val = df["avg_val"].iloc[0]
    return None if pd.isna(val) else val


@st.cache_data(ttl=30)
def get_last_7_days_steps(_conn):
    df = pd.read_sql_query(
        """
        SELECT date, steps FROM (
            SELECT date, steps FROM garmin_daily ORDER BY date DESC LIMIT 7
        ) ORDER BY date ASC
        """,
        _conn,
    )
    return df


@st.cache_data(ttl=30)
def get_recent_runs(_conn):
    return pd.read_sql_query(
        "SELECT date, distance_mi, duration FROM garmin_runs ORDER BY date DESC", _conn
    )


def _load_ics(source):
    """Accept either an http(s) iCal URL or a local .ics file path."""
    if source.startswith("http://") or source.startswith("https://"):
        resp = requests.get(source, timeout=15)
        resp.raise_for_status()
        return resp.text
    with open(source, "r", encoding="utf-8") as f:
        return f.read()


@st.cache_data(ttl=300)
def get_week_events(ics_sources, week_start_iso):
    """Merge the Mon-Sun week of events from one or more iCal sources.

    Returns (days, calendars, errors) where days is {weekday 0-6: [events]},
    calendars is [{name, color}] for the legend, and errors is [(name, msg)]
    for any source that failed to load. Recurring events are expanded."""
    week_start = datetime.date.fromisoformat(week_start_iso)
    week_end = week_start + datetime.timedelta(days=7)

    days = {i: [] for i in range(7)}
    calendars = []
    errors = []

    for ci, source in enumerate(ics_sources):
        color = CAL_PALETTE[ci % len(CAL_PALETTE)]
        fallback_name = f"Calendar {ci + 1}"
        try:
            cal = ICalendar.from_ical(_load_ics(source))
        except Exception as exc:
            calendars.append({"name": fallback_name, "color": color})
            errors.append((fallback_name, str(exc)))
            continue

        name = str(cal.get("X-WR-CALNAME") or fallback_name)
        calendars.append({"name": name, "color": color})

        occurrences = recurring_ical_events.of(cal).between(week_start, week_end)
        for comp in occurrences:
            start = comp.get("DTSTART").dt
            if isinstance(start, datetime.datetime):
                local = start.astimezone() if start.tzinfo else start
                day = local.date()
                all_day = False
                time_label = local.strftime("%-I:%M %p")
                sort_key = (1, local.time())
            else:
                day = start
                all_day = True
                time_label = "All day"
                sort_key = (0, datetime.time.min)

            idx = (day - week_start).days
            if 0 <= idx < 7:
                days[idx].append(
                    {
                        "time": time_label,
                        "summary": str(comp.get("SUMMARY", "(no title)")),
                        "all_day": all_day,
                        "sort": sort_key,
                        "color": color,
                        "calendar": name,
                    }
                )

    for i in days:
        days[i].sort(key=lambda e: e["sort"])
    return days, calendars, errors


def render_html(html):
    """st.markdown treats lines indented 4+ spaces as code blocks, so strip
    leading whitespace before handing multi-line HTML off to it."""
    st.markdown(re.sub(r"^[ \t]+", "", html, flags=re.MULTILINE), unsafe_allow_html=True)


def format_k(value):
    if value is None or pd.isna(value):
        return "–"
    value = int(value)
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def diff_status(today_val, avg_val, invert=False):
    if today_val is None or avg_val is None or pd.isna(today_val) or pd.isna(avg_val):
        return GRAY, "No 7-day average yet"
    diff = today_val - avg_val
    good = diff < -3 if not invert else diff > 3
    bad = diff > 3 if not invert else diff < -3
    color = AMBER if bad else GREEN if good else GRAY
    sign = "+" if diff >= 0 else ""
    return color, f"{sign}{diff:.0f} vs 7-day avg ({avg_val:.0f})"


def sleep_label(score):
    if score is None or pd.isna(score):
        return "–", GRAY
    if score > 80:
        return "Good", GREEN
    if score >= 60:
        return "Fair", AMBER
    return "Low", RED


def estimate_bmr():
    if BIO_SEX.startswith("f"):
        offset = -161
    else:
        offset = 5
    weight_kg = BIO_WEIGHT_LB * 0.453592
    height_cm = BIO_HEIGHT_IN * 2.54
    return 10 * weight_kg + 6.25 * height_cm - 5 * BIO_AGE + offset


def steps_ring_svg(steps, goal):
    steps = steps or 0
    size, stroke = 132, 13
    radius = (size - stroke) / 2
    circumference = 2 * 3.14159265 * radius
    pct = max(0.0, min(steps / goal, 1.0))
    offset = circumference * (1 - pct)
    center = size / 2
    return f"""
    <svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">
        <circle cx="{center}" cy="{center}" r="{radius}" fill="none"
                stroke="#333" stroke-width="{stroke}"/>
        <circle cx="{center}" cy="{center}" r="{radius}" fill="none"
                stroke="{GREEN}" stroke-width="{stroke}" stroke-linecap="round"
                stroke-dasharray="{circumference:.2f}" stroke-dashoffset="{offset:.2f}"
                transform="rotate(-90 {center} {center})"/>
    </svg>
    """


st.markdown(
    f"""
    <style>
    header[data-testid="stHeader"] {{ display: none; }}
    .block-container {{ padding-top: 1.5rem; padding-bottom: 1rem; }}
    .greeting {{
        display: flex; justify-content: space-between; align-items: baseline;
        flex-wrap: wrap; gap: 0.4rem 1.5rem; margin: 0 0 1rem 0;
    }}
    .greet-hi {{ font-size: 2.5rem; font-weight: 700; color: #fafafa; line-height: 1.1; }}
    .greet-when {{ font-size: 2rem; color: {GRAY}; white-space: nowrap; }}
    .card {{
        background-color: {CARD_BG};
        border-radius: 12px;
        padding: 0.8rem 0.9rem;
        height: 100%;
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }}
    .card-title {{
        font-size: 0.8rem;
        color: {GRAY};
        margin-bottom: 0.3rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }}
    .card-big {{ font-size: 1.85rem; font-weight: 700; color: #fafafa; }}
    .card-sub {{ font-size: 0.82rem; color: {GRAY}; margin-top: 0.15rem; }}
    .status-line {{ font-size: 0.82rem; font-weight: 600; margin-top: 0.3rem; }}
    .bento-row1 {{
        display: grid;
        grid-template-columns: 1.4fr 1fr 1fr;
        grid-template-rows: 1fr 1fr;
        gap: 0.7rem;
        margin-bottom: 0.7rem;
    }}
    .card-steps {{ grid-column: 1; grid-row: 1 / span 2; }}
    .card-hr {{ grid-column: 2; grid-row: 1; }}
    .card-sleep {{ grid-column: 3; grid-row: 1; }}
    .card-hrv {{ grid-column: 2; grid-row: 2; }}
    .card-cal {{ grid-column: 3; grid-row: 2; }}
    .steps-inner {{ display: flex; align-items: center; gap: 1.2rem; }}
    .runs-row {{
        display: flex; justify-content: space-between; align-items: baseline;
        padding: 0.4rem 0; border-bottom: 1px solid #2a2a2a; font-size: 0.92rem;
    }}
    .runs-row:last-child {{ border-bottom: none; }}
    .runs-date {{ color: {GRAY}; font-size: 0.85rem; }}
    .st-key-steps_chart_card {{
        background-color: {CARD_BG};
        border-radius: 12px;
        padding: 0.8rem 0.9rem;
    }}
    .section-title {{
        font-size: 0.8rem;
        color: {GRAY};
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin: 0.8rem 0 0.5rem 0;
    }}
    .week-grid {{
        display: grid;
        grid-template-columns: repeat(7, 1fr);
        gap: 0.6rem;
    }}
    .day-col {{
        background-color: {CARD_BG};
        border-radius: 12px;
        padding: 0.55rem 0.5rem;
        min-height: 88px;
        border: 1px solid transparent;
    }}
    .day-col.today {{ border-color: {GREEN}; }}
    .day-head {{
        font-size: 0.72rem; color: {GRAY};
        text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 0.5rem;
    }}
    .day-head .num {{
        display: block; color: #fafafa; font-weight: 700; font-size: 1.15rem;
    }}
    .day-col.today .day-head .num {{ color: {GREEN}; }}
    .evt {{
        background-color: #2a2a2a; border-radius: 6px;
        border-left: 3px solid {GRAY};
        padding: 0.3rem 0.4rem; margin-bottom: 0.35rem;
        font-size: 0.76rem; line-height: 1.25; color: #eaeaea;
    }}
    .evt .t {{ display: block; font-size: 0.68rem; }}
    .evt-empty {{ color: #555; font-size: 0.85rem; }}
    .cal-legend {{
        display: flex; flex-wrap: wrap; gap: 0.75rem 1.25rem;
        margin: -0.35rem 0 0.85rem 0;
    }}
    .cal-chip {{
        display: inline-flex; align-items: center; gap: 0.4rem;
        font-size: 0.78rem; color: {GRAY};
    }}
    .cal-chip .dot {{
        width: 10px; height: 10px; border-radius: 3px; display: inline-block;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

now = datetime.datetime.now()
if now.hour < 12:
    greeting = "Good morning"
elif now.hour < 17:
    greeting = "Good afternoon"
else:
    greeting = "Good evening"
render_html(
    f'<div class="greeting">'
    f'<div class="greet-hi">{greeting}, {html.escape(USER_NAME)}</div>'
    f'<div class="greet-when">{now.strftime("%A, %B %-d, %Y")} &middot; '
    f'{now.strftime("%-I:%M %p")}</div>'
    f"</div>"
)

# Floating fullscreen toggle. Injected into the parent document so the click
# is a real user gesture on the top page (required to enter fullscreen).
components.html(
    """
    <script>
    (function () {
        const doc = window.parent.document;
        if (doc.getElementById('fs-toggle')) return;
        const btn = doc.createElement('button');
        btn.id = 'fs-toggle';
        btn.type = 'button';
        btn.title = 'Toggle fullscreen';
        btn.innerHTML = '\\u26F6';
        btn.style.cssText =
            'position:fixed;top:12px;right:14px;z-index:100000;width:38px;height:38px;'
            + 'border-radius:10px;border:1px solid #333;background:#1e1e1e;color:#eaeaea;'
            + 'font-size:18px;cursor:pointer;display:flex;align-items:center;'
            + 'justify-content:center;line-height:1;box-shadow:0 2px 8px rgba(0,0,0,0.45);';
        btn.onmouseenter = function () { btn.style.background = '#2a2a2a'; };
        btn.onmouseleave = function () { btn.style.background = '#1e1e1e'; };
        const sync = function () {
            btn.innerHTML = doc.fullscreenElement ? '\\u2715' : '\\u26F6';
        };
        btn.onclick = function () {
            if (doc.fullscreenElement) {
                doc.exitFullscreen();
            } else {
                const p = doc.documentElement.requestFullscreen();
                if (p && p.catch) p.catch(function () {});
            }
        };
        doc.addEventListener('fullscreenchange', sync);
        doc.body.appendChild(btn);
    })();
    </script>
    """,
    height=0,
)

conn = get_connection()

tables = {
    row[0]
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
}
if not {"garmin_daily", "garmin_runs"}.issubset(tables):
    st.warning(
        "health.db has no data yet. Run `python pull_garmin.py full` first "
        "to create the tables and pull today's stats."
    )
    st.stop()

today = datetime.date.today().isoformat()
today_row = get_today_row(conn, today)

if today_row is None:
    st.warning("No data found in health.db yet. Run pull_garmin.py to populate it.")
    st.stop()

steps = today_row["steps"] if pd.notna(today_row["steps"]) else 0
resting_hr = today_row["resting_hr"] if pd.notna(today_row["resting_hr"]) else None
calories = today_row["calories"] if pd.notna(today_row["calories"]) else None
sleep_score = today_row["sleep_score"] if pd.notna(today_row["sleep_score"]) else None
hrv = today_row["hrv"] if pd.notna(today_row["hrv"]) else None

avg_hr = get_week_avg(conn, "resting_hr")
avg_hrv = get_week_avg(conn, "hrv")

hr_color, hr_status_text = diff_status(resting_hr, avg_hr, invert=False)
hrv_color, hrv_status_text = diff_status(hrv, avg_hrv, invert=True)
sleep_text, sleep_color = sleep_label(sleep_score)

bmr = estimate_bmr()
if calories is not None:
    active = max(calories - bmr, 0)
    cal_sub = f"{bmr:.0f} BMR + {active:.0f} active (est.)"
else:
    cal_sub = "No data"

row1_html = f"""
<div class="bento-row1">
    <div class="card card-steps">
        <div class="card-title">Steps today</div>
        <div class="steps-inner">
            {steps_ring_svg(steps, STEP_GOAL)}
            <div>
                <div class="card-big">{steps:,.0f}</div>
                <div class="card-sub">of {STEP_GOAL:,} step goal</div>
            </div>
        </div>
    </div>
    <div class="card card-hr">
        <div class="card-title">Resting HR</div>
        <div class="card-big">{f"{resting_hr:.0f}" if resting_hr is not None else "–"} <span style="font-size:1rem;color:{GRAY};">bpm</span></div>
        <div class="status-line" style="color:{hr_color};">{hr_status_text}</div>
    </div>
    <div class="card card-sleep">
        <div class="card-title">Sleep score</div>
        <div class="card-big">{f"{sleep_score:.0f}" if sleep_score is not None else "–"}</div>
        <div class="status-line" style="color:{sleep_color};">{sleep_text}</div>
    </div>
    <div class="card card-hrv">
        <div class="card-title">HRV</div>
        <div class="card-big">{f"{hrv:.0f}" if hrv is not None else "–"} <span style="font-size:1rem;color:{GRAY};">ms</span></div>
        <div class="status-line" style="color:{hrv_color};">{hrv_status_text}</div>
    </div>
    <div class="card card-cal">
        <div class="card-title">Calories</div>
        <div class="card-big">{f"{calories:,.0f}" if calories is not None else "–"}</div>
        <div class="card-sub">{cal_sub}</div>
    </div>
</div>
"""
render_html(row1_html)

left, right = st.columns([1.6, 1])

with left:
    with st.container(key="steps_chart_card"):
        st.markdown('<div class="card-title">Steps, last 7 days</div>', unsafe_allow_html=True)
        steps_df = get_last_7_days_steps(conn)
        if steps_df.empty:
            st.caption("No step data in the last 7 days.")
        else:
            labels = [format_k(v) for v in steps_df["steps"]]
            x_labels = [
                datetime.date.fromisoformat(str(d)[:10]).strftime("%b %-d")
                for d in steps_df["date"]
            ]
            fig = go.Figure(
                go.Scatter(
                    x=x_labels,
                    y=steps_df["steps"],
                    mode="lines+markers+text",
                    text=labels,
                    textposition="top center",
                    textfont=dict(color="#fafafa", size=13),
                    line=dict(color=GREEN, width=3),
                    marker=dict(color=GREEN, size=8),
                )
            )
            fig.update_layout(
                paper_bgcolor=CARD_BG,
                plot_bgcolor=CARD_BG,
                margin=dict(l=10, r=10, t=30, b=10),
                height=200,
                xaxis=dict(showgrid=False, color=GRAY, type="category"),
                yaxis=dict(
                    showgrid=False,
                    showticklabels=False,
                    zeroline=False,
                    range=[0, steps_df["steps"].max() * 1.25],
                ),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

with right:
    runs_df = get_recent_runs(conn)
    rows_html = ""
    if runs_df.empty:
        rows_html = '<div class="card-sub">No recent runs.</div>'
    else:
        for _, r in runs_df.iterrows():
            date_str = str(r["date"]).split(" ")[0]
            rows_html += f"""
            <div class="runs-row">
                <span class="runs-date">{date_str}</span>
                <span>{r['distance_mi']:.1f} mi</span>
                <span>{r['duration']}</span>
            </div>
            """
    render_html(f'<div class="card"><div class="card-title">Recent runs</div>{rows_html}</div>')


# ---- Row 3: Google Calendar week view (with prev/next navigation) ----
if "week_offset" not in st.session_state:
    st.session_state.week_offset = 0


def _shift_week(delta):
    st.session_state.week_offset += delta


def _reset_week():
    st.session_state.week_offset = 0


today_date = datetime.date.today()
# Week starts Sunday: weekday() is Mon=0..Sun=6, so (weekday+1) % 7 days back.
current_week_start = today_date - datetime.timedelta(days=(today_date.weekday() + 1) % 7)
week_offset = st.session_state.week_offset
week_start = current_week_start + datetime.timedelta(days=7 * week_offset)

if week_offset == 0:
    week_title = "This week"
else:
    week_title = f"Week of {week_start.strftime('%b %-d, %Y')}"

if not GCAL_ICS_SOURCES:
    st.markdown('<div class="section-title">This week</div>', unsafe_allow_html=True)
    render_html(
        '<div class="card"><div class="card-sub">'
        "Set <code>GCAL_ICS_URL</code> in .env to your Google Calendar "
        "&ldquo;Secret address in iCal format&rdquo; to show your week here. "
        "Separate multiple calendars with commas."
        "</div></div>"
    )
else:
    st.markdown(
        f'<div class="section-title">{week_title}</div>', unsafe_allow_html=True
    )
    nav = st.columns([1.3, 1.3, 1.3, 6.1])
    nav[0].button(
        "◀", key="cal_prev", on_click=_shift_week, args=(-1,),
        help="Previous week", use_container_width=True,
    )
    nav[1].button(
        "Today", key="cal_today", on_click=_reset_week,
        disabled=week_offset == 0, use_container_width=True,
    )
    nav[2].button(
        "▶", key="cal_next", on_click=_shift_week, args=(1,),
        help="Next week", use_container_width=True,
    )

    try:
        week_events, calendars, cal_errors = get_week_events(
            GCAL_ICS_SOURCES, week_start.isoformat()
        )
    except Exception as exc:
        week_events = None
        render_html(
            f'<div class="card"><div class="card-sub">'
            f"Couldn't load calendars: {html.escape(str(exc))}</div></div>"
        )

    if week_events is not None:
        if len(calendars) > 1:
            legend = "".join(
                f'<span class="cal-chip"><span class="dot" '
                f'style="background:{c["color"]}"></span>{html.escape(c["name"])}</span>'
                for c in calendars
            )
            render_html(f'<div class="cal-legend">{legend}</div>')

        for name, msg in cal_errors:
            render_html(
                f'<div class="card"><div class="card-sub">'
                f"Couldn't load {html.escape(name)}: {html.escape(msg)}</div></div>"
            )

        cols_html = ""
        for i in range(7):
            d = week_start + datetime.timedelta(days=i)
            today_cls = " today" if d == today_date else ""
            events = week_events.get(i, [])
            if events:
                evts_html = ""
                for e in events:
                    evts_html += (
                        f'<div class="evt" style="border-left-color:{e["color"]};">'
                        f'<span class="t" style="color:{e["color"]};">'
                        f'{html.escape(e["time"])}</span>'
                        f'{html.escape(e["summary"])}</div>'
                    )
            else:
                evts_html = '<div class="evt-empty">—</div>'
            cols_html += (
                f'<div class="day-col{today_cls}">'
                f'<div class="day-head">{WEEKDAYS[i]}<span class="num">{d.day}</span></div>'
                f"{evts_html}</div>"
            )
        render_html(f'<div class="week-grid">{cols_html}</div>')
