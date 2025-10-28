import os
import csv
import requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# только ты
ALLOWED_USER = 235538565  # твой chat_id

# токен из переменной окружения
TOKEN = os.getenv("BOT_TOKEN")
if TOKEN is None:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")


async def is_allowed(update: Update) -> bool:
    """
    Разрешаем общаться только владельцу.
    Остальных игнорим и просто логируем попытку.
    """
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_USER:
        print(f"⛔ Unauthorized access attempt from chat_id={chat_id}")
        return False
    return True


# ---------- Вспомогательная часть: котировки ----------

def fetch_last_price_from_stooq(ticker: str):
    """
    Берём последнюю цену с stooq.
    ticker вида 'vwce.de', '^spx.us', 'xauusd.us', и т.д.
    Возвращаем float либо None.
    """
    url = f"https://stooq.com/q/l/?s={ticker}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
    except Exception as e:
        print(f"[price] request failed for {ticker}: {e}")
        return None

    # stooq возвращает CSV вида:
    # Symbol,Date,Time,Open,High,Low,Close,Volume
    # ^SPX.US,2025-10-28,21:59:00,5000,5050,4980,5022,123456
    try:
        lines = resp.text.strip().splitlines()
        reader = csv.DictReader(lines)
        row = next(reader, None)
        if not row:
            print(f"[price] empty CSV for {ticker}")
            return None

        close_str = row.get("Close")
        if close_str is None or close_str == "N/A":
            print(f"[price] no Close for {ticker}, row={row}")
            return None

        return float(close_str)
    except Exception as e:
        print(f"[price] parse failed for {ticker}: {e}")
        return None


def get_price_snapshot():
    """
    Пытаемся получить цены по ключевым штукам:
    - S&P 500
    - золото
    - VWCE (или близкий прокси, если не найдём напрямую)
    Возвращаем текст.
    """

    # S&P 500 (индекс широкого рынка США)
    spx_price = fetch_last_price_from_stooq("^spx.us")

    # Золото (XAUUSD, унция в долларах)
    gold_price = fetch_last_price_from_stooq("xauusd.us")

    # VWCE — это Vanguard FTSE All-World Accumulating (EUR, Xetra)
    # Попробуем сначала 'vwce.de'
    vwce_price = fetch_last_price_from_stooq("vwce.de")
    # если None, попробуем 'vwrl.us' (очень близкий глобальный индексный ETF в долларах)
    if vwce_price is None:
        vwce_price = fetch_last_price_from_stooq("vwrl.us")

    lines = []
    lines.append("💹 Цены прямо сейчас:")

    if spx_price is not None:
        lines.append(f"• S&P 500: ~{spx_price}")
    else:
        lines.append("• S&P 500: не смог получить")

    if gold_price is not None:
        lines.append(f"• Gold (XAU/USD): ~{gold_price} $/oz")
    else:
        lines.append("• Gold: не смог получить")

    if vwce_price is not None:
        lines.append(f"• VWCE (глобалка): ~{vwce_price}")
    else:
        lines.append("• VWCE: не смог получить (биржа не дала цену)")

    return "\n".join(lines)


def build_risk_comment() -> str:
    """
    Ручная секция "что может шатать рынок".
    Потом можно будет автоматизировать (календарь макродат).
    """
    upcoming_events = [
        "📅 ФРС (ставка США) — влияет на акции США (S&P 500, VWCE)",
        "📅 CPI США (инфляция) — если инфляция высокая, рынок может просесть, золото растёт",
        "📅 США vs Китай — влияет на технологический сектор",
    ]

    out = "Рыночные риски ближайшего плана:\n"
    for e in upcoming_events:
        out += f"• {e}\n"
    return out


# ---------- Команды бота ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - приветствие и подсказка по командам
    """
    if not await is_allowed(update):
        return

    await update.message.reply_text(
        "✅ Бот запущен.\n"
        "Команды:\n"
        "• /status — сводка по портфелю и рискам\n"
        "• /pingprices — текущие цены (S&P 500, золото, VWCE)\n"
        "• просто напиши текст — пришлю твой chat_id\n"
        "Только ты можешь это вызвать 🔒"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status — статический отчёт,
    с портфелем и рисками, + печать времени.
    """
    if not await is_allowed(update):
        return

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    portfolio_lines = [
        "📦 Портфель (идея):",
        "• S&P 500 (через индексный ETF США)",
        "• VWCE (весь мир, диверсификация)",
        "• Gold (страховка, защита от шторма)",
        "",
        "Логика: акции = рост, золото = защита. Балансируешь мозг, а не угадываешь рынок.",
    ]

    msg = (
        f"⏰ {now_utc}\n"
        f"👤 chat_id: {ALLOWED_USER}\n\n"
        + "\n".join(portfolio_lines)
        + "\n\n"
        + build_risk_comment()
        + "\n\n"
        "Команды:\n"
        "• /pingprices — живые цены\n"
        "• /status — эта сводка\n"
        "• /start — помощь\n"
    )

    await update.message.reply_text(msg)


async def pingprices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pingprices — живые цены с рынка (через stooq).
    """
    if not await is_allowed(update):
        return

    snapshot_text = get_price_snapshot()
    await update.message.reply_text(snapshot_text)


async def echo_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Любой текст без команды → отправляем твой chat_id.
    """
    if not await is_allowed(update):
        return

    chat_id = update.effective_chat.id
    print(f"CHAT_ID: {chat_id}")  # будет видно в логах Render
    await update.message.reply_text(
        f"Твой chat_id: {chat_id}\n"
        "Я отвечаю только тебе 🔒"
    )


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pingprices", pingprices_cmd))

    # любой текст (без команды)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_chat_id))

    print("🚀 Bot is running. Only allowed user can interact.")
    app.run_polling()


if __name__ == "__main__":
    main()
