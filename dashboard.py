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
# Week runs Sunday -> Saturday.
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# ---- Palette: black, greys, one light blue accent ----
BG = "#000000"
PANEL = "#111315"
LINE = "#23272C"
TEXT = "#F2F4F6"
TEXT_2 = "#A3ABB4"
TEXT_3 = "#6B737C"
ACCENT = "#8CCBFF"        # light blue: progress, goals met, improvements
ACCENT_MUTED = "#35536E"  # same hue, recessive: days short of goal
WARN = "#E2B65C"          # reserved for a low sleep score

# Calendar identity colors, validated for color-vision deficiency on the panel
# surface. A third or later calendar falls back to neutral gray, named in the
# legend, rather than a generated hue.
CAL_PALETTE = ["#4797E0", "#2A5DB8"]
CAL_OVERFLOW = "#8E98A4"

FONT_TEXT = "'Barlow', system-ui, sans-serif"
FONT_NUM = "'Barlow Condensed', 'Barlow', system-ui, sans-serif"

# Used only to estimate BMR (Mifflin-St Jeor) for the Calories subtext, since
# garmin_daily only stores a single total-calories figure. Override via .env
# (BIO_AGE, BIO_SEX, BIO_WEIGHT_LB, BIO_HEIGHT_IN) for an accurate split.
BIO_AGE = int(os.getenv("BIO_AGE", 30))
BIO_SEX = os.getenv("BIO_SEX", "male").lower()
BIO_WEIGHT_LB = float(os.getenv("BIO_WEIGHT_LB", 170))
BIO_HEIGHT_IN = float(os.getenv("BIO_HEIGHT_IN", 69))


st.set_page_config(page_title="Health Dashboard", layout="wide")


# ---------------------------------------------------------------- data access

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


def cal_color(index):
    return CAL_PALETTE[index] if index < len(CAL_PALETTE) else CAL_OVERFLOW


@st.cache_data(ttl=300)
def get_week_events(ics_sources, week_start_iso):
    """Merge the Sun-Sat week of events from one or more iCal sources.

    Returns (days, calendars, errors) where days is {weekday 0-6: [events]},
    calendars is [{name, color}] for the legend, and errors is [(name, msg)]
    for any source that failed to load. Recurring events are expanded."""
    week_start = datetime.date.fromisoformat(week_start_iso)
    week_end = week_start + datetime.timedelta(days=7)

    days = {i: [] for i in range(7)}
    calendars = []
    errors = []

    for ci, source in enumerate(ics_sources):
        color = cal_color(ci)
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
                time_label = local.strftime("%-I:%M %p")
                sort_key = (1, local.time())
            else:
                day = start
                time_label = "All day"
                sort_key = (0, datetime.time.min)

            idx = (day - week_start).days
            if 0 <= idx < 7:
                days[idx].append(
                    {
                        "time": time_label,
                        "summary": str(comp.get("SUMMARY", "(no title)")),
                        "sort": sort_key,
                        "color": color,
                    }
                )

    for i in days:
        days[i].sort(key=lambda e: e["sort"])
    return days, calendars, errors


# ------------------------------------------------------------------- helpers

def render_html(markup):
    """st.markdown treats lines indented 4+ spaces as code blocks, so strip
    leading whitespace before handing multi-line HTML off to it."""
    st.markdown(re.sub(r"^[ \t]+", "", markup, flags=re.MULTILINE), unsafe_allow_html=True)


def is_missing(value):
    return value is None or pd.isna(value)


def delta_line(today_val, avg_val, unit, lower_is_better):
    """Arrow + plain-language comparison to the 7-day average. Improvements get
    the accent; anything else stays neutral gray, so direction never relies on
    color alone."""
    if is_missing(today_val) or is_missing(avg_val):
        return f'<div class="delta">Not enough history for a 7-day average yet</div>'
    diff = round(today_val - avg_val)
    if diff == 0:
        return f'<div class="delta">Right at your 7-day average of {avg_val:.0f} {unit}</div>'
    arrow = "&#9650;" if diff > 0 else "&#9660;"
    word = "above" if diff > 0 else "below"
    improved = (diff < 0) if lower_is_better else (diff > 0)
    cls = "delta good" if improved else "delta"
    return (
        f'<div class="{cls}"><span class="arrow">{arrow}</span>'
        f"{abs(diff)} {word} your 7-day average of {avg_val:.0f}</div>"
    )


