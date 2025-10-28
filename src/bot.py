import os
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

# -------- команды --------

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
    # Пока заглушка. Чуть позже сюда внедрим реальные котировки.
    text = (
        "💹 Цены прямо сейчас:\n"
        "• S&P 500: не смог получить\n"
        "• Gold: не смог получить\n"
        "• VWCE (глобалка): не смог получить\n"
    )
    await update.message.reply_text(text)

# Это чтобы ты мог написать что угодно, а бот ответил чем-то базовым —
# чтобы проверить, что он вообще получает апдейты.
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я тебя слышу 👂")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pingprices", pingprices))

    # fallback echo
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    print("🚀 Bot is running. Send /start or /status in Telegram.")
    app.run_polling()

if __name__ == "__main__":
    main()
