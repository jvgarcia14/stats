import os
import time
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
import psycopg2.extras

DB_DSN = os.getenv("DB_DSN", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

INFLOWW_APP_TOKEN = os.getenv("INFLOWW_APP_TOKEN", "")
INFLOWW_MODELS_BASE = os.getenv("INFLOWW_MODELS_BASE", "").rstrip("/")
INFLOWW_STATS_BASE = os.getenv("INFLOWW_STATS_BASE", "").rstrip("/")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "7200"))  # 2 hours default


def must(v: str, name: str) -> str:
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def ensure_env():
    must(DB_DSN, "DB_DSN")
    must(TG_TOKEN, "TELEGRAM_BOT_TOKEN")
    must(INFLOWW_APP_TOKEN, "INFLOWW_APP_TOKEN")
    must(INFLOWW_MODELS_BASE, "INFLOWW_MODELS_BASE")
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
        print("No registered telegram chats (use /register in the group).")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for chat_id in chat_ids:
        r = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=30)
        if r.status_code >= 400:
            print("Telegram send failed:", chat_id, r.status_code, r.text)


def infloww_headers():
    # From your Fiddler capture, "x-requested-with: infloww" was present.
    return {
        "accept": "application/json",
        "app-token": INFLOWW_APP_TOKEN,
        "x-requested-with": "infloww",
    }


def get_models():
    url = f"{INFLOWW_MODELS_BASE}/model/bind/list"
    params = {
        "onlyVerified": "true",
        "size": 100,
        "dataPermissionType": 2,
        "platformEnum": "OF",
    }
    r = requests.get(url, params=params, headers=infloww_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success", True):
        raise RuntimeError(f"Models API returned success=false: {data}")
    return data.get("data") or []


def last_30_days_window():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=30)
    return start.isoformat(), end.isoformat()


def fetch_earnings_chart(model_id=None):
    """
    Fetch earnings chart. If model_id is supported, you'll get per-model stats.
    If model_id isn't supported on this host, it will fallback to all-creators.
    """
    url = f"{INFLOWW_STATS_BASE}/a3/api2/v2/earnings/chart"
    start_date, end_date = last_30_days_window()

    params = {
        "startDate": start_date,
        "endDate": end_date,
        "withTotal": "true",
        "filter[total_count]": "total_count",
        "filter[total_amount]": "total_amount",
    }
    if model_id is not None:
        params["modelId"] = str(model_id)

    r = requests.get(url, params=params, headers=infloww_headers(), timeout=30)

    # If modelId isn't accepted, retry without it
    if r.status_code in (400, 404) and model_id is not None:
        params.pop("modelId", None)
        r = requests.get(url, params=params, headers=infloww_headers(), timeout=30)

    r.raise_for_status()
    return r.json()


def pick_latest_point(chart_json: dict):
    """
    Returns latest day (tooltip):
      - date
      - earnings (chartAmount last)
      - transactions (chartCount last matched by date)
    """
    total = chart_json.get("total") or {}
    amounts = total.get("chartAmount") or []
    counts = total.get("chartCount") or []
    if not amounts:
        return None

    last_amount = amounts[-1]
    point_date = last_amount.get("date")
    earnings = last_amount.get("count")

    tx = None
    if counts:
        if counts[-1].get("date") == point_date:
            tx = counts[-1].get("count")
        else:
            by_date = {c.get("date"): c.get("count") for c in counts}
            tx = by_date.get(point_date)

    return {
        "date": point_date,
        "earnings": earnings,
        "transactions": tx,
        "total_30d": total.get("total"),
        "gross_30d": total.get("gross"),
    }


def model_name(m: dict) -> str:
    # Your responses showed "name" commonly
    return m.get("name") or m.get("modelName") or m.get("model_name") or "Model"


def model_id(m: dict):
    return m.get("modelId") or m.get("id") or m.get("model_id")


def send_stats_once():
    models = get_models()

    lines = ["📊 Infloww Stats (latest day per model)"]
    sent_any = False

    for m in models:
        mid = model_id(m)
        name = model_name(m)
        if mid is None:
            continue

        chart = fetch_earnings_chart(model_id=mid)
        latest = pick_latest_point(chart)
        if not latest:
            continue

        sent_any = True
        lines.append(
            f"\n{name}\n"
            f"Date: {latest['date']}\n"
            f"Earnings: ${latest['earnings']}\n"
            f"Transactions: {latest['transactions']}"
        )

    if not sent_any:
        lines.append("\n(No stats returned — check token/hosts or modelId support.)")

    tg_send("\n".join(lines))


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
