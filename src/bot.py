import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# Разрешённый пользователь (только он может общаться с ботом)
ALLOWED_USER = 235538565  # <- твой chat_id

# Токен бота берём из переменной окружения BOT_TOKEN
TOKEN = os.getenv("BOT_TOKEN")

if TOKEN is None:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")


async def is_allowed(update: Update) -> bool:
    """
    Проверяем, что пишет именно владелец.
    Если чат не твой — бот молча игнорит.
    """
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_USER:
        # можно залогать попытку
        print(f"⛔ Unauthorized access attempt from chat_id={chat_id}")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Реакция на /start
    """
    if not await is_allowed(update):
        return

    await update.message.reply_text(
        "✅ Бот запущен и привязан к тебе.\n"
        "Напиши мне любое сообщение — верну твой chat_id.\n"
        "Скоро я смогу слать тебе обновления по портфелю 📈"
    )


async def echo_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Реакция на любое обычное сообщение (не команда):
    шлём пользователю его chat_id и логируем в консоль.
    """
    if not await is_allowed(update):
        return

    chat_id = update.effective_chat.id
    print(f"CHAT_ID: {chat_id}")  # это будет видно в логах Render

    await update.message.reply_text(
        f"Твой chat_id: {chat_id}\n"
        "Я тебя запомнил 🔒"
    )


def main():
    # Собираем приложение телеграм-бота
    app = ApplicationBuilder().token(TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", start))

    # Любой текст (кроме команд)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_chat_id))

    print("🚀 Bot is running. Only allowed user can interact.")
    app.run_polling()


if __name__ == "__main__":
    main()
