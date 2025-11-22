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
    "Edificio A": "FF5733", 
    "Edificio B": "33FF57",
    "Edificio C": "3357FF",
    "Edificio D": "F1C40F",
    "Edificio E": "9B59B6",
    "Edificio F": "E91E63",
    "Edificio L": "1ABC9C",
    "Edificio M": "E91E63",
}
DEFAULT_COLOR = "808080"
USER_PREFS = {}

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
        "🔎 **Cerca:** Digita il mio username per cercare aule.\n"
        "🗺️ **Mappa:** Usa /mappa per vedere la cartina.\n"
        "⚙️ **Impostazioni:** Usa /settings per il formato.\n"
        "🌐 **Info:** Usa /sito o /github.\n\n"
        "👇 Prova ora:"
    )
    keyboard = [[InlineKeyboardButton("Cerca un'aula", switch_inline_query="")]]
    await update.message.reply_text(
        text, 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.MARKDOWN
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_pref = USER_PREFS.get(user_id, "short")
    status_text = "Breve (Solo Nome)" if current_pref == "short" else "Completo (Percorso + Nome)"
    
    text = (
        f"⚙️ **Impostazioni Formato Messaggio**\n\n"
        f"Attualmente invii: *{status_text}*\n\n"
        "Come vuoi inviare il link dell'aula?"
    )
    keyboard = [
        [
            InlineKeyboardButton("Solo Nome (Breve)", callback_data="set_format_short"),
            InlineKeyboardButton("Percorso Completo", callback_data="set_format_full")
        ]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "set_format_short":
        USER_PREFS[user_id] = "short"
        new_status = "Breve (Solo Nome)"
    elif data == "set_format_full":
        USER_PREFS[user_id] = "full"
        new_status = "Completo (Percorso + Nome)"
    
    await query.edit_message_text(
        text=f"✅ **Impostazione salvata!**\n\nOra il formato è: *{new_status}*",
        parse_mode=ParseMode.MARKDOWN
    )

# --- COMANDI AGGIUNTIVI ---
async def github_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"🐙 **Codice Sorgente**\n\nRepo GitHub:\n{GITHUB_URL}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def sito_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"🌐 **Sito Web**\n\nVisita il sito:\n{SITE_URL}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def mappa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_photo(
        photo=MAP_URL,
        caption="🗺️ **Mappa Aule - Polo Fibonacci**",
        parse_mode=ParseMode.MARKDOWN
    )

# --- INLINE QUERY ---
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.lower()
    user_id = update.inline_query.from_user.id
    user_format = USER_PREFS.get(user_id, "short")
    
    results = []

    # 1. RISORSE SPECIALI (GitHub, Sito e MAPPA)
    special_items = [
        {
            "id": "special_map",
            "type": "photo",
            "title": "Mappa Aule",
            # Descrizione breve per la mappa
            "description": "Visualizza la planimetria completa del Polo", 
            "photo_url": MAP_URL,
            "thumb_url": MAP_URL,
            "keywords": ["mappa", "cartina", "foto", "image", "dove", "piantina"]
        },
        {
            "id": "special_github",
            "type": "article",
            "title": "GitHub Repository",
            # Descrizione richiesta con link esplicito
            "description": f"Copia {GITHUB_URL} negli appunti.",
            "url": GITHUB_URL,
            "thumb": "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png",
            "keywords": ["github", "code", "codice", "git", "repo"]
        },
        {
            "id": "special_site",
            "type": "article",
            "title": "Sito Web Ufficiale",
            # Descrizione richiesta con link esplicito
            "description": f"Copia {SITE_URL} negli appunti.",
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
                        description=special.get("description"), # Aggiunta descrizione anche alla foto
                        caption="🗺️ **Mappa Aule - Polo Fibonacci**",
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

            if not query or (query in title.lower()):
                
                raw_input = item.get("input_message_content", {})
                raw_text = raw_input.get("message_text", "")
                parse_mode = raw_input.get("parse_mode", "Markdown")
                url = extract_url_from_markdown(raw_text)
                
                if url:
                    if user_format == "full":
                        clean_desc = description.split("\n")[0].strip()
                        final_text = f"[{clean_desc} › {title}]({url})"
                    else:
                        final_text = f"[{title}]({url})"
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
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("github", github_command))
    app.add_handler(CommandHandler("sito", sito_command))
    app.add_handler(CommandHandler("mappa", mappa_command))
    
    app.add_handler(CallbackQueryHandler(button_handler))
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