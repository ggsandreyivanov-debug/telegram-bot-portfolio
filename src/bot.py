
import os
import csv
import requests
from math import isnan
from datetime import time, datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
    JobQueue,
)

# ----------------- НАСТРОЙКИ -----------------
ALLOWED_USER = 235538565  # твой chat_id (только ты получаешь ответы и алерты)
TOKEN = os.getenv("BOT_TOKEN")
if TOKEN is None:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")

# Пороговые изменения
CRYPTO_ALERT_MOVE = 0.04   # ±4% для крипты
ASSET_ALERT_MOVE  = 0.01   # ±1% для фондов/золота

# Таймзона и расписание
RIGA_TZ = ZoneInfo("Europe/Riga")
DAILY_TIME = time(11, 0, tzinfo=RIGA_TZ)      # каждый день 11:00 по Риге
WEEKLY_TIME = time(19, 0, tzinfo=RIGA_TZ)     # каждое воскресенье 19:00 по Риге
WEEKLY_DAY = 6                                 # Sunday (0=Mon ... 6=Sun)

# Крипта (CoinGecko ids)
CRYPTO_IDS = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "AVAX": "avalanche-2",
    "DOGE": "dogecoin",
    "LINK": "chainlink",
}

# Фонды/золото (stooq тикеры с запасными вариантами)
ASSET_TICKERS = {
    # прокси на S&P500
    "S&P 500 (SPY)": ["spy.us", "^spx.us"],
    # глобальный индекс в евро
    "VWCE": ["vwce.de", "vwrl.us"],
    # золото (берём в $/oz; если найдём хороший EUR-тикер — добавим во второй элемент)
    "GOLD (XAU/USD)": ["xauusd.us", "xauusd", "gold.us"],
}

# Храним последние цены для вычисления % движения с ПОСЛЕДНЕЙ проверки
last_crypto_prices: dict[str, float] = {}
last_asset_prices: dict[str, float] = {}

# ----------------- УТИЛЫ -----------------
def pct_change(prev: float | None, new: float | None) -> float | None:
    if prev is None or new is None:
        return None
    if prev == 0 or isnan(prev) or isnan(new):
        return None
    return (new - prev) / prev

def fetch_stooq_close(ticker: str) -> float | None:
    """
    Берём Close с stooq CSV.
    """
    url = f"https://stooq.com/q/l/?s={ticker}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, timeout=6)
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        reader = csv.DictReader(lines)
        row = next(reader, None)
        if not row:
            return None
        close = row.get("Close")
        if not close or close == "N/A":
            return None
        return float(close)
    except Exception as e:
        print(f"[stooq] {ticker} failed: {e}")
        return None

def fetch_asset_price(name: str) -> tuple[str, float | None]:
    """
    Перебираем список тикеров, возвращаем первый успешный.
    """
    for t in ASSET_TICKERS[name]:
        price = fetch_stooq_close(t)
        if price is not None:
            return (t, price)
    return (ASSET_TICKERS[name][0], None)

def fetch_coingecko_prices(ids: list[str]) -> dict[str, float | None]:
    """
    CoinGecko simple price (без ключа). Возвращает {id: price_usd}.
    """
    joined = ",".join(ids)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={joined}&vs_currencies=usd"
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        out: dict[str, float | None] = {}
        for _id in ids:
            out[_id] = float(data.get(_id, {}).get("usd")) if data.get(_id) else None
        return out
    except Exception as e:
        print(f"[coingecko] failed: {e}")
        return {i: None for i in ids}

def fmt_price(v: float | None, suffix="") -> str:
    return f"{v:.4g}{suffix}" if isinstance(v, (float, int)) else "—"

def sign_pct(x: float) -> str:
    return f"{x*100:+.2f}%"

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ----------------- СООБЩЕНИЯ -----------------
def build_prices_snapshot() -> str:
    # Крипта
    cg_ids = list(CRYPTO_IDS.values())
    cg_map = fetch_coingecko_prices(cg_ids)
    id_to_ticker = {v: k for k, v in CRYPTO_IDS.items()}

    lines = [f"⏰ {now_utc_str()}", "💹 Цены прямо сейчас:"]
    for cid, price in cg_map.items():
        lines.append(f"• {id_to_ticker[cid]}: {fmt_price(price, ' $')}")

    # Фонды/золото
    for name in ASSET_TICKERS:
        used_ticker, price = fetch_asset_price(name)
        suffix = " $"
        if "VWCE" in name:
            suffix = " €"  # это ориентир; stooq отдаёт в валюте бумаги (у VWCE евро)
        lines.append(f"• {name}: {fmt_price(price, suffix)} (via {used_ticker})")

    return "\n".join(lines)

