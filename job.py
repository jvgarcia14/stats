import os
import time
import requests
import psycopg2
import psycopg2.extras

DB_DSN = os.getenv("DB_DSN")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

INFLOWW_APP_TOKEN = os.getenv("INFLOWW_APP_TOKEN")
INFLOWW_MODELS_BASE = os.getenv("INFLOWW_MODELS_BASE")
INFLOWW_STATS_BASE = os.getenv("INFLOWW_STATS_BASE")


def get_enabled_chats():
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT chat_id FROM telegram_chats WHERE is_enabled = TRUE")
            return cur.fetchall()


def send_telegram(message):
    chats = get_enabled_chats()

    for chat in chats:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": chat["chat_id"],
                "text": message
            }
        )


def get_models():
    r = requests.get(
        f"{INFLOWW_MODELS_BASE}/model/bind/list",
        headers={
            "app-token": INFLOWW_APP_TOKEN
        }
    )

    return r.json()["data"]


def get_earnings():
    r = requests.get(
        f"{INFLOWW_STATS_BASE}/a3/api2/v2/earnings/chart",
        headers={
            "app-token": INFLOWW_APP_TOKEN
        }
    )

    data = r.json()["total"]["chartAmount"]
    today = data[-1]["count"]

    return today


def send_stats():

    models = get_models()
    earnings = get_earnings()

    message = "📊 Infloww Stats\n\n"

    for model in models:
        name = model.get("modelName", "Model")
        message += f"{name}\n${earnings}\n\n"

    send_telegram(message)


def main():

    while True:

        try:
            send_stats()
            print("Stats sent successfully")

        except Exception as e:
            print("Error:", e)

        # wait 2 hours
        time.sleep(7200)


if __name__ == "__main__":
    main()
