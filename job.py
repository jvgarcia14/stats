import os
import psycopg2
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DB_DSN = os.getenv("DB_DSN", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def must(v: str, name: str) -> str:
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


DB_DSN = must(DB_DSN, "DB_DSN")
TG_TOKEN = must(TG_TOKEN, "TELEGRAM_BOT_TOKEN")


def ensure_tables():
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_chats (
                  chat_id BIGINT PRIMARY KEY,
                  title TEXT,
                  type TEXT,
                  is_enabled BOOLEAN DEFAULT TRUE,
                  created_at TIMESTAMPTZ DEFAULT now(),
                  updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
        conn.commit()


def upsert_chat(chat_id: int, title: str, chat_type: str, enabled: bool = True):
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telegram_chats (chat_id, title, type, is_enabled, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (chat_id)
                DO UPDATE SET
                  title=EXCLUDED.title,
                  type=EXCLUDED.type,
                  is_enabled=EXCLUDED.is_enabled,
                  updated_at=now();
                """,
                (chat_id, title, chat_type, enabled),
            )
        conn.commit()


def set_enabled(chat_id: int, enabled: bool):
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE telegram_chats SET is_enabled=%s, updated_at=now() WHERE chat_id=%s",
                (enabled, chat_id),
            )
        conn.commit()


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    title = chat.title or chat.username or chat.first_name or "Unknown"
    upsert_chat(chat.id, title, chat.type, True)
    await update.message.reply_text(
        "✅ Registered this chat for reports.\n"
        f"Chat: {title}\n"
        "Now the cron job will send updates here automatically."
    )


async def stop_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    set_enabled(chat.id, False)
    await update.message.reply_text("🛑 Reports disabled for this chat. Use /register to enable again.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"pong ✅\nchat_id: {chat.id}\ntype: {chat.type}")


def main():
    ensure_tables()
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("stopreports", stop_reports))
    app.add_handler(CommandHandler("ping", ping))
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
