import os
import aiohttp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
if TOKEN is None:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")

# --- настройки тикеров ---

# Крипта: символы для CoinGecko
COINS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "AVAX": "avalanche-2",
    "DOGE": "dogecoin",
    "LINK": "chainlink",
}

# ETF/Золото: тикеры для stooq (бесплатно)
# Stooq использует форматы типа:
# - VWCE.DE может быть 'vwce.de' или 'vwce.de' -> попробуем в нижнем регистре
# - Золото физическое в евро: часто торгуется как Xetra Gold (4GLD.DE),
#   но твой инструмент "X IE PHYSICAL GOLD ETC EUR" может иметь другой тикер
#   На тест поставлю 4GLD.DE как замену. Потом подгоним.
VWCE_TICKER = "vwce.de"
GOLD_TICKER = "4gld.de"  # временно так, мы потом уточним твой точный ISIN и тикер

# S&P500 мы можем эмулировать через SPY (SPDR S&P 500 ETF) с Yahoo/Stooq.
SP500_TICKER = "spy.us"  # SPY в долларах как прокси индекса


# ==========================
#  HTTP утилиты
# ==========================

async def fetch_json(session: aiohttp.ClientSession, url: str, params=None):
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        return None

async def fetch_text(session: aiohttp.ClientSession, url: str, params=None):
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                return None
            return await resp.text()
    except Exception:
        return None


# ==========================
#  Получение цен
# ==========================

async def get_crypto_prices(session: aiohttp.ClientSession):
    """
    Возвращает dict вида:
    {
      "BTC": {"usd": 64234.12, "change_24h": -2.13},
      ...
    }
    Используем CoinGecko /simple/price
    """
    ids = ",".join(COINS.values())
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": ids,
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    data = await fetch_json(session, url, params=params)
    if not data:
        return None

    out = {}
    for symbol, cg_id in COINS.items():
        if cg_id not in data:
            continue
        entry = data[cg_id]
        price = entry.get("usd")
        chg = entry.get("usd_24h_change")
        out[symbol] = {
            "usd": price,
            "change_24h": chg,
        }
    return out


async def get_stooq_price(session: aiohttp.ClientSession, ticker: str):
    """
    Stooq CSV API: https://stooq.com
    Формат запроса:
      https://stooq.com/q/l/?s=spy.us&i=d
    Ответ CSV, последняя строка содержит цену close.

    Вернём float или None.
    """
    url = "https://stooq.com/q/l/"
    params = {
        "s": ticker,
        "i": "d",  # daily
    }
    txt = await fetch_text(session, url, params=params)
    if not txt:
        return None

    # пример CSV:
    # "Symbol","Date","Time","Open","High","Low","Close","Volume"
    # "spy.us","2024-10-27","22:00:06","504.42","506.68","503.79","505.10","..."
    lines = txt.strip().splitlines()
    if len(lines) < 2:
        return None
    last_line = lines[1].strip()
    parts = [p.strip('"') for p in last_line.split(",")]
    # parts[6] = Close
    try:
        close_val = float(parts[6])
        return close_val
    except Exception:
        return None


# ==========================
#  Команды бота
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я онлайн.\n"
        "Твои команды:\n"
        "/status – проверить, работаю ли я\n"
        "/pingprices – показать цены (черновик)\n"
        "/start – показать это сообщение"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот жив и слушает команды.")

async def pingprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Кажется простым, но делает реальную работу:
    - тащит крипту с CoinGecko
    - тащит VWCE, Золото и SP500-плейсхолдер через stooq
    - форматирует текст
    """
    async with aiohttp.ClientSession() as session:
        # крипта
        crypto = await get_crypto_prices(session)

        # фондовые/ETF
        sp500 = await get_stooq_price(session, SP500_TICKER)
        vwce = await get_stooq_price(session, VWCE_TICKER)
        gold = await get_stooq_price(session, GOLD_TICKER)

    # форматируем ответ
    lines = ["💹 Цены прямо сейчас:"]

    # S&P 500
    if sp500 is not None:
        lines.append(f"• S&P 500 (SPY): ~{sp500:.2f} USD")
    else:
        lines.append("• S&P 500: не смог получить")

    # Gold
    if gold is not None:
        lines.append(f"• Gold (EUR ETF): ~{gold:.2f}")
    else:
        lines.append("• Gold: не смог получить")

    # VWCE
    if vwce is not None:
        lines.append(f"• VWCE: ~{vwce:.2f} EUR")
    else:
        lines.append("• VWCE: не смог получить")

    lines.append("")  # пустая строка перед криптой
    lines.append("Крипта:")

    if crypto:
        for sym in ["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK"]:
            info = crypto.get(sym)
            if not info:
                lines.append(f"• {sym}: н/д")
                continue
            price = info["usd"]
            chg = info["change_24h"]
            if price is None:
                lines.append(f"• {sym}: н/д")
                continue
            # формат +/- изменения
            if chg is not None:
                lines.append(f"• {sym}: ${price:,.2f} ({chg:+.2f}%)")
            else:
                lines.append(f"• {sym}: ${price:,.2f}")
    else:
        lines.append("• не смог получить котировки")

    text = "\n".join(lines)
    await update.message.reply_text(text)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я тебя слышу 👂")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pingprices", pingprices))

    # fallback на обычный текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    print("🚀 Bot is running. Send /status or /pingprices in Telegram.")
    app.run_polling()

if __name__ == "__main__":
    main()
