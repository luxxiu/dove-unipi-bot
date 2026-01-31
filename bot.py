import logging
import os
import json
import uuid
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import pytz

# --- FIX APSCHEDULER TIMEZONE ---
def patch_apscheduler():
    try:
        import apscheduler.util
        from datetime import timezone
        import pytz as pz
        
        orig_astimezone = apscheduler.util.astimezone
        def patched_astimezone(obj):
            try:
                return orig_astimezone(obj)
            except TypeError:
                if obj is None:
                    return pz.UTC
                return orig_astimezone(pz.timezone(str(obj)))
        apscheduler.util.astimezone = patched_astimezone
    except Exception:
        pass

patch_apscheduler()

from telegram import (
    Update, 
    InlineQueryResultArticle, 
    InlineQueryResultPhoto, 
    InputTextMessageContent, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    InlineQueryResultsButton
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
    "Edificio A": "da21ac", 
    "Edificio B": "8e21da",
    "Edificio C": "21dad4",
    "Edificio D": "da5321",
    "Edificio E": "2160da",
    "Edificio X": "aea4b2"
}
DEFAULT_COLOR = "808080"
TZ_ROME = pytz.timezone('Europe/Rome')

# Costanti per /status
POLO_FIBONACCI_CALENDAR_ID = "63223a029f080a0aab032afc"
AULE_PER_PAGE = 5

# API per calendario
API_URL = 'https://apache.prod.up.cineca.it/api/Impegni/getImpegniCalendarioPubblico'
CLIENT_ID = '628de8b9b63679f193b87046'

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

