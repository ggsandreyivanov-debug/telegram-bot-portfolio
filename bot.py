import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, CommandHandler, filters

TOKEN = os.getenv("8298932729:AAF5hv6ZC_No6jYszDwUn7g5LqxPtndNdQ0")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот запущен! Напиши мне что-нибудь — я покажу твой chat_id.")

async def echo_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    print("CHAT_ID:", chat_id)
    await update.message.reply_text(f"Твой chat_id: {chat_id}")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_chat_id))
    print("🚀 Bot is running. Send /start to your bot in Telegram.")
    app.run_polling()

if __name__ == "__main__":
    main()