def sleep_rating(score):
    if is_missing(score):
        return "No data", TEXT_3
    if score > 80:
        return "Good", ACCENT
    if score >= 60:
        return "Fair", TEXT_2
    return "Low", WARN


def estimate_bmr():
    offset = -161 if BIO_SEX.startswith("f") else 5
    weight_kg = BIO_WEIGHT_LB * 0.453592
    height_cm = BIO_HEIGHT_IN * 2.54
    return 10 * weight_kg + 6.25 * height_cm - 5 * BIO_AGE + offset


def duration_seconds(text):
    """'MM:SS' or 'H:MM:SS' -> seconds."""
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def format_pace(seconds_per_mile):
    minutes, seconds = divmod(int(round(seconds_per_mile)), 60)
    return f"{minutes}:{seconds:02d}"


def steps_ring_svg(steps, goal, size=176, stroke=12):
    steps = steps or 0
    radius = (size - stroke) / 2
    circumference = 2 * 3.14159265 * radius
    pct = max(0.0, min(steps / goal, 1.0))
    offset = circumference * (1 - pct)
    c = size / 2
    return f"""
    <svg class="ring" width="{size}" height="{size}" viewBox="0 0 {size} {size}" role="img"
         aria-label="{pct:.0%} of daily step goal">
        <circle cx="{c}" cy="{c}" r="{radius}" fill="none" stroke="{LINE}" stroke-width="{stroke}"/>
        <circle cx="{c}" cy="{c}" r="{radius}" fill="none" stroke="{ACCENT}" stroke-width="{stroke}"
                stroke-linecap="round" stroke-dasharray="{circumference:.2f}"
                stroke-dashoffset="{offset:.2f}" transform="rotate(-90 {c} {c})"/>
        <text x="{c}" y="{c + 2}" text-anchor="middle" dominant-baseline="middle"
              font-family="Barlow Condensed, Barlow, sans-serif" font-weight="600"
              font-size="40" fill="{TEXT}">{pct * 100:.0f}%</text>
        <text x="{c}" y="{c + 30}" text-anchor="middle" font-family="Barlow, sans-serif"
              font-size="13" fill="{TEXT_3}">of goal</text>
    </svg>
    """


# -------------------------------------------------------------------- styles