def build_weekly_calendar_text() -> str:
    # Базовый набросок ключевых рисков недели (без внешних API).
    items = [
        "📅 ФРС/ставка США — тон влияет на акции США и VWCE",
        "📅 CPI/инфляция США — высокая инфляция → давление на акции, поддержка золоту",
        "📅 Отчёты крупных компаний США — волатильность индекса",
        "🌏 США—Китай — риторика/встречи → техи/полупроводники",
        "💬 Спичи центробанков (Пауэлл/Лагард) — чувствительно для риска",
    ]
    out = ["🗓 Календарь риска на неделю (набросок):"]
    out += [f"• {x}" for x in items]
    out.append("\nP.S. Могу расширить с реальными датами по запросу.")
    return "\n".join(out)

# ----------------- БОТ-ХЕНДЛЕРЫ -----------------
async def is_allowed(update: Update) -> bool:
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_USER:
        print(f"⛔ Unauthorized access attempt from chat_id={chat_id}")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await update.message.reply_text(
        "✅ Я здесь. Команды:\n"
        "• /status — сводка по портфелю и рискам\n"
        "• /pingprices — живые цены (крипта + фонды)\n"
        "\nАлерты:\n"
        "• Крипта: ±4%\n"
        "• VWCE/SPY/Gold: ±1%\n"
        "\nАвто-дайджест:\n"
        "• Ежедневно 11:00 (Рига) — цены\n"
        "• Воскресенье 19:00 — календарь недели\n"
        "Только для тебя 🔒"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    portfolio = [
        "📦 Идея портфеля:",
        "• S&P 500 (через SPY) — рост",
        "• VWCE (весь мир) — диверсификация",
        "• Gold — защита",
        "",
        "Логика: акции = рост, золото = защита. Балансируешь, а не гадаешь.",
    ]
    risks = [
        "Риски ближайшего плана:",
        "• ФРС/ставка, CPI США",
        "• Риторика США–Китай",
        "• Отчёты мейджоров США",
    ]
    msg = f"⏰ {now_utc_str()}\n" + "\n".join(portfolio) + "\n\n" + "\n".join(risks)
    await update.message.reply_text(msg)

async def pingprices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await update.message.reply_text(build_prices_snapshot())

async def echo_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await update.message.reply_text(f"Твой chat_id: {ALLOWED_USER}")

# ----------------- JOBS: ПОВТОРЯЮЩИЕСЯ ПРОВЕРКИ -----------------
def collect_current_prices():
    """
    Возвращает:
      crypto_prices: {ticker: price_usd}
      asset_prices:  {name: price}
    """
    # крипта
    crypto_prices: dict[str, float | None] = {}
    cg_map = fetch_coingecko_prices(list(CRYPTO_IDS.values()))
    for sym, cid in CRYPTO_IDS.items():
        crypto_prices[sym] = cg_map.get(cid)

    # фонды/золото
    asset_prices: dict[str, float | None] = {}
    for name in ASSET_TICKERS:
        _, price = fetch_asset_price(name)
        asset_prices[name] = price

    return crypto_prices, asset_prices

async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    """
    Каждые 10 минут: считаем % изменения vs прошлый чек,
    шлём алерты только если превысили порог.
    """
    chat_id = ALLOWED_USER
    crypto_curr, asset_curr = collect_current_prices()

    # Крипта (±4%)
    for sym, newp in crypto_curr.items():
        prevp = last_crypto_prices.get(sym)
        chg = pct_change(prevp, newp)
        if chg is not None and abs(chg) >= CRYPTO_ALERT_MOVE:
            sign = "🔺" if chg > 0 else "🔻"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{sign} {sym} {sign_pct(chg)} (с последней проверки)\nТекущая: {fmt_price(newp, ' $')}"
            )
        # обновляем базу (после сравнения)
        if newp is not None:
            last_crypto_prices[sym] = newp

    # Фонды/золото (±1%)
    for name, newp in asset_curr.items():
        prevp = last_asset_prices.get(name)
        chg = pct_change(prevp, newp)
        if chg is not None and abs(chg) >= ASSET_ALERT_MOVE:
            sign = "🔺" if chg > 0 else "🔻"
            suffix = " €" if "VWCE" in name else " $"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{sign} {name} {sign_pct(chg)} (с последней проверки)\nТекущая: {fmt_price(newp, suffix)}"
            )
        if newp is not None:
            last_asset_prices[name] = newp

async def daily_digest(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=ALLOWED_USER, text="📬 Ежедневный дайджест\n" + build_prices_snapshot())

async def weekly_calendar(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=ALLOWED_USER, text=build_weekly_calendar_text())

# ----------------- MAIN -----------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pingprices", pingprices_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_chat_id))

    # Jobs
    jq: JobQueue = app.job_queue
    # каждые 10 минут — проверка на ±4%/±1%
    jq.run_repeating(periodic_check, interval=600, first=10, name="price_watch")

    # ежедневно 11:00 по Риге — дайджест цен
    jq.run_daily(daily_digest, time=DAILY_TIME, name="daily_digest")

    # еженедельно вс 19:00 по Риге — календарь недели
    jq.run_daily(weekly_calendar, time=WEEKLY_TIME, days=(WEEKLY_DAY,), name="weekly_calendar")

    print("🚀 Bot is running. Alerts: crypto ±4%, assets ±1%.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
