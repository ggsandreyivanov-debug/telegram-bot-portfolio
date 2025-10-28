import os
import math
import asyncio
import traceback
import aiohttp
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time
from pytz import timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === ENV ===
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # ваш chat_id для уведомлений
if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")
if not CHAT_ID:
    print("⚠ CHAT_ID не установлен — уведомления будут отключены")

# === CONFIG ===
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(connect=10, total=15)
RIGA_TZ = timezone("Europe/Riga")

YF_TICKERS = {
    "VWCE": "VWCE.DE",
    "GOLD": "4GLD.DE",
    "XETRA_GOLD": "EWG2.DE",
    "SP500": "SPY",
}

COINS = {
    "BTC": ("bitcoin", "BTCUSDT"),
    "ETH": ("ethereum", "ETHUSDT"),
    "SOL": ("solana", "SOLUSDT"),
    "AVAX": ("avalanche-2", "AVAXUSDT"),
    "DOGE": ("dogecoin", "DOGEUSDT"),
    "LINK": ("chainlink", "LINKUSDT"),
}

THRESHOLDS = {"stocks": 1.0, "crypto": 4.0}
last_prices: Dict[str, float] = {}

# === HTTP UTILS ===
async def get_json(session: aiohttp.ClientSession, url: str, params=None):
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT) as r:
            if r.status != 200:
                print(f"⚠ {url} -> HTTP {r.status}")
                return None
            return await r.json()
    except Exception as e:
        print(f"❌ get_json({url}) error:", e)
        return None

# === DATA SOURCES ===
async def get_yahoo_prices(session: aiohttp.ClientSession):
    out = {}
    for k, sym in YF_TICKERS.items():
        try:
            data = await get_json(session, f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}", {"interval": "1d", "range": "1d"})
            if not data or not data.get("chart", {}).get("result"):
                continue
            meta = data["chart"]["result"][0].get("meta", {})
            price = meta.get("regularMarketPrice")
            cur = meta.get("currency", "USD")
            if price:
                out[k] = (float(price), cur)
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"⚠ {k}:", e)
    return out

async def get_coinpaprika_price(session: aiohttp.ClientSession, coin_id: str):
    mapping = {
        "bitcoin": "btc-bitcoin",
        "ethereum": "eth-ethereum",
        "solana": "sol-solana",
        "avalanche-2": "avax-avalanche",
        "dogecoin": "doge-dogecoin",
        "chainlink": "link-chainlink",
    }
    paprika_id = mapping.get(coin_id)
    if not paprika_id:
        return None
    data = await get_json(session, f"https://api.coinpaprika.com/v1/tickers/{paprika_id}")
    if data:
        quotes = data.get("quotes", {}).get("USD", {})
        return {"usd": quotes.get("price"), "change_24h": quotes.get("percent_change_24h")}
    return None

async def get_crypto_prices(session: aiohttp.ClientSession):
    base = {}
    for sym, (coin_id, _) in COINS.items():
        data = await get_coinpaprika_price(session, coin_id)
        if data:
            base[sym] = data
        await asyncio.sleep(0.2)
    return base

# === MONITORING ===
async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return
    try:
        async with aiohttp.ClientSession() as session:
            yf = await get_yahoo_prices(session)
            crypto = await get_crypto_prices(session)

        alerts = []
        for key, (price, _) in yf.items():
            if price is None:
                continue
            cache_key = f"stock_{key}"
            if cache_key in last_prices:
                diff = ((price - last_prices[cache_key]) / last_prices[cache_key]) * 100
                if abs(diff) >= THRESHOLDS["stocks"]:
                    emoji = "📈" if diff > 0 else "📉"
                    alerts.append(f"{emoji} <b>{key}</b>: {diff:+.2f}% → {price:.2f}")
            last_prices[cache_key] = price

        for sym, data in crypto.items():
            price = data.get("usd")
            if price is None:
                continue
            cache_key = f"crypto_{sym}"
            if cache_key in last_prices:
                diff = ((price - last_prices[cache_key]) / last_prices[cache_key]) * 100
                if abs(diff) >= THRESHOLDS["crypto"]:
                    emoji = "🚀" if diff > 0 else "⚠️"
                    alerts.append(f"{emoji} <b>{sym}</b>: {diff:+.2f}% → ${price:,.2f}")
            last_prices[cache_key] = price

        if alerts:
            await context.bot.send_message(chat_id=CHAT_ID, text="🔔 <b>Ценовые алерты:</b>\n\n" + "\n\n".join(alerts), parse_mode='HTML')

    except Exception as e:
        print("❌ check_price_alerts:", e)

# === DAILY & WEEKLY ===
async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return
    async with aiohttp.ClientSession() as session:
        yf = await get_yahoo_prices(session)
        crypto = await get_crypto_prices(session)
    from datetime import datetime
    now = datetime.now(RIGA_TZ).strftime("%d.%m.%Y")
    msg = [f"🌅 <b>Утренние цены ({now})</b>\n"]
    msg.append("<b>📊 Фондовый рынок:</b>")
    for k in ["VWCE", "GOLD", "XETRA_GOLD", "SP500"]:
        price, cur = yf.get(k, (None, None))
        msg.append(f"• {k}: {price:.2f} {cur}" if price else f"• {k}: н/д")
    msg.append("\n<b>₿ Криптовалюты:</b>")
    for sym, d in crypto.items():
        p, ch = d.get("usd"), d.get("change_24h")
        msg.append(f"• {sym}: ${p:,.2f} ({ch:+.2f}%)" if p and ch else f"• {sym}: н/д")
    await context.bot.send_message(chat_id=CHAT_ID, text="\n".join(msg), parse_mode='HTML')

# === COMMANDS ===
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Бот активен! Используй /portfolio или /pingprices.", parse_mode='HTML')

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Получаю цены...")
    async with aiohttp.ClientSession() as session:
        yf = await get_yahoo_prices(session)
        crypto = await get_crypto_prices(session)
    lines = ["💼 <b>Портфель:</b>"]
    for key in ["VWCE", "GOLD", "XETRA_GOLD"]:
        p, c = yf.get(key, (None, None))
        lines.append(f"• {key}: {p:.2f} {c}" if p else f"• {key}: н/д")
    for sym, d in crypto.items():
        p, ch = d.get("usd"), d.get("change_24h")
        if p:
            emoji = "🟢" if ch and ch > 0 else "🔴"
            lines.append(f"{emoji} {sym}: ${p:,.2f} ({ch:+.2f}%)" if ch else f"• {sym}: ${p:,.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

# === MAIN ===
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    job_queue = app.job_queue
    if CHAT_ID:
        job_queue.run_repeating(check_price_alerts, interval=600, first=30)
        job_queue.run_daily(daily_report, time=dt_time(hour=11, tzinfo=RIGA_TZ))
        print("✅ Scheduler active (Riga time).")
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