st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@500;600&display=swap');

    .stApp {{ background: {BG}; }}
    .stApp, .stApp p, .stApp div, .stApp span, .stApp button {{ font-family: {FONT_TEXT}; }}
    header[data-testid="stHeader"] {{ display: none; }}
    .block-container {{ padding: 2rem 2.5rem 1.5rem; max-width: 1600px; }}

    /* Header */
    .top {{
        display: flex; justify-content: space-between; align-items: flex-end;
        flex-wrap: wrap; gap: 0.5rem 2rem; margin-bottom: 1.6rem;
    }}
    .hello {{
        font-family: {FONT_NUM}; font-weight: 600; font-size: 3.1rem;
        line-height: 1; color: {TEXT}; letter-spacing: -0.01em;
    }}
    .when {{ text-align: right; }}
    .when .time {{
        font-family: {FONT_NUM}; font-weight: 500; font-size: 2.1rem;
        line-height: 1; color: {TEXT};
    }}
    .when .date {{ font-size: 1rem; color: {TEXT_2}; margin-top: 0.3rem; }}

    /* Shared panel */
    .panel {{
        background: {PANEL}; border: 1px solid {LINE}; border-radius: 16px;
        box-sizing: border-box;
    }}
    .panel-head {{
        display: flex; justify-content: space-between; align-items: baseline;
        gap: 1rem; margin-bottom: 0.9rem;
    }}
    .panel-title {{ font-size: 1.05rem; font-weight: 600; color: {TEXT}; }}
    .panel-note {{ font-size: 0.85rem; color: {TEXT_3}; }}

    /* Row 1: steps hero + vitals */
    .row1 {{
        display: grid; grid-template-columns: minmax(340px, 1fr) 1.55fr;
        gap: 1rem; margin-bottom: 1rem;
    }}
    .hero {{ padding: 1.4rem 1.6rem; display: flex; align-items: center; gap: 1.8rem; }}
    .hero .label {{ font-size: 0.95rem; color: {TEXT_2}; }}
    .hero .big {{
        font-family: {FONT_NUM}; font-weight: 600; font-size: 4.4rem;
        line-height: 0.95; color: {TEXT}; margin: 0.2rem 0 0.35rem;
    }}
    .hero .sub {{ font-size: 0.95rem; color: {TEXT_2}; }}
    .hero .sub strong {{ color: {TEXT}; font-weight: 600; }}
    .hero .meta {{ font-size: 0.85rem; color: {TEXT_3}; margin-top: 0.9rem; }}

    .vitals {{
        display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr;
        overflow: hidden;
    }}
    .vital {{ padding: 1.05rem 1.4rem; }}
    .vital:nth-child(1), .vital:nth-child(2) {{ border-bottom: 1px solid {LINE}; }}
    .vital:nth-child(odd) {{ border-right: 1px solid {LINE}; }}
    .vital .label {{ font-size: 0.9rem; color: {TEXT_2}; }}
    .vital .value {{
        font-family: {FONT_NUM}; font-weight: 600; font-size: 2.6rem;
        line-height: 1; color: {TEXT}; margin: 0.35rem 0 0.4rem;
    }}
    .vital .unit {{ font-family: {FONT_TEXT}; font-size: 0.95rem; font-weight: 500; color: {TEXT_3}; margin-left: 0.25rem; }}
    .delta {{ font-size: 0.85rem; color: {TEXT_2}; }}
    .delta.good {{ color: {ACCENT}; }}
    .delta .arrow {{ font-size: 0.65rem; margin-right: 0.35rem; position: relative; top: -1px; }}
    .meter {{ height: 4px; background: {LINE}; border-radius: 2px; margin: 0.15rem 0 0.5rem; max-width: 220px; }}
    .meter > span {{ display: block; height: 100%; border-radius: 2px; }}

    /* Row 2: steps chart (Streamlit container) + running */
    .st-key-steps_panel {{
        background: {PANEL}; border: 1px solid {LINE}; border-radius: 16px;
        padding: 1.2rem 1.4rem 0.6rem; min-height: 340px;
    }}
    .st-key-steps_panel [data-testid="stVerticalBlock"] {{ gap: 0; }}
    .keys {{ display: flex; gap: 1.1rem; font-size: 0.85rem; color: {TEXT_2}; }}
    .key {{ display: inline-flex; align-items: center; gap: 0.45rem; }}
    .key .swatch {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
    .key .rule {{ width: 16px; height: 0; display: inline-block; border-top: 2px solid {TEXT_2}; }}
    .key .rule.avg {{ border-top: 2px dotted {TEXT_3}; }}

    .runs {{ padding: 1.2rem 1.4rem; min-height: 340px; }}
    .runs .total {{ display: flex; align-items: baseline; gap: 0.5rem; }}
    .runs .total .num {{
        font-family: {FONT_NUM}; font-weight: 600; font-size: 4rem; line-height: 0.95; color: {TEXT};
    }}
    .runs .total .word {{ font-size: 1.05rem; color: {TEXT_2}; }}
    .runs .summary {{ font-size: 0.95rem; color: {TEXT_2}; margin: 0.45rem 0 1.2rem; }}
    .runs .summary strong {{ color: {TEXT}; font-weight: 600; }}
    .run {{
        display: grid; grid-template-columns: 2.6rem 1fr 4.6rem; align-items: center;
        gap: 0.8rem; padding: 0.5rem 0;
    }}
    .run + .run {{ border-top: 1px solid {LINE}; }}
    .run .run-day {{ font-size: 0.9rem; color: {TEXT_2}; }}
    .run .track {{ display: flex; align-items: center; gap: 0.6rem; min-width: 0; }}
    .run .bar {{ height: 8px; border-radius: 0 4px 4px 0; background: {ACCENT}; flex: none; }}
    .run .dist {{ font-family: {FONT_NUM}; font-size: 1.2rem; font-weight: 600; color: {TEXT}; white-space: nowrap; }}
    .run .pace {{ text-align: right; font-size: 0.9rem; color: {TEXT_2}; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .run .pace small {{ color: {TEXT_3}; font-size: 0.75rem; }}
    .empty {{ font-size: 0.95rem; color: {TEXT_3}; }}

    /* Row 3: calendar */
    .cal-wrap {{ margin-top: 1.6rem; }}
    .cal-title {{ font-size: 1.05rem; font-weight: 600; color: {TEXT}; }}
    .cal-legend {{ display: inline-flex; gap: 1rem; margin-left: 1.2rem; font-size: 0.85rem; color: {TEXT_2}; font-weight: 400; }}
    div[class*="st-key-cal_"] button {{
        min-height: 0; height: 32px; padding: 0 0.85rem; border-radius: 8px;
        background: transparent; border: 1px solid {LINE}; color: {TEXT_2};
        font-size: 0.85rem; line-height: 1;
    }}
    div[class*="st-key-cal_"] button:hover {{ border-color: {TEXT_3}; color: {TEXT}; background: {PANEL}; }}
    div[class*="st-key-cal_"] button:focus-visible {{ outline: 2px solid {ACCENT}; outline-offset: 2px; }}
    div[class*="st-key-cal_"] button:disabled {{ opacity: 0.4; }}
    .week {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 0.6rem; margin-top: 0.8rem; }}
    .day {{
        background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px;
        padding: 0.7rem 0.6rem 0.5rem; min-height: 120px; position: relative; overflow: hidden;
    }}
    .day.today::before {{
        content: ""; position: absolute; left: 0; right: 0; top: 0; height: 3px; background: {ACCENT};
    }}
    .day-head {{ display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.6rem; }}
    .day-head .dow {{ font-size: 0.85rem; color: {TEXT_2}; }}
    .day-head .num {{ font-family: {FONT_NUM}; font-size: 1.45rem; font-weight: 600; color: {TEXT}; line-height: 1; }}
    .day.today .day-head .num {{ color: {ACCENT}; }}
    .evt {{
        border-left: 3px solid {CAL_OVERFLOW}; padding: 0.15rem 0 0.15rem 0.5rem;
        margin-bottom: 0.55rem; line-height: 1.25;
    }}
    .evt .t {{ display: block; font-size: 0.72rem; color: {TEXT_3}; font-variant-numeric: tabular-nums; }}
    .evt .s {{ font-size: 0.82rem; color: {TEXT}; }}
    .day .none {{ font-size: 0.8rem; color: {TEXT_3}; }}

    @media (max-width: 1100px) {{
        .row1 {{ grid-template-columns: 1fr; }}
        .week {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

# -------------------------------------------------------------------- header

now = datetime.datetime.now()
if now.hour < 12:
    greeting = "Good morning"
elif now.hour < 17:
    greeting = "Good afternoon"
else:
    greeting = "Good evening"
render_html(
    f'<div class="top">'
    f'<div class="hello">{greeting}, {html.escape(USER_NAME)}</div>'
    f'<div class="when"><div class="time">{now.strftime("%-I:%M %p")}</div>'
    f'<div class="date">{now.strftime("%A, %B %-d")}</div></div>'
    f"</div>"
)

# Floating fullscreen toggle. Injected into the parent document so the click
# is a real user gesture on the top page (required to enter fullscreen).
components.html(
    f"""
    <script>
    (function () {{
        const doc = window.parent.document;
        if (doc.getElementById('fs-toggle')) return;
        const btn = doc.createElement('button');
        btn.id = 'fs-toggle';
        btn.type = 'button';
        btn.title = 'Toggle fullscreen';
        btn.setAttribute('aria-label', 'Toggle fullscreen');
        btn.innerHTML = '\\u26F6';
        btn.style.cssText =
            'position:fixed;top:12px;right:14px;z-index:100000;width:34px;height:34px;'
            + 'border-radius:8px;border:1px solid {LINE};background:{PANEL};color:{TEXT_2};'
            + 'font-size:16px;cursor:pointer;display:flex;align-items:center;'
            + 'justify-content:center;line-height:1;opacity:0.6;';
        btn.onmouseenter = function () {{ btn.style.opacity = '1'; }};
        btn.onmouseleave = function () {{ btn.style.opacity = '0.6'; }};
        const sync = function () {{
            btn.innerHTML = doc.fullscreenElement ? '\\u2715' : '\\u26F6';
        }};
        btn.onclick = function () {{
            if (doc.fullscreenElement) {{
                doc.exitFullscreen();
            }} else {{
                const p = doc.documentElement.requestFullscreen();
                if (p && p.catch) p.catch(function () {{}});
            }}
        }};
        doc.addEventListener('fullscreenchange', sync);
        doc.body.appendChild(btn);
    }})();
    </script>
    """,
    height=0,
)

# ---------------------------------------------------------------------- data

conn = get_connection()

tables = {
    row[0]
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
}
if not {"garmin_daily", "garmin_runs"}.issubset(tables):
    st.warning(
        "health.db has no data yet. Run `python pull_garmin.py full` first "
        "to create the tables and pull today's stats."
    )
    st.stop()

today_date = datetime.date.today()
today = today_date.isoformat()
today_row = get_today_row(conn, today)

if today_row is None:
    st.warning("No data found in health.db yet. Run `python pull_garmin.py full` to populate it.")
    st.stop()

steps = today_row["steps"] if pd.notna(today_row["steps"]) else 0
resting_hr = today_row["resting_hr"] if pd.notna(today_row["resting_hr"]) else None
calories = today_row["calories"] if pd.notna(today_row["calories"]) else None
sleep_score = today_row["sleep_score"] if pd.notna(today_row["sleep_score"]) else None
hrv = today_row["hrv"] if pd.notna(today_row["hrv"]) else None

avg_hr = get_week_avg(conn, "resting_hr")
avg_hrv = get_week_avg(conn, "hrv")

steps_df = get_last_7_days_steps(conn)
past_steps = steps_df[steps_df["date"].astype(str).str[:10] != today]["steps"].dropna()
avg_steps = past_steps.mean() if not past_steps.empty else None

# --------------------------------------------------------- row 1: steps + vitals

remaining = STEP_GOAL - steps
if remaining > 0:
    progress = f"<strong>{remaining:,.0f}</strong> more to reach"
else:
    progress = f"<strong>{-remaining:,.0f}</strong> past your goal of"
avg_text = f"Your 7-day average is {avg_steps:,.0f}" if avg_steps is not None else ""

sleep_text, sleep_color = sleep_rating(sleep_score)
sleep_pct = 0 if is_missing(sleep_score) else max(0, min(sleep_score, 100))

bmr = estimate_bmr()
if calories is not None:
    active = max(calories - bmr, 0)
    cal_sub = f"About {bmr:,.0f} resting and {active:,.0f} active"
else:
    cal_sub = "No data yet today"


def num(value, fmt="{:.0f}"):
    return "&ndash;" if is_missing(value) else fmt.format(value)


render_html(
    f"""
    <div class="row1">
        <div class="panel hero">
            {steps_ring_svg(steps, STEP_GOAL)}
            <div>
                <div class="label">Steps today</div>
                <div class="big">{steps:,.0f}</div>
                <div class="sub">{progress} {STEP_GOAL:,}</div>
                <div class="meta">{avg_text}</div>
            </div>
        </div>
        <div class="panel vitals">
            <div class="vital">
                <div class="label">Resting heart rate</div>
                <div class="value">{num(resting_hr)}<span class="unit">bpm</span></div>
                {delta_line(resting_hr, avg_hr, "bpm", lower_is_better=True)}
            </div>
            <div class="vital">
                <div class="label">Heart rate variability</div>
                <div class="value">{num(hrv)}<span class="unit">ms</span></div>
                {delta_line(hrv, avg_hrv, "ms", lower_is_better=False)}
            </div>
            <div class="vital">
                <div class="label">Sleep score</div>
                <div class="value">{num(sleep_score)}<span class="unit">/ 100</span></div>
                <div class="meter"><span style="width:{sleep_pct}%;background:{sleep_color};"></span></div>
                <div class="delta" style="color:{sleep_color};">{sleep_text}</div>
            </div>
            <div class="vital">
                <div class="label">Calories burned</div>
                <div class="value">{num(calories, "{:,.0f}")}<span class="unit">kcal</span></div>
                <div class="delta">{cal_sub}</div>
            </div>
        </div>
    </div>
    """
)

# ------------------------------------------------ row 2: steps chart + running

left, right = st.columns([1.6, 1], gap="medium")

with left:
    with st.container(key="steps_panel"):
        render_html(
            f"""
            <div class="panel-head">
                <div class="panel-title">Steps this week</div>
                <div class="keys">
                    <span class="key"><span class="swatch" style="background:{ACCENT}"></span>Goal met</span>
                    <span class="key"><span class="swatch" style="background:{ACCENT_MUTED}"></span>Short of goal</span>
                    <span class="key"><span class="rule"></span>Goal</span>
                    <span class="key"><span class="rule avg"></span>7-day average</span>
                </div>
            </div>
            """
        )
        if steps_df.empty:
            render_html('<div class="empty">No step data in the last 7 days.</div>')
        else:
            dates = [datetime.date.fromisoformat(str(d)[:10]) for d in steps_df["date"]]
            labels = ["Today" if d == today_date else d.strftime("%a %-d") for d in dates]
            values = steps_df["steps"].fillna(0).tolist()
            colors = [ACCENT if v >= STEP_GOAL else ACCENT_MUTED for v in values]
            y_max = max(max(values), STEP_GOAL) * 1.18

            fig = go.Figure(
                go.Bar(
                    x=labels,
                    y=values,
                    marker=dict(color=colors, line=dict(width=0)),
                    width=0.34,
                    hovertemplate="%{x}<br><b>%{y:,.0f}</b> steps<extra></extra>",
                )
            )
            fig.add_hline(y=STEP_GOAL, line=dict(color=TEXT_2, width=1.5))
            if avg_steps is not None:
                fig.add_hline(y=avg_steps, line=dict(color=TEXT_3, width=1.5, dash="dot"))
            # Direct label on today's bar only; the axis and tooltip carry the rest.
            fig.add_annotation(
                x=labels[-1], y=values[-1], text=f"{values[-1]:,.0f}", showarrow=False,
                yshift=14, font=dict(family=FONT_NUM, size=15, color=TEXT),
            )
            fig.update_layout(
                barcornerradius=4,
                paper_bgcolor=PANEL,
                plot_bgcolor=PANEL,
                margin=dict(l=4, r=4, t=8, b=4),
                height=304,
                showlegend=False,
                bargap=0.4,
                font=dict(family=FONT_TEXT, color=TEXT_2, size=13),
                hoverlabel=dict(bgcolor="#1B1F23", bordercolor=LINE, font=dict(family=FONT_TEXT, color=TEXT)),
                xaxis=dict(showgrid=False, type="category", tickfont=dict(color=TEXT_2), fixedrange=True),
                yaxis=dict(
                    range=[0, y_max], gridcolor=LINE, gridwidth=1, zeroline=False,
                    tickvals=[0, 5000, 10000, 15000, 20000], ticktext=["0", "5k", "10k", "15k", "20k"],
                    tickfont=dict(color=TEXT_3, size=12), fixedrange=True, ticks="", side="left",
                ),
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

with right:
    runs_df = get_recent_runs(conn)
    cutoff = today_date - datetime.timedelta(days=6)
    week_runs = []
    for _, r in runs_df.iterrows():
        run_day = datetime.date.fromisoformat(str(r["date"])[:10])
        if run_day < cutoff:
            continue
        secs = duration_seconds(r["duration"])
        dist = float(r["distance_mi"] or 0)
        pace = secs / dist if secs and dist > 0 else None
        week_runs.append({"day": run_day, "dist": dist, "pace": pace})

    if not week_runs:
        body = '<div class="empty">No runs logged in the last 7 days.</div>'
    else:
        total = sum(r["dist"] for r in week_runs)
        longest = max(r["dist"] for r in week_runs)
        paces = [r["pace"] for r in week_runs if r["pace"]]
        fastest = min(paces) if paces else None
        count = len(week_runs)
        bits = [f"{count} run{'s' if count != 1 else ''}", f"longest <strong>{longest:.1f} mi</strong>"]
        if fastest:
            bits.append(f"best pace <strong>{format_pace(fastest)} /mi</strong>")
        summary = ", ".join(bits[:-1]) + f", and {bits[-1]}" if len(bits) > 2 else " and ".join(bits)
        summary = summary[0].upper() + summary[1:]

        rows = ""
        for r in week_runs:
            width = 100 * r["dist"] / longest if longest else 0
            pace_html = f'{format_pace(r["pace"])} <small>/mi</small>' if r["pace"] else "&ndash;"
            rows += (
                f'<div class="run"><span class="run-day">{r["day"].strftime("%a")}</span>'
                f'<span class="track"><span class="bar" style="width:calc({width:.0f}% - 3.6rem)"></span>'
                f'<span class="dist">{r["dist"]:.1f} mi</span></span>'
                f'<span class="pace">{pace_html}</span></div>'
            )
        body = (
            f'<div class="total"><span class="num">{total:.1f}</span><span class="word">miles this week</span></div>'
            f'<div class="summary">{summary}.</div>{rows}'
        )

    render_html(
        f'<div class="panel runs"><div class="panel-head"><div class="panel-title">Running</div>'
        f'<div class="panel-note">Last 7 days</div></div>{body}</div>'
    )

# ---------------------------------------------------- row 3: calendar week view

if "week_offset" not in st.session_state:
    st.session_state.week_offset = 0


def _shift_week(delta):
    st.session_state.week_offset += delta


def _reset_week():
    st.session_state.week_offset = 0


# Week starts Sunday: weekday() is Mon=0..Sun=6, so (weekday+1) % 7 days back.
current_week_start = today_date - datetime.timedelta(days=(today_date.weekday() + 1) % 7)
week_offset = st.session_state.week_offset
week_start = current_week_start + datetime.timedelta(days=7 * week_offset)
week_title = "This week" if week_offset == 0 else f"Week of {week_start.strftime('%b %-d')}"

render_html('<div class="cal-wrap"></div>')

if not GCAL_ICS_SOURCES:
    render_html(
        '<div class="cal-title">This week</div>'
        '<div class="panel" style="padding:1rem 1.2rem;margin-top:0.8rem;"><div class="empty">'
        "Add your Google Calendar &ldquo;Secret address in iCal format&rdquo; as "
        "<code>GCAL_ICS_URL</code> in .env to see your week here. "
        "Separate multiple calendars with commas.</div></div>"
    )
else:
    try:
        week_events, calendars, cal_errors = get_week_events(GCAL_ICS_SOURCES, week_start.isoformat())
    except Exception as exc:
        week_events, calendars, cal_errors = None, [], []
        load_error = str(exc)

    legend = ""
    if len(calendars) > 1:
        legend = '<span class="cal-legend">' + "".join(
            f'<span class="key"><span class="swatch" style="background:{c["color"]}"></span>'
            f'{html.escape(c["name"])}</span>'
            for c in calendars
        ) + "</span>"

    head = st.columns([4, 1], vertical_alignment="center")
    with head[0]:
        render_html(f'<div class="cal-title">{week_title}{legend}</div>')
    with head[1]:
        with st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            st.button("‹", key="cal_prev", on_click=_shift_week, args=(-1,), help="Previous week")
            st.button("Today", key="cal_today", on_click=_reset_week, disabled=week_offset == 0)
            st.button("›", key="cal_next", on_click=_shift_week, args=(1,), help="Next week")

    if week_events is None:
        render_html(f'<div class="empty">Couldn\'t load your calendars: {html.escape(load_error)}</div>')
    else:
        for name, msg in cal_errors:
            render_html(f'<div class="empty">Couldn\'t load {html.escape(name)}: {html.escape(msg)}</div>')

        cols_html = ""
        for i in range(7):
            d = week_start + datetime.timedelta(days=i)
            today_cls = " today" if d == today_date else ""
            events = week_events.get(i, [])
            if events:
                evts_html = "".join(
                    f'<div class="evt" style="border-left-color:{e["color"]};">'
                    f'<span class="t">{html.escape(e["time"])}</span>'
                    f'<span class="s">{html.escape(e["summary"])}</span></div>'
                    for e in events
                )
            else:
                evts_html = '<div class="none">Nothing scheduled</div>'
            cols_html += (
                f'<div class="day{today_cls}">'
                f'<div class="day-head"><span class="dow">{WEEKDAYS[i]}</span>'
                f'<span class="num">{d.day}</span></div>{evts_html}</div>'
            )
        render_html(f'<div class="week">{cols_html}</div>')
