import logging
import os
import json
import uuid
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, InlineQueryHandler, ContextTypes

# Configurazione Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Funzione per leggere i dati
def get_data():
    try:
        with open('data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Errore lettura data.json: {e}")
        return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo.")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.lower()
    items = get_data()
    results = []

    for item in items:
        if item.get("type") == "article":
            title = item.get("title", "")
            # Descrizione serve solo per l'estetica del risultato, non per la ricerca
            description = item.get("description", "")

            # MODIFICA QUI: Cerca SOLO nel titolo
            # Se query è vuota mostra tutto, altrimenti controlla solo il titolo
            if not query or (query in title.lower()):
                
                input_content = item.get("input_message_content", {})
                message_text = input_content.get("message_text", "Errore")
                parse_mode = input_content.get("parse_mode", "Markdown")

                results.append(
                    InlineQueryResultArticle(
                        id=item.get("id", str(uuid.uuid4())),
                        title=title,
                        description=description,
                        input_message_content=InputTextMessageContent(
                            message_text=message_text,
                            parse_mode=parse_mode,
                            disable_web_page_preview=True
                        )
                    )
                )

    await update.inline_query.answer(results, cache_time=0)

def main():
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    PORT = int(os.environ.get("PORT", "8443"))
    WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

    if not TOKEN:
        logger.error("ERRORE: TELEGRAM_TOKEN mancante.")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(InlineQueryHandler(inline_query))

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()