def load_aule_json() -> dict:
    """Carica il file aule.json."""
    try:
        with open('aule.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Errore lettura aule.json: {e}")
        return {}

# --- HELPERS ---
def find_dove_item(items: List[Dict], aula_nome: str) -> Optional[Dict]:
    """Trova l'item corrispondente in data.json per l'aula specificata."""
    aula_nome_lower = aula_nome.lower()
    for item in items:
        if item.get("type") == "article":
            item_title = item.get("title", "").lower()
            item_keywords = [k.lower() for k in item.get("keywords", [])]
            if aula_nome_lower == item_title or any(aula_nome_lower in k for k in item_keywords):
                return item
    return None

def get_building_thumb(description):
    color = DEFAULT_COLOR
    text = "" 
    for edificio, hex_code in BUILDING_COLORS.items():
        if edificio.lower() in description.lower():
            color = hex_code
            text = edificio.split()[-1] 
            break
    return f"https://placehold.co/100/{color}/ffffff.png?text={text}"

def extract_url_from_markdown(markdown_text):
    try:
        if "](" in markdown_text:
            return markdown_text.split("](")[-1].strip(")")
        return ""
    except:
        return ""

def format_docenti_with_links(docenti_str: str) -> dict:
    """
    Converte i nomi dei professori.
    Ritorna un dizionario con:
    - 'full_names': lista di nomi completi (nome cognome)
    - 'links': lista di link con solo cognome in caps
    """
    if not docenti_str:
        return {'full_names': [], 'links': []}
    
    items = get_data()
    
    # Crea un dizionario nome -> url per i professori (description contiene "Stanza")
    prof_urls = {}
    for item in items:
        if item.get("type") == "article":
            description = item.get("description", "").lower()
            # I professori hanno "stanza" nella description
            if "stanza" in description:
                title = item.get("title", "")
                raw_input = item.get("input_message_content", {})
                raw_text = raw_input.get("message_text", "")
                url = extract_url_from_markdown(raw_text)
                if title and url:
                    # Salva in lowercase per matching insensibile al case
                    prof_urls[title.lower()] = (title, url)
    
    # Separa i docenti (possono essere separati da virgola)
    docenti_list = [d.strip() for d in docenti_str.split(",")]
    
    full_names = []
    links = []
    
    for docente in docenti_list:
        docente_lower = docente.lower()
        # Formatta nome completo: Prima lettera maiuscola per ogni parola
        parts = docente.split()
        if parts:
            # Cognome Nome -> Nome Cognome con capitalizzazione corretta
            formatted_name = ' '.join(p.capitalize() for p in parts)
            full_names.append(formatted_name)
        
        # Estrai il cognome (prima parola)
        cognome = parts[0] if parts else docente
        
        # Cerca match per link
        if docente_lower in prof_urls:
            original_name, url = prof_urls[docente_lower]
            cognome_display = original_name.split()[0].upper()
            links.append(f"[{cognome_display}↗]({url})")
        else:
            # Match più robusto: token subset
            found = False
            docente_tokens = set(docente_lower.split())
            
            if docente_tokens:
                for prof_name_lower, (original_name, url) in prof_urls.items():
                    prof_tokens = set(prof_name_lower.split())
                    if docente_tokens.issubset(prof_tokens):
                        cognome_display = original_name.split()[0].upper()
                        links.append(f"[{cognome_display}↗]({url})")
                        found = True
                        break
            
            # Se non trovato, nessun link per questo docente
    
    return {'full_names': full_names, 'links': links}

# --- API CALENDARIO ---
def fetch_day_events(calendar_id: str, day: datetime) -> List[Dict[str, Any]]:
    """Recupera tutti gli eventi per un giorno specifico."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    headers = {
        'content-type': 'application/json;charset=UTF-8',
    }
    payload = {
        'mostraImpegniAnnullati': True,
        'mostraIndisponibilitaTotali': False,
        'linkCalendarioId': calendar_id,
        'clienteId': CLIENT_ID,
        'pianificazioneTemplate': False,
        'dataInizio': start.isoformat(),
        'dataFine': end.isoformat(),
    }
    
    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Errore fetch eventi: {e}")
        return []

def get_aula_status(aula_nome: str, events: List[Dict], now: datetime) -> Dict:
    """
    Calcola lo stato di un'aula.
    Returns: {
        'is_free': bool,
        'free_until': datetime or None,
        'busy_until': datetime or None,
        'next_events': List[Dict]
    }
    """
    # Normalizza il nome per matching con API
    aula_api_variants = []
    
    # Genera varianti del nome per strict matching
    # Es: "Aula A" -> "Fib A"
    # Es: "Laboratorio 1" -> "Fib Lab 1"
    
    strict_variants = set()
    
    if aula_nome.startswith("Aula "):
        base = aula_nome[5:]  # "A"
        strict_variants.add(f"FIB {base}")
        strict_variants.add(f"Fib {base}")
        strict_variants.add(base)  # Solo se il codice è esattamente "A", molto raro ma possibile
    elif aula_nome.startswith("Laboratorio "):
        num = aula_nome[12:]
        strict_variants.add(f"FIB LAB {num}")
        strict_variants.add(f"Fib Lab {num}")
        strict_variants.add(f"FIS LAB {num}") # Aggiungo variante FIS vista nel debug
    else:
        strict_variants.add(f"FIB {aula_nome}")
        strict_variants.add(f"Fib {aula_nome}")
        strict_variants.add(aula_nome)
    
    # Aggiungi varianti upper per sicurezza
    strict_variants_upper = {v.upper() for v in strict_variants}
    
    # Filtra eventi per questa aula
    aula_events = []
    for event in events:
        event_aule = event.get('aule', [])
        for aula in event_aule:
            codice = aula.get('codice', '').strip()
            descrizione = aula.get('descrizione', '').strip()
            
            # STRICT MATCHING: Uguaglianza esatta invece di 'in'
            # Controlla se il codice o la descrizione (upper) sono nel set delle varianti attese
            match_found = (codice.upper() in strict_variants_upper) or (descrizione.upper() in strict_variants_upper)
            
            if match_found:
                try:
                    # Parsing date as offset-aware (from ISO string with Z)
                    start = datetime.fromisoformat(event['dataInizio'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(event['dataFine'].replace('Z', '+00:00'))
                    
                    # Convert to Rome time
                    start = start.astimezone(TZ_ROME)
                    end = end.astimezone(TZ_ROME)
                    
                    # Estrai docenti (nome completo)
                    docenti_list = event.get('docenti', [])
                    # Keep original names as they appear
                    docenti_nomi = []
                    for d in docenti_list:
                        if d:
                            # Try to get the complete name or construct it
                            nome_compl = d.get('cognomeNome')
                            if not nome_compl:
                                n = d.get('nome', '').strip()
                                c = d.get('cognome', '').strip()
                                nome_compl = f"{n} {c}".strip()
                            
                            if nome_compl:
                                docenti_nomi.append(nome_compl)
                    docenti_str = ', '.join(docenti_nomi) if docenti_nomi else ''
                    
                    # Verifica che l'evento sia del giorno corrente
                    if start.date() == now.date():
                        aula_events.append({
                            'nome': event.get('nome', 'N/D').split('-')[0].strip(),
                            'start': start,
                            'end': end,
                            'docenti': docenti_str,
                        })
                except Exception as e:
                    logger.error(f"Errore parsing evento: {e}")
                break
    
    # Ordina per orario
    aula_events.sort(key=lambda x: x['start'])
    
    # Determina stato attuale
    is_free = True
    free_until = None
    busy_until = None
    current_event = None
    
    for event in aula_events:
        # Ensure now is comparable (aware vs aware)
        if now.tzinfo is None:
             now = now.astimezone(TZ_ROME)
             
        if event['start'] <= now <= event['end']:
            is_free = False
            busy_until = event['end']
            current_event = event
            break
        elif event['start'] > now:
            free_until = event['start']
            break
    
    # Prossimi eventi (dopo ora)
    next_events = [e for e in aula_events if e['start'] > now]
    
    return {
        'is_free': is_free,
        'free_until': free_until,
        'busy_until': busy_until,
        'current_event': current_event,
        'next_events': next_events[:5]  # Max 5 prossimi
    }

# --- FUNZIONI AULE ---
def get_edifici(polo: str) -> List[str]:
    """Restituisce lista degli edifici per un polo."""
    data = load_aule_json()
    try:
        edifici = data['polo'][polo]['edificio'].keys()
        return sorted(list(edifici))
    except:
        return []

def get_piani(polo: str, edificio: str) -> List[str]:
    """Restituisce lista dei piani per un edificio."""
    data = load_aule_json()
    try:
        piani = data['polo'][polo]['edificio'][edificio]['piano'].keys()
        return sorted(list(piani))
    except:
        return []

def get_aule_edificio(polo: str, edificio: str) -> List[Dict]:
    """Restituisce tutte le aule di un edificio (tutti i piani)."""
    data = load_aule_json()
    aule = []
    try:
        piani = data['polo'][polo]['edificio'][edificio]['piano']
        for piano, aule_piano in piani.items():
            for aula in aule_piano:
                aula_copy = aula.copy()
                aula_copy['piano'] = piano
                aula_copy['edificio'] = edificio
                aule.append(aula_copy)
    except:
        pass
    return aule

def get_aule_polo(polo: str) -> List[Dict]:
    """Restituisce tutte le aule di un polo."""
    data = load_aule_json()
    aule = []
    try:
        polo_data = data['polo'][polo]
        polo_nome = polo_data.get('nome', polo.capitalize())
        edifici = polo_data['edificio']
        for edificio, edificio_data in edifici.items():
            for piano, aule_piano in edificio_data['piano'].items():
                for aula in aule_piano:
                    aula_copy = aula.copy()
                    aula_copy['piano'] = piano
                    aula_copy['edificio'] = edificio
                    aula_copy['polo_nome'] = polo_nome
                    aule.append(aula_copy)
    except:
        pass
    return aule

# --- FORMATTAZIONE MESSAGGI ---
def format_aula_header(aula: Dict) -> str:
    """Formatta l'intestazione standard dell'aula (Nome, Edificio, Piano, Capienza)."""
    nome = aula.get('nome', 'N/D')
    edificio = aula.get('edificio', '?').upper()
    piano = aula.get('piano', '?')
    capienza = aula.get('capienza', 'N/D')
    
    display_piano = "terra" if str(piano) == "0" else str(piano)
    
    # Rimuovi prefisso "Aula " se già presente per evitare duplicati
    display_nome = nome.upper()
    if display_nome.startswith("AULA "):
        display_nome = display_nome[5:]  # Rimuovi "AULA "
    
    msg = f"*AULA {display_nome}*\n"
    msg += f"Edificio {edificio} › Piano {display_piano}\n"
    if capienza and capienza != 'N/D':
        msg += f"Capienza: {capienza} posti\n"
    
    return msg

def format_single_aula_status(aula: Dict, status: Dict, now: datetime, dove_url: str = None) -> str:
    """Formatta il messaggio di stato per una singola aula."""
    msg = format_aula_header(aula) + "\n"
    
    # Raccogli tutti i link dei docenti per metterli alla fine
    all_docenti_links = []
    
    if status['is_free']:
        if status['free_until']:
            msg += f"LIBERA fino alle {status['free_until'].strftime('%H:%M')}\n"
        else:
            msg += "LIBERA per il resto della giornata\n"
    else:
        msg += f"OCCUPATA fino alle {status['busy_until'].strftime('%H:%M')}\n"
        if status['current_event']:
            event = status['current_event']
            time_str = f"{event['start'].strftime('%H:%M')}-{event['end'].strftime('%H:%M')}"
            docenti_info = format_docenti_with_links(event.get('docenti', ''))
            docenti_names = '\n'.join(docenti_info['full_names'])
            if docenti_names:
                msg += f"```\n{time_str} {event['nome']}\n{docenti_names}\n```\n"
            else:
                msg += f"```\n{time_str} {event['nome']}\n```\n"
            all_docenti_links.extend(docenti_info['links'])
    
    # Prossime occupazioni
    if status['next_events']:
        msg += "\nProssime occupazioni:\n"
        code_block_content = ""
        for i, event in enumerate(status['next_events']):
            time_str = f"{event['start'].strftime('%H:%M')}-{event['end'].strftime('%H:%M')}"
            docenti_info = format_docenti_with_links(event.get('docenti', ''))
            docenti_names = '\n'.join(docenti_info['full_names'])
            
            # Aggiungi separatore se non è il primo elemento
            if i > 0:
                code_block_content += "-----------\n"
            
            code_block_content += f"{time_str} {event['nome']}\n"
            if docenti_names:
                code_block_content += f"{docenti_names}\n"
            
            all_docenti_links.extend(docenti_info['links'])
        
        msg += f"```\n{code_block_content}```\n"
    
    # Aggiungi link alla fine (DOVE?UNIPI + tutti i docenti)
    footer_links = []
    if dove_url:
        footer_links.append(f"[DOVE?UNIPI↗]({dove_url})")
    # Rimuovi duplicati dai link docenti mantenendo l'ordine
    seen = set()
    for link in all_docenti_links:
        if link not in seen:
            seen.add(link)
            footer_links.append(link)
    
    if footer_links:
        msg += "\n" + "  ".join(footer_links)
    
    
    
    return msg

def format_edificio_status(polo: str, edificio: str, events: List[Dict], now: datetime) -> str:
    """Formatta lo stato di tutte le aule di un edificio."""
    aule = get_aule_edificio(polo, edificio)
    
    msg = f"*Edificio {edificio.upper()} - Polo Fibonacci*\n"
    msg += f"Stato aule alle {now.strftime('%H:%M')}\n\n"
    
    # Raggruppa per piano
    aule_per_piano = {}
    for aula in aule:
        piano = aula.get('piano', '0')
        if piano not in aule_per_piano:
            aule_per_piano[piano] = []
        aule_per_piano[piano].append(aula)
    
    for piano in sorted(aule_per_piano.keys()):
        msg += f"*Piano {piano}:*\n"
        for aula in aule_per_piano[piano]:
            status = get_aula_status(aula['nome'], events, now)
            symbol = "✓" if status['is_free'] else "✗"
            nome_breve = aula['nome']
            
            if status['is_free']:
                if status['free_until']:
                    msg += f"{symbol} {nome_breve} - libera fino {status['free_until'].strftime('%H:%M')}\n"
                else:
                    msg += f"{symbol} {nome_breve} - libera\n"
            else:
                msg += f"{symbol} {nome_breve} - occupata fino {status['busy_until'].strftime('%H:%M')}\n"
        msg += "\n"
    
    return msg

def format_piano_status(polo: str, edificio: str, piano: str, events: List[Dict], now: datetime) -> str:
    """Formatta lo stato di tutte le aule di un piano."""
    aule = get_aule_edificio(polo, edificio)
    aule = [a for a in aule if a.get('piano') == piano]
    
    msg = f"*Edificio {edificio.upper()} - Piano {piano}*\n"
    msg += f"Stato alle {now.strftime('%H:%M')}\n\n"
    
    for aula in aule:
        status = get_aula_status(aula['nome'], events, now)
        symbol = "✓" if status['is_free'] else "✗"
        nome_breve = aula['nome']
        
        if status['is_free']:
            if status['free_until']:
                msg += f"{symbol} {nome_breve} - libera fino {status['free_until'].strftime('%H:%M')}\n"
            else:
                msg += f"{symbol} {nome_breve} - libera\n"
        else:
            msg += f"{symbol} {nome_breve} - occupata fino {status['busy_until'].strftime('%H:%M')}\n"
    
    return msg

def format_polo_status(polo: str, events: List[Dict], now: datetime) -> str:
    """Formatta lo stato di tutte le aule di un polo."""
    msg = f"*Polo Fibonacci*\n"
    msg += f"Stato aule alle {now.strftime('%H:%M')}\n\n"
    
    edifici = get_edifici(polo)
    for edificio in edifici:
        msg += f"━━━ *Edificio {edificio.upper()}* ━━━\n"
        aule = get_aule_edificio(polo, edificio)
        
        # Raggruppa per piano
        aule_per_piano = {}
        for aula in aule:
            piano = aula.get('piano', '0')
            if piano not in aule_per_piano:
                aule_per_piano[piano] = []
            aule_per_piano[piano].append(aula)
        
        for piano in sorted(aule_per_piano.keys()):
            msg += f"*Piano {piano}:*\n"
            for aula in aule_per_piano[piano]:
                status = get_aula_status(aula['nome'], events, now)
                symbol = "✓" if status['is_free'] else "✗"
                nome_breve = aula['nome']
                
                if status['is_free']:
                    if status['free_until']:
                        msg += f"{symbol} {nome_breve} - fino {status['free_until'].strftime('%H:%M')}\n"
                    else:
                        msg += f"{symbol} {nome_breve}\n"
                else:
                    msg += f"{symbol} {nome_breve} - fino {status['busy_until'].strftime('%H:%M')}\n"
            msg += "\n"
    
    return msg

def format_day_schedule(aula: Dict, events: List[Dict], target_date: datetime) -> str:
    """Formatta il programma di una giornata specifica."""
    # Formato per giorni futuri/passati: Header + Programma
    text = format_aula_header(aula) + "\n"
    # Formato per giorni futuri/passati: Header + Programma
    GIORNI = ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"]
    day_caps = GIORNI[target_date.weekday()]
    text += f"PROGRAMMA {day_caps} {target_date.strftime('%d/%m')}\n\n"
    
    # Recupera eventi del giorno
    start_of_day = target_date.replace(hour=0, minute=0, second=1)
    status_day = get_aula_status(aula['nome'], events, start_of_day)
    
    # Raccogli tutti i link dei docenti
    all_docenti_links = []
    
    if not status_day['next_events'] and not status_day['current_event']:
            text += "Nessuna occupazione prevista.\n"
    else:
            all_events = status_day['next_events']
            if status_day['current_event']:
                all_events.insert(0, status_day['current_event'])
            
            code_block_content = ""
            for i, event in enumerate(all_events):
                time_str = f"{event['start'].strftime('%H:%M')}-{event['end'].strftime('%H:%M')}"
                docenti_info = format_docenti_with_links(event.get('docenti', ''))
                docenti_names = '\n'.join(docenti_info['full_names'])
                
                # Divisore
                if i > 0:
                    code_block_content += "-----------\n"
                
                code_block_content += f"{time_str} {event['nome']}\n"
                if docenti_names:
                    code_block_content += f"{docenti_names}\n"
                
                all_docenti_links.extend(docenti_info['links'])
            
            text += f"```\n{code_block_content}```\n"
    
    # Aggiungi link alla fine
    items = get_data()
    dove_url = None
    item = find_dove_item(items, aula['nome'])
    if item:
        raw_input = item.get("input_message_content", {})
        dove_url = extract_url_from_markdown(raw_input.get("message_text", ""))
    
    footer_links = []
    if dove_url:
        footer_links.append(f"[DOVE?UNIPI↗]({dove_url})")
    seen = set()
    for link in all_docenti_links:
        if link not in seen:
            seen.add(link)
            footer_links.append(link)
    
    if footer_links:
        text += "\n" + "  ".join(footer_links)

    
    
    return text

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
        "<b>DOVE?UNIPI</b>\n\n"
        "Trova aule e uffici dei professori dell'Universita di Pisa.\n\n"
        "<b>Ricerca Inline</b>\n"
        "In qualsiasi chat, digita:\n"
        "<code>@doveunipibot nome aula o cognome professore</code>\n\n"
        "<b>Cerca Lezione</b>\n"
        "Per cercare una lezione per materia:\n"
        "<code>@doveunipibot l:nome materia</code>\n"
        "Supporta anche giorni successivi (+1, +2...)\n\n"
        "<b>Stato Aula</b>\n"
        "Per vedere lo stato di un'aula:\n"
        "<code>@doveunipibot s:nome aula</code>\n"
        "Puoi aggiungere <code>+1</code>, <code>+2</code>... per i giorni successivi.\n\n"
        "<b>Stato Aula Interattivo</b>\n"
        "Per vedere lo stato con navigazione giorni:\n"
        "<code>@doveunipibot sl:nome aula</code>\n\n"
        "<b>Comandi</b>\n"
        "/start - Messaggio di benvenuto\n"
        "/occupazione - Stato aule\n"
        "/links - Link utili\n"
        "/help - Guida all'uso"
    )
    
    keyboard = [
        [InlineKeyboardButton("Cerca aula", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("Cerca lezione", switch_inline_query_current_chat="l: ")],
        [InlineKeyboardButton("Stato aula", switch_inline_query_current_chat="s:")],
        [InlineKeyboardButton("Stato interattivo", switch_inline_query_current_chat="sl:")],
        [InlineKeyboardButton("Occupazione", callback_data="status:start")]
    ]
    
    await update.message.reply_text(
        text, 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True 
    )

async def occupazione_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /occupazione - mostra menu selezione polo."""
    text = "<b>Stato Aule</b>\n\nSeleziona un polo:"
    
    keyboard = [
        [InlineKeyboardButton("Polo Fibonacci", callback_data="status:polo:fibonacci")]
    ]
    
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /links - mostra tutti i link utili."""
    text = (
        "<b>Link Utili</b>\n\n"
        f"GitHub: {GITHUB_URL}\n\n"
        f"Sito Web: {SITE_URL}\n\n"
        f"Instagram: {INSTAGRAM_URL}\n\n"
        "Twitter: https://x.com/doveunipi"
    )
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /help - mostra guida all'uso."""
    text = (
        "<b>GUIDA ALL'USO</b>\n\n"
        "<b>Comandi Principali</b>\n"
        "/start - Avvia il bot e mostra il benvenuto\n"
        "/occupazione - Mostra lo stato delle aule navigando per edifici\n"
        "/links - Link utili (GitHub, Sito, Social)\n"
        "/help - Mostra questo messaggio\n\n"
        "<b>1. Ricerca Rapida (Inline)</b>\n"
        "Puoi cercare <b>Aule, Biblioteche e Uffici dei professori</b> (per cognome o numero) direttamente in qualsiasi chat.\n\n"
        "Digita il nome del bot seguito dalla ricerca:\n"
        "Esempio:\n"
        "<code>@doveunipibot Rossi</code>\n"
        "Output:\n"
        "<pre>Polo Fibonacci › Edificio A › Piano 1 › Stanza 21 › Rossi\nClicca per aprire su DOVE?UNIPI↗</pre>\n\n"
        "<b>2. Verifica Stato Aula</b>\n"
        "Vedi se un'aula è libera o occupata:\n"
        "<code>@doveunipibot s:F</code>\n"
        "Per vedere i giorni successivi, aggiungi un numero:\n"
        "<code>@doveunipibot s:F +1</code> (domani)\n\n"
        "<b>3. Stato con Navigazione</b>\n"
        "Vedi lo stato con i tasti per cambiare giorno:\n"
        "<code>@doveunipibot sl:C</code>\n\n"
        "<b>4. Ricerca Lezione</b>\n"
        "Cerca dove si svolge una lezione:\n"
        "<code>@doveunipibot l:Analisi</code>\n"
        "<i>Se non ci sono lezioni oggi, cercherà automaticamente nei prossimi 7 giorni.</i>\n"
        "Per domani: <code>@doveunipibot l:Analisi +1</code>\n\n"
        "<b>Pulsanti e Navigazione</b>\n"
        "<b>○</b>: Indietro / Menu Superiore\n"
        "<b>↺</b>: Aggiorna dati correnti\n"
        "<b>◀ ▶</b>: Cambia pagina o giorno\n\n"
        "I pulsanti si trovano sempre nella stessa posizione (es. 'Indietro' è sempre al centro, 'Aggiorna' sempre a destra).\n\n"
        "<b>Colori</b>\n"
        "I colori degli edifici e dello stato delle aule corrispondono esattamente a quelli visibili su DOVE?UNIPI, per un'esperienza visiva coerente."
    )
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )

def get_day_navigation_keyboard(aula_id: str, offset: int) -> InlineKeyboardMarkup:
    """Crea la tastiera per navigare tra i giorni."""
    row = []
    
    # Bottone Indietro
    row.append(InlineKeyboardButton("◀", callback_data=f"status:day_offset:{aula_id}:{offset-1}"))
    
    # Bottone Oggi (se non siamo a oggi, altrimenti placeholder o niente? Utente vuole cerchio)
    # Se offset è 0, magari disabilitato o visibile
    # Utente vuole "triangolo indietro, cerchio oggi, triangolo avanti"
    # Se offset è 0, il cerchio ricarica oggi (come refresh)
    row.append(InlineKeyboardButton("○", callback_data=f"status:day_offset:{aula_id}:0"))
        
    # Bottone Avanti
    row.append(InlineKeyboardButton("▶", callback_data=f"status:day_offset:{aula_id}:{offset+1}"))
    
    # Bottone Aggiorna (solo simbolo) su riga separata, allineato a destra
    row_refresh = [
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton("↺", callback_data=f"status:day_offset:{aula_id}:{offset}")
    ]
    
    return InlineKeyboardMarkup([row, row_refresh])

# --- CALLBACK HANDLER ---
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce tutti i callback del menu /status."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Gestione mappa
    if data == "show_map":
        await query.message.reply_photo(photo=MAP_URL)
        return
    
    if not data.startswith("status:"):
        return
    
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    
    now = datetime.now(TZ_ROME)
    
    # status:noop - Bottone placeholder che non fa nulla
    if action == "noop":
        return
    
    # status:start - Menu iniziale
    if action == "start":
        text = "*Stato Aule*\n\nSeleziona un polo:"
        keyboard = [
            [InlineKeyboardButton("Polo Fibonacci", callback_data="status:polo:fibonacci")]
        ]
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    # status:polo:<polo> - Menu edifici del polo
    elif action == "polo":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edifici = get_edifici(polo)
        
        text = f"*Polo Fibonacci*\n\nSeleziona un edificio:"
        
        keyboard = [
            [InlineKeyboardButton("TUTTI", callback_data=f"status:tutti_polo:{polo}")]
        ]
        
        # Bottoni edifici (2 per riga)
        row = []
        for i, edificio in enumerate(edifici):
            row.append(InlineKeyboardButton(
                f"Edificio {edificio.upper()}", 
                callback_data=f"status:edificio:{polo}:{edificio}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton(" ", callback_data="status:noop"),
                         InlineKeyboardButton("○", callback_data="status:start"),
                         InlineKeyboardButton(" ", callback_data="status:noop")])
        
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    # status:tutti_polo:<polo> - Stato tutte le aule
    elif action == "tutti_polo":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        
        # Carica eventi
        events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, now)
        
        text = format_polo_status(polo, events, now)
        
        # Messaggio potrebbe essere troppo lungo, dividiamolo se necessario
        if len(text) > 4000:
            text = text[:3900] + "\n\n_...messaggio troncato_"
        
        keyboard = [
            [InlineKeyboardButton(" ", callback_data="status:noop"),
             InlineKeyboardButton("○", callback_data=f"status:polo:{polo}"),
             InlineKeyboardButton("↺", callback_data=f"status:tutti_polo:{polo}")]
        ]
        
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    # status:day_offset:<aula_id>:<offset> - Cambio giorno
    elif action == "day_offset":
        aula_id = parts[2] if len(parts) > 2 else ""
        try:
            offset = int(parts[3]) if len(parts) > 3 else 0
        except:
            offset = 0
            
        # Trova l'aula (cerca in tutto il polo fibonacci per semplicità, ma potremmo ottimizzare)
        aule = get_aule_polo("fibonacci")
        aula = None
        for a in aule:
            if a.get('id') == aula_id:
                aula = a
                break
        
        if not aula:
            await query.answer("Aula non trovata", show_alert=True)
            return

        # Calcola data target
        target_date = datetime.now(TZ_ROME) + timedelta(days=offset)
        
        # Fetch eventi per QUELLA data
        events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, target_date)
        status = get_aula_status(aula['nome'], events, target_date)
        
        # Formatta messaggio per il giorno specifico
        # Se offset == 0 usa formato standard, altrimenti formato programma
        if offset == 0:
            # Trova URL per link DOVE?UNIPI (copiato da logica esistente)
            dove_url = None
            items = get_data()
            item = find_dove_item(items, aula['nome'])
            if item:
                raw_input = item.get("input_message_content", {})
                dove_url = extract_url_from_markdown(raw_input.get("message_text", ""))
            
            text = format_single_aula_status(aula, status, target_date, dove_url)
        else:
            # Usa il nuovo helper
            text = format_day_schedule(aula, events, target_date)
        
        keyboard = get_day_navigation_keyboard(aula_id, offset)
        
        # Importante: per messaggi inline, edit_message_text funziona solo se il contenuto cambia
        try:
            await query.edit_message_text(
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        except Exception as e:
            # Se il messaggio non è cambiato, Telegram dà errore, lo ignoriamo (o mostriamo alert "Già aggiornato")
            if "Message is not modified" in str(e):
                await query.answer("Già aggiornato!")
                return
            logger.error(f"Errore edit message day offset: {e}")

    elif action == "edificio":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        
        await show_edificio_piani_menu(query, polo, edificio)

    # status:tutti_edificio:<polo>:<edificio> - Stato tutte le aule edificio
    elif action == "tutti_edificio":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        
        events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, now)
        text = format_edificio_status(polo, edificio, events, now)
        
        if len(text) > 4000:
            text = text[:3900] + "\n\n_...messaggio troncato_"
        
        keyboard = [
            [InlineKeyboardButton(" ", callback_data="status:noop"),
             InlineKeyboardButton("○", callback_data=f"status:edificio:{polo}:{edificio}"),
             InlineKeyboardButton("↺", callback_data=f"status:tutti_edificio:{polo}:{edificio}")]
        ]
        
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    # status:tutti_piano:<polo>:<edificio>:<piano> - Stato tutte le aule di un piano
    elif action == "tutti_piano":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        piano = parts[4] if len(parts) > 4 else "0"
        
        events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, now)
        text = format_piano_status(polo, edificio, piano, events, now)
        
        if len(text) > 4000:
            text = text[:3900] + "\n\n_...messaggio troncato_"
        
        keyboard = [
            [InlineKeyboardButton(" ", callback_data="status:noop"),
             InlineKeyboardButton("○", callback_data=f"status:piano:{polo}:{edificio}:{piano}"),
             InlineKeyboardButton("↺", callback_data=f"status:tutti_piano:{polo}:{edificio}:{piano}")]
        ]
        
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    # status:piano:<polo>:<edificio>:<piano> - Menu aule piano
    elif action == "piano":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        piano = parts[4] if len(parts) > 4 else "0"
        page = 0
        
        await show_piano_aule_menu(query, polo, edificio, piano, page)
    
    # status:page:<polo>:<edificio>:<piano>:<page> - Paginazione aule
    elif action == "page":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        piano = parts[4] if len(parts) > 4 else "0"
        page = int(parts[5]) if len(parts) > 5 else 0
        
        await show_piano_aule_menu(query, polo, edificio, piano, page)
    
    # status:aula:<polo>:<edificio>:<piano>:<aula_id> - Singola aula
    elif action == "aula":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        piano = parts[4] if len(parts) > 4 else "0"
        aula_id = parts[5] if len(parts) > 5 else ""
        
        # Trova l'aula
        aule = get_aule_edificio(polo, edificio)
        aula = None
        for a in aule:
            if a.get('id') == aula_id:
                aula = a
                break
        
        if not aula:
            await query.message.edit_text("Aula non trovata")
            return
        
        events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, now)
        status = get_aula_status(aula['nome'], events, now)
        
        # Trova URL per link DOVE?UNIPI
        dove_url = None
        items = get_data()
        item = find_dove_item(items, aula['nome'])
        if item:
            raw_input = item.get("input_message_content", {})
            dove_url = extract_url_from_markdown(raw_input.get("message_text", ""))
        
        text = format_single_aula_status(aula, status, now, dove_url)
        
        keyboard = [
            [InlineKeyboardButton(" ", callback_data="status:noop"),
             InlineKeyboardButton("○", callback_data=f"status:piano:{polo}:{edificio}:{piano}"),
             InlineKeyboardButton("↺", callback_data=f"status:aula:{polo}:{edificio}:{piano}:{aula_id}")]
        ]
        
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

