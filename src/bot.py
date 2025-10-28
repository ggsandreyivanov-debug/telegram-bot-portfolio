import os
import math
import traceback
import aiohttp
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time

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
CHAT_ID = os.getenv("CHAT_ID")  # ваш chat_id для автоматических уведомлений
if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")
if not CHAT_ID:
    print("⚠ CHAT_ID не установлен - автоматические уведомления будут отключены")

# === CONFIG ===
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AndreyBot/1.0; +https://t.me/)"}
TIMEOUT = aiohttp.ClientTimeout(total=10)

# Тикеры на Yahoo Finance
YF_TICKERS = {
    "VWCE": "VWCE.DE",
    "GOLD": "4GLD.DE",
    "SP500": "SPY",
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

# Пороги для алертов (можно менять через /setalert)
THRESHOLDS = {
    "stocks": 1.0,   # ±1% для акций/ETF
    "crypto": 4.0,   # ±4% для криптовалют
}

# Хранилище последних цен для алертов
last_prices: Dict[str, float] = {}

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
    Возвращает { 'VWCE': (price, currency), 'GOLD': (price, currency), 'SP500': (price, currency) }
    """
    symbols = ",".join(YF_TICKERS.values())
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    data = await get_json(session, url, {"symbols": symbols})
    out: Dict[str, Tuple[Optional[float], Optional[str]]] = {k: (None, None) for k in YF_TICKERS}

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

# ----------------- MONITORING LOGIC -----------------
async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Проверка цен каждые 10 минут и отправка алертов"""
    if not CHAT_ID:
        return
    
    try:
        async with aiohttp.ClientSession() as session:
            yf = await get_yahoo_prices(session)
            crypto = await get_crypto_prices(session)
        
        alerts = []
        
        # Проверяем акции/ETF
        for key, (price, currency) in yf.items():
            if price is None:
                continue
            
            cache_key = f"stock_{key}"
            if cache_key in last_prices:
                old_price = last_prices[cache_key]
                change_pct = ((price - old_price) / old_price) * 100
                
                if abs(change_pct) >= THRESHOLDS["stocks"]:
                    emoji = "📈" if change_pct > 0 else "📉"
                    alerts.append(
                        f"{emoji} <b>{key}</b>: {change_pct:+.2f}%\n"
                        f"Цена: {price:.2f} {currency or ''}"
                    )
            
            last_prices[cache_key] = price
        
        # Проверяем криптовалюты
        for sym, data in crypto.items():
            price = data.get("usd")
            if price is None:
                continue
            
            cache_key = f"crypto_{sym}"
            if cache_key in last_prices:
                old_price = last_prices[cache_key]
                change_pct = ((price - old_price) / old_price) * 100
                
                if abs(change_pct) >= THRESHOLDS["crypto"]:
                    emoji = "🚀" if change_pct > 0 else "⚠️"
                    alerts.append(
                        f"{emoji} <b>{sym}</b>: {change_pct:+.2f}%\n"
                        f"Цена: ${price:,.2f}"
                    )
            
            last_prices[cache_key] = price
        
        # Отправляем алерты
        if alerts:
            message = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(alerts)
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=message,
                parse_mode='HTML'
            )
    
    except Exception as e:
        print(f"❌ check_price_alerts error: {e}", traceback.format_exc())

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный отчёт в 11:00 по Риге"""
    if not CHAT_ID:
        return
    
    try:
        async with aiohttp.ClientSession() as session:
            yf = await get_yahoo_prices(session)
            crypto = await get_crypto_prices(session)
        
        from datetime import datetime
        now = datetime.now().strftime("%d.%m.%Y")
        
        lines = [f"🌅 <b>Утренние цены ({now})</b>\n"]
        
        # Акции/ETF
        lines.append("<b>📊 Фондовый рынок:</b>")
        for key in ["VWCE", "GOLD", "SP500"]:
            price, currency = yf.get(key, (None, None))
            if price:
                lines.append(f"• {key}: {price:.2f} {currency or ''}")
            else:
                lines.append(f"• {key}: н/д")
        
        # Криптовалюты
        lines.append("\n<b>₿ Криптовалюты:</b>")
        for sym in ["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK"]:
            data = crypto.get(sym, {})
            price = data.get("usd")
            chg = data.get("change_24h")
            if price:
                if isinstance(chg, (int, float)) and not math.isnan(chg):
                    lines.append(f"• {sym}: ${price:,.2f} ({chg:+.2f}%)")
                else:
                    lines.append(f"• {sym}: ${price:,.2f}")
            else:
                lines.append(f"• {sym}: н/д")
        
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="\n".join(lines),
            parse_mode='HTML'
        )
    
    except Exception as e:
        print(f"❌ daily_report error: {e}", traceback.format_exc())

async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный отчёт в воскресенье 19:00"""
    if not CHAT_ID:
        return
    
    try:
        async with aiohttp.ClientSession() as session:
            yf = await get_yahoo_prices(session)
            crypto = await get_crypto_prices(session)
        
        lines = ["📆 <b>Еженедельный отчёт</b>\n"]
        
        # Акции/ETF
        lines.append("<b>📊 Фондовый рынок:</b>")
        for key in ["VWCE", "GOLD", "SP500"]:
            price, currency = yf.get(key, (None, None))
            if price:
                lines.append(f"• {key}: {price:.2f} {currency or ''}")
        
        # Криптовалюты
        lines.append("\n<b>₿ Криптовалюты:</b>")
        for sym in ["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK"]:
            data = crypto.get(sym, {})
            price = data.get("usd")
            chg = data.get("change_24h")
            if price:
                if isinstance(chg, (int, float)) and not math.isnan(chg):
                    lines.append(f"• {sym}: ${price:,.2f} ({chg:+.2f}%)")
                else:
                    lines.append(f"• {sym}: ${price:,.2f}")
        
        lines.append("\n<i>События недели отслеживаются вручную</i>")
        
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="\n".join(lines),
            parse_mode='HTML'
        )
    
    except Exception as e:
        print(f"❌ weekly_report error: {e}", traceback.format_exc())

