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

COINS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "AVAX": "avalanche-2",
    "DOGE": "dogecoin",
    "LINK": "chainlink",
}

VWCE_TICKER = "vwce.de"
GOLD_TICKER = "4gld.de"
SP500_TICKER = "spy.us"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AndreyBot/1.0; +https://t.me/)"}


async def fetch_json(session: aiohttp.ClientSession, url: str, params=None):
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=10) as resp:
            if resp.status != 200:
                print(f"⚠ fetch_json {url} failed: {resp.status}")
                return None
            return await resp.json()
    except Exception as e:
        print(f"⚠ fetch_json error: {e}")
        return None


async def fetch_text(session: aiohttp.ClientSession, url: str, params=None):
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=10) as resp:
            if resp.status != 200:
                print(f"⚠ fetch_text {url} failed: {resp.status}")
                return None
            return await resp.text()
    except Exception as e:
        print(f"⚠ fetch_text error: {e}")
        return None


async def get_crypto_prices(session: aiohttp.ClientSession):
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
        out[symbol] = {"usd": price, "change_24h": chg}
    return out


async def get_stooq_price(session: aiohttp.ClientSession, ticker: str):
    url = "https://stooq.com/q/l/"
    params = {"s": ticker, "i": "d"}
    txt = await fetch_text(session, url, params=params)
    if not txt:
        return None

    lines = txt.strip().splitlines()
    if len(lines) < 2:
        return None
    last_line = lines[1].strip()
    parts = [p.strip('"') for p in last_line.split(",")]
    try:
        return float(parts[6])
    except Exception:
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я онлайн.\n"
        "Твои команды:\n"
        "/status – проверить, жив ли я\n"
        "/pingprices – показать цены\n"
        "/start – показать это сообщение"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот жив и слушает команды.")


async def pingprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiohttp.ClientSession() as session:
        crypto = await get_crypto_prices(session)
        sp500 = await get_stooq_price(session, SP500_TICKER)
        vwce = await get_stooq_price(session, VWCE_TICKER)
        gold = await get_stooq_price(session, GOLD_TICKER)

    lines = ["💹 Цены прямо сейчас:"]

    if sp500:
        lines.append(f"• S&P 500 (SPY): ~{sp500:.2f} USD")
    else:
        lines.append("• S&P 500: не смог получить")

    if gold:
        lines.append(f"• Gold (ETF): ~{gold:.2f} EUR")
    else:
        lines.append("• Gold: не смог получить")

    if vwce:
        lines.append(f"• VWCE: ~{vwce:.2f} EUR")
    else:
        lines.append("• VWCE: не смог получить")

    lines.append("\nКрипта:")

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
            if chg is not None:
                lines.append(f"• {sym}: ${price:,.2f} ({chg:+.2f}%)")
            else:
                lines.append(f"• {sym}: ${price:,.2f}")
    else:
        lines.append("• не смог получить котировки")

    await update.message.reply_text("\n".join(lines))


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я тебя слышу 👂")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pingprices", pingprices))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    print("🚀 Bot is running. Send /pingprices in Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()
