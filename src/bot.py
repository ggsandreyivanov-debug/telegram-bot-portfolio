import os
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# Разрешённый пользователь (только он может общаться с ботом)
ALLOWED_USER = 235538565  # твой chat_id

# Токен бота из переменной окружения Render
TOKEN = os.getenv("BOT_TOKEN")

if TOKEN is None:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")


async def is_allowed(update: Update) -> bool:
    """
    Проверяем, что пишет именно владелец.
    Если чат не твой — бот молча игнорит (и пишет попытку в лог).
    """
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_USER:
        print(f"⛔ Unauthorized access attempt from chat_id={chat_id}")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - приветствие
    """
    if not await is_allowed(update):
        return

    await update.message.reply_text(
        "✅ Бот запущен и привязан к тебе.\n"
        "Команды:\n"
        "• /status — сводка по портфелю и рискам\n"
        "• просто напиши текст — пришлю твой chat_id\n"
    )


async def echo_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Любое обычное сообщение (не команда):
    шлём твой chat_id.
    """
    if not await is_allowed(update):
        return

    chat_id = update.effective_chat.id
    print(f"CHAT_ID: {chat_id}")  # видно в логах Render

    await update.message.reply_text(
        f"Твой chat_id: {chat_id}\n"
        "Я отвечаю только тебе 🔒"
    )


def build_risk_comment() -> str:
    """
    Здесь мы можем жёстко прописать важные события,
    которые могут шатать рынок.
    Версия 1: статический календарь.
    Потом можно будет обновлять из сети.
    """

    # пример будущих триггеров
    upcoming_events = [
        "📅 ФРС (ставка США) — риск волатильности для S&P500 и VWCE",
        "📅 CPI США (инфляция) — может ударить по акциям, золото обычно защищает",
        "📅 Политика США/Китай — влияет на чипы/техи",
    ]

    lines = "Рыночные риски ближайшего плана:\n"
    for e in upcoming_events:
        lines += f"• {e}\n"

    return lines


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status — быстрая сводка, что у нас вообще за портфель и какие риски.
    Тут пока нет live-котировок (версия 1), только структура и календарь.
    """
    if not await is_allowed(update):
        return

    chat_id = update.effective_chat.id

    # Время генерации статуса
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Твоя базовая корзина. Мы можем редактировать это, если ты изменишь портфель.
    portfolio_lines = [
        "📦 Портфель (идея):",
        "• S&P 500 (через индексный ETF, типа VANG / SPDR / iShares на США)",
        "• VWCE (весь мир, диверсификация вне США)",
        "• Gold / золото (защита от шторма)",
        "",
        "Логика: акции = рост, золото = защита.",
    ]

    msg = (
        f"⏰ {now_utc}\n"
        f"👤 chat_id: {chat_id}\n\n"
        + "\n".join(portfolio_lines)
        + "\n\n"
        + build_risk_comment()
        + "\n\n"
        "Команды:\n"
        "• /status — это сообщение\n"
        "• /start — помощь\n"
        "• Напиши что-то — я просто повторю твой chat_id\n"
        "\n"
        "Дальше можем прикрутить котировки в реальном времени и авто-алерты 📈"
    )

    await update.message.reply_text(msg)


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))

    # Любой текст без команды -> echo_chat_id
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_chat_id))

    print("🚀 Bot is running. Only allowed user can interact.")
    app.run_polling()


if __name__ == "__main__":
    main()