async def show_edificio_piani_menu(query, polo: str, edificio: str):
    """Mostra il menu dei piani di un edificio."""
    piani = get_piani(polo, edificio)
    
    # Se c'è un solo piano, mostra direttamente le aule
    if len(piani) == 1:
        await show_piano_aule_menu(query, polo, edificio, piani[0], 0)
        return
    
    text = f"*Edificio {edificio.upper()}*\n\nSeleziona un piano:"
    
    keyboard = [
        [InlineKeyboardButton("TUTTI", callback_data=f"status:tutti_edificio:{polo}:{edificio}")]
    ]
    
    # Bottoni piani (2 per riga)
    row = []
    for piano in piani:
        row.append(InlineKeyboardButton(
            f"Piano {piano}", 
            callback_data=f"status:piano:{polo}:{edificio}:{piano}"
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(" ", callback_data="status:noop"),
                     InlineKeyboardButton("○", callback_data=f"status:polo:{polo}"),
                     InlineKeyboardButton(" ", callback_data="status:noop")])
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def show_piano_aule_menu(query, polo: str, edificio: str, piano: str, page: int):
    """Mostra il menu delle aule di un piano con paginazione."""
    aule = get_aule_edificio(polo, edificio)
    # Filtra per piano
    aule = [a for a in aule if a.get('piano') == piano]
    total_aule = len(aule)
    total_pages = max(1, (total_aule + AULE_PER_PAGE - 1) // AULE_PER_PAGE)
    
    # Assicurati che la pagina sia valida
    page = max(0, min(page, total_pages - 1))
    
    text = f"*Edificio {edificio.upper()} - Piano {piano}*\n"
    text += f"Seleziona un'aula:\n"
    if total_pages > 1:
        text += f"Pagina {page + 1}/{total_pages}"
    
    keyboard = [
        [InlineKeyboardButton("TUTTI", callback_data=f"status:tutti_piano:{polo}:{edificio}:{piano}")]
    ]
    
    # Aule per questa pagina
    start_idx = page * AULE_PER_PAGE
    end_idx = min(start_idx + AULE_PER_PAGE, total_aule)
    page_aule = aule[start_idx:end_idx]
    
    for aula in page_aule:
        nome = aula.get('nome', 'N/D')
        aula_id = aula.get('id', '')
        keyboard.append([InlineKeyboardButton(
            f"{nome}",
            callback_data=f"status:aula:{polo}:{edificio}:{piano}:{aula_id}"
        )])
    
    # Navigazione compatta (◀ ○ ▶ sulla stessa riga) - sempre 3 bottoni per muscle memory
    nav_row = []
    
    # Bottone indietro (o placeholder vuoto)
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀", callback_data=f"status:page:{polo}:{edificio}:{piano}:{page-1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="status:noop"))
    
    # Se edificio ha un solo piano, il tasto ○ torna al polo per evitare loop
    piani_edificio = get_piani(polo, edificio)
    if len(piani_edificio) == 1:
        back_data = f"status:polo:{polo}"
    else:
        back_data = f"status:edificio:{polo}:{edificio}"
        
    nav_row.append(InlineKeyboardButton("○", callback_data=back_data))
    
    # Bottone avanti (o placeholder vuoto)
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶", callback_data=f"status:page:{polo}:{edificio}:{piano}:{page+1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="status:noop"))
    keyboard.append(nav_row)
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# --- COMANDI AGGIUNTIVI ---