# ----------------- BOT handlers -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Привет! Я бот для мониторинга портфеля</b>\n\n"
        "<b>Доступные команды:</b>\n"
        "/portfolio - показать текущие цены портфеля\n"
        "/pingprices - показать все цены (включая SP500)\n"
        "/alerts - настройки алертов\n"
        "/setalert - изменить пороги уведомлений\n"
        "/status - проверка работы бота\n"
        "/help - подробная помощь\n\n"
        "🔔 <b>Автоматические уведомления:</b>\n"
        "• Алерты каждые 10 минут\n"
        "• Утренний отчёт в 11:00 (Рига)\n"
        "• Недельный отчёт в Вс 19:00 (Рига)",
        parse_mode='HTML'
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S %d.%m.%Y")
    monitored = len(YF_TICKERS) + len(COINS)
    await update.message.reply_text(
        f"✅ <b>Бот работает!</b>\n\n"
        f"🕐 Время: {now}\n"
        f"📊 Отслеживается активов: {monitored}\n"
        f"💾 В кэше цен: {len(last_prices)}",
        parse_mode='HTML'
    )

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать только портфельные активы (без SP500)"""
    try:
        await update.message.reply_text("🔄 Получаю цены портфеля...")
        
        async with aiohttp.ClientSession() as session:
            yf = await get_yahoo_prices(session)
            crypto = await get_crypto_prices(session)

        lines = ["💼 <b>Портфель:</b>\n"]
        
        # Только VWCE и GOLD
        lines.append("<b>📊 ETF:</b>")
        for key in ["VWCE", "GOLD"]:
            price, currency = yf.get(key, (None, None))
            if price:
                name = "VWCE" if key == "VWCE" else "X IE Physical Gold ETC EUR"
                lines.append(f"• {name}: {price:.2f} {currency or ''}")
            else:
                lines.append(f"• {key}: н/д")
        
        # Криптовалюты
        lines.append("\n<b>₿ Криптовалюты:</b>")
        for sym in ["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK"]:
            data = crypto.get(sym, {})
            price = data.get("usd")
            chg = data.get("change_24h")
            if price:
                if isinstance(chg, (int, float)) and not math.isnan(chg):
                    emoji = "🟢" if chg >= 0 else "🔴"
                    lines.append(f"{emoji} {sym}: ${price:,.2f} ({chg:+.2f}%)")
                else:
                    lines.append(f"• {sym}: ${price:,.2f}")
            else:
                lines.append(f"• {sym}: н/д")

        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    except Exception as e:
        print("❌ /portfolio error:", e, traceback.format_exc())
        await update.message.reply_text("⚠ Не удалось получить данные. Попробуй ещё раз.")

async def cmd_pingprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все цены включая SP500"""
    try:
        async with aiohttp.ClientSession() as session:
            yf = await get_yahoo_prices(session)
            crypto = await get_crypto_prices(session)

        lines = ["💹 <b>Все цены:</b>\n"]
        
        lines.append("<b>📊 Фондовый рынок:</b>")
        for key in ["SP500", "VWCE", "GOLD"]:
            price, currency = yf.get(key, (None, None))
            if price:
                name = {"SP500": "S&P 500 (SPY)", "VWCE": "VWCE", "GOLD": "Gold (ETF)"}[key]
                lines.append(f"• {name}: {price:.2f} {currency or ''}")
            else:
                lines.append(f"• {key}: н/д")

        lines.append("\n<b>₿ Криптовалюты:</b>")
        for sym in ["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK"]:
            data = crypto.get(sym, {})
            price = data.get("usd")
            chg = data.get("change_24h")
            if price:
                if isinstance(chg, (int, float)) and not math.isnan(chg):
                    lines.append(f"• {sym}: ${price:,.2f} ({chg:+.2f}%)")
                else:
                    lines.append(f"• {sym}: ${price:,.2f}")
            else:
                lines.append(f"• {sym}: н/д")

        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    except Exception as e:
        print("❌ /pingprices error:", e, traceback.format_exc())
        await update.message.reply_text("⚠ Не удалось получить данные. Попробуй ещё раз.")

