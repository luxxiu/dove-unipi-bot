import logging
import os
import json
import uuid
import asyncio
import requests
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import pytz

# --- FIX APSCHEDULER TIMEZONE ---
def patch_apscheduler():
    try:
        import apscheduler.util
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
    MessageHandler,
    filters,
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data", "unified.json")

DEFAULT_COLOR = "808080"
TZ_ROME = pytz.timezone('Europe/Rome')
WEEKDAYS_SHORT = ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"]

# Costanti per /status
AULE_PER_PAGE = 5

# API per calendario
API_URL = os.environ.get("API_URL", "https://apache.prod.up.cineca.it/api/Impegni/getImpegniCalendarioPubblico")
CLIENT_ID = os.environ.get("CLIENT_ID", "628de8b9b63679f193b87046")

# Varianti codici laboratorio (separate da |). Usa {num} come placeholder.
LAB_CODE_VARIANTS = os.environ.get("LAB_CODE_VARIANTS", "FIS LAB {num}").split("|")

# --- LINK FISSI ---
GITHUB_URL = os.environ.get("GITHUB_URL", "https://github.com/plumkewe/dove-unipi")
SITE_URL = os.environ.get("SITE_URL", "https://plumkewe.github.io/dove-unipi/")
GITHUB_ICON_URL = os.environ.get(
    "GITHUB_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/github.png",
)
GLOBE_ICON_URL = os.environ.get(
    "GLOBE_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/globe.png",
)
MAP_ICON_URL = os.environ.get(
    "MAP_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/map.png",
)
MAP_URL = os.environ.get(
    "MAP_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/mappa.png",
)
INSTAGRAM_URL = os.environ.get("INSTAGRAM_URL", "https://www.instagram.com/doveunipi")
INSTAGRAM_ICON_URL = os.environ.get(
    "INSTAGRAM_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/instagram.png",
)
INFO_ICON_URL = os.environ.get(
    "INFO_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/info.png?v=1",
)

# --- CARICAMENTO DATI ---
_UNIFIED_CACHE = None
_UNIFIED_MTIME = None
_GENERATED_DATA_CACHE = None

def _get_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except Exception:
        return None

def load_unified_json() -> dict:
    """Carica il file data/unified.json."""
    global _UNIFIED_CACHE, _UNIFIED_MTIME
    path = DATA_PATH
    try:
        mtime = _get_mtime(path)
        if _UNIFIED_CACHE is not None and _UNIFIED_MTIME == mtime:
            return _UNIFIED_CACHE
        with open(path, 'r', encoding='utf-8') as f:
            # Add explicit reconnect or retry? No need for file open.
            content = f.read()
            # Basic JSON validation
            if not content.strip():
                return {}
            _UNIFIED_CACHE = json.loads(content)
            _UNIFIED_MTIME = mtime
            # Invalidate generated cache when source changes
            global _GENERATED_DATA_CACHE
            _GENERATED_DATA_CACHE = None
            return _UNIFIED_CACHE
    except Exception as e:
        logger.error(f"Errore lettura data/unified.json: {e}")
        return {}

def get_polos() -> List[str]:
    data = load_unified_json()
    polos = list(data.get("polo", {}).keys())
    if not polos:
        return ["fibonacci", "carmignani"]
    return sorted(polos)

def get_polo_display_name(polo_key: str) -> str:
    data = load_unified_json()
    return data.get("polo", {}).get(polo_key, {}).get("nome", polo_key.capitalize())

def build_polo_keyboard(callback_prefix: str = "status:polo:") -> List[List[InlineKeyboardButton]]:
    keyboard = []
    for polo in get_polos():
        display_name = get_polo_display_name(polo)
        keyboard.append([InlineKeyboardButton(f"Polo {display_name}", callback_data=f"{callback_prefix}{polo}")])
    return keyboard

def normalize_short_code(value):
    return value.strip().lower().replace(" ", "") if value else ""

def get_room_short_code(room):
    if not room:
        return None
    
    aliases = room.get('alias', [])
    if isinstance(aliases, list):
        valid_alias = next((alias.strip() for alias in aliases if alias and alias.strip()), None)
        if valid_alias:
            return valid_alias

    if room.get('nome') and room['nome'].strip():
        return room['nome'].strip()

    if room.get('id') and room['id'].strip():
        return room['id'].strip()

    return None

def generate_search_index(data):
    structured_links = []
    id_counter = 1
    eligible_types = {'aula', 'dipartimento', 'laboratorio', 'sala', 'biblioteca', 'studio', 'persona'}

    # Iteriamo su TUTTI i poli, non solo Fibonacci
    for polo_key, polo_data in data.get('polo', {}).items():
        polo_name = polo_key.capitalize()
        
        for building, building_data in polo_data.get('edificio', {}).items():
            for floor, rooms in building_data.get('piano', {}).items():
                for room in rooms:
                    room_type = room.get('type')
                    if not room_type: continue
                    
                    room_types_list = room_type if isinstance(room_type, list) else [room_type]
                    
                    # Filtro tipi eleggibili
                    if not any(t in eligible_types for t in room_types_list):
                        continue

                    # --- GESTIONE PERSONA ---
                    if 'persona' in room_types_list:
                        person_name = room.get('ricerca', '')
                        if not person_name or not person_name.strip():
                            continue
                        
                        # Link SOLO se presente nel JSON
                        short_link = room.get('link-dove-unipi')
                        
                        floor_label = "Piano Terra" if floor == "0" else f"Piano {floor}"
                        
                        room_alias = ""
                        aliases = room.get('alias', [])
                        if aliases and len(aliases) > 0:
                            room_alias = aliases[0]
                        
                        room_ref = room_alias if room_alias else room.get('room', '')
                        
                        # FIX: Handle empty building
                        building_part = f"Edificio {building.upper()} › " if building and building != '?' and building.lower() != polo_name.lower() else ""
                        description = f"Polo {polo_name} › {building_part}{floor_label}"
                        
                        if room_ref:
                             description += f" › Stanza {room_ref}"
                        
                        categ = room.get('categoria')
                        if categ:
                            c_text = ', '.join(categ) if isinstance(categ, list) else str(categ)
                            description += f"\n{c_text}"

                        keywords = aliases if isinstance(aliases, list) else []
                        
                        if short_link:
                            msg_text = f"[{person_name}]({short_link})"
                        else:
                            msg_text = f"*{person_name}*"

                        structured_links.append({
                            "type": "article",
                            "id": f"s_{id_counter}",
                            "title": person_name,
                            "keywords": keywords,
                            "description": description,
                            "input_message_content": {
                                "message_text": msg_text,
                                "parse_mode": "Markdown"
                            }
                        })
                        id_counter += 1
                        
                        if len(room_types_list) == 1 and room_types_list[0] == 'persona':
                            continue

                    # --- GESTIONE AULA/ALTRO ---
                    other_types = [t for t in room_types_list if t != 'persona']
                    if not other_types:
                        continue
                    if not any(t in eligible_types for t in other_types):
                         continue

                    # Link SOLO se presente nel JSON
                    short_link = room.get('link-dove-unipi')
                    
                    floor_label = "Piano Terra" if floor == "0" else f"Piano {floor}"

                    # FIX: Handle empty building
                    building_part = f"Edificio {building.upper()} › " if building and building != '?' and building.lower() != polo_name.lower() else ""
                    description = f"Polo {polo_name} › {building_part}{floor_label}"
                    
                    cap = room.get('capienza')
                    if cap:
                        description += f"\nCapienza: {cap}"

                    keywords = room.get('alias', [])
                    if not isinstance(keywords, list):
                        keywords = []

                    room_name = room.get('nome', 'Unknown Room')
                    
                    if short_link:
                        msg_text = f"[{room_name}]({short_link})"
                    else:
                        msg_text = f"*{room_name}*"

                    structured_links.append({
                        "type": "article",
                        "id": str(id_counter),
                        "title": room_name,
                        "keywords": keywords,
                        "description": description,
                        "input_message_content": {
                            "message_text": msg_text,
                            "parse_mode": "Markdown"
                        }
                    })
                    id_counter += 1

    return structured_links

def get_data():
    global _GENERATED_DATA_CACHE
    
    # Trigger load to check mtime
    unified_data = load_unified_json()
    
    # If we have valid generated data, return it
    if _GENERATED_DATA_CACHE is not None:
        return _GENERATED_DATA_CACHE
        
    if not unified_data:
        return []

    try:
        _GENERATED_DATA_CACHE = generate_search_index(unified_data)
        return _GENERATED_DATA_CACHE
    except Exception as e:
        logger.error(f"Errore generazione dati: {e}")
        return []

def parse_query_modifiers(query: str) -> dict:
    """
    Parse query modifiers like +1, +fib, +car from a search string.
    Returns: {'offset': int, 'polo_filter': str or None, 'clean_query': str}
    """
    offset = 0
    polo_filter = None
    parts = query.split()
    clean_parts = []
    
    for part in parts:
        if part.startswith('+'):
            val = part[1:].strip().lower()
            if not val:
                continue
            
            # Check for day offset (+1, +7, etc.)
            if val.isdigit():
                offset = int(val)
                continue
            
            # Check for polo aliases
            if val.startswith('fib'):
                polo_filter = 'fibonacci'
                continue
            elif val.startswith('car') or val.startswith('por'):
                polo_filter = 'carmignani'
                continue
            elif val in ['fibonacci', 'carmignani']:
                polo_filter = val
                continue
        
        clean_parts.append(part)
    
    return {
        'offset': offset,
        'polo_filter': polo_filter,
        'clean_query': ' '.join(clean_parts).strip()
    }

def get_calendar_id(polo="fibonacci"):
    data = load_unified_json()
    try:
        if not polo:
            polo = "fibonacci"
        return data['polo'][polo]['calendar_id']
    except Exception:
        logger.error(f"Calendar ID not found for polo {polo}")
        return None

def get_polo_prefix(polo="fibonacci"):
    data = load_unified_json()
    try:
        if not polo:
            polo = "fibonacci"
        return data['polo'][polo].get('prefix', 'Fib')
    except Exception:
        return 'Fib'

