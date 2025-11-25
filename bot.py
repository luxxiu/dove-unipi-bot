import logging
import os
import json
import uuid
import requests
from telegram import (
    Update, 
    InlineQueryResultArticle, 
    InlineQueryResultPhoto, 
    InputTextMessageContent, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, 
    CommandHandler, 
    InlineQueryHandler, 
    CallbackQueryHandler, 
    ContextTypes
)

# Configurazione Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CONFIGURAZIONE ---
BUILDING_COLORS = {
    "Edificio A": "edafb8", 
    "Edificio B": "f7e1d7",
    "Edificio C": "dedbd2",
    "Edificio D": "b0c4b1",
    "Edificio E": "4a5759",
}
DEFAULT_COLOR = "808080"

# --- LINK FISSI ---
GITHUB_URL = "https://github.com/plumkewe/dove-unipi"
SITE_URL = "https://plumkewe.github.io/dove-unipi/"
MAP_URL = "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/mappa.png"

# --- CARICAMENTO DATI ---
def get_data():
    try:
        with open('data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Errore lettura data.json: {e}")
        return []

# --- HELPERS ---
def get_building_thumb(description):
    color = DEFAULT_COLOR
    text = "" 
    for edificio, hex_code in BUILDING_COLORS.items():
        if edificio.lower() in description.lower():
            color = hex_code
            text = edificio.split()[-1] 
            break
    return f"https://placehold.co/100/{color}/FFFFFF.png?text={text}"

def extract_url_from_markdown(markdown_text):
    try:
        if "](" in markdown_text:
            return markdown_text.split("](")[-1].strip(")")
        return ""
    except:
        return ""

# --- SELF PING ---
async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        try:
            requests.get(url, timeout=5)
        except:
            pass

# --- COMANDI STANDARD ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Benvenuto su *DOVE?UNIPI*\n\n"
        "Questo bot ti permette di cercare rapidamente le aule dell'Università di Pisa.\n\n"
        "*Come funziona:*\n"
        "Il bot è inline. Chiamami in qualsiasi chat digitando il mio username `@doveunipibot`.\n\n"
        "*Risorse:*\n"
        f"- [Sito Web]({SITE_URL})\n"
        f"- [GitHub]({GITHUB_URL})\n\n"
        "Premi il pulsante per provare."
    )
    
    keyboard = [
        [InlineKeyboardButton("Cerca un'aula qui", switch_inline_query_current_chat="")]
    ]
    
    await update.message.reply_text(
        text, 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True 
    )



# --- COMANDI AGGIUNTIVI ---
async def github_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"**Codice Sorgente**\n\nRepo GitHub:\n{GITHUB_URL}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def sito_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"**Sito Web**\n\nVisita il sito:\n{SITE_URL}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def mappa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_photo(
        photo=MAP_URL,
        parse_mode=ParseMode.MARKDOWN
    )

# --- INLINE QUERY ---
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.lower()
    
    results = []

    # 1. RISORSE SPECIALI
    special_items = [
        {
            "id": "special_map",
            "type": "photo",
            "title": "Mappa Edifici",
            "description": "Invia la mappa completa del Polo",
            "photo_url": MAP_URL,
            "thumb_url": MAP_URL,
            "keywords": ["mappa", "cartina", "foto", "image", "dove", "piantina"]
        },
        {
            "id": "special_github",
            "type": "article",
            "title": "GitHub Repository",
            "description": "Invia il link al codice sorgente",
            "url": GITHUB_URL,
            "thumb": "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png",
            "keywords": ["github", "code", "codice", "git", "repo"]
        },
        {
            "id": "special_site",
            "type": "article",
            "title": "Sito Web Ufficiale",
            "description": "Invia il link al sito web ufficiale",
            "url": SITE_URL,
            "thumb": "https://placehold.co/100/000000/FFFFFF.png?text=WWW",
            "keywords": ["sito", "web", "site", "link", "url"]
        }
    ]

    for special in special_items:
        if not query or any(k in query for k in special["keywords"]):
            
            if special["type"] == "photo":
                results.append(
                    InlineQueryResultPhoto(
                        id=special["id"],
                        photo_url=special["photo_url"],
                        thumbnail_url=special["thumb_url"],
                        title=special["title"],
                        description=special.get("description"),
                        parse_mode=ParseMode.MARKDOWN
                    )
                )
            else:
                results.append(
                    InlineQueryResultArticle(
                        id=special["id"],
                        title=special["title"],
                        description=special["description"],
                        input_message_content=InputTextMessageContent(
                            message_text=f"[{special['title']}]({special['url']})",
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=False
                        ),
                        thumbnail_url=special["thumb"],
                        thumbnail_width=100, 
                        thumbnail_height=100
                    )
                )

    # 2. RICERCA AULE
    items = get_data()
    for item in items:
        if item.get("type") == "article":
            title = item.get("title", "")
            description = item.get("description", "")
            keywords = item.get("keywords", [])

            found_keyword = False
            if isinstance(keywords, list):
                found_keyword = any(query in k.lower() for k in keywords)
            
            if not query or (query in title.lower()) or found_keyword:
                
                raw_input = item.get("input_message_content", {})
                raw_text = raw_input.get("message_text", "")
                parse_mode = raw_input.get("parse_mode", "Markdown")
                url = extract_url_from_markdown(raw_text)
                
                if url:
                    clean_desc = description.split("\n")[0].strip()
                    final_text = f"[{clean_desc} › {title}]({url})"
                else:
                    final_text = raw_text

                thumb = get_building_thumb(description)

                results.append(
                    InlineQueryResultArticle(
                        id=item.get("id", str(uuid.uuid4())),
                        title=title,
                        description=description,
                        input_message_content=InputTextMessageContent(
                            message_text=final_text,
                            parse_mode=parse_mode,
                            disable_web_page_preview=True
                        ),
                        thumbnail_url=thumb,
                        thumbnail_width=100,
                        thumbnail_height=100
                    )
                )

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
    app.add_handler(CommandHandler("github", github_command))
    app.add_handler(CommandHandler("sito", sito_command))
    app.add_handler(CommandHandler("mappa", mappa_command))
    
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