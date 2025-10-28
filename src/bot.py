import os
import math
import traceback
import aiohttp
from typing import Dict, Any, Optional, Tuple, List

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === ENV ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")

# === CONFIG ===
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AndreyBot/1.0; +https://t.me/)"}
TIMEOUT = aiohttp.ClientTimeout(total=10)

# Тикеры на Yahoo Finance
YF_TICKERS = {
    "SP500": "SPY",        # можно заменить на ^GSPC, но SPY чаще возвращает цену
    "VWCE": "VWCE.DE",
    "GOLD": "4GLD.DE",
}

# Крипта: CoinGecko id + Binance-символы (для фолбэка)
COINS = {
    "BTC": ("bitcoin", "BTCUSDT"),
    "ETH": ("ethereum", "ETHUSDT"),
    "SOL": ("solana", "SOLUSDT"),
    "AVAX": ("avalanche-2", "AVAXUSDT"),
    "DOGE": ("dogecoin", "DOGEUSDT"),
    "LINK": ("chainlink", "LINKUSDT"),
}

# ----------------- HTTP helpers -----------------
async def get_json(session: aiohttp.ClientSession, url: str, params=None) -> Optional[Dict[str, Any]]:
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT) as r:
            if r.status != 200:
                print(f"⚠ {url} -> HTTP {r.status}")
                return None
            return await r.json()
    except Exception as e:
        print(f"⚠ get_json({url}) error: {e}")
        return None

# ----------------- PRICES: Yahoo Finance -----------------
async def get_yahoo_prices(session: aiohttp.ClientSession) -> Dict[str, Tuple[Optional[float], Optional[str]]]:
    """
    Возвращает { 'SP500': (price, currency), 'VWCE': (price, currency), 'GOLD': (price, currency) }
    """
    symbols = ",".join(YF_TICKERS.values())
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    data = await get_json(session, url, {"symbols": symbols})
    out: Dict[str, Tuple[Optional[float], Optional[str]]] = {"SP500": (None, None), "VWCE": (None, None), "GOLD": (None, None)}

    try:
        res = (data or {}).get("quoteResponse", {}).get("result", [])
        by_symbol = {it.get("symbol"): it for it in res}
        for k, sym in YF_TICKERS.items():
            item = by_symbol.get(sym)
            if item:
                price = item.get("regularMarketPrice")
                cur = item.get("currency")
                out[k] = (float(price) if price is not None else None, cur)
    except Exception as e:
        print("⚠ parse_yahoo error:", e, traceback.format_exc())
    return out

# ----------------- PRICES: CoinGecko + fallback Binance -----------------
async def get_coingecko(session: aiohttp.ClientSession) -> Dict[str, Dict[str, Optional[float]]]:
    ids = ",".join(v[0] for v in COINS.values())
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"}
    data = await get_json(session, url, params)
    out: Dict[str, Dict[str, Optional[float]]] = {}
    if not data:
        return out

    # map id->sym
    id_to_sym = {v[0]: k for k, v in COINS.items()}
    for cg_id, payload in data.items():
        sym = id_to_sym.get(cg_id)
        if not sym:
            continue
        price = payload.get("usd")
        chg = payload.get("usd_24h_change")
        out[sym] = {"usd": float(price) if price is not None else None,
                    "change_24h": float(chg) if chg is not None else None}
    return out

async def get_binance_price(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    url = "https://api.binance.com/api/v3/ticker/price"
    data = await get_json(session, url, {"symbol": symbol})
    try:
        if data and "price" in data:
            return float(data["price"])
    except Exception as e:
        print(f"⚠ parse_binance {symbol} error:", e)
    return None

async def get_crypto_prices(session: aiohttp.ClientSession) -> Dict[str, Dict[str, Optional[float]]]:
    """
    Пытаемся через CoinGecko; что не пришло — дотягиваем ценой с Binance (без % изменения).
    """
    base = await get_coingecko(session)
    # fallback для пустых
    tasks: List[Tuple[str, str]] = []
    for sym, (_, bin_sym) in COINS.items():
        if sym not in base or base[sym].get("usd") is None:
            tasks.append((sym, bin_sym))

    for sym, bin_sym in tasks:
        price = await get_binance_price(session, bin_sym)
        if price is not None:
            base.setdefault(sym, {})["usd"] = price
            base[sym].setdefault("change_24h", None)

    return base

# ----------------- BOT handlers -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я онлайн.\n"
        "Твои команды:\n"
        "/status – проверить, работаю ли я\n"
        "/pingprices – показать цены\n"
        "/start – показать это сообщение"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот жив и слушает команды.")

async def cmd_pingprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            yf = await get_yahoo_prices(session)
            crypto = await get_crypto_prices(session)

        lines = ["💹 Цены прямо сейчас:"]

        sp, sp_cur = yf.get("SP500", (None, None))
        vw, vw_cur = yf.get("VWCE", (None, None))
        au, au_cur = yf.get("GOLD", (None, None))

        if sp is not None:
            lines.append(f"• S&P 500 (SPY): ~{sp:.2f} {sp_cur or ''}".strip())
        else:
            lines.append("• S&P 500: не смог получить")

        if au is not None:
            lines.append(f"• Gold (ETF): ~{au:.2f} {au_cur or ''}".strip())
        else:
            lines.append("• Gold: не смог получить")

        if vw is not None:
            lines.append(f"• VWCE: ~{vw:.2f} {vw_cur or ''}".strip())
        else:
            lines.append("• VWCE: не смог получить")

        lines.append("\nКрипта:")
        if crypto:
            order = ["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK"]
            for sym in order:
                item = crypto.get(sym)
                if not item or item.get("usd") is None:
                    lines.append(f"• {sym}: н/д")
                    continue
                price = item["usd"]
                chg = item.get("change_24h")
                if isinstance(chg, (int, float)) and not math.isnan(chg):
                    lines.append(f"• {sym}: ${price:,.2f} ({chg:+.2f}%)")
                else:
                    lines.append(f"• {sym}: ${price:,.2f}")
        else:
            lines.append("• не смог получить котировки")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        print("❌ /pingprices error:", e, traceback.format_exc())
        await update.message.reply_text("⚠ Не удалось получить данные (временная ошибка). Попробуй ещё раз.")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я тебя слышу 👂")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("❌ Global error handler:", context.error, traceback.format_exc())
    # не падаем ни при каких ошибках

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pingprices", cmd_pingprices))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_error_handler(on_error)

    print("🚀 Bot is running.")
    app.run_polling()

if __name__ == "__main__":
    main()
