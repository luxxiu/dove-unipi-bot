import logging
import os
import json
import uuid
import requests
from telegram import (
    Update, 
    InlineQueryResultArticle, 
    InputTextMessageContent,
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, InlineQueryHandler, ContextTypes

# Configurazione Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CARICAMENTO DATI ---
def get_data():
    try:
        with open('data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Errore lettura data.json: {e}")
        return []

# --- SELF PING (ANTI-SLEEP) ---
async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        try:
            requests.get(url, timeout=10)
            logger.info(f"⏰ Ping inviato a {url}")
        except Exception as e:
            logger.error(f"⚠️ Errore ping: {e}")

# --- START ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Benvenuto su *DOVE?UNIPI*\n\n"
        "Questo bot ti permette di cercare rapidamente le aule dell'Università di Pisa.\n\n"
        "*Come funziona:*\n"
        "Il bot è inline. Chiamami in qualsiasi chat digitando il mio username @doveunipibot.\n\n"
        "*Risorse:*\n"
        "- [Sito Web](https://plumkewe.github.io/dove-unipi/)\n"
        "- [GitHub](https://github.com/plumkewe/dove-unipi)\n\n"
        "Premi il pulsante per provare."
    )
    keyboard = [[InlineKeyboardButton("Cerca un'aula", switch_inline_query="")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text, 
        reply_markup=reply_markup, 
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True 
    )

# --- INLINE QUERY ---
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.lower()
    items = get_data()
    results = []

    for item in items:
        if item.get("type") == "article":
            title = item.get("title", "")
            description = item.get("description", "")

            # Cerca solo nel titolo
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
                        ),
                        thumbnail_url=item.get("thumb_url")
                    )
                )

    # --- FIX IMPORTANTE ---
    # Limitiamo i risultati a 50 per evitare l'errore "Too many inline query results"
    # Se ci sono più di 50 aule, mostra le prime 50 trovate.
    await update.inline_query.answer(results[:50], cache_time=0)

# --- MAIN ---
def main():
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    PORT = int(os.environ.get("PORT", "8443"))
    WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

    if not TOKEN:
        logger.error("ERRORE: Token mancante.")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(InlineQueryHandler(inline_query))

    if WEBHOOK_URL:
        if app.job_queue:
            app.job_queue.run_repeating(self_ping, interval=840, first=60)
        
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