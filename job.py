import os
import json
import decimal
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
import psycopg2.extras


def must_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


APP_TOKEN = must_env("INFLOWW_APP_TOKEN")
MODELS_BASE = must_env("INFLOWW_MODELS_BASE").rstrip("/")   # example: https://vnlxs2e0fr3.api2.infloww.com
STATS_BASE  = must_env("INFLOWW_STATS_BASE").rstrip("/")    # example: https://u691aw3.fc-cdn.infloww.com
DB_DSN      = must_env("DB_DSN")

TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional filters
ONLY_VERIFIED = os.getenv("ONLY_VERIFIED", "true")
PLATFORM_ENUM = os.getenv("PLATFORM_ENUM", "OF")  # from your capture: platformEnum=OF


def dec(x):
    if x is None:
        return None
    try:
        return decimal.Decimal(str(x))
    except Exception:
        return None


def tg_send(message: str):
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    # simple Telegram call without extra libs
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=30).raise_for_status()


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS infloww_model_daily_latest (
          id BIGSERIAL PRIMARY KEY,
          model_id TEXT NOT NULL,
          model_name TEXT,
          point_date TIMESTAMPTZ NOT NULL,
          earnings NUMERIC,
          transactions INTEGER,
          total_30d NUMERIC,
          gross_30d NUMERIC,
          raw_json JSONB,
          created_at TIMESTAMPTZ DEFAULT now(),
          UNIQUE (model_id, point_date)
        );
        """)
    conn.commit()


def get_models():
    """
    From your capture:
    GET /model/bind/list?onlyVerified=true&size=10&dataPermissionType=2&platformEnum=OF
    """
    url = f"{MODELS_BASE}/model/bind/list"
    params = {
        "onlyVerified": ONLY_VERIFIED,
        "size": 100,
        "dataPermissionType": 2,
        "platformEnum": PLATFORM_ENUM,
    }
    headers = {
        "accept": "application/json",
        "app-token": APP_TOKEN,
        "x-requested-with": "infloww",
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Models API not success: {data}")
    return data.get("data") or []


def last_30_days_window():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=30)
    return start.isoformat(), end.isoformat()


def fetch_earnings_chart(model_id=None):
    """
    From your capture:
    GET /a3/api2/v2/earnings/chart?startDate=...&endDate=...&withTotal=true&filter[total_count]=...&filter[total_amount]=...
    """
    start_date, end_date = last_30_days_window()
    url = f"{STATS_BASE}/a3/api2/v2/earnings/chart"
    params = {
        "startDate": start_date,
        "endDate": end_date,
        "withTotal": "true",
        "filter[total_count]": "total_count",
        "filter[total_amount]": "total_amount",
    }
    # Some installs support per-model via modelId; we try it if provided.
    if model_id is not None:
        params["modelId"] = str(model_id)

    headers = {
        "accept": "application/json",
        "app-token": APP_TOKEN,
        "x-requested-with": "infloww",
    }

    r = requests.get(url, params=params, headers=headers, timeout=30)

    # If modelId isn't supported, fallback to all-creators call
    if r.status_code in (400, 404) and model_id is not None:
        params.pop("modelId", None)
        r = requests.get(url, params=params, headers=headers, timeout=30)

    r.raise_for_status()
    return r.json()


def pick_latest_point(chart_json: dict):
    """
    Matches your tooltip:
    latest earnings = total.chartAmount[-1].count
    latest tx       = total.chartCount[-1].count
    """
    t = chart_json.get("total") or {}
    chart_amount = t.get("chartAmount") or []
    chart_count  = t.get("chartCount")  or []
    if not chart_amount:
        return None

    last_amount = chart_amount[-1]
    date_str = last_amount.get("date")
    earnings = last_amount.get("count")

    tx = None
    if chart_count:
        # if dates mismatch, match by date
        if chart_count[-1].get("date") == date_str:
            tx = chart_count[-1].get("count")
        else:
            tx_map = {x.get("date"): x.get("count") for x in chart_count}
            tx = tx_map.get(date_str)

    return {
        "point_date": date_str,
        "earnings": earnings,
        "transactions": tx,
        "total_30d": t.get("total"),
        "gross_30d": t.get("gross"),
    }


def upsert_latest(conn, model_id, model_name, point):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO infloww_model_daily_latest
              (model_id, model_name, point_date, earnings, transactions, total_30d, gross_30d, raw_json)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model_id, point_date)
            DO UPDATE SET
              model_name = EXCLUDED.model_name,
              earnings = EXCLUDED.earnings,
              transactions = EXCLUDED.transactions,
              total_30d = EXCLUDED.total_30d,
              gross_30d = EXCLUDED.gross_30d,
              raw_json = EXCLUDED.raw_json
            """,
            (
                str(model_id),
                model_name,
                point["point_date"],
                dec(point["earnings"]),
                int(point["transactions"]) if point["transactions"] is not None else None,
                dec(point["total_30d"]),
                dec(point["gross_30d"]),
                psycopg2.extras.Json(point.get("raw_json") or {}),
            ),
        )
    conn.commit()


def main():
    models = get_models()

    with psycopg2.connect(DB_DSN) as conn:
        ensure_tables(conn)

        lines = ["📊 Infloww (latest day per model)"]
        ok_count = 0

        for m in models:
            # your model object keys may vary; keep it flexible
            model_id = m.get("modelId") or m.get("id") or m.get("model_id")
            model_name = m.get("name") or m.get("modelName") or m.get("model_name") or str(model_id)

            if model_id is None:
                continue

            chart = fetch_earnings_chart(model_id=model_id)
            point = pick_latest_point(chart)
            if not point:
                continue

            # keep raw json small
            point["raw_json"] = {
                "picked": point,
            }

            upsert_latest(conn, model_id, model_name, point)
            ok_count += 1

            lines.append(
                f"- {model_name}: {point['point_date']} | "
                f"Earnings ${point['earnings']} | Tx {point['transactions']}"
            )

        lines.append(f"\nModels updated: {ok_count}")
        tg_send("\n".join(lines))


if __name__ == "__main__":
    main()