def find_aula_by_id(aula_id: str):
    """Cerca un'aula in tutti i poli basandosi sull'ID."""
    data = load_unified_json()
    if not data or 'polo' not in data:
        return None, None 
        
    for polo_key, polo_data in data['polo'].items():
        edifici = polo_data.get('edificio', {})
        for edificio_key, edificio_data in edifici.items():
            piani = edificio_data.get('piano', {})
            for piano_key, aule in piani.items():
                for aula in aule:
                    if aula.get('id') == aula_id:
                        # Arricchiamo l'oggetto aula con i metadati di posizione
                        aula_full = aula.copy()
                        aula_full['polo'] = polo_key
                        aula_full['edificio'] = edificio_key
                        aula_full['piano'] = piano_key
                        return aula_full, polo_key
    return None, None

# --- HELPERS ---
def find_dove_item(items: List[Dict], aula_nome: str, polo: str = None) -> Optional[Dict]:
    """Trova l'item corrispondente in data.json per l'aula specificata, opzionalmente filtrando per polo."""
    aula_nome_lower = aula_nome.lower()
    msg_logger_prefix = f"[find_dove_item(..., {aula_nome}, {polo})]"
    
    for item in items:
        if item.get("type") == "article":
            item_title = item.get("title", "").lower()
            item_keywords = [k.lower() for k in item.get("keywords", [])]
            
            # Check description for polo match if polo is provided
            item_desc = item.get("description", "").lower()
            if polo and polo.lower() not in item_desc:
                continue

            if aula_nome_lower == item_title or any(aula_nome_lower in k for k in item_keywords):
                return item
    return None

def get_building_thumb(description=None, polo=None, edificio=None):
    unified_data = load_unified_json()
    
    color = DEFAULT_COLOR
    text = ""
    fg_color = "ffffff"
    
    # 1. Parsing Fallback: se mancano polo/edificio, cercali in description
    if description and (not polo or not edificio):
        desc_lower = description.lower()
        for p_key, p_val in unified_data.get('polo', {}).items():
            # Check lasco: se il nome del polo es. "fibonacci" è nella stringa
            if p_key in desc_lower:
                polo = p_key
                
                # Cerchiamo l'edificio
                found_edificio = None
                
                # Prima cerchiamo match espliciti "edificio X"
                for b_key in p_val.get('edificio', {}):
                    if b_key: 
                        # Pattern robusti per intercettare "Edificio A", "Ed. A", ecc.
                        patterns = [f"edificio {b_key}", f"ed. {b_key}", f"ed {b_key}"]
                        is_match = False
                        for pat in patterns:
                            # Verifica che il pattern sia presente. 
                            # Per evitare che "Edificio A" matchi con "Audio", controlliamo i boundary se necessario
                            # Ma "a" è l'ultima lettera o seguita da spazi solitamente
                            if pat in desc_lower:
                                is_match = True
                                break
                        
                        if is_match:
                            found_edificio = b_key
                            break
                
                # Se non trovato match esplicito, controlliamo se esiste l'edificio vuoto (es. Carmignani)
                if found_edificio is None and "" in p_val.get('edificio', {}):
                    found_edificio = ""
                
                edificio = found_edificio
                break

    # 2. Lookup nel JSON
    if polo and polo in unified_data.get('polo', {}):
        edifici_dict = unified_data['polo'][polo].get('edificio', {})
        
        target_item = None
        
        # Check esatto
        if edificio is not None and edificio in edifici_dict:
            target_item = edifici_dict[edificio]
        
        # Check case-insensitive (es. "A" -> "a")
        elif edificio is not None:
             ed_str = str(edificio).strip().lower()
             if ed_str in edifici_dict:
                 target_item = edifici_dict[ed_str]

        if target_item:
            color = target_item.get('color', color)
            text = target_item.get('text', text)
            fg_color = target_item.get('text_foreground', fg_color)
    
    import urllib.parse
    safe_text = urllib.parse.quote(text)
    return f"https://placehold.co/100/{color}/{fg_color}.png?text={safe_text}"

def extract_url_from_markdown(markdown_text):
    try:
        if "](" in markdown_text:
            return markdown_text.split("](")[-1].strip(")")
        return ""
    except Exception:
        return ""

def _extract_surname_display(full_name: str) -> str:
    parts = full_name.split()
    if not parts:
        return ""
    
    # Common particles in Italian/European surnames
    particles = {"del", "della", "de", "di", "lo", "la", "le", "van", "von", "san", "da"}
    
    # Check if first word is a particle
    if len(parts) > 1 and parts[0].lower() in particles:
        return f"{parts[0]} {parts[1]}".upper()
    
    return parts[0].upper()

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
            cognome_display = _extract_surname_display(original_name)
            links.append(f"[{cognome_display}↗]({url})")
        else:
            # Match più robusto: token subset
            found = False
            docente_tokens = set(docente_lower.split())
            
            if docente_tokens:
                for prof_name_lower, (original_name, url) in prof_urls.items():
                    prof_tokens = set(prof_name_lower.split())
                    
                    # 1. Calendar is subset of DB (es. "Del Corso" -> "Gianna Del Corso")
                    if docente_tokens.issubset(prof_tokens):
                        cognome_display = _extract_surname_display(original_name)
                        links.append(f"[{cognome_display}↗]({url})")
                        found = True
                        break
                    
                    # 2. DB is subset of Calendar (es. "Gianna Del Corso" -> "Gianna Maria Del Corso")
                    # Richiediamo almeno 2 token matchati per evitare falsi positivi con cognomi corti o particelle
                    if prof_tokens.issubset(docente_tokens) and len(prof_tokens) >= 2:
                        cognome_display = _extract_surname_display(original_name)
                        links.append(f"[{cognome_display}↗]({url})")
                        found = True
                        break
            
            # Se non trovato, nessun link per questo docente
    
    return {'full_names': full_names, 'links': links}

# --- API CALENDARIO ---
def fetch_day_events(calendar_id: str, day: datetime) -> List[Dict[str, Any]]:
    """Recupera tutti gli eventi per un giorno specifico."""
    if not calendar_id:
        return []
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

async def fetch_day_events_async(calendar_id: str, day: datetime) -> List[Dict[str, Any]]:
    """Wrapper async per evitare blocchi dell'event loop."""
    return await asyncio.to_thread(fetch_day_events, calendar_id, day)

def get_aula_status(aula_nome: str, events: List[Dict], now: datetime, polo: str = "fibonacci") -> Dict:
    """
    Calcola lo stato di un'aula.
    Returns: {
        'is_free': bool,
        'free_until': datetime or None,
        'busy_until': datetime or None,
        'next_events': List[Dict]
    }
    """
    # Genera varianti del nome per strict matching
    # Usa il prefix del polo
    prefix = get_polo_prefix(polo)
    prefix_upper = prefix.upper()
    prefix_cap = prefix.capitalize()
    
    strict_variants = set()
    
    if aula_nome.startswith("Aula "):
        base = aula_nome[5:]  # "A"
        strict_variants.add(f"{prefix_upper} {base}")
        strict_variants.add(f"{prefix_cap} {base}")
        strict_variants.add(base)  # Solo se il codice è esattamente "A", molto raro ma possibile
    elif aula_nome.startswith("Laboratorio "):
        num = aula_nome[12:]
        strict_variants.add(f"{prefix_upper} LAB {num}")
        strict_variants.add(f"{prefix_cap} Lab {num}")
        for template in LAB_CODE_VARIANTS:
            templ = template.strip()
            if templ:
                strict_variants.add(templ.format(num=num))
    else:
        strict_variants.add(f"{prefix_upper} {aula_nome}")
        strict_variants.add(f"{prefix_cap} {aula_nome}")
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

STATUS_ELIGIBLE_TYPES = {'aula', 'laboratorio', 'sala', 'studio', 'biblioteca'}

def _is_status_eligible(room: Dict) -> bool:
    rtype = room.get('type')
    if isinstance(rtype, list):
        return any(t in STATUS_ELIGIBLE_TYPES for t in rtype)
    return rtype in STATUS_ELIGIBLE_TYPES

def get_edifici(polo: str) -> List[str]:
    """Restituisce lista degli edifici per un polo che hanno aule monitorabili."""
    data = load_unified_json()
    try:
        buildings_data = data['polo'][polo]['edificio']
        valid_buildings = []
        for b_name, b_data in buildings_data.items():
            has_rooms = False
            piani = b_data.get('piano', {})
            for rooms in piani.values():
                if any(_is_status_eligible(r) for r in rooms):
                    has_rooms = True
                    break
            
            if has_rooms:
                valid_buildings.append(b_name)
                
        return sorted(valid_buildings)
    except Exception:
        return []

def get_piani(polo: str, edificio: str) -> List[str]:
    """Restituisce lista dei piani per un edificio che hanno aule monitorabili."""
    data = load_unified_json()
    try:
        piani_data = data['polo'][polo]['edificio'][edificio]['piano']
        valid_piani = []
        for piano, rooms in piani_data.items():
             if any(_is_status_eligible(r) for r in rooms):
                valid_piani.append(piano)
        return sorted(valid_piani)
    except Exception:
        return []

def get_aule_edificio(polo: str, edificio: str) -> List[Dict]:
    """Restituisce tutte le aule monitorabili di un edificio."""
    data = load_unified_json()
    aule = []
    try:
        piani = data['polo'][polo]['edificio'][edificio]['piano']
        for piano, aule_piano in piani.items():
            for aula in aule_piano:
                if _is_status_eligible(aula):
                    aula_copy = aula.copy()
                    aula_copy['piano'] = piano
                    aula_copy['edificio'] = edificio
                    aula_copy['polo'] = polo
                    aule.append(aula_copy)
    except Exception:
        pass
    return aule