async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать настройки алертов"""
    message = (
        "⚙️ <b>Настройки алертов:</b>\n\n"
        f"<b>Фондовый рынок:</b> ±{THRESHOLDS['stocks']}%\n"
        f"<b>Криптовалюты:</b> ±{THRESHOLDS['crypto']}%\n\n"
        "<b>📅 Расписание:</b>\n"
        "• Проверка цен: каждые 10 минут\n"
        "• Утренний отчёт: 11:00 (Рига)\n"
        "• Недельный отчёт: Вс 19:00 (Рига)\n\n"
        f"💾 В кэше отслеживается: {len(last_prices)} цен"
    )
    await update.message.reply_text(message, parse_mode='HTML')

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменить пороги алертов"""
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "Использование: <code>/setalert [stocks|crypto] [процент]</code>\n\n"
            "Примеры:\n"
            "<code>/setalert stocks 2</code> — алерты для акций при ±2%\n"
            "<code>/setalert crypto 5</code> — алерты для крипты при ±5%",
            parse_mode='HTML'
        )
        return
    
    asset_type = context.args[0].lower()
    try:
        threshold = float(context.args[1])
        if threshold <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Процент должен быть положительным числом")
        return
    
    if asset_type not in ["stocks", "crypto"]:
        await update.message.reply_text("❌ Тип должен быть 'stocks' или 'crypto'")
        return
    
    THRESHOLDS[asset_type] = threshold
    name = "акций/ETF" if asset_type == "stocks" else "криптовалют"
    await update.message.reply_text(
        f"✅ Порог алертов для {name} установлен: ±{threshold}%",
        parse_mode='HTML'
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подробная помощь"""
    message = (
        "📖 <b>Помощь по командам:</b>\n\n"
        "<b>/portfolio</b> — показать портфель (VWCE, Gold, крипта)\n"
        "<b>/pingprices</b> — показать все цены (+ SP500)\n"
        "<b>/alerts</b> — текущие настройки уведомлений\n"
        "<b>/setalert</b> — изменить пороги алертов\n"
        "<b>/status</b> — проверка работы бота\n"
        "<b>/help</b> — это сообщение\n\n"
        "<b>🔔 Автоматика:</b>\n"
        "Бот проверяет цены каждые 10 минут и отправляет алерты, "
        "если цена изменилась больше установленного порога.\n\n"
        "Ежедневно в 11:00 по Риге приходит утренний отчёт, "
        "а по воскресеньям в 19:00 — недельный отчёт."
    )
    await update.message.reply_text(message, parse_mode='HTML')

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я тебя слышу 👂")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("❌ Global error handler:", context.error, traceback.format_exc())

def main():
    # Используем Application.builder() вместо ApplicationBuilder()
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("pingprices", cmd_pingprices))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_error_handler(on_error)

    # Планировщик заданий
    job_queue = app.job_queue
    
    if job_queue and CHAT_ID:
        # Проверка алертов каждые 10 минут
        job_queue.run_repeating(check_price_alerts, interval=600, first=60)
        
        # Ежедневный отчёт в 11:00 по Риге (Europe/Riga = UTC+2/UTC+3)
        job_queue.run_daily(
            daily_report,
            time=dt_time(hour=11, minute=0),
            days=(0, 1, 2, 3, 4, 5, 6),  # каждый день
            name='daily_report'
        )
        
        # Еженедельный отчёт в воскресенье 19:00 по Риге
        job_queue.run_daily(
            weekly_report,
            time=dt_time(hour=19, minute=0),
            days=(6,),  # воскресенье = 6
            name='weekly_report'
        )
        
        print("🚀 Bot is running with monitoring enabled.")
        print("📊 Alert checks: every 10 minutes")
        print("🌅 Daily report: 11:00 Riga time")
        print("📆 Weekly report: Sunday 19:00 Riga time")
    else:
        print("🚀 Bot is running (monitoring disabled - CHAT_ID not set).")
    
    app.run_polling()

if __name__ == "__main__":
    main()
