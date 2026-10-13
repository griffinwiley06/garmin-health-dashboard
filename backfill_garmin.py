"""
Backfill garmin_daily with historical data so the dashboard charts and
7-day averages have a full window to work with.

Usage:
    python backfill_garmin.py         # last 7 days
    python backfill_garmin.py 14      # last 14 days

Uses the same credentials/.env and API calls as pull_garmin.py. Runs are
already backfilled for the last 7 days by `pull_garmin.py full`, so this
script only fills the garmin_daily table.
"""

import sys
import sqlite3
import datetime

from garminconnect import Garmin

from pull_garmin import get_client, init_db, DB_PATH


def daily_stats_for(client, day):
    stats = client.get_stats(day)
    steps = stats.get("totalSteps")
    resting_hr = stats.get("restingHeartRate")
    calories = stats.get("totalKilocalories")

    try:
        sleep = client.get_sleep_data(day)
        sleep_score = (
            sleep.get("dailySleepDTO", {})
            .get("sleepScores", {})
            .get("overall", {})
            .get("value")
        )
    except Exception:
        sleep_score = None

    try:
        hrv = client.get_hrv_data(day)
        hrv_value = hrv.get("hrvSummary", {}).get("lastNightAvg") if hrv else None
    except Exception:
        hrv_value = None

    return steps, resting_hr, calories, sleep_score, hrv_value


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    client = get_client()

    today = datetime.date.today()
    filled = 0
    for offset in range(days, -1, -1):
        day = (today - datetime.timedelta(days=offset)).isoformat()
        try:
            steps, resting_hr, calories, sleep_score, hrv_value = daily_stats_for(
                client, day
            )
        except Exception as exc:
            print(f"[skip] {day}: {exc}")
            continue

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
            (day, steps, resting_hr, calories, sleep_score, hrv_value),
        )
        conn.commit()
        filled += 1
        print(
            f"[ok] {day} steps={steps} resting_hr={resting_hr} "
            f"calories={calories} sleep_score={sleep_score} hrv={hrv_value}"
        )

    conn.close()
    print(f"Backfilled {filled} day(s).")


if __name__ == "__main__":
    main()