def get_aule_polo(polo: str) -> List[Dict]:
    """Restituisce tutte le aule di un polo."""
    data = load_unified_json()
    aule = []
    try:
        polo_data = data['polo'][polo]
        polo_nome = polo_data.get('nome', polo.capitalize())
        edifici = polo_data['edificio']
        for edificio, edificio_data in edifici.items():
            for piano, aule_piano in edificio_data['piano'].items():
                for aula in aule_piano:
                    # Se stiamo cercando aule per mapping eventi, vogliamo solo quelle "fisiche" dove si fanno lezioni
                    # Le eligibility le usiamo per i menu di stato, ma qui serve anche
                    if _is_status_eligible(aula):
                        aula_copy = aula.copy()
                        aula_copy['piano'] = piano
                        aula_copy['edificio'] = edificio
                        aula_copy['polo'] = polo
                        aule.append(aula_copy)
    except Exception as e:
        logger.error(f"Error in get_aule_polo({polo}): {e}")
    return aule

def get_all_aule() -> List[Dict]:
    """Restituisce tutte le aule di tutti i poli."""
    data = load_unified_json()
    all_aule = []
    try:
        for polo in data.get('polo', {}):
             all_aule.extend(get_aule_polo(polo))
    except Exception as e:
        logger.error(f"Error in get_all_aule: {e}")
    return all_aule

# --- FORMATTAZIONE MESSAGGI ---
def format_aula_header(aula: Dict) -> str:
    """Formatta l'intestazione standard dell'aula (Nome, Edificio, Piano, Capienza)."""
    nome = aula.get('nome', 'N/D')
    edificio = aula.get('edificio', '').strip()
    piano = aula.get('piano', '?')
    capienza = aula.get('capienza', 'N/D')
    polo = aula.get('polo', 'fibonacci').capitalize()
    
    display_piano = "terra" if str(piano) == "0" else str(piano)
    
    # Rimuovi prefisso "Aula " se già presente per evitare duplicati
    display_nome = nome.upper()
    if display_nome.startswith("AULA "):
        display_nome = display_nome[5:]  # Rimuovi "AULA "
    
    msg = f"*AULA {display_nome}*\n"
    
    # Verifica se mostrare l'edificio
    # Nascondi se: vuoto, '?' (vecchio default), o uguale al nome del polo
    should_show_edificio = True
    if not edificio:
        should_show_edificio = False
    elif edificio == '?':
        should_show_edificio = False
    elif edificio.lower() == polo.lower():
        should_show_edificio = False
        
    if should_show_edificio:
        msg += f"Polo {polo} › Edificio {edificio.upper()} › Piano {display_piano}\n"
    else:
        msg += f"Polo {polo} › Piano {display_piano}\n"
    
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
    polo = aula.get('polo', 'fibonacci')
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
    polo_display = polo.capitalize()
    
    if not edificio or edificio == '?' or edificio.lower() == polo.lower():
        msg = f"*Polo {polo_display}*\n"
    else:
        msg = f"*Edificio {edificio.upper()} - Polo {polo_display}*\n"
    
    msg += f"Stato aule alle {now.strftime('%H:%M')} del {now.strftime('%d/%m')}\n\n"
    
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
            status = get_aula_status(aula['nome'], events, now, polo=polo)
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
    polo_display = polo.capitalize()
    
    if not edificio or edificio == '?' or edificio.lower() == polo.lower():
         msg = f"*Polo {polo_display} - Piano {piano}*\n"
    else:
         msg = f"*Polo {polo_display} - Edificio {edificio.upper()} - Piano {piano}*\n"

    msg += f"Stato alle {now.strftime('%H:%M')} del {now.strftime('%d/%m')}\n\n"
    
    for aula in aule:
        status = get_aula_status(aula['nome'], events, now, polo=polo)
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
    polo_display = polo.capitalize()
    msg = f"*Polo {polo_display}*\n"
    msg += f"Stato aule alle {now.strftime('%H:%M')} del {now.strftime('%d/%m')}\n\n"
    
    edifici = get_edifici(polo)
    for edificio in edifici:
        if edificio and edificio != '?' and edificio.lower() != polo.lower():
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
                status = get_aula_status(aula['nome'], events, now, polo=polo)
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

def format_day_schedule(aula: Dict, events: List[Dict], target_date: datetime, show_title: bool = True) -> str:
    """Formatta il programma di una giornata specifica."""
    # Formato per giorni futuri/passati: Header + Programma
    text = format_aula_header(aula) + "\n"
    
    if show_title:
        # Formato per giorni futuri/passati: Header + Programma
        day_caps = WEEKDAYS_SHORT[target_date.weekday()]
        text += f"PROGRAMMA {day_caps} {target_date.strftime('%d/%m')}\n\n"
    
    # Recupera eventi del giorno
    start_of_day = target_date.replace(hour=0, minute=0, second=0)
    status_day = get_aula_status(aula['nome'], events, start_of_day, polo=aula.get('polo', 'fibonacci'))
    
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
    dove_url = aula.get('link-dove-unipi')
    
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
            await asyncio.to_thread(requests.get, url, timeout=5)
        except Exception:
            pass

# --- FEEDBACK TEXT ---
FEEDBACK_TEXT = (
    "\n\n<b>Feedback e Supporto</b>\n"
    "Hai suggerimenti o vuoi segnalare un bug?\n"
    "Invia una mail: <code>lyubomyr.malay@gmail.com</code>\n"
    "<a href='https://github.com/plumkewe/dove-unipi/issues'>Apri una issue su GitHub</a>\n"
    "Scrivici su <a href='https://www.instagram.com/doveunipi/'>Instagram</a>"
)

