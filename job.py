import os
import time
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
import psycopg2.extras

DB_DSN = os.getenv("DB_DSN", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

INFLOWW_APP_TOKEN = os.getenv("INFLOWW_APP_TOKEN", "")
INFLOWW_STATS_BASE = os.getenv("INFLOWW_STATS_BASE", "").rstrip("/")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "7200"))  # default 2 hours


def must(v: str, name: str) -> str:
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def ensure_env():
    must(DB_DSN, "DB_DSN")
    must(TG_TOKEN, "TELEGRAM_BOT_TOKEN")
    must(INFLOWW_APP_TOKEN, "INFLOWW_APP_TOKEN")
    must(INFLOWW_STATS_BASE, "INFLOWW_STATS_BASE")


def get_enabled_chats():
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT chat_id FROM telegram_chats WHERE is_enabled = TRUE")
            rows = cur.fetchall()

    return [r["chat_id"] for r in rows]


def tg_send(message: str):
    chat_ids = get_enabled_chats()

    if not chat_ids:
        print("No Telegram chats registered (use /register)")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    for chat_id in chat_ids:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message
            },
            timeout=30
        )

        if r.status_code >= 400:
            print("Telegram send failed:", chat_id, r.status_code, r.text)


def infloww_headers():
    return {
        "accept": "application/json",
        "app-token": INFLOWW_APP_TOKEN,
        "x-requested-with": "infloww",
    }


def last_30_days_window():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=30)

    return start.isoformat(), end.isoformat()


def fetch_earnings_chart():
    url = f"{INFLOWW_STATS_BASE}/ja3/api2/v2/earnings/chart"

    start_date, end_date = last_30_days_window()

    params = {
        "startDate": start_date,
        "endDate": end_date,
        "withTotal": "true",
        "filter[total_count]": "total_count",
        "filter[total_amount]": "total_amount",
    }

    r = requests.get(
        url,
        params=params,
        headers=infloww_headers(),
        timeout=30
    )

    r.raise_for_status()

    return r.json()


def pick_latest_point(chart_json: dict):

    total = chart_json.get("total") or {}

    chart_amount = total.get("chartAmount") or []
    chart_count = total.get("chartCount") or []

    if not chart_amount:
        return None

    latest_amount = chart_amount[-1]
    date = latest_amount.get("date")
    earnings = latest_amount.get("count")

    tx = None

    if chart_count:

        if chart_count[-1].get("date") == date:
            tx = chart_count[-1].get("count")

        else:
            by_date = {c.get("date"): c.get("count") for c in chart_count}
            tx = by_date.get(date)

    return {
        "date": date,
        "earnings": earnings,
        "transactions": tx,
        "total_30d": total.get("total"),
        "gross_30d": total.get("gross"),
    }


def send_stats_once():

    chart = fetch_earnings_chart()

    latest = pick_latest_point(chart)

    if not latest:
        tg_send("⚠️ Could not fetch Infloww stats")
        return

    msg = (
        "📊 Infloww Stats\n\n"
        f"Date: {latest['date']}\n"
        f"Earnings: ${latest['earnings']}\n"
        f"Transactions: {latest['transactions']}\n\n"
        f"30d Net: ${latest['total_30d']}\n"
        f"30d Gross: ${latest['gross_30d']}"
    )

    tg_send(msg)


def main():

    ensure_env()

    while True:

        try:
            send_stats_once()
            print("Stats sent successfully")

        except Exception as e:
            print("Error:", repr(e))

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
