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
    "Edificio A": "00FF5E", 
    "Edificio B": "FF0091",
    "Edificio C": "FF0000",
    "Edificio D": "00FF12",
    "Edificio E": "FFC700",
    "Edificio X": "0000FF"
}
DEFAULT_COLOR = "808080"

# --- LINK FISSI ---
GITHUB_URL = "https://github.com/plumkewe/dove-unipi"
SITE_URL = "https://plumkewe.github.io/dove-unipi/"
GITHUB_ICON_URL = "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/github.png"
GLOBE_ICON_URL = "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/globe.png"
MAP_ICON_URL = "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/map.png"
MAP_URL = "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/mappa.png"
INSTAGRAM_URL = "https://www.instagram.com/doveunipi"
INSTAGRAM_ICON_URL = "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/instagram.png"

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
    return f"https://placehold.co/100/{color}/000000.png?text={text}"

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
        "Trova aule e uffici dei professori dell'Università di Pisa in un lampo! \n\n"
        "*Come usarmi:*\n"
        "Sono un bot **inline**. In *qualsiasi* chat (anche nei gruppi!), digita:\n"
        "`@doveunipibot nome aula`\n"
        "...e clicca sul risultato per inviare la posizione.\n\n"
        "*Risorse:*\n"
        f"- [Sito Web]({SITE_URL})\n"
        f"- [Instagram]({INSTAGRAM_URL})\n"
        f"- [GitHub]({GITHUB_URL})\n\n"
        "*Progetti simili:*\n"
        "- [Bot Ingegneria in movimento](https://t.me/inginmovbot)\n\n"
        "*Prova subito col pulsante qui sotto!*"
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
    await update.message.reply_text(GITHUB_URL)

async def sito_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(SITE_URL)

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
            "title": "Mappa Polo",
            "description": "Invia la mappa completa del Polo",
            "photo_url": MAP_URL,
            "thumb_url": MAP_ICON_URL,
            "keywords": ["mappa", "cartina", "foto", "image", "dove", "piantina"]
        },
        {
            "id": "special_github",
            "type": "article",
            "title": "GitHub Repository",
            "description": "Invia il link al codice sorgente",
            "url": GITHUB_URL,
            "thumb": GITHUB_ICON_URL,
            "keywords": ["github", "code", "codice", "git", "repo"]
        },
        {
            "id": "special_site",
            "type": "article",
            "title": "Sito Web Ufficiale",
            "description": "Invia il link al sito web ufficiale",
            "url": SITE_URL,
            "thumb": GLOBE_ICON_URL,
            "keywords": ["sito", "web", "site", "link", "url"]
        },
        {
            "id": "special_instagram",
            "type": "article",
            "title": "Instagram",
            "description": "Seguici su Instagram",
            "url": INSTAGRAM_URL,
            "thumb": INSTAGRAM_ICON_URL,
            "keywords": ["instagram", "social", "ig", "foto", "doveunipi"]
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
                            message_text=special['url'],
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

    # 3. ORDINAMENTO RISULTATI
    # Priorità: aule prima dei professori, poi risultati che iniziano con la query
    if len(results) > 0 and query:
        def sort_key(result):
            # Estrai title, description e keywords
            result_title = getattr(result, 'title', '').lower()
            result_description = getattr(result, 'description', '').lower()
            
            # Per le aule, cerchiamo nelle keywords (che fungono da alias)
            keywords = []
            # Trova l'item originale per accedere alle keywords
            for item in items:
                if item.get("id") == result.id and item.get("type") == "article":
                    keywords = [k.lower() for k in item.get("keywords", [])]
                    break
            
            # Determina se è un professore (ha "stanza" nella description)
            is_professor = "stanza" in result_description
            
            # Controlla se title o keywords iniziano con la query
            title_starts = result_title.startswith(query)
            keywords_start = any(k.startswith(query) for k in keywords)
            starts_with = title_starts or keywords_start
            
            # Ritorna tupla: (priorità professor, priorità inizio query, nome alfabetico)
            # False viene prima di True in Python, quindi:
            # - is_professor = True per professori (vengono dopo)
            # - not starts_with = False per match che iniziano con query (vengono prima)
            return (is_professor, not starts_with, result_title)
        
        results.sort(key=sort_key)

    await update.inline_query.answer(results[:10], cache_time=0)

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