# --- INLINE QUERY ---
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.lower().strip()
    
    results = []
    
    
    # Import corretto per il bottone (aggiungi se manca in alto, ma qui lo uso inline se possibile o modifico import)
    # Per semplicità, rimuovo il bottone "Nessun risultato" per ora, o uso switch_inline_query_current_chat se necessario.
    # Ma switch_pm serviva per switchare al PM. 
    # V20: button=InlineQueryResultsButton(text="Nessun risultato", start_parameter="empty")
    
    # GESTIONE s: PER STATUS AULA
    if query.startswith("s:"):
        aula_search = query[2:].strip()
        if aula_search:
            results = await search_aula_status_inline(aula_search, interactive=False)
            if len(results) == 0:
                no_results_button = InlineQueryResultsButton(text="Nessun risultato trovato", start_parameter="empty")
                await update.inline_query.answer(results, cache_time=0, button=no_results_button)
            else:
                await update.inline_query.answer(results[:10], cache_time=0)
        else:
            # Query vuota, mostra suggerimento
            search_button = InlineQueryResultsButton(text="Cerca un'aula", start_parameter="empty")
            await update.inline_query.answer([], cache_time=0, button=search_button)
        return

    # GESTIONE sl: PER STATUS AULA INTERATTIVO (con giorni)
    if query.startswith("sl:"):
        aula_search = query[3:].strip()
        if aula_search:
            results = await search_aula_status_inline(aula_search, interactive=True)
            if len(results) == 0:
                no_results_button = InlineQueryResultsButton(text="Nessun risultato trovato", start_parameter="empty")
                await update.inline_query.answer(results, cache_time=0, button=no_results_button)
            else:
                await update.inline_query.answer(results[:10], cache_time=0)
        else:
            # Query vuota, mostra suggerimento
            search_button = InlineQueryResultsButton(text="Cerca un'aula", start_parameter="empty")
            await update.inline_query.answer([], cache_time=0, button=search_button)
        return
        
    # GESTIONE l: PER RICERCA LEZIONI
    if query.startswith("l:"):
        lesson_search = query[2:].strip()
        if lesson_search:
            results = await search_lessons_inline(lesson_search, interactive=False)
            if len(results) == 0:
                no_results_button = InlineQueryResultsButton(text="Nessun risultato trovato", start_parameter="empty")
                await update.inline_query.answer(results, cache_time=0, button=no_results_button)
            else:
                 # Max 50 risultati
                await update.inline_query.answer(results[:50], cache_time=0)
        else:
             # Query vuota
            search_button = InlineQueryResultsButton(text="Cerca una lezione", start_parameter="empty")
            await update.inline_query.answer([], cache_time=0, button=search_button)
        return

    # --- LOGICA DI RICERCA GENERALE ---
    # ...


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
                
                # PULIZIA LINK VECCHIO e AGGIUNTA FOOTER
                if url:
                    # Usa la descrizione pulita (solitamente contiene il path)
                    # Es. "Polo Fibonacci > Edificio L > ..."
                    clean_desc = description.split("\n")[0].strip()
                    # Formato richiesto: Path › Name
                    final_text = f"{clean_desc} › {title}\n\nClicca per aprire su [DOVE?UNIPI↗]({url})"
                else:
                    final_text = raw_text
                
                # OLD LINK OVERWRITE REMOVED
                # Questo blocco sovrascriveva la nostra logica del footer. Lo rimuoviamo.

                thumb = get_building_thumb(description)

                results.append(
                    InlineQueryResultArticle(
                        id=item.get("id", str(uuid.uuid4())),
                        title=title + " (Posizione)",
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
    # Priorità: match esatti > match che iniziano con query > altri match > professori
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
            
            # Controlla match ESATTO (query == title o query == keyword)
            # Per le aule, considera anche "aula X" dove X è la query
            title_exact = (result_title == query) or (result_title == f"aula {query}")
            keywords_exact = any(k == query for k in keywords)
            is_exact_match = title_exact or keywords_exact
            
            # Controlla se title o keywords iniziano con la query
            title_starts = result_title.startswith(query) or result_title.startswith(f"aula {query}")
            keywords_start = any(k.startswith(query) for k in keywords)
            starts_with = title_starts or keywords_start
            
            # Priorità (valori più bassi = più in alto):
            # 0 = match esatto (aule)
            # 1 = match che inizia con query (aule) 
            # 2 = altri match (aule)
            # 3 = professori
            if is_professor:
                priority = 3
            elif is_exact_match:
                priority = 0
            elif starts_with:
                priority = 1
            else:
                priority = 2
            
            return (priority, result_title)
        
        results.sort(key=sort_key)

    # Mostra messaggio "nessun risultato" se la ricerca non trova nulla
    if len(results) == 0:
        no_results_button = None
        if query:
            no_results_button = InlineQueryResultsButton(text="Nessun risultato trovato", start_parameter="empty")
        
        await update.inline_query.answer(results, cache_time=0, button=no_results_button)
    else:
        await update.inline_query.answer(results[:10], cache_time=0)


async def search_aula_status_inline(aula_search: str, interactive: bool = False) -> list:
    """Cerca un'aula e restituisce il suo status come risultato inline. Se interactive=True, aggiunge tastiera giorni."""
    results = []
    
    # Parsing offset "+N"
    offset = 0
    import re
    # Cerca pattern "+<numero>" alla fine della stringa
    match = re.search(r'\+(\d+)$', aula_search)
    if match:
        offset = int(match.group(1))
        # Rimuovi l'offset dalla stringa di ricerca
        aula_search = aula_search[:match.start()].strip()
    
    now = datetime.now(TZ_ROME)
    target_date = now + timedelta(days=offset)
    
    # Se offset > 0 fetchiamo eventi di quel giorno invece che oggi
    if offset > 0:
        events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, target_date)
    else:
        events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, now)
    
    # Cerca in tutte le aule del polo
    aule = get_aule_polo("fibonacci")
    
    # Trova anche il risultato normale dalla ricerca standard
    items = get_data()
    
    # Prima raccogli tutte le aule che matchano con il loro punteggio di priorità
    matched_aule = []
    for aula in aule:
        nome = aula.get('nome', '').lower()
        alias_list = aula.get('alias', [])
        
        # Verifica match con nome o alias
        match = aula_search in nome
        if not match:
            for alias in alias_list:
                if aula_search in alias.lower():
                    match = True
                    break
        
        if match:
            # Calcola priorità: 0 = match esatto, 1 = inizia con, 2 = contiene
            nome_lower = nome
            # Estrai solo il codice dell'aula (es. "aula o" -> "o")
            nome_code = nome_lower.replace("aula ", "").strip()
            
            # Match esatto
            if nome_code == aula_search or nome_lower == aula_search or nome_lower == f"aula {aula_search}":
                priority = 0
            # Match che inizia con la query
            elif nome_code.startswith(aula_search) or nome_lower.startswith(aula_search) or nome_lower.startswith(f"aula {aula_search}"):
                priority = 1
            # Altro match
            else:
                priority = 2
            
            matched_aule.append((priority, nome_lower, aula))
    
    # Ordina per priorità e poi alfabeticamente
    matched_aule.sort(key=lambda x: (x[0], x[1]))
    
    # Ora processa le aule ordinate
    for priority, nome_lower, aula in matched_aule:
            edificio = aula.get('edificio', '?').upper()
            piano = aula.get('piano', '?')
            
            if offset > 0:
                # Per giorni futuri usiamo lo start of day per il calcolo status (per vedere eventi)
                check_time = target_date.replace(hour=0, minute=0, second=1)
                status = get_aula_status(aula['nome'], events, check_time)
            else:
                status = get_aula_status(aula['nome'], events, now)
            
            # --- TENTATIVO DI MATCH CON ITEM DI DOVE?UNIPI ---
            item = find_dove_item(items, aula['nome'])
            dove_url = None
            final_text_main = ""
            
            if item:
                raw_input = item.get("input_message_content", {})
                dove_url = extract_url_from_markdown(raw_input.get("message_text", ""))
                
                # Prepara testo per il risultato "standard" (Punto 1)
                if dove_url:
                    description = item.get("description", "")
                    clean_desc = description.split("\n")[0].strip()
                    # Formato richiesto: Path › Name
                    final_text_main = f"{clean_desc} › {item.get('title', '')}\n\nClicca per aprire su [DOVE?UNIPI↗]({dove_url})"
                else:
                    final_text_main = raw_input.get("message_text", "")
            else:
                # Fallback se non trovato in data.json
                final_text_main = f"Aula {aula['nome']} (Edificio {edificio})"

            # 1. Prima aggiungi il risultato ESATTAMENTE come la ricerca normale (se item esiste)
            if item:
                parse_mode_item = item.get("input_message_content", {}).get("parse_mode", "Markdown")
                results.append(
                    InlineQueryResultArticle(
                        id=item.get("id", str(uuid.uuid4())),
                        title=item.get("title", aula['nome']) + " (Posizione)",
                        description=item.get("description", f"Edificio {edificio} › Piano {piano}"),
                        input_message_content=InputTextMessageContent(
                            message_text=final_text_main,
                            parse_mode=parse_mode_item,
                            disable_web_page_preview=True
                        ),
                        thumbnail_url=get_building_thumb(f"Edificio {edificio}"),
                        thumbnail_width=100,
                        thumbnail_height=100
                    )
                )
            
            # 2. Aggiungi risultato status attuale con thumbnail colorato
            if status['is_free']:
                if status['free_until']:
                    status_description = f"Libera fino alle {status['free_until'].strftime('%H:%M')}"
                else:
                    status_description = "Libera per il resto della giornata"
                # Thumbnail verde per libera
                status_thumb = "https://placehold.co/100x100/8cacaa/8cacaa.png"
            else:
                status_description = f"Occupata fino alle {status['busy_until'].strftime('%H:%M')}"
                # Thumbnail rosso per occupata
                status_thumb = "https://placehold.co/100x100/b04859/b04859.png"
            
            # Formatta messaggio status
            if offset > 0:
                 status_msg = format_day_schedule(aula, events, target_date)
                 
                 # Per i giorni futuri, descrizione adattata
                 if status['next_events'] or status['current_event']:
                     status_description = f"Programma del {target_date.strftime('%d/%m')} - Occupata"
                     # Thumbnail rosso se ci sono eventi
                     status_thumb = "https://placehold.co/100x100/b04859/b04859.png"
                 else:
                     status_description = f"Programma del {target_date.strftime('%d/%m')} - Libera"
                     status_thumb = "https://placehold.co/100x100/8cacaa/8cacaa.png"
                     
            else:
                 status_msg = format_single_aula_status(aula, status, now, dove_url)
            
            # --- CREAZIONE TASTIERA ---
            reply_markup = None
            if interactive:
                aula_id = aula.get('id', '')
                reply_markup = get_day_navigation_keyboard(aula_id, offset)

            # --- CREAZIONE RISULTATI INLINE ---
            # Tutti i risultati useranno lo STESSO IDENTICO status_msg come contenuto del messaggio inviato
            
            # 1. Risultato Stato Attuale (O Header Futuro)
            if offset == 0:
                header_title = "STATO ATTUALE" + (" (Aggiornabile)" if interactive else "")
            else:
                GIORNI = ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"]
                header_title = f"{GIORNI[target_date.weekday()]} {target_date.strftime('%d/%m')}"

            # Sempre aggiungi l'header card (che sia Stato o Data futura)
            if offset == 0:
                results.append(
                    InlineQueryResultArticle(
                        id=f"status_{aula.get('id', str(uuid.uuid4()))}_{offset}",
                        title=header_title,
                        description=status_description,
                        input_message_content=InputTextMessageContent(
                            message_text=status_msg,
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=True
                        ),
                        reply_markup=reply_markup,
                        thumbnail_url=status_thumb,
                        thumbnail_width=100,
                        thumbnail_height=100
                    )
                )
            
            # 2. Se c'è una lezione in corso (SOLO OGGI), aggiungila come opzione cliccabile
            if status['current_event']:
                event = status['current_event']
                results.append(
                    InlineQueryResultArticle(
                        id=f"current_{aula.get('id')}_{str(uuid.uuid4())[:8]}",
                        title=f"IN CORSO: {event['nome']}",
                        description=f"{event['start'].strftime('%H:%M')} - {event['end'].strftime('%H:%M')}" + (f"\n{event['docenti']}" if event.get('docenti') else ""),
                        input_message_content=InputTextMessageContent(
                            message_text=status_msg,  # USA LO STESSO MESSAGGIO
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=True
                        ),
                        thumbnail_url=status_thumb,
                        thumbnail_width=100,
                        thumbnail_height=100
                    )
                )
            
            # 3. Aggiungi le occupazioni future (SOLO OGGI) o TUTTE (SE OFFSET > 0)
            if status['next_events']:
                # Thumbnail rosso per occupazioni future
                future_thumb = "https://placehold.co/100x100/b04859/b04859.png"
                
                for i, event in enumerate(status['next_events'][:5]):
                    results.append(
                        InlineQueryResultArticle(
                            id=f"event_{aula.get('id')}_{i}_{str(uuid.uuid4())[:8]}",
                            title=event['nome'],
                            description=f"{event['start'].strftime('%H:%M')} - {event['end'].strftime('%H:%M')}" + (f" • {GIORNI[target_date.weekday()]} {target_date.strftime('%d/%m')}" if offset > 0 else "") + (f"\n{event['docenti']}" if event.get('docenti') else ""),
                            input_message_content=InputTextMessageContent(
                                message_text=status_msg,  # USA LO STESSO MESSAGGIO
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True
                            ),
                            thumbnail_url=future_thumb,
                            thumbnail_width=100,
                            thumbnail_height=100
                        )
                    )
    
    return results


    return results