# --- COMANDI STANDARD ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>DOVE?UNIPI</b>\n\n"
        "Trova aule e uffici dei professori dell'Universita di Pisa.\n\n"
        "<b>Ricerca Inline</b>\n"
        "In qualsiasi chat, digita:\n"
        "<code>@doveunipibot nome aula o cognome professore</code>\n\n"
        "<b>Mappe e Filtri</b>\n"
        "• <b>Mappa Polo:</b> <code>@doveunipibot [nome polo]</code>\n"
        "• <b>Filtra per Polo:</b> <code>@doveunipibot [aula] +[polo]</code>\n"
        "(es. <code>@doveunipibot A +fib</code>)\n\n"
        "<b>Cerca Lezione</b>\n"
        "Per cercare una lezione per materia:\n"
        "<code>@doveunipibot l:nome materia</code>\n"
        "Supporta anche giorni successivi (+1, +2...)\n\n"
        "<b>Cerca Professore</b>\n"
        "Per cercare un professore e le sue lezioni (usare solo il cognome):\n"
        "<code>@doveunipibot p:cognome</code>\n\n"
        "<b>Stato Aula</b>\n"
        "Per vedere lo stato di un'aula:\n"
        "<code>@doveunipibot s:nome aula</code>\n"
        "Puoi aggiungere <code>+1</code>, <code>+2</code>... per i giorni successivi.\n\n"
        "<b>Stato Aula Interattivo</b>\n"
        "Per vedere lo stato con navigazione giorni:\n"
        "<code>@doveunipibot si:nome aula</code>\n\n"
        "<b>Comandi</b>\n"
        "/occupazione - Stato aule\n"
        "/links - Link utili\n"
        "/help - Guida all'uso" +
        FEEDBACK_TEXT
    )
    
    keyboard = [
        [InlineKeyboardButton("Cerca aula", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("Cerca lezione", switch_inline_query_current_chat="l: ")],
        [InlineKeyboardButton("Cerca professore", switch_inline_query_current_chat="p:")],
        [InlineKeyboardButton("Stato aula", switch_inline_query_current_chat="s:")],
        [InlineKeyboardButton("Stato interattivo", switch_inline_query_current_chat="si:")],
        [InlineKeyboardButton("Occupazione", callback_data="status:init")]
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

    keyboard = build_polo_keyboard("status:polo:")
    
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
    
    # Load polos dynamically
    data = load_unified_json()
    polo_lines = []
    if data and 'polo' in data:
        for p_key, p_val in data['polo'].items():
             name = p_val.get('nome', p_key.capitalize())
             prefix = p_val.get('prefix', '')
             # Show the +param suggestion if prefix exists
             param_hint = f" (+{prefix.lower()})" if prefix else ""
             polo_lines.append(f"• <b>{name}</b>{param_hint}")
            
    polo_list_text = "\n".join(polo_lines) if polo_lines else "• Fibonacci (+fib)\n• Carmignani (+car)"

    text = (
        "<b>GUIDA ALL'USO</b>\n\n"
        "<b>Comandi Principali</b>\n"
        "/start - Avvia il bot e mostra il benvenuto\n"
        "/occupazione - Mostra lo stato delle aule navigando per edifici\n"
        "/links - Link utili (GitHub, Sito, Social)\n"
        "/help - Mostra questo messaggio\n\n"
        "<b>1. Ricerca Inline</b>\n"
        "Puoi cercare <b>Aule, Biblioteche e Uffici dei professori</b> (per cognome o numero) direttamente in qualsiasi chat.\n\n"
        "Digita il nome del bot seguito dalla ricerca:\n"
        "Esempio:\n"
        "<code>@doveunipibot Rossi</code>\n"
        "Output:\n"
        "<pre>Polo Fibonacci › Edificio A › Piano 1 › Stanza 21 › Rossi\nClicca per aprire su DOVE?UNIPI↗</pre>\n\n"
        "<b>2. Mappe e Filtri Poli</b>\n"
        "Puoi visualizzare la mappa di un polo o filtrare i risultati.\n"
        "<b>Mappa:</b> <code>@doveunipibot [nome polo]</code>\n"
        "<b>Filtro:</b> <code>@doveunipibot Aula +[codice]</code>\n"
        "Esempio: <code>@doveunipibot A +fib</code>\n\n"
        "<b>Poli Supportati:</b>\n"
        f"{polo_list_text}\n\n"
        "<b>3. Verifica Stato Aula</b>\n"
        "Vedi se un'aula è libera o occupata:\n"
        "<code>@doveunipibot s:F</code>\n"
        "Per vedere i giorni successivi, aggiungi un numero:\n"
        "<code>@doveunipibot s:F +1</code> (domani)\n\n"
        "<b>4. Stato con Navigazione</b>\n"
        "Vedi lo stato con i tasti per cambiare giorno:\n"
        "<code>@doveunipibot si:C</code>\n\n"
        "<b>5. Ricerca Lezione</b>\n"
        "Cerca dove si svolge una lezione:\n"
        "<code>@doveunipibot l:Analisi</code>\n"
        "<i>Se non ci sono lezioni oggi, cercherà automaticamente nei prossimi 7 giorni.</i>\n"
        "Per domani: <code>@doveunipibot l:Analisi +1</code>\n\n"
        "<b>6. Cerca Professore</b>\n"
        "Cerca un professore per cognome e vedi le sue lezioni dei prossimi 7 giorni:\n"
        "<code>@doveunipibot p:Rossi</code>\n"
        "<i>Inserisci solo il cognome per la ricerca.</i>\n\n"
        "<b>Pulsanti e Navigazione</b>\n"
        "<b>○</b>: Indietro / Menu Superiore\n"
        "<b>↺</b>: Aggiorna dati correnti\n"
        "<b>◀ ▶</b>: Cambia pagina o giorno\n\n"
        "I pulsanti si trovano sempre nella stessa posizione (es. 'Indietro' è sempre al centro, 'Aggiorna' sempre a destra).\n\n"
        "<b>Colori</b>\n"
        "I colori degli edifici e dello stato delle aule corrispondono esattamente a quelli visibili su DOVE?UNIPI, per un'esperienza visiva coerente." +
        FEEDBACK_TEXT
    )
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


def get_occupazione_aula_keyboard(aula_id: str, offset: int, parent_callback: str = None) -> InlineKeyboardMarkup:
    """Crea la tastiera per navigare tra i giorni (versione /occupazione: avanti/indietro + smart back)."""
    row = []
    
    # Left Button: Always Back (Day - 1)
    row.append(InlineKeyboardButton("◀", callback_data=f"status:day_offset:{aula_id}:{offset-1}"))
    
    # Center Smart Button (Back to Parent if today, or Today if not today)
    if offset != 0:
         # Back to Today
        row.append(InlineKeyboardButton("○", callback_data=f"status:day_offset:{aula_id}:0"))
    else:
        # Back to Parent or simple refresh if no parent
        if parent_callback:
             row.append(InlineKeyboardButton("○", callback_data=parent_callback))
        else:
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


def get_day_navigation_keyboard(aula_id: str, offset: int) -> InlineKeyboardMarkup:
    """Crea la tastiera per navigare tra i giorni (versione si: completa)."""
    row = []
    
    # Bottone Indietro
    row.append(InlineKeyboardButton("◀", callback_data=f"status:si_offset:{aula_id}:{offset-1}"))
    
    # Bottone Oggi
    row.append(InlineKeyboardButton("○", callback_data=f"status:si_offset:{aula_id}:0"))
        
    # Bottone Avanti
    row.append(InlineKeyboardButton("▶", callback_data=f"status:si_offset:{aula_id}:{offset+1}"))
    
    # Bottone Aggiorna (solo simbolo) su riga separata, allineato a destra
    row_refresh = [
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton("↺", callback_data=f"status:si_offset:{aula_id}:{offset}")
    ]
    
    return InlineKeyboardMarkup([row, row_refresh])

def get_smart_back_keyboard(offset: int, parent_callback: str, current_callback_base: str) -> InlineKeyboardMarkup:
    """Crea la tastiera per navigazione 'Tutti' (solo avanti, back smart)."""
    row_nav = []
    
    # Left placeholder (no navigating back to past)
    row_nav.append(InlineKeyboardButton(" ", callback_data="status:noop"))
    
    # Smart Circle Button (Middle)
    if offset > 0:
        # Back to Today
        row_nav.append(InlineKeyboardButton("○", callback_data=f"{current_callback_base}:0"))
    else:
        # Back to Parent
        row_nav.append(InlineKeyboardButton("○", callback_data=parent_callback))
    
    # Forward Button (Next Day)
    row_nav.append(InlineKeyboardButton("▶", callback_data=f"{current_callback_base}:{offset+1}"))
    
    row_refresh = [
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton("↺", callback_data=f"{current_callback_base}:{offset}")
    ]

    return InlineKeyboardMarkup([row_nav, row_refresh])


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

    # status:init - Menu iniziale (Nuovo Messaggio)
    if action == "init":
        text = "*Stato Aule*\n\nSeleziona un polo:"
        keyboard = build_polo_keyboard("status:polo:")
        
        await query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # status:start - Menu iniziale (Edit Messaggio)
    if action == "start":
        text = "*Stato Aule*\n\nSeleziona un polo:"
        keyboard = build_polo_keyboard("status:polo:")
        
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    # status:polo:<polo> - Menu edifici del polo
    elif action == "polo":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edifici = get_edifici(polo)
        
        # Se c'è un solo edificio, saltiamo direttamente al menu dei piani (o a quello che farebbe status:edificio)
        if len(edifici) == 1:
            edificio = edifici[0]
            # Chiamiamo direttamente la logica per mostrare i piani di quell'edificio
            # Dobbiamo creare una funzione o chiamare show_edificio_piani_menu
            await show_edificio_piani_menu(query, polo, edificio, parent_callback="status:start")
            return

        text = f"*Polo {polo.capitalize()}*\n\nSeleziona un edificio:"
        
        keyboard = [
            [InlineKeyboardButton("TUTTI", callback_data=f"status:tutti_polo:{polo}")]
        ]
        
        # Bottoni edifici (2 per riga)
        row = []
        for i, edificio in enumerate(edifici):
            display_name = f"Edificio {edificio.upper()}"
            # Se il nome edificio è uguale al nome polo (case insensitive), mostriamo "Edificio Unico" o simile,
            # ma qui siamo nel blocco else (più edifici), quindi probabilmente non succederà spesso per Carmignani.
            
            row.append(InlineKeyboardButton(
                display_name, 
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
    
    # status:tutti_polo:<polo>:<offset> - Stato tutte le aule
    elif action == "tutti_polo":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        try:
            offset = int(parts[3]) if len(parts) > 3 else 0
        except Exception:
            offset = 0
            
        target_date = now + timedelta(days=offset)
        if offset != 0:
            target_date = target_date.replace(hour=0, minute=1, second=0, microsecond=0)
        
        # Carica eventi
        events = await fetch_day_events_async(get_calendar_id(polo), target_date)
        
        text = format_polo_status(polo, events, target_date)
        
        # Messaggio potrebbe essere troppo lungo, dividiamolo se necessario
        if len(text) > 4000:
            text = text[:3900] + "\n\n_...messaggio troncato_"
        
        keyboard = get_smart_back_keyboard(offset, f"status:polo:{polo}", f"status:tutti_polo:{polo}")
        
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    # status:day_offset:<aula_id>:<offset> - Cambio giorno
    elif action in ["day_offset", "si_offset"]:
        aula_id = parts[2] if len(parts) > 2 else ""
        try:
            offset = int(parts[3]) if len(parts) > 3 else 0
        except Exception:
            offset = 0
            
        # Trova l'aula usando id univoco
        aula, polo = find_aula_by_id(aula_id)
        
        if not aula:
            await query.answer("Aula non trovata", show_alert=True)
            return

        # Calcola data target
        target_date = datetime.now(TZ_ROME) + timedelta(days=offset)
        
        # Fetch eventi per QUELLA data
        events = await fetch_day_events_async(get_calendar_id(polo), target_date)
        status = get_aula_status(aula['nome'], events, target_date, polo=polo)
        
        # Formatta messaggio per il giorno specifico
        # Se offset == 0 usa formato standard, altrimenti formato programma
        if offset == 0:
            # Trova URL per link DOVE?UNIPI
            dove_url = aula.get('link-dove-unipi')
            
            text = format_single_aula_status(aula, status, target_date, dove_url)
        else:
            # Usa il nuovo helper
            text = format_day_schedule(aula, events, target_date)
        
        if action == "day_offset":
            # Determine parent callback for Smart Back
            # polo is already set
            edificio = aula.get('edificio', 'a').lower()
            piano = aula.get('piano', '0')
            parent_callback = f"status:piano:{polo}:{edificio}:{piano}"
            
            keyboard = get_occupazione_aula_keyboard(aula_id, offset, parent_callback)
        else: # si_offset
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

    # status:tutti_edificio:<polo>:<edificio>:<offset> - Stato tutte le aule edificio
    elif action == "tutti_edificio":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        try:
            offset = int(parts[4]) if len(parts) > 4 else 0
        except Exception:
            offset = 0
            
        target_date = now + timedelta(days=offset)
        if offset != 0:
            target_date = target_date.replace(hour=0, minute=1, second=0, microsecond=0)
        
        events = await fetch_day_events_async(get_calendar_id(polo), target_date)
        text = format_edificio_status(polo, edificio, events, target_date)
        
        if len(text) > 4000:
            text = text[:3900] + "\n\n_...messaggio troncato_"
        
        keyboard = get_smart_back_keyboard(offset, f"status:edificio:{polo}:{edificio}", f"status:tutti_edificio:{polo}:{edificio}")
        
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    # status:tutti_piano:<polo>:<edificio>:<piano>:<offset> - Stato tutte le aule di un piano
    elif action == "tutti_piano":
        polo = parts[2] if len(parts) > 2 else "fibonacci"
        edificio = parts[3] if len(parts) > 3 else "a"
        piano = parts[4] if len(parts) > 4 else "0"
        try:
            offset = int(parts[5]) if len(parts) > 5 else 0
        except Exception:
            offset = 0
            
        target_date = now + timedelta(days=offset)
        if offset != 0:
            target_date = target_date.replace(hour=0, minute=1, second=0, microsecond=0)
        
        events = await fetch_day_events_async(get_calendar_id(polo), target_date)
        text = format_piano_status(polo, edificio, piano, events, target_date)
        
        if len(text) > 4000:
            text = text[:3900] + "\n\n_...messaggio troncato_"
        
        keyboard = get_smart_back_keyboard(offset, f"status:piano:{polo}:{edificio}:{piano}", f"status:tutti_piano:{polo}:{edificio}:{piano}")
        
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
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
        
        events = await fetch_day_events_async(get_calendar_id(polo), now)
        status = get_aula_status(aula['nome'], events, now)
        
        # Trova URL per link DOVE?UNIPI
        dove_url = aula.get('link-dove-unipi')
        
        text = format_single_aula_status(aula, status, now, dove_url)
        
        # Use navigation keyboard with offset 0 and parent pointer
        parent_callback = f"status:piano:{polo}:{edificio}:{piano}"
        keyboard = get_occupazione_aula_keyboard(aula_id, 0, parent_callback)
        
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

async def show_edificio_piani_menu(query, polo: str, edificio: str, parent_callback: str = None):
    """Mostra il menu dei piani di un edificio."""
    piani = get_piani(polo, edificio)
    
    # Determina il parent corretto
    if parent_callback is None:
        edifici_polo = get_edifici(polo)
        if len(edifici_polo) == 1:
            # Se c'è un solo edificio, il menu edifici è saltato, quindi torniamo alla scelta del polo
            parent_callback = "status:start"
        else:
            # Altrimenti torniamo alla lista edifici
            parent_callback = f"status:polo:{polo}"

    # Se c'è un solo piano, mostra direttamente le aule (passando il parent corretto per tornare indietro)
    if len(piani) == 1:
        # Nota: show_piano_aule_menu dovrà essere aggiornata per accettare parent custom o gestirlo
        # Al momento show_piano_aule_menu ha hardcoded "status:edificio:..." come back se non specificato
        # Ma show_piano_aule_menu costruisce il back button dinamicamente? 
        # Vediamo show_piano_aule_menu...
        await show_piano_aule_menu(query, polo, edificio, piani[0], 0, parent_callback=parent_callback)
        return
    
    if not edificio or edificio == '?' or normalize_short_code(polo) == normalize_short_code(edificio):
        text = f"*Polo {polo.capitalize()}*\n\nSeleziona un piano:"
    else:
        text = f"*Polo {polo.capitalize()} - Edificio {edificio.upper()}*\n\nSeleziona un piano:"
    
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
                     InlineKeyboardButton("○", callback_data=parent_callback),
                     InlineKeyboardButton(" ", callback_data="status:noop")])
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def show_piano_aule_menu(query, polo: str, edificio: str, piano: str, page: int, parent_callback: str = None):
    """Mostra il menu delle aule di un piano con paginazione."""
    aule = get_aule_edificio(polo, edificio)
    # Filtra per piano
    aule = [a for a in aule if a.get('piano') == piano]
    total_aule = len(aule)
    total_pages = max(1, (total_aule + AULE_PER_PAGE - 1) // AULE_PER_PAGE)
    
    # Assicurati che la pagina sia valida
    page = max(0, min(page, total_pages - 1))
    
    if not edificio or edificio == '?' or normalize_short_code(polo) == normalize_short_code(edificio):
        text = f"*Polo {polo.capitalize()} - Piano {piano}*\n\n"
    else:
        text = f"*Polo {polo.capitalize()} - Edificio {edificio.upper()} - Piano {piano}*\n\n"
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
    
    # Calcola back data
    if parent_callback:
        back_data = parent_callback
    else:
        # Calcolo dinamico del percorso indietro corretto
        piani_edificio = get_piani(polo, edificio)
        edifici_polo = get_edifici(polo)
        
        # Logica:
        # 1. Se l'edificio ha un solo piano, il menu piani è stato saltato.
        #    Dobbiamo tornare a dove puntava il menu piani (Polo o Start).
        if len(piani_edificio) == 1:
            if len(edifici_polo) == 1:
                # Caso Carmignani (se avesse 1 piano): Start -> [Skip Polo] -> [Skip Edificio] -> Aule
                back_data = "status:start"
            else:
                # Caso Edificio unico piano in Polo multi-edificio: Start -> Polo -> [Skip Edificio] -> Aule
                back_data = f"status:polo:{polo}"
        else:
            # 2. Se l'edificio ha più piani, il menu piani è stato mostrato.
            #    Torniamo alla lista dei piani.
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

# --- INLINE QUERY ---
async def handle_polo_map_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce messaggi di testo per mostrare mappe dei poli/edifici."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower().strip()
    data = load_unified_json()
    
    found_polo = None
    
    for polo_key, polo_data in data.get("polo", {}).items():
        keywords = [polo_key]
        nome_display = polo_data.get("nome", polo_key.capitalize())
        keywords.append(nome_display.lower())
        
        if "alias" in polo_data:
            keywords.extend([a.lower() for a in polo_data["alias"]])

        is_mappa_explicit = "mappa" in text
        
        match = False
        for kw in keywords:
            kw_clean = kw.replace("polo", "").strip()
            
            if is_mappa_explicit:
                if kw_clean in text:
                    match = True
                    break
            else:
                text_clean = text.replace("polo", "").strip()
                if text_clean == kw_clean:
                    match = True
                    break
        
        if match:
            found_polo = (polo_key, polo_data)
            break
    
    if found_polo:
        polo_key, polo_data = found_polo
        mappa_file = polo_data.get("mappa")
        
        if mappa_file:
            img_path = os.path.join(BASE_DIR, "assets", "img", "mappe", mappa_file)
            
            if os.path.exists(img_path):
                polo_name = polo_data.get("nome", polo_key.capitalize())
                if not polo_data.get("nome"):
                    polo_name = "Polo " + polo_key.capitalize() if not polo_key.lower().startswith("polo") else polo_key.capitalize()

                gmaps = polo_data.get("google_maps", "")
                amaps = polo_data.get("apple_maps", "")
                
                caption = f"*{polo_name}*\n"
                links_parts = []
                if gmaps:
                    links_parts.append(f"[Google Maps]({gmaps}) ↗")
                if amaps:
                    links_parts.append(f"[Apple Maps]({amaps}) ↗")
                
                if links_parts:
                    caption += "  ".join(links_parts)
                
                await update.message.reply_photo(
                    photo=open(img_path, 'rb'),
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )

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

    # GESTIONE si: PER STATUS AULA INTERATTIVO (con giorni)
    if query.startswith("si:"):
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

    # GESTIONE p: PER RICERCA PROFESSORI
    if query.startswith("p:"):
        prof_search = query[2:].strip()
        if prof_search:
            results = await search_professor_inline(prof_search)
            if len(results) == 0:
                no_results_button = InlineQueryResultsButton(text="Nessun risultato. Usa solo il cognome!", start_parameter="empty")
                await update.inline_query.answer(results, cache_time=0, button=no_results_button)
            else:
                await update.inline_query.answer(results[:20], cache_time=0)
        else:
            # Query vuota
            search_button = InlineQueryResultsButton(text="Cerca un professore", start_parameter="empty")
            await update.inline_query.answer([], cache_time=0, button=search_button)
        return

    # --- LOGICA DI RICERCA GENERALE ---
    
    # 1. RISORSE SPECIALI (LINKS)
    special_links = [
        {
            "id": "special_github",
            "type": "article",
            "title": "GitHub Repository",
            "description": "Mettici una stella!",
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

    # SE LA QUERY È VUOTA: Istruzioni -> Mappa -> Link
    if not query:
        # A. Istruzioni (Mini-guida)
        instructions = [
            {
                "id": "inst_inline",
                "title": "Ricerca Inline",
                "desc": "<nome> (es. A1, Rossi, Biblioteca)",
                "text": "@doveunipibot "
            },
            {
                "id": "inst_s",
                "title": "Stato Aula",
                "desc": "s:<aula> (es. s:B, s:N1 +1)",
                "text": "@doveunipibot s: "
            },
            {
                "id": "inst_si",
                "title": "Stato Interattivo",
                "desc": "si:<aula> (es. si:C, si:A1)",
                "text": "@doveunipibot si: "
            },
            {
                "id": "inst_l",
                "title": "Cerca Lezione",
                "desc": "l:<materia> (es. l:Analisi)",
                "text": "@doveunipibot l: "
            },
            {
                "id": "inst_p",
                "title": "Cerca Professore",
                "desc": "p:<cognome> (es. p:Rossi)",
                "text": "@doveunipibot p: "
            },
            {
                "id": "inst_filter",
                "title": "Filtra per Polo",
                "desc": "<query> +<polo> (es. A +fib, Aula 1 +car)",
                "text": "@doveunipibot +fib "
            },
            {
                "id": "inst_map",
                "title": "Mappe dei Poli",
                "desc": "Scrivi il nome del polo (es. fibonacci)",
                "text": "@doveunipibot "
            }
        ]
        
        for inst in instructions:
            results.append(
                InlineQueryResultArticle(
                    id=inst["id"],
                    title=inst["title"],
                    description=inst["desc"],
                    input_message_content=InputTextMessageContent(
                        message_text=inst["text"],
                        parse_mode=ParseMode.MARKDOWN
                    ),
                    thumbnail_url=INFO_ICON_URL,
                    thumbnail_width=100, 
                    thumbnail_height=100
                )
            )


            
        # C. Link
        for link in special_links:
            results.append(
                InlineQueryResultArticle(
                    id=link["id"],
                    title=link["title"],
                    description=link["description"],
                    input_message_content=InputTextMessageContent(
                        message_text=link['url'],
                        disable_web_page_preview=False
                    ),
                    thumbnail_url=link["thumb"],
                    thumbnail_width=100, 
                    thumbnail_height=100
                )
            )

    # SE LA QUERY NON È VUOTA: Cerca tra Mappa, Link e Aule
    else:
        # 0. CERCA MAPPE POLI (Dynamic from unified.json)
        unified_data = load_unified_json()
        
        # --- PARSING PARAMETRO POLO (+param) ---
        polo_filter = None
        invalid_polo_param = None
        
        if "+" in query:
            parts = query.rsplit("+", 1)
            if len(parts) == 2:
                param_cand = parts[1].strip().lower()
                
                if param_cand:
                    query_candidate = parts[0].strip()
                    
                    # Costruisci mappa dei poli validi
                    valid_polos = {}
                    
                    for p_key, p_val in unified_data.get("polo", {}).items():
                        valid_polos[p_key] = p_key
                        if "prefix" in p_val:
                            pref = p_val["prefix"].lower()
                            valid_polos[pref] = p_key
                    
                    # Alias comuni extra
                    extra_aliases = {"fib": "fibonacci", "car": "carmignani", "carm": "carmignani"}
                    valid_polos.update(extra_aliases)
                    
                    if param_cand in valid_polos:
                        polo_filter = valid_polos[param_cand]
                        query = query_candidate # Aggiorna la query usata per cercare
                    else:
                        # Parametro sconosciuto
                        invalid_polo_param = param_cand
                        short_hints = set()
                        for p_data in unified_data.get("polo", {}).values():
                             if "prefix" in p_data:
                                 short_hints.add(f"+{p_data['prefix'].lower()}")
                        valid_hints_str = ", ".join(sorted(list(short_hints)))

        # SE PARAMETRO INVALIDO: Restituisci nessun risultato
        if invalid_polo_param:
             no_results_button = InlineQueryResultsButton(text="Nessun risultato trovato", start_parameter="empty")
             await update.inline_query.answer([], cache_time=0, button=no_results_button)
             return

        # Base URL per le immagini pubbliche (richiesto per InlineQueryResultPhoto).
        # Default: GitHub raw sul branch main. Override con env `MAPS_RAW_BASE_URL`.
        raw_repo_base = os.environ.get(
            "MAPS_RAW_BASE_URL",
            "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/img/mappe/",
        )
        
        for polo_key, polo_data in unified_data.get("polo", {}).items():
            if polo_filter and polo_key != polo_filter:
                continue
            
            mappa_file = polo_data.get("mappa")
            if not mappa_file:
                continue
                
            keywords = [polo_key]
            nome_display = polo_data.get("nome", polo_key.capitalize())
            keywords.append(nome_display.lower())
            
            if "alias" in polo_data:
                keywords.extend([a.lower() for a in polo_data["alias"]])
            
            # Logic MATCH per Inline
            match_polo = False
            
            # MOSTRA TUTTE LE MAPPE SE UTENTE CERCA SOLO "Mappa"
            if query in ["mappa", "mappe", "map", "maps"]:
                match_polo = True

            # Se query contiene "mappa" e il nome del polo
            elif "mappa" in query:
                for kw in keywords:
                    kw_clean = kw.replace("polo", "").strip()
                    if kw_clean in query:
                        match_polo = True
                        break
            else:
                # Se query scatta un match diretto con il nome o alias del polo (ignora "polo " nella query)
                query_clean = query.replace("polo", "").strip()
                for kw in keywords:
                    kw_clean = kw.replace("polo", "").strip()
                    if query_clean == kw_clean or query == kw or (kw_clean in query and len(query) < len(kw_clean) + 5):
                        match_polo = True
                        break

            if match_polo:
                # Costruisci caption
                polo_name_cap = polo_data.get("nome", polo_key.capitalize())
                if not polo_data.get("nome"):
                     polo_name_cap = "Polo " + polo_key.capitalize() if not polo_key.lower().startswith("polo") else polo_key.capitalize()

                address = polo_data.get("address", "")
                gmaps = polo_data.get("google_maps", "")
                amaps = polo_data.get("apple_maps", "")
                
                # Format: Nome Polo \n Indirizzo \n Link
                caption = f"*{polo_name_cap}*\n"
                if address:
                    caption += f"{address}\n\n"
                
                links_parts = []
                if gmaps:
                    links_parts.append(f"[Google Maps↗]({gmaps})")
                if amaps:
                    links_parts.append(f"[Apple Maps↗]({amaps})")
                
                if links_parts:
                    caption += "  ".join(links_parts)

                # Rimosso timestamp per evitare problemi di cache/caricamento con GitHub Raw
                # Assicuriamoci che l'URL non abbia spazi o caratteri strani
                safe_filename = urllib.parse.quote(mappa_file)
                # Add timestamp to bust telegram cache if image changed or was broken
                ts_buster = int(time.time())
                photo_u = f"{raw_repo_base}{safe_filename}?v={ts_buster}"
                
                logger.info(f"Generated Map URL: {photo_u}")
                
                # FIX: Telegram richiede un extension valida nell'URL per le foto se non è chiara
                # Aggiungiamo un dummy param alla fine se serve, ma github raw finisce con .png di solito.
                
                results.append(
                    InlineQueryResultPhoto(
                        id=f"map_{polo_key}",
                        photo_url=photo_u,
                        thumbnail_url=photo_u, 
                        title=f"Mappa {polo_name_cap}",
                        description="Visualizza mappa e link",
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN
                    )
                )

        # B. Cerca Link
        for link in special_links:
            # Match keywords più stringente
            # La query deve coincidere con una keyword oppure contenere la keyword delimitata (parola esatta)
            # Ma per semplicità: check if keyword IS in query.
            # Fix "ig" in "carmignani":
            # "ig" è troppo corto per essere matchato come sottostringa generica se non è delimitata.
            
            should_add = False
            for k in link["keywords"]:
                if len(k) < 3:
                     # Key corte (es. 'ig', 'git'): match esatto o delimitato da spazi
                     # Se query è "ig" -> ok
                     # Se query è "sito ig" -> ok
                     # Se query è "carmignani" -> NO (anche se contiene 'ig')
                     padded_query = f" {query} "
                     if f" {k} " in padded_query:
                         should_add = True
                         break
                else:
                    # Key lunghe: va bene sottostringa
                    if k in query:
                         should_add = True
                         break
            
            if should_add:
                results.append(
                    InlineQueryResultArticle(
                        id=link["id"],
                        title=link["title"],
                        description=link["description"],
                        input_message_content=InputTextMessageContent(
                            message_text=link['url'],
                            disable_web_page_preview=False
                        ),
                        thumbnail_url=link["thumb"],
                        thumbnail_width=100, 
                        thumbnail_height=100
                    )
                )

        # C. Ricerca Aule
        items = get_data()
        for item in items:
            if item.get("type") == "article":
                title = item.get("title", "")
                description = item.get("description", "")
                
                # APPLY POLO FILTER
                if polo_filter and polo_filter not in description.lower():
                    continue

                keywords = item.get("keywords", [])
    
                found_keyword = False
                if isinstance(keywords, list):
                    found_keyword = any(query in k.lower() for k in keywords)
                
                # Controllo match
                if (query in title.lower()) or found_keyword:
                    
                    raw_input = item.get("input_message_content", {})
                    raw_text = raw_input.get("message_text", "")
                    parse_mode = raw_input.get("parse_mode", "Markdown")
                    url = extract_url_from_markdown(raw_text)
                    
                    # PULIZIA LINK VECCHIO e AGGIUNTA FOOTER
                    if url:
                        clean_desc = description.split("\n")[0].strip()
                        final_text = f"{clean_desc} › {title}\n\nClicca per aprire su [DOVE?UNIPI↗]({url})"
                    else:
                        final_text = raw_text
                    
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
        if len(results) > 0:
            def sort_key(result):
                result_title = getattr(result, 'title', '').lower()
                result_description = getattr(result, 'description', '').lower()
                result_id = getattr(result, 'id', '')
                
                # Id risultati speciali hanno priorità
                if result_id.startswith("special_"):
                    return (-1, result_title)

                # Mappe poli (dinamiche) subito dopo i risultati speciali
                if result_id.startswith("map_"):
                    return (-0.5, result_title)
                
                # Per le aule, cerchiamo nelle keywords (che fungono da alias)
                keywords = []
                for item in items:
                    if item.get("id") == result.id and item.get("type") == "article":
                        keywords = [k.lower() for k in item.get("keywords", [])]
                        break
                
                is_professor = "stanza" in result_description
                
                # Match esatto
                title_exact = (result_title == query) or (result_title == f"aula {query}")
                keywords_exact = any(k == query for k in keywords)
                is_exact_match = title_exact or keywords_exact
                
                # Match starts with
                title_starts = result_title.startswith(query) or result_title.startswith(f"aula {query}")
                keywords_start = any(k.startswith(query) for k in keywords)
                starts_with = title_starts or keywords_start
                
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

            # Se la query matcha un polo, la mappa potrebbe essere tagliata fuori dal limit (50).
            # Portiamo sempre i risultati map_* in cima, mantenendo l'ordine già ordinato.
            if query:
                map_results = []
                other_results = []
                for r in results:
                    rid = getattr(r, 'id', '')
                    if isinstance(rid, str) and rid.startswith('map_'):
                        map_results.append(r)
                    else:
                        other_results.append(r)
                if map_results:
                    results = map_results + other_results
                    logger.info(f"InlineQuery: Found {len(map_results)} maps for query '{query}'. Top: {map_results[0].id}")

    # Mostra messaggio "nessun risultato" se la ricerca non trova nulla
    if len(results) == 0:
        logger.info(f"InlineQuery: No results for '{query}'")
        no_results_button = None
        if query:
            no_results_button = InlineQueryResultsButton(text="Nessun risultato trovato", start_parameter="empty")
        
        await update.inline_query.answer(results, cache_time=0, button=no_results_button)
    else:
        # Se query vuota (menu default), mostra tutto. Se ricerca, max 50.
        limit = 50 if query else 20
        await update.inline_query.answer(results[:limit], cache_time=0)


async def search_aula_status_inline(aula_search: str, interactive: bool = False) -> list:
    """Cerca un'aula e restituisce il suo status come risultato inline. Se interactive=True, aggiunge tastiera giorni."""
    results = []
    
    # Parse query modifiers (+1, +fib, etc.)
    parsed = parse_query_modifiers(aula_search)
    offset = parsed['offset']
    filter_polo = parsed['polo_filter']
    aula_search = parsed['clean_query']
    
    now = datetime.now(TZ_ROME)
    target_date = now + timedelta(days=offset)
    
    # Se offset > 0 fetchiamo eventi di quel giorno invece che oggi
    # Fetch events for ALL polos
    events_by_polo = {}
    unified_data = load_unified_json()
    polos = unified_data.get('polo', {}).keys()
    
    for polo in polos:
        cid = get_calendar_id(polo)
        if offset > 0:
            events_by_polo[polo] = await fetch_day_events_async(cid, target_date)
        else:
            events_by_polo[polo] = await fetch_day_events_async(cid, now)
    
    # Cerca in tutte le aule di tutti i poli (Updated)
    aule = get_all_aule()
    
    # Trova anche il risultato normale dalla ricerca standard
    items = get_data()
    
    # Prima raccogli tutte le aule che matchano con il loro punteggio di priorità
    matched_aule = []
    for aula in aule:
        # FILTER: Se c'è un filtro polo e l'aula non corrisponde, salta
        if filter_polo and aula.get('polo', '').lower() != filter_polo:
            continue

        nome = aula.get('nome', '').lower()
        alias_list = aula.get('alias', [])
        
        # Verifica match con nome o alias
        match = aula_search in nome
        
        # Robustezza: se la ricerca contiene "aula", prova anche senza
        if not match:
             aula_search_clean = aula_search.replace("aula ", "").strip()
             if aula_search_clean and len(aula_search_clean) > 0 and aula_search_clean in nome:
                 match = True

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
            
            # logger.info(f"DEBUG: Found match '{nome}' with priority {priority}")
            matched_aule.append((priority, nome_lower, aula))
    
    # Ordina per priorità e poi alfabeticamente
    matched_aule.sort(key=lambda x: (x[0], x[1]))
    
    # Ora processa le aule ordinate
    for priority, nome_lower, aula in matched_aule:
            edificio = aula.get('edificio', '?').upper()
            piano = aula.get('piano', '?')
            polo = aula.get('polo', 'fibonacci')
            events = events_by_polo.get(polo, [])
            
            if offset > 0:
                # Per giorni futuri usiamo lo start of day per il calcolo status (per vedere eventi)
                check_time = target_date.replace(hour=0, minute=0, second=1)
                status = get_aula_status(aula['nome'], events, check_time, polo=polo)
            else:
                status = get_aula_status(aula['nome'], events, now, polo=polo)
            
            # --- LINK DOVE?UNIPI ---
            dove_url = aula.get('link-dove-unipi')
            final_text_main = ""

            item = find_dove_item(items, aula.get("nome", ""), polo=polo)
            if item:
                # Prepara testo per il risultato "standard" (Punto 1)
                if dove_url:
                    description = item.get("description", "")
                    clean_desc = description.split("\n")[0].strip()
                    # Formato richiesto: Path › Name
                    final_text_main = f"{clean_desc} › {item.get('title', '')}\n\nClicca per aprire su [DOVE?UNIPI↗]({dove_url})"
                else:
                    raw_input = item.get("input_message_content", {})
                    final_text_main = raw_input.get("message_text", "")
            else:
                # Fallback se non trovato in data.json
                final_text_main = f"Aula {aula['nome']} (Edificio {edificio})"

            # 1. Prima aggiungi il risultato ESATTAMENTE come la ricerca normale (se item esiste)
            if item:
                parse_mode_item = item.get("input_message_content", {}).get("parse_mode", "Markdown")
                # Use a unique ID combining name and polo to avoid duplicates if multiple polos match
                unique_pos_id = f"pos_{polo}_{aula.get('nome','id')}_{item.get('id', str(uuid.uuid4()))}"
                results.append(
                    InlineQueryResultArticle(
                        id=unique_pos_id,
                        title=item.get("title", aula['nome']) + " (Posizione)",
                        description=item.get("description", f"Edificio {edificio} › Piano {piano}"),
                        input_message_content=InputTextMessageContent(
                            message_text=final_text_main,
                            parse_mode=parse_mode_item,
                            disable_web_page_preview=True
                        ),
                        thumbnail_url=get_building_thumb(polo=polo, edificio=edificio),
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
            # UNICA LOGICA: Mostra sempre il programma completo del giorno, senza header
            # Questo soddisfa la richiesta "mi mostra tutte le lezioni di quel giorno senza scrivere occupata fino a..."
            status_msg = format_day_schedule(aula, events, target_date, show_title=False)
            
            # Per descrizione e thumb manteniamo logica attuale (utile per anteprima)
            if offset > 0:
                 # Per i giorni futuri, descrizione adattata
                 if status['next_events'] or status['current_event']:
                     status_description = f"Programma del {target_date.strftime('%d/%m')} - Occupata"
                     # Thumbnail rosso se ci sono eventi
                     status_thumb = "https://placehold.co/100x100/b04859/b04859.png"
                 else:
                     status_description = f"Programma del {target_date.strftime('%d/%m')} - Libera"
                     status_thumb = "https://placehold.co/100x100/8cacaa/8cacaa.png"
                     
            # else: REMOVED to keep status_msg = format_day_schedule
            #      status_msg = format_single_aula_status(aula, status, now, dove_url)
            
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
                header_title = f"{WEEKDAYS_SHORT[target_date.weekday()]} {target_date.strftime('%d/%m')}"

            # Sempre aggiungi l'header card (che sia Stato o Data futura)
            if offset == 0 or offset > 0:
                # FIX DUPLICATE ID: Combine polo, aula name and offset
                # aula 'id' might be missing or not unique enough across poles if just "123"
                unique_status_id = f"status_{polo}_{aula.get('nome')}_{offset}"
                
                results.append(
                    InlineQueryResultArticle(
                        id=unique_status_id,
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
                            description=f"{event['start'].strftime('%H:%M')} - {event['end'].strftime('%H:%M')}" + (f" • {WEEKDAYS_SHORT[target_date.weekday()]} {target_date.strftime('%d/%m')}" if offset > 0 else "") + (f"\n{event['docenti']}" if event.get('docenti') else ""),
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


async def search_lessons_inline(lesson_search: str, interactive: bool = False) -> list:
    """Cerca lezioni per nome e restituisce lista risultati."""
    results = []
    
    # Parsing offset "+N"
    offset = 0
    match = re.search(r'\+(\d+)$', lesson_search)
    if match:
        offset = int(match.group(1))
        lesson_search = lesson_search[:match.start()].strip()
    
    now = datetime.now(TZ_ROME)
    target_date = now + timedelta(days=offset)
    
    # Fetch eventi per tutti i poli
    matched_events = []
    
    # Store FULL events for each polo to generate schedules later
    full_events_by_polo = {}
    
    search_lower = lesson_search.lower()
    
    # Ottieni lista poli (aggiornato per multi-polo)
    unified_data = load_unified_json()
    polos = unified_data.get('polo', {}).keys()
    
    for polo in polos:
        cid = get_calendar_id(polo)
        if not cid: continue
            
        polo_events = await fetch_day_events_async(cid, target_date)
        full_events_by_polo[polo] = polo_events
        
        # Filtra eventi per nome
        for event in polo_events:
            nome_evento = event.get('nome', '').lower()
            if search_lower in nome_evento:
                # Arricchisci evento con info polo
                event['polo'] = polo
                matched_events.append(event)
    
    # PULIZIA EVENTI PASSATI (SOLO SE SIAMO NELLA RICERCA "OGGI" INIZIALE)
    if offset == 0:
        filtered_events = []
        for event in matched_events:
            try:
                end = datetime.fromisoformat(event['dataFine'].replace('Z', '+00:00')).astimezone(TZ_ROME)
                if end >= now:
                    filtered_events.append(event)
            except Exception:
                pass
        matched_events = filtered_events

    # --- SMART LOOK-AHEAD: SE NESSUN RISULTATO "OGGI", CERCA NEI PROSSIMI GIORNI ---
    if offset == 0 and len(matched_events) == 0:
        for i in range(1, 8): # Cerca nei prossimi 7 giorni
            check_date = now + timedelta(days=i)
            # Accumula match futuri da tutti i poli
            matches_future = []
            future_events_dict = {}
            
            for polo in polos:
                cid = get_calendar_id(polo)
                if not cid: continue
                
                future_events = await fetch_day_events_async(cid, check_date)
                future_events_dict[polo] = future_events
                
                for event in future_events:
                    nome_evento = event.get('nome', '').lower()
                    if search_lower in nome_evento:
                        event['polo'] = polo
                        matches_future.append(event)
            
            if matches_future:
                # Trovato! Usiamo questo giorno
                matched_events = matches_future
                target_date = check_date
                # Update logic cache with future events
                full_events_by_polo = future_events_dict
                break
    
    # Ordina per orario
    matched_events.sort(key=lambda x: datetime.fromisoformat(x['dataInizio'].replace('Z', '+00:00')))
    
    # Carica dati aule per mapping
    all_aule = get_all_aule()
    # NON usiamo più una mappa piatta {nome: aula} perché i nomi possono essere duplicati tra poli
    # aula_map = {a['nome'].upper(): a for a in all_aule}
    
    for event in matched_events:
        # Recupera dati evento
        nome = event.get('nome', 'N/D')
        polo_evento = event.get('polo', 'fibonacci') # Default fallback
        
        try:
            start = datetime.fromisoformat(event['dataInizio'].replace('Z', '+00:00')).astimezone(TZ_ROME)
            end = datetime.fromisoformat(event['dataFine'].replace('Z', '+00:00')).astimezone(TZ_ROME)
        except Exception:
            continue
        
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
            raw_codice = aule_evento[0].get('codice', '').strip()
            # Rimuovi prefisso specifico del polo se presente
            prefix = get_polo_prefix(polo_evento)
            
            # Simple fuzzy cleanup
            clean_codice = raw_codice
            if clean_codice.upper().startswith(prefix.upper()):
                 clean_codice = clean_codice[len(prefix):].strip()
            
            # Also generic fallback just in case
            clean_codice = clean_codice.replace('FIB ','').replace('Fib ','').strip()
            
            aula_nome_display = clean_codice
            
            # Cerca l'oggetto aula corrispondente, FILTRANDO per polo
            found_aula = None
            
            # Filtra candidati solo del polo corretto (se specificato)
            candidates = [a for a in all_aule if a.get('polo') == polo_evento]
            if not candidates:
                 # Fallback: cerca ovunque se non trovi nel polo (magari mapping errato?)
                 candidates = all_aule

            # Tentativo 1: Match Esatto "clean_codice" == aula['nome']
            for a in candidates:
                if a['nome'].upper() == clean_codice.upper():
                    found_aula = a
                    break
            
            # Tentativo 2: Match Esatto "raw_codice" == aula['nome']
            if not found_aula:
                for a in candidates:
                    if a['nome'].upper() == raw_codice.upper():
                        found_aula = a
                        break
            
            # Tentativo 3: Contiene (più rischioso, ma utile se clean_code è parziale)
            if not found_aula:
                for a in candidates:
                    if clean_codice.upper() in a['nome'].upper():
                        found_aula = a
                        break
            
            aula_obj = found_aula
        
        # Prepara il messaggio di risposta (Programma dell'aula per quel giorno)
        if aula_obj:
             # Use the FULL events list for that polo to correctly determine free/busy slots
             polo_evs = full_events_by_polo.get(polo_evento, [])
             msg_content = format_day_schedule(aula_obj, polo_evs, target_date)
        else:
             # Fallback se non troviamo l'aula mappata
             msg_content = f"*{nome}*\n{time_str}\nAula: {aula_nome_display}\n\n{docenti_str}"

        # Thumbnail rosso sempre per lezione
        thumb_url = "https://placehold.co/100x100/b04859/ffffff.png?text=Lez"
        
        description = f"{time_str} • {aula_nome_display}"
        
        # Se la data non è oggi, aggiungiamola alla descrizione
        if target_date.date() != now.date():
            day_str = WEEKDAYS_SHORT[target_date.weekday()]
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

async def search_professor_inline(prof_search: str) -> list:
    """Cerca professori per cognome e restituisce la loro posizione + lezioni."""
    results = []
    # Parse query modifiers
    parsed = parse_query_modifiers(prof_search)
    offset = parsed['offset']
    prof_search = parsed['clean_query']
    
    items = get_data()
    prof_search_lower = prof_search.lower().strip()
    
    if not prof_search_lower:
        return results
    
    # Trova tutti i professori che matchano
    matched_profs = []
    for item in items:
        if item.get("type") == "article":
            description = item.get("description", "").lower()
            if "stanza" in description:
                title = item.get("title", "")
                title_lower = title.lower()
                
                # MATCH SOLO COGNOME (inizio stringa)
                if title_lower.startswith(prof_search_lower):
                    matched_profs.append(item)
    
    now = datetime.now(TZ_ROME)
    target_date = now + timedelta(days=offset)

    polos = get_polos()

    # Mappa aule per recupero oggetto (per polo)
    aule_by_polo = {p: get_aule_polo(p) for p in polos}
    aula_maps = {p: {a['nome'].upper(): a for a in aule_by_polo.get(p, [])} for p in polos}

    # Cache eventi per evitare chiamate duplicate
    events_cache = {}

    async def get_events_for_day(polo_key: str, day_offset_rel: int):
        """Ottiene eventi per un giorno specifico (relativo a target_date) e polo."""
        check_date = target_date + timedelta(days=day_offset_rel)
        cache_key = (polo_key, (check_date.date() - now.date()).days)

        if cache_key not in events_cache:
            calendar_id = get_calendar_id(polo_key)
            if calendar_id:
                events_cache[cache_key] = await fetch_day_events_async(calendar_id, check_date)
            else:
                events_cache[cache_key] = []

        return events_cache[cache_key]

    def filter_events_for_prof(prof_name, events_list, date_for_filter=None):
        """Filtra la lista eventi per il professore specificato."""
        filtered = []
        prof_parts = prof_name.lower().split()
        prof_cognome = prof_parts[0] if prof_parts else ""
        
        for event in events_list:
            docenti_list = event.get('docenti', [])
            match_found = False
            
            for d in docenti_list:
                cognome_api = d.get('cognome', '').lower()
                nome_api = d.get('nome', '').lower()
                cognome_nome = d.get('cognomeNome', '').lower()
                
                if prof_cognome and cognome_api and prof_cognome in cognome_api:
                    match_found = True
                elif prof_cognome and cognome_nome and prof_cognome in cognome_nome:
                    match_found = True
                elif prof_parts:
                    full_doc_name = f"{nome_api} {cognome_api}".lower()
                    if all(p in full_doc_name or p in cognome_nome for p in prof_parts):
                        match_found = True
                
                if match_found:
                    try:
                        # Parsing date
                        end = datetime.fromisoformat(event['dataFine'].replace('Z', '+00:00')).astimezone(TZ_ROME)
                        # Filtra solo se richiesto (es. eventi passati di OGGI)
                        if date_for_filter and date_for_filter.date() == now.date() and end < now:
                            break
                        filtered.append(event)
                    except Exception:
                        pass
                    break
        return filtered

    # Fetch iniziale del giorno target (per ottimizzare primo rendering)
    for polo in polos:
        await get_events_for_day(polo, 0)

    for prof_item in matched_profs[:2]:  # Max 2 professori per evitare timeout
        prof_name = prof_item.get("title", "")
        description = prof_item.get("description", "")
        raw_input = prof_item.get("input_message_content", {})
        raw_text = raw_input.get("message_text", "")
        prof_url = extract_url_from_markdown(raw_text)
        
        prof_events = []
        
        # LOGICA 7 GIORNI:
        # Se offset == 0, cerca oggi + prossimi 7 giorni (totale 8 giorni)
        # Se offset > 0, cerca solo quel giorno
        days_range = range(8) if offset == 0 else [0]
        
        for polo in polos:
            for i in days_range:
                day_evs = await get_events_for_day(polo, i)
                # Filtra eventi del professore
                # Passa data solo se è oggi (i=0 e offset=0) per nascondere passati
                filter_dt = target_date if (i == 0 and offset == 0) else None

                day_matches = filter_events_for_prof(prof_name, day_evs, date_for_filter=filter_dt)
                for ev in day_matches:
                    ev['polo'] = polo
                prof_events.extend(day_matches)
        
        # Ordina eventi aggregati
        prof_events.sort(key=lambda x: datetime.fromisoformat(x['dataInizio'].replace('Z', '+00:00')))
        
        # --- 1. RISULTATO POSIZIONE ---
        clean_desc = description.split("\n")[0].strip()
        thumb = get_building_thumb(description)
        
        if prof_url:
            position_text = f"{clean_desc} › {prof_name}\n\nClicca per aprire su [DOVE?UNIPI↗]({prof_url})"
        else:
            position_text = f"{clean_desc} › {prof_name}"
            
        results.append(
            InlineQueryResultArticle(
                id=f"prof_{prof_item.get('id', str(uuid.uuid4()))}",
                title=f"{prof_name} (Posizione)",
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=position_text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                ),
                thumbnail_url=thumb,
                thumbnail_width=100,
                thumbnail_height=100
            )
        )
        
        # --- 2. RISULTATI LEZIONI ---
        # Mostra più lezioni visto che copriamo una settimana
        for event in prof_events[:10]: # Max 10 lezioni per prof
            nome_lezione = event.get('nome', 'N/D').split('-')[0].strip()
            
            try:
                start = datetime.fromisoformat(event['dataInizio'].replace('Z', '+00:00')).astimezone(TZ_ROME)
                end = datetime.fromisoformat(event['dataFine'].replace('Z', '+00:00')).astimezone(TZ_ROME)
                time_str = f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"
                actual_date = start
            except Exception:
                continue

            # Recupera docenti (tutti)
            docenti_nomi = []
            for d in event.get('docenti', []):
                 if d.get('cognome'):
                     docenti_nomi.append(f"{d.get('nome','')} {d.get('cognome','')}".strip())
            docenti_str = ", ".join(docenti_nomi)
            
            # Recupera Aula
            aule_evento = event.get('aule', [])
            aula_nome_display = "N/D"
            aula_obj = None
            
            if aule_evento:
                raw_codice = aule_evento[0].get('codice', '').strip()
                polo_evento = event.get('polo', polos[0] if polos else 'fibonacci')
                prefix = get_polo_prefix(polo_evento)

                clean_codice = raw_codice
                if clean_codice.upper().startswith(prefix.upper()):
                    clean_codice = clean_codice[len(prefix):].strip()
                clean_codice = clean_codice.replace('FIB ', '').replace('Fib ', '').strip()

                aula_nome_display = clean_codice

                polo_aula_map = aula_maps.get(polo_evento, {})
                if clean_codice.upper() in polo_aula_map:
                    aula_obj = polo_aula_map[clean_codice.upper()]
                elif raw_codice.upper() in polo_aula_map:
                    aula_obj = polo_aula_map[raw_codice.upper()]
                else:
                    for a_nome, a_obj in polo_aula_map.items():
                        if clean_codice.upper() in a_nome:
                            aula_obj = a_obj
                            break
            
            # Genera contenuto messaggio using format_day_schedule
            # Dobbiamo passare gli eventi DEL GIORNO della lezione
            # Abbiamo cached events per quel giorno, recuperiamoli
            day_diff = (actual_date.date() - now.date()).days
            
            # Recupera eventi del giorno specifico per mostrare conflitti/schedule completo
            polo_evento = event.get('polo', polos[0] if polos else 'fibonacci')
            day_events_for_schedule = events_cache.get((polo_evento, day_diff), [])
            if not day_events_for_schedule:
                # Fallback, rigenera se mancante (non dovrebbe accadere se logica loop corretta)
                calendar_id = get_calendar_id(polo_evento)
                if calendar_id:
                    day_events_for_schedule = await fetch_day_events_async(calendar_id, actual_date)

            if aula_obj:
                msg_content = format_day_schedule(aula_obj, day_events_for_schedule, actual_date)
            else:
                msg_content = f"*{nome_lezione}*\n{time_str}\nAula: {aula_nome_display}\n\n{docenti_str}"
            
            # Description
            thumb_url = "https://placehold.co/100x100/b04859/ffffff.png?text=Lez"
            
            # Format: 'HH:MM • Aula X' oppure 'HH:MM • GGG DD/MM • Aula X'
            # Se la lezione è oggi, omettiamo la data?
            # Nella ricerca "tutti i 7 giorni", è meglio mettere SEMPRE la data se non è oggi.
            description_text = f"{time_str} • {aula_nome_display}"
            
            if actual_date.date() != now.date():
                day_str = WEEKDAYS_SHORT[actual_date.weekday()]
                date_str = actual_date.strftime('%d/%m')
                description_text = f"{time_str} • {day_str} {date_str} • {aula_nome_display}"
            
            if docenti_str:
                description_text += f"\n{docenti_str}"
            
            results.append(
                InlineQueryResultArticle(
                    id=f"profl_{str(uuid.uuid4())[:12]}",
                    title=nome_lezione,
                    description=description_text,
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

    # Gestione messaggi testuali (Mappe Poli)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_polo_map_message))
    
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