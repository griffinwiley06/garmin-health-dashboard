"""
Garmin daily data pull script.

Usage:
    python pull_garmin.py full     # run once in the morning
    python pull_garmin.py partial  # run 2-3x during the day

Requires a .env file in the same directory with:
    GARMIN_EMAIL=your_email@example.com
    GARMIN_PASSWORD=your_password
"""

import sys
import sqlite3
import datetime
import os
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

DB_PATH = "health.db"
MILES_PER_METER = 1 / 1609.34


def get_client():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError("GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env")
    client = Garmin(email, password)
    client.login()
    return client


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS garmin_daily (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            resting_hr INTEGER,
            calories INTEGER,
            sleep_score INTEGER,
            hrv INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS garmin_runs (
            date TEXT,
            distance_mi REAL,
            duration TEXT
        )
    """)
    conn.commit()


def format_duration(duration_sec):
    minutes = int(duration_sec // 60)
    seconds = int(duration_sec % 60)
    return f"{minutes}:{seconds:02d}"


def pull_full(client, conn, today):
    stats = client.get_stats(today)
    steps = stats.get("totalSteps")
    resting_hr = stats.get("restingHeartRate")
    calories = stats.get("totalKilocalories")

    sleep = client.get_sleep_data(today)
    sleep_score = (
        sleep.get("dailySleepDTO", {})
        .get("sleepScores", {})
        .get("overall", {})
        .get("value")
    )

    try:
        hrv = client.get_hrv_data(today)
        hrv_value = hrv.get("hrvSummary", {}).get("lastNightAvg") if hrv else None
    except Exception:
        hrv_value = None

    conn.execute(
        """
        INSERT INTO garmin_daily (date, steps, resting_hr, calories, sleep_score, hrv)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            steps = excluded.steps,
            resting_hr = excluded.resting_hr,
            calories = excluded.calories,
            sleep_score = excluded.sleep_score,
            hrv = excluded.hrv
        """,
        (today, steps, resting_hr, calories, sleep_score, hrv_value),
    )
    conn.commit()

    week_ago = (
        datetime.date.today() - datetime.timedelta(days=7)
    ).isoformat()
    activities = client.get_activities_by_date(week_ago, today, activitytype="running")

    conn.execute("DELETE FROM garmin_runs WHERE date >= ?", (week_ago,))
    for a in activities:
        distance_mi = round(a.get("distance", 0) * MILES_PER_METER, 1)
        duration = format_duration(a.get("duration", 0))
        run_date = a.get("startTimeLocal", today)
        conn.execute(
            "INSERT INTO garmin_runs (date, distance_mi, duration) VALUES (?, ?, ?)",
            (run_date, distance_mi, duration),
        )
    conn.commit()

    print(f"[full] steps={steps} resting_hr={resting_hr} calories={calories} "
          f"sleep_score={sleep_score} hrv={hrv_value} runs={len(activities)}")


def pull_partial(client, conn, today):
    stats = client.get_stats(today)
    steps = stats.get("totalSteps")
    calories = stats.get("totalKilocalories")

    conn.execute(
        """
        INSERT INTO garmin_daily (date, steps, calories)
        VALUES (?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            steps = excluded.steps,
            calories = excluded.calories
        """,
        (today, steps, calories),
    )
    conn.commit()

    print(f"[partial] steps={steps} calories={calories}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "partial"
    if mode not in ("full", "partial"):
        print("Usage: python pull_garmin.py [full|partial]")
        sys.exit(1)

    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    client = get_client()

    if mode == "full":
        pull_full(client, conn, today)
    else:
        pull_partial(client, conn, today)

    conn.close()


if __name__ == "__main__":
    main()