async def search_lessons_inline(lesson_search: str, interactive: bool = False) -> list:
    """Cerca lezioni per nome e restituisce lista risultati."""
    results = []
    
    # Parsing offset "+N"
    offset = 0
    import re
    match = re.search(r'\+(\d+)$', lesson_search)
    if match:
        offset = int(match.group(1))
        lesson_search = lesson_search[:match.start()].strip()
    
    now = datetime.now(TZ_ROME)
    target_date = now + timedelta(days=offset)
    
    # Fetch eventi (di tutto il polo, non filtrati per aula specifica)
    events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, target_date)
    
    # Filtra eventi per nome
    matched_events = []
    search_lower = lesson_search.lower()
    
    for event in events:
        nome_evento = event.get('nome', '').lower()
        if search_lower in nome_evento:
            matched_events.append(event)
    
    # PULIZIA EVENTI PASSATI (SOLO SE SIAMO NELLA RICERCA "OGGI" INIZIALE)
    if offset == 0:
        filtered_events = []
        for event in matched_events:
            try:
                end = datetime.fromisoformat(event['dataFine'].replace('Z', '+00:00')).astimezone(TZ_ROME)
                if end >= now:
                    filtered_events.append(event)
            except:
                pass
        matched_events = filtered_events

    # --- SMART LOOK-AHEAD: SE NESSUN RISULTATO "OGGI", CERCA NEI PROSSIMI GIORNI ---
    if offset == 0 and len(matched_events) == 0:
        for i in range(1, 8): # Cerca nei prossimi 7 giorni
            check_date = now + timedelta(days=i)
            # Fetch eventi per quel giorno
            future_events = fetch_day_events(POLO_FIBONACCI_CALENDAR_ID, check_date)
            # Filtra per nome
            matches_future = []
            for event in future_events:
                nome_evento = event.get('nome', '').lower()
                if search_lower in nome_evento:
                    matches_future.append(event)
            
            if matches_future:
                # Trovato! Usiamo questo giorno
                matched_events = matches_future
                target_date = check_date
                match_day_str = check_date.strftime('%d/%m')
                # Aggiorna offset fittizio per logiche successive (se servissero)
                break
    
    # Ordina per orario
    matched_events.sort(key=lambda x: datetime.fromisoformat(x['dataInizio'].replace('Z', '+00:00')))
    
    # Carica dati aule per mapping nome -> oggetto aula
    data = load_aule_json()
    all_aule = get_aule_polo("fibonacci")
    aula_map = {a['nome'].upper(): a for a in all_aule}
    # Mappa estesa per includere varianti API
    
    for event in matched_events:
        # Recupera dati evento
        nome = event.get('nome', 'N/D')
        
        try:
            start = datetime.fromisoformat(event['dataInizio'].replace('Z', '+00:00')).astimezone(TZ_ROME)
            end = datetime.fromisoformat(event['dataFine'].replace('Z', '+00:00')).astimezone(TZ_ROME)
        except:
            continue
        
        # Filtro "offset 0" fatto sopra nella fase di selezione giorno
        # Quindi qui processiamo tutto quello che è rimasto in matched_events

        time_str = f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"
        
        # Docenti
        docenti_nomi = []
        for d in event.get('docenti', []):
             if d.get('cognome'):
                 docenti_nomi.append(f"{d.get('nome','')} {d.get('cognome','')}".strip())
        docenti_str = ", ".join(docenti_nomi)
        
        # Aula
        aule_evento = event.get('aule', [])
        aula_nome_display = "N/D"
        aula_obj = None
        
        if aule_evento:
            # Prendi la prima aula (spesso è unica)
            raw_codice = aule_evento[0].get('codice', '').replace('FIB ','').replace('Fib ','').strip()
            aula_nome_display = raw_codice
            
            # Cerca l'oggetto aula corrispondente per poter chiamare format_day_schedule
            # Prova match esatto o quasi
            if raw_codice.upper() in aula_map:
                aula_obj = aula_map[raw_codice.upper()]
            else:
                # Fallback ricerca
                for a_nome, a_obj in aula_map.items():
                    if raw_codice.upper() in a_nome:
                        aula_obj = a_obj
                        break
        
        # Prepara il messaggio di risposta (Programma dell'aula per quel giorno)
        if aula_obj:
             # Dobbiamo filtrare gli eventi per quell'aula specifica per passare a format_day_schedule
             # O semplicemente richiamare get_aula_status che filtra internamente
             # Ma format_day_schedule richiede (aula, events, date) e filtra lui?
             # No, format_day_schedule chiama get_aula_status(aula['nome'], events, ...)
             # Quindi possiamo passare TUTTI gli events e lui filtra per l'aula.
             msg_content = format_day_schedule(aula_obj, events, target_date)
        else:
             # Fallback se non troviamo l'aula mappata
             msg_content = f"*{nome}*\n{time_str}\nAula: {aula_nome_display}\n\nImpossibile recuperare il programma completo dell'aula."

        # Thumbnail rosso sempre per lezione
        thumb_url = "https://placehold.co/100x100/b04859/ffffff.png?text=Lez"
        
        description = f"{time_str} • {aula_nome_display}"
        
        # Se la data non è oggi, aggiungiamola alla descrizione
        if target_date.date() != now.date():
            weekday_map = {0:'LUN', 1:'MAR', 2:'MER', 3:'GIO', 4:'VEN', 5:'SAB', 6:'DOM'}
            day_str = weekday_map.get(target_date.weekday(), '')
            date_str = target_date.strftime('%d/%m')
            description = f"{time_str} • {day_str} {date_str} • {aula_nome_display}"
            
        if docenti_str:
            description += f"\n{docenti_str}"
            
        results.append(
            InlineQueryResultArticle(
                id=f"lesson_{str(uuid.uuid4())[:8]}",
                title=nome,
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=msg_content,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                ),
                thumbnail_url=thumb_url,
                thumbnail_width=100,
                thumbnail_height=100
            )
        )
        
    return results

# --- CHOSEN INLINE RESULT HANDLER ---

# --- MAIN ---
def main():
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    PORT = int(os.environ.get("PORT", "8443"))
    WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

    if not TOKEN:
        logger.error("ERRORE: Token mancante.")
        return

    app = Application.builder().token(TOKEN).build()
    
    # Imposta i comandi del bot su Telegram
    async def post_init(application):
        from telegram import BotCommand
        commands = [
            BotCommand("start", "Messaggio di benvenuto"),
            BotCommand("occupazione", "Stato aule"),
            BotCommand("links", "Link utili"),
            BotCommand("help", "Guida all'uso"),
        ]
        await application.bot.set_my_commands(commands)
    
    app.post_init = post_init
    
    # Comandi
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("occupazione", occupazione_command))
    app.add_handler(CommandHandler("links", links_command))
    app.add_handler(CommandHandler("help", help_command))

    
    # Callback per bottoni
    app.add_handler(CallbackQueryHandler(status_callback))
    
    # Inline query
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