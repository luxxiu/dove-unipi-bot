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
from typing import List, Dict, Any, Optional, Union
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
    InlineQueryResultsButton,
    ReplyKeyboardMarkup,
    KeyboardButton
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
BIBLIOTECHE_DATA_PATH = os.path.join(BASE_DIR, "data", "biblioteche.json")
SBA_API_URL = "https://www.sba.unipi.it/it/opening_hours/instances"
SBA_CACHE_TTL = 600  # secondi (10 minuti)
_sba_cache: dict = {}  # key -> (timestamp, data)

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
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/github.png?v=3",
)
GLOBE_ICON_URL = os.environ.get(
    "GLOBE_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/globe.png?v=3",
)
MAP_ICON_URL = os.environ.get(
    "MAP_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/map.png?v=3",
)
MAP_URL = os.environ.get(
    "MAP_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/mappa.png",
)
INSTAGRAM_URL = os.environ.get("INSTAGRAM_URL", "https://www.instagram.com/unipilamappaorg")
INSTAGRAM_ICON_URL = os.environ.get(
    "INSTAGRAM_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/instagram.png?v=3",
)
LIBRARY_ICON_URL = os.environ.get(
    "LIBRARY_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/library.png?v=3",
)
INFO_ICON_URL = os.environ.get(
    "INFO_ICON_URL",
    "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/icons/info.png?v=3",
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
    """Carica il file data/aule2.geojson e lo converte nel formato legacy."""
    global _UNIFIED_CACHE, _UNIFIED_MTIME
    path = os.path.join(BASE_DIR, "data", "aule2.geojson")
    try:
        mtime = _get_mtime(path)
        if _UNIFIED_CACHE is not None and _UNIFIED_MTIME == mtime:
            return _UNIFIED_CACHE
        with open(path, 'r', encoding='utf-8') as f:
            geojson = json.load(f)
            
        legacy_data = {"polo": {}}
        # Build polo mapping
        for polo_id, polo_data in geojson.get("poli", {}).items():
            key = polo_data.get("id_database", "").replace("polo_", "")
            if not key:
                continue
            
            # extract links
            links = polo_data.get("links", {})
            legacy_data["polo"][key] = {
                "id": polo_id,
                "nome": polo_data.get("nome", key.capitalize()),
                "alternative_names": polo_data.get("alias", []) + [polo_data.get("nome")],
                "google_maps": links.get("google_maps", ""),
                "apple_maps": links.get("apple_maps", ""),
                "doveunipi": links.get("doveunipi", ""),
                "calendar_id": polo_id,
                "edificio": {}
            }
            
        # Build edifici
        edifici = geojson.get("edifici", {})
        for edif_id, edif_data in edifici.items():
            polo_id = edif_data.get("polo_id")
            if polo_id in geojson.get("poli", {}):
                polo_key = geojson["poli"][polo_id].get("id_database", "").replace("polo_", "")
                if polo_key and polo_key in legacy_data["polo"]:
                    edif_key = edif_data.get("nome", "A") # default or map it
                    # fallback to a string safe key
                    edif_key_safe = edif_key.lower().replace("edificio ", "").replace(" ", "_")
                    if "edificio" not in legacy_data["polo"][polo_key]:
                        legacy_data["polo"][polo_key]["edificio"] = {}
                    legacy_data["polo"][polo_key]["edificio"][edif_key_safe] = {
                        "text": edif_key,
                        "piano": {}
                    }
                    # Map the edif_id to polo_key and edif_key_safe for later use
                    edif_data["_mapped_polo"] = polo_key
                    edif_data["_mapped_edif"] = edif_key_safe

        # Map piani
        piani = geojson.get("piani", {})
        for piano_id, piano_data in piani.items():
            edif_id = piano_data.get("edificio_id")
            if edif_id in edifici:
                edif_obj = edifici[edif_id]
                polo_key = edif_obj.get("_mapped_polo")
                edif_key_safe = edif_obj.get("_mapped_edif")
                if polo_key and edif_key_safe:
                    livello = str(piano_data.get("livello", 0))
                    if livello not in legacy_data["polo"][polo_key]["edificio"][edif_key_safe]["piano"]:
                        legacy_data["polo"][polo_key]["edificio"][edif_key_safe]["piano"][livello] = []
                    piano_data["_mapped_polo"] = polo_key
                    piano_data["_mapped_edif"] = edif_key_safe
                    piano_data["_mapped_livello"] = livello

        # Add aule (pois)
        # Normalizza tipi GeoJSON al formato legacy
        _TYPE_MAP = {
            "aula didattica": "aula",
            "sala": "aula",
            "studio": "studio",
            "biblioteca": "biblioteca",
        }
        for poi in geojson.get("pois", []):
            piano_id = poi.get("piano_id")
            if piano_id in piani:
                piano_obj = piani[piano_id]
                polo_key = piano_obj.get("_mapped_polo")
                edif_key_safe = piano_obj.get("_mapped_edif")
                livello = piano_obj.get("_mapped_livello")
                
                if polo_key and edif_key_safe and livello:
                    raw_type = poi.get("tipo", "").lower().strip()
                    mapped_type = _TYPE_MAP.get(raw_type, "aula")  # default to aula
                    
                    # Extract DOVE?UNIPI link if available
                    links = poi.get("links") or {}
                    dove_link = links.get("doveunipi", "")
                    
                    legacy_data["polo"][polo_key]["edificio"][edif_key_safe]["piano"][livello].append({
                        "id": poi.get("id"),
                        "nome": poi.get("nome"),
                        "alias": poi.get("alias", []) + [poi.get("codice", "")],
                        "type": mapped_type,
                        "note": poi.get("note", ""),
                        "ricerca": poi.get("nome"),
                        "hasStatus": True,
                        "codice": poi.get("codice", ""),
                        "link-dove-unipi": dove_link
                    })
                    
        _UNIFIED_CACHE = legacy_data
        _UNIFIED_MTIME = mtime
        global _GENERATED_DATA_CACHE
        _GENERATED_DATA_CACHE = None
        return _UNIFIED_CACHE
    except Exception as e:
        logger.error(f"Errore lettura aule2.geojson: {e}")
        return {}

def load_biblioteche_json() -> list:
    """Carica il file data/biblioteche2.geojson e lo converte in lista legacy."""
    try:
        path = os.path.join(BASE_DIR, "data", "biblioteche2.geojson")
        with open(path, 'r', encoding='utf-8') as f:
            geojson = json.load(f)
        legacy_list = []
        for feature in geojson.get("features", []):
            props = feature.get("properties", {})
            name = props.get("name", "")
            if name.lower().startswith("biblioteca "):
                name = name[11:].strip()
            legacy_list.append({
                "id": feature.get("id", ""),
                "nome": name,
                "alias": props.get("alias", []),
                "type": props.get("type", "biblioteca"),
                "nid": props.get("data", {}).get("nid", ""),
                "indirizzo": props.get("data", {}).get("indirizzo", ""),
                "capienza": props.get("data", {}).get("capienza", 0),
                "contacts": props.get("contacts", {}),
                "links": props.get("links", {})
            })
        return legacy_list
    except Exception as e:
        logger.error(f"Errore lettura biblioteche2.geojson: {e}")
        return []

def fetch_sba_opening_hours(nid: str, from_date: str, to_date: str) -> list:
    """Fetch orari SBA per una biblioteca (con cache TTL)."""
    cache_key = f"{nid}:{from_date}:{to_date}"
    now_ts = time.time()
    cached = _sba_cache.get(cache_key)
    if cached and now_ts - cached[0] < SBA_CACHE_TTL:
        return cached[1]
    try:
        params = {
            "from_date": from_date,
            "to_date": to_date,
            "nid": nid
        }
        response = requests.get(SBA_API_URL, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        result = data if isinstance(data, list) else []
        _sba_cache[cache_key] = (now_ts, result)
        return result
    except Exception as e:
        logger.error(f"Errore API SBA nid={nid}: {e}")
        return []

async def fetch_sba_opening_hours_async(nid: str, from_date: datetime, to_date: datetime) -> list:
    f_str = from_date.strftime("%Y-%m-%d")
    t_str = to_date.strftime("%Y-%m-%d")
    return await asyncio.to_thread(fetch_sba_opening_hours, nid, f_str, t_str)

def get_polos() -> List[str]:
    data = load_unified_json()
    polos = list(data.get("polo", {}).keys())
    if not polos:
        return ["fibonacci", "carmignani"]
    return sorted(polos)

def get_polo_display_name(polo_key: str) -> str:
    data = load_unified_json()
    return data.get("polo", {}).get(polo_key, {}).get("nome", polo_key.capitalize())

def build_polo_reply_keyboard() -> ReplyKeyboardMarkup:
    """Crea la reply keyboard persistente con un bottone per ogni polo."""
    polos = get_polos()
    rows = []
    for i in range(0, len(polos), 2):
        row = [KeyboardButton(get_polo_display_name(polos[i]))]
        if i + 1 < len(polos):
            row.append(KeyboardButton(get_polo_display_name(polos[i + 1])))
        rows.append(row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, input_field_placeholder="Cerca o seleziona un polo...")

def build_polo_keyboard(callback_prefix: str = "status:polo:") -> List[List[InlineKeyboardButton]]:
    keyboard = []
    polos = get_polos()
    
    # Process in chunks of 2
    for i in range(0, len(polos), 2):
        row = []
        # First item
        polo1 = polos[i]
        display_name1 = get_polo_display_name(polo1)
        row.append(InlineKeyboardButton(display_name1, callback_data=f"{callback_prefix}{polo1}"))
        
        # Second item if exists
        if i + 1 < len(polos):
            polo2 = polos[i+1]
            display_name2 = get_polo_display_name(polo2)
            row.append(InlineKeyboardButton(display_name2, callback_data=f"{callback_prefix}{polo2}"))
            
        keyboard.append(row)
        
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
        polo_name = polo_data.get('nome', polo_key.capitalize())
        
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
                        short_link = room.get('link') or room.get('link-dove-unipi')
                        
                        floor_label = "Piano Terra" if floor == "0" else f"Piano {floor}"
                        
                        room_alias = ""
                        aliases = room.get('alias', [])
                        if aliases and len(aliases) > 0:
                            room_alias = aliases[0]
                        
                        room_ref = room_alias if room_alias else room.get('room', '')
                        
                        # FIX: Handle empty building or single building in polo
                        polo_buildings = data.get('polo', {}).get(polo_key, {}).get('edificio', {})
                        if building and building != '?' and building.lower() != polo_name.lower() and len(polo_buildings) > 1:
                            building_part = f"{get_edificio_display_name(polo_key, building, short=False)} › "
                        else:
                            building_part = ""
                            
                        description = f"{polo_name} › {building_part}{floor_label}"
                        
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
                    short_link = room.get('link') or room.get('link-dove-unipi')
                    
                    floor_label = "Piano Terra" if floor == "0" else f"Piano {floor}"

                    # FIX: Handle empty building or single building in polo
                    polo_buildings = data.get('polo', {}).get(polo_key, {}).get('edificio', {})
                    if building and building != '?' and building.lower() != polo_name.lower() and len(polo_buildings) > 1:
                        building_part = f"{get_edificio_display_name(polo_key, building, short=False)} › "
                    else:
                        building_part = ""
                        
                    description = f"{polo_name} › {building_part}{floor_label}"
                    
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
    Parse query modifiers like +1, +fib, +ing from a search string.
    Returns: {'offset': int, 'polo_filter': str or None, 'edificio_filter': str or None, 'clean_query': str}
    """
    offset = 0
    polo_filter = None
    edificio_filter = None
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
            
            # Filter ONLY for +fib, +ing, +car, +sr
            if val in ['fib', 'fibonacci']:
                polo_filter = 'fibonacci'
                continue
            
            if val in ['ing', 'ingegneria']:
                polo_filter = 'ingegneria'
                continue
                
            if val in ['car', 'carmignani']:
                polo_filter = 'carmignani'
                continue

            if val in ['sr', 'sanrossore', 'san_rossore']:
                polo_filter = 'san_rossore'
                continue

            if val in ['pia', 'piagge']:
                polo_filter = 'piagge'
                continue
        
        clean_parts.append(part)
    
    return {
        'offset': offset,
        'polo_filter': polo_filter,
        'edificio_filter': edificio_filter,
        'clean_query': ' '.join(clean_parts).strip()
    }

def get_calendar_id(polo="fibonacci") -> Union[str, List[str], None]:
    data = load_unified_json()
    try:
        if not polo:
            polo = "fibonacci"
        polo_data = data['polo'][polo]
        if 'calendar_id' in polo_data:
            return polo_data['calendar_id']
        
        # Se non c'è a livello di polo, cerchiamo negli edifici (es. ingegneria)
        calendar_ids = []
        if 'edificio' in polo_data:
            for ed_data in polo_data['edificio'].values():
                if 'calendar_id' in ed_data and ed_data['calendar_id']:
                    if ed_data['calendar_id'] not in calendar_ids:
                        calendar_ids.append(ed_data['calendar_id'])
        
        if calendar_ids:
            return calendar_ids
            
        return None
    except Exception:
        logger.error(f"Calendar ID not found for polo {polo}")
        return None

def get_polo_prefix(polo="fibonacci"):
    data = load_unified_json()
    try:
        if not polo:
            polo = "fibonacci"
        polo_data = data['polo'][polo]
        if 'prefix' in polo_data:
            return polo_data['prefix']
            
        # Se non c'è a livello di polo, cerchiamo nel primo edificio
        if 'edificio' in polo_data:
            for ed_data in polo_data['edificio'].values():
                if 'prefix' in ed_data:
                    return ed_data['prefix']
                    
        return 'Fib'
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

def find_aula_in_polo_smart(polo_key: str, raw_code: str) -> Optional[Dict]:
    """
    Cerca un'aula all'interno di un polo specifico usando matching intelligente e rigoroso.
    Priorità:
    1. Match ID esatto
    2. Match Nome esatto (case insensitive)
    3. Match Alias esatto (case insensitive)
    4. Match "Aula " + code (es. input "B" -> cerca "Aula B")
    5. Containment (SOLO se codice > 3 caratteri)
    """
    if not raw_code:
        return None
        
    data = load_unified_json()
    if not data or 'polo' not in data or polo_key not in data['polo']:
        # Fallback: se polo non trovato, non cerchiamo a caso per evitare falsi positivi
        return None

    # Normalizza codice input
    # Rimuovi prefisso polo o edificio se presente (es "FIB ", "Etr ")
    prefixes = []
    if 'prefix' in data['polo'][polo_key]:
        prefixes.append(data['polo'][polo_key]['prefix'])
    if 'edificio' in data['polo'][polo_key]:
        for ed_data in data['polo'][polo_key]['edificio'].values():
            if 'prefix' in ed_data and ed_data['prefix'] not in prefixes:
                prefixes.append(ed_data['prefix'])
    
    clean_code = raw_code.strip()
    for prefix in prefixes:
        if prefix and clean_code.upper().startswith(prefix.upper()):
            clean_code = clean_code[len(prefix):].strip()
            break
    
    # Rimuovi eventuali "Aula " dall'input per avere il codice puro
    if clean_code.lower().startswith("aula "):
        clean_code = clean_code[5:].strip()
        
    clean_code_upper = clean_code.upper()
    
    # Raccogli candidati del polo
    candidates = []
    edifici = data['polo'][polo_key].get('edificio', {})
    for edificio_key, edificio_data in edifici.items():
        piani = edificio_data.get('piano', {})
        for piano_key, aule in piani.items():
            for aula in aule:
                # Skip persone o altro
                if aula.get('type') == 'persona':
                    continue
                
                c = aula.copy()
                c['polo'] = polo_key
                c['edificio'] = edificio_key
                c['piano'] = piano_key
                candidates.append(c)

    # 1. Match ID Esatto
    for aula in candidates:
        if aula.get('id') == raw_code:
            return aula

    # 2. Match Nome Esatto (o "Aula " + Code)
    #    & 3. Match Alias Esatto
    for aula in candidates:
        nome = aula.get('nome', '').upper()
        aliases = [a.upper() for a in aula.get('alias', [])]
        
        # Check Nome
        if nome == clean_code_upper:
            return aula
        if nome == f"AULA {clean_code_upper}":
            return aula
            
        # Check Alias
        if clean_code_upper in aliases:
            return aula
            
    # 4. Check inverso: se l'input è "Aula B", e il nome è "B" (raro ma possibile)
    raw_upper = raw_code.upper()
    for aula in candidates:
         nome = aula.get('nome', '').upper()
         if raw_upper == nome:
             return aula

    # 5. Containment (SOLO PER CODICI LUNGHI)
    #    Evita che "B" matchi "Biblioteca"
    if len(clean_code) > 2:
        for aula in candidates:
             nome = aula.get('nome', '').upper()
             if clean_code_upper in nome:
                 return aula
                 
    return None

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
            if polo:
                polo_norm = polo.lower().replace('_', ' ')  # san_rossore -> san rossore
                if polo.lower() not in item_desc and polo_norm not in item_desc:
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
        
        # Iterazione su TUTTI i poli per trovare match nella descrizione
        for p_key, p_val in unified_data.get('polo', {}).items():
            # Check 1: NOME DEL POLO (es. "fibonacci") o ALIAS (es. "polo fibonacci")
            match_polo = False
            if p_key in desc_lower:
                match_polo = True
            
            if not match_polo and 'alias' in p_val:
                for alias in p_val['alias']:
                    if alias.lower() in desc_lower:
                         match_polo = True
                         break

            # Also match on the display name (handles keys with underscores, e.g. san_rossore -> "San Rossore")
            if not match_polo:
                p_nome = p_val.get('nome', '').lower()
                if p_nome and p_nome in desc_lower:
                    match_polo = True
            
            if match_polo:
                polo = p_key
                # Cerchiamo l'edificio SPECIFICO dentro questo polo
                found_edificio = None
                
                if 'edificio' in p_val:
                    for b_key, b_val in p_val['edificio'].items():
                        # Match su ID edificio (es. "c", "b") se lungo o con prefisso
                        if len(b_key) > 2:
                             if b_key in desc_lower:
                                 found_edificio = b_key
                                 break
                        
                        # Match su pattern espliciti (es. "Edificio C", "Polo C")
                        patterns = [f"edificio {b_key}", f"ed. {b_key}", f"polo {b_key}"]
                        is_match = False
                        for pat in patterns:
                             if pat in desc_lower:
                                 is_match = True
                                 break
                        
                        # Match su ALIAS edificio
                        if not is_match and 'alias' in b_val:
                             for alias in b_val['alias']:
                                 if alias.lower() in desc_lower:
                                     is_match = True
                                     break
                        
                        if is_match:
                            found_edificio = b_key
                            break
                
                if found_edificio:
                    edificio = found_edificio
                
                # Abbiamo trovato il polo (e forse l'edificio), stop
                break

    # 2. Lookup nel JSON per ottenere COLORE e TESTO
    target_item = None
    
    if polo and polo in unified_data.get('polo', {}):
        p_data = unified_data['polo'][polo]
        
        # Se c'è un edificio specifico, cerca lì
        if edificio:
            # Normalizza edificio key (es. "C" -> "c")
            ed_key = str(edificio).strip().lower()
            if 'edificio' in p_data and ed_key in p_data['edificio']:
                target_item = p_data['edificio'][ed_key]
        
        # Se non trovato specifico o no edificio, usa polo come fallback (se ha colore) o cerca default
        if not target_item:
             # Se esiste un edificio vuoto (es. Carmignani), usalo come fallback
             if 'edificio' in p_data and "" in p_data['edificio']:
                 target_item = p_data['edificio'][""]
             else:
                 target_item = p_data # Fallback al polo intero

    if target_item:
        color = target_item.get('color', color)
        text = target_item.get('text', text) # text potrebbe essere "C", "F", ecc.
        fg_color = target_item.get('text_foreground', fg_color)
    
    # Se il colore è ancora quello di default, usiamo una palette fissa o un hash
    if color == DEFAULT_COLOR and polo:
        POLO_COLORS = {
            "fibonacci": "da21ac",
            "ingegneria": "da5c21",
            "carmignani": "5cda21",
            "piagge": "3b82f6",
            "san_rossore": "214fda"
        }
        polo_key = str(polo).lower()
        if polo_key in POLO_COLORS:
            color = POLO_COLORS[polo_key]
        else:
            import hashlib
            h = int(hashlib.md5(polo_key.encode('utf-8')).hexdigest(), 16)
            colors = ["FF3366", "33CCFF", "FF9933", "33FF99", "CC33FF", "FFD700", "FF3333"]
            color = colors[h % len(colors)]
            
    # Generiamo il testo dall'edificio se manca
    if not text:
        polo_buildings = {}
        if polo and unified_data.get('polo', {}).get(polo):
            polo_buildings = unified_data['polo'][polo].get('edificio', {})
            
        if len(polo_buildings) > 1:
            if edificio:
                if len(str(edificio)) <= 3:
                    text = str(edificio).upper()
                else:
                    text = str(edificio)[0].upper()
            else:
                text = str(polo)[0].upper() if polo else ""
        else:
            text = ""
    
    import urllib.parse
    safe_text = urllib.parse.quote(text) if text else "%20"
    return f"https://placehold.co/100/{color}/{fg_color}.png?text={safe_text}&font=montserrat"

def extract_url_from_markdown(markdown_text):
    try:
        if "](" in markdown_text:
            return markdown_text.split("](")[-1].strip(")")
        return ""
    except Exception:
        return ""

def _extract_surname_display(full_name: str) -> str:
    """Extracts surname assuming 'Surname Name' or just 'Surname' format (Local DB)."""
    parts = full_name.split()
    if not parts:
        return ""
    
    # Common particles in Italian/European surnames
    particles = {"del", "della", "de", "di", "lo", "la", "le", "van", "von", "san", "da"}
    
    # Check if first word is a particle
    if len(parts) > 1 and parts[0].lower() in particles:
        return f"{parts[0]} {parts[1]}".upper()
    
    return parts[0].upper()

def _extract_surname_from_api_title(title: str) -> str:
    """Extracts surname assuming 'Name Surname' format (API)."""
    parts = title.split()
    if not parts:
        return ""
    
    # Common particles in Italian/European surnames
    particles = {"del", "della", "de", "di", "lo", "la", "le", "van", "von", "san", "da"}
    
    # Start from the end
    surname_cut = -1
    
    # Check if second to last word is a particle (e.g. Mario Del Rossi)
    if len(parts) > 1 and parts[-2].lower() in particles:
        surname_cut = -2
        # Check if third to last is also particle (very rare, e.g. De La)
        if len(parts) > 2 and parts[-3].lower() in particles:
             surname_cut = -3

    surname_parts = parts[surname_cut:]
    return " ".join(surname_parts).upper()

def fetch_day_events(calendar_id: Union[str, List[str]], day: datetime) -> List[Dict[str, Any]]:
    """Recupera tutti gli eventi per un giorno specifico."""
    if not calendar_id:
        return []
        
    if isinstance(calendar_id, list):
        all_events = []
        for cid in calendar_id:
            events = fetch_day_events(cid, day)
            if events:
                all_events.extend(events)
        return all_events
        
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

async def fetch_day_events_async(calendar_id: Union[str, List[str]], day: datetime) -> List[Dict[str, Any]]:
    """Wrapper async per evitare blocchi dell'event loop. Se lista, fetcha in parallelo."""
    if isinstance(calendar_id, list):
        if not calendar_id:
            return []
        results = await asyncio.gather(*[asyncio.to_thread(fetch_day_events, cid, day) for cid in calendar_id])
        return [event for result in results for event in result]
    return await asyncio.to_thread(fetch_day_events, calendar_id, day)

async def search_unipi_person(name_query: str) -> Optional[Dict[str, str]]:
    """
    Cerca una persona tramite API WP di Unipi.
    Restituisce dittionario con {link, title} o None.
    """
    if not name_query or len(name_query) < 3:
        return None
        
    url = "https://www.unipi.it/wp-json/wp/v2/unipi_persone"
    # Cerca intero nome stringa
    params = {"search": name_query, "per_page": 3}
    
    try:
        response = await asyncio.to_thread(requests.get, url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data and isinstance(data, list):
                # Cerchiamo il match migliore
                query_parts = name_query.lower().split()
                
                for person in data:
                    title = person.get("title", {}).get("rendered", "")
                    title_lower = title.lower()
                    
                    # Verifica semplice: tutti i token della query sono presenti?
                    if all(part in title_lower for part in query_parts):
                         return {
                             "link": person.get("link"),
                             "title": title # Nome completo trovato
                         }
    except Exception as e:
        logger.warning(f"Errore ricerca persona Unipi '{name_query}': {e}")
        
    return None

def get_aula_status(aula_nome: str, events: List[Dict], now: datetime, polo: str = "fibonacci", edificio: str = None) -> Dict:
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
    # Usa il prefix del polo o dell'edificio
    prefix = 'Fib'
    data = load_unified_json()
    try:
        polo_data = data['polo'][polo]
        if edificio and 'edificio' in polo_data and edificio in polo_data['edificio']:
            prefix = polo_data['edificio'][edificio].get('prefix', polo_data.get('prefix', 'Fib'))
        else:
            prefix = get_polo_prefix(polo)
    except Exception:
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

STATUS_ELIGIBLE_TYPES = {'aula', 'biblioteca', 'studio'}

def _is_status_eligible(room: Dict) -> bool:
    """Returns True if this room should appear in /occupazione.
    - aula: only if hasStatus=True (live calendar data available)
    - biblioteca, studio: always shown regardless of hasStatus
    """
    rtype = room.get('type')
    types = rtype if isinstance(rtype, list) else [rtype]
    # biblioteca and studio: always show
    if any(t in ('biblioteca', 'studio') for t in types):
        return True
    # aula: only if hasStatus is True
    if 'aula' in types:
        return room.get('hasStatus', False)
    return False

def _has_live_status(room: Dict) -> bool:
    """Returns True if this room has real-time calendar status."""
    return bool(room.get('hasStatus', False))

def _compute_biblio_live_status(hours_today: List[Dict], now: datetime):
    """Returns (is_open: bool, closes_at: str|None, opens_at: str|None) from SBA hours for today."""
    now_str = now.strftime('%H:%M')
    times = sorted(
        [(e['start_time'].strip(), e['end_time'].strip()) for e in hours_today
         if e.get('start_time') and e.get('end_time')],
        key=lambda x: x[0]
    )
    if not times:
        return False, None, None
    for start, end in times:
        if now_str >= start and now_str < end:
            return True, end, None
    for start, end in times:
        if start > now_str:
            return False, None, start
    return False, None, None

async def _fetch_scope_biblio_hours(rooms: List[Dict], target_date: datetime) -> Dict[str, List]:
    """Fetch SBA opening hours for biblioteca rooms (hasStatus=False) in the given list."""
    result: Dict[str, List] = {}
    nids: List[str] = []
    tasks = []
    today_iso = target_date.strftime('%Y-%m-%d')
    for room in rooms:
        rtype = room.get('type')
        types = rtype if isinstance(rtype, list) else [rtype]
        if 'biblioteca' in types and not room.get('hasStatus') and room.get('nid'):
            nid = str(room['nid'])
            if nid not in nids:
                nids.append(nid)
                tasks.append(fetch_sba_opening_hours_async(nid, target_date, target_date))
    if tasks:
        fetched = await asyncio.gather(*tasks, return_exceptions=True)
        for nid, res in zip(nids, fetched):
            result[nid] = [e for e in res if e.get('date') == today_iso] if not isinstance(res, Exception) else []
    return result

def _format_room_line(
    aula: Dict,
    events: List[Dict],
    now: datetime,
    polo: str,
    edificio: str = None,
    short: bool = False,
    biblio_hours: Optional[Dict] = None,
) -> str:
    """Returns the formatted status line (with trailing newline) for a room in /occupazione.
    - biblioteca: aperta / chiusa with times (calendar events = open hours)
    - aula with hasStatus: libera / occupata with times
    - studio (no biblioteca tag): always ✓
    - everything else: •
    """
    label = _aula_link_label(aula)
    rtype = aula.get('type')
    types = rtype if isinstance(rtype, list) else [rtype]
    has_live = aula.get('hasStatus', False)

    # ── Biblioteca ──────────────────────────────────────────────────────────
    if 'biblioteca' in types:
        if has_live:
            status = get_aula_status(aula['nome'], events, now, polo=polo, edificio=edificio)
            if not status['is_free']:
                # calendar event ongoing = biblioteca OPEN
                if status['busy_until']:
                    return f"{label} - aperta, chiude alle {status['busy_until'].strftime('%H:%M')}\n"
                return f"{label} - aperta\n"
            else:
                # no current event = biblioteca CLOSED
                if status['free_until']:
                    return f"{label} - chiusa, apre alle {status['free_until'].strftime('%H:%M')}\n"
                return f"{label} - chiusa\n"
        nid = str(aula.get('nid', ''))
        if biblio_hours and nid in biblio_hours:
            is_open, closes_at, opens_at = _compute_biblio_live_status(biblio_hours[nid], now)
            if is_open:
                suffix = f" - chiude alle {closes_at}" if closes_at else ""
                return f"{label}{suffix}\n"
            else:
                suffix = f" - apre alle {opens_at}" if opens_at else " - chiusa"
                return f"{label}{suffix}\n"
        return f"{label}\n"

    # ── Aula with live status (takes priority over studio tag) ───────────────
    if 'aula' in types and has_live:
        status = get_aula_status(aula['nome'], events, now, polo=polo, edificio=edificio)
        symbol = "✓" if status['is_free'] else "✗"
        if status['is_free']:
            if status['free_until']:
                suffix = "fino" if short else "libera fino"
                return f"{symbol} {label} - {suffix} {status['free_until'].strftime('%H:%M')}\n"
            return f"{symbol} {label}\n" if short else f"{symbol} {label} - libera\n"
        else:
            suffix = "fino" if short else "occupata fino"
            return f"{symbol} {label} - {suffix} {status['busy_until'].strftime('%H:%M')}\n"

    # ── Studio: always free/available ────────────────────────────────────────
    if 'studio' in types:
        return f"{label}\n"

    return f"• {label}\n"

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

def get_edificio_display_name(polo: str, edificio: str, short: bool = True) -> str:
    """Restituisce il nome da visualizzare per un edificio (es. il primo alias per ingegneria).
    Se short=True, rimuove 'Polo ' dall'alias (es. 'Polo Porta Nuova' -> 'Porta Nuova').
    Se short=False, restituisce 'Edificio B68 (Polo Porta Nuova)'.
    """
    data = load_unified_json()
    try:
        b_data = data['polo'][polo]['edificio'][edificio]
        if polo.lower() == "ingegneria" and "alias" in b_data and b_data["alias"]:
            alias = b_data["alias"][0]
            if short:
                if alias.lower().startswith("polo "):
                    return alias[5:].strip()
                return alias
            else:
                return f"{edificio.upper()} ({alias})"
    except Exception:
        pass
        
    if len(edificio) > 3:
        return edificio.capitalize()
    return edificio.upper()

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

# --- OCCUPANCY TIME FILTER ---

TIME_FILTER_HINT = (
    "\n_Rispondi a questo messaggio con_ `13:00` _per le aule libere da quell\'ora a fine giornata,_"
    "\n_oppure_ `13:00-15:00` _per quelle libere nell\'intero intervallo._"
    "\n_Premi_ ↺ _per tornare indietro._"
)

BACK_HINT = "\n_Premi_ ↺ _per tornare indietro._"


_MD_LINK_RE = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_MD_SYNTAX_RE = re.compile(r'[*_`]')

def _rendered_len(text: str) -> int:
    """Compute the visible/rendered length of a Markdown string.
    Telegram counts message length AFTER entity parsing, so [label](url)
    counts as len(label), and *, _, ` markers don't count."""
    # Replace [label](url) with just label
    stripped = _MD_LINK_RE.sub(r'\1', text)
    # Remove standalone markdown syntax characters
    stripped = _MD_SYNTAX_RE.sub('', stripped)
    return len(stripped)

def _safe_truncate(text: str, max_len: int = 4096) -> str:
    """Truncate text so its rendered (parsed) length <= max_len.
    Telegram counts message length after entity parsing, so markdown link
    URLs don't contribute to the limit. Cuts at the last newline."""
    if _rendered_len(text) <= max_len:
        return text
    # Cut line by line until rendered length fits
    lines = text.split('\n')
    result_lines = []
    current_rendered = 0
    for line in lines:
        line_rendered = _rendered_len(line + '\n')
        if current_rendered + line_rendered > max_len:
            break
        result_lines.append(line)
        current_rendered += line_rendered
    return '\n'.join(result_lines)

def _aula_link_label(aula: Dict) -> str:
    """Returns '[nome](url)' if the room has a LA MAPPA UniPi link, otherwise just 'nome'."""
    nome = aula.get('nome', 'N/D')
    aula_id = aula.get('id')
    
    if aula_id:
        return f"[{nome}](https://unipi.lamappa.org/{aula_id})"
        
    return nome

_TIME_RE_SINGLE = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*$')
_TIME_RE_RANGE  = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*[-\u2013]\s*(\d{1,2}):(\d{2})\s*$')

def parse_time_filter(text: str) -> Optional[Dict]:
    """
    Parse a time-filter reply from the user.
    Returns:
      {'type': 'from', 'start': datetime}         – single time
      {'type': 'range', 'start': datetime, 'end': datetime} – range
      None if the text is not a time filter.
    """
    now = datetime.now(TZ_ROME)

    m = _TIME_RE_RANGE.match(text)
    if m:
        h1, m1, h2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if 0 <= h1 <= 23 and 0 <= m1 <= 59 and 0 <= h2 <= 23 and 0 <= m2 <= 59:
            start = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
            end   = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
            return {'type': 'range', 'start': start, 'end': end}

    m = _TIME_RE_SINGLE.match(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            start = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            return {'type': 'from', 'start': start}

    return None


def is_aula_free_in_period(
    aula_nome: str,
    events: List[Dict],
    start_time: datetime,
    end_time: datetime,
    polo: str = "fibonacci",
    edificio: str = None,
) -> bool:
    """Return True if the aula has no events in [start_time, end_time]."""
    status = get_aula_status(aula_nome, events, start_time, polo=polo, edificio=edificio)
    if not status['is_free']:
        return False
    for ev in status['next_events']:
        if ev['start'] < end_time:
            return False
    return True


# --- FORMATTAZIONE MESSAGGI ---
def format_aula_header(aula: Dict) -> str:
    """Formatta l'intestazione standard dell'aula (Nome, Edificio, Piano, Capienza)."""
    nome = aula.get('nome', 'N/D')
    edificio = aula.get('edificio', '').strip()
    piano = aula.get('piano', '?')
    capienza = aula.get('capienza', 'N/D')
    polo_key = aula.get('polo', 'fibonacci')
    polo = get_polo_display_name(polo_key)  # Usa nome display corretto (evita underscore come in "San_rossore")
    
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
    elif edificio.lower() == polo_key.lower():
        should_show_edificio = False
    else:
        # Nascondi se il polo ha un solo edificio
        data = load_unified_json()
        polo_buildings = data.get('polo', {}).get(polo_key, {}).get('edificio', {})
        if len(polo_buildings) <= 1:
            should_show_edificio = False
        
    if should_show_edificio:
        msg += f"{get_polo_display_name(polo_key)} › {get_edificio_display_name(polo_key, edificio, short=False)} › Piano {display_piano}\n"
    else:
        msg += f"{get_polo_display_name(polo_key)} › Piano {display_piano}\n"
    
    return msg

async def format_single_aula_status(aula: Dict, status: Dict, now: datetime, dove_url: str = None) -> str:
    """Formatta il messaggio di stato per una singola aula."""
    msg = format_aula_header(aula) + "\n"
    
    
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
            docenti_names = event.get('docenti', '')
            if docenti_names:
                msg += f"```\n{time_str} {event['nome']}\n{docenti_names}\n```\n"
            else:
                msg += f"```\n{time_str} {event['nome']}\n```\n"
    
    # Prossime occupazioni
    if status['next_events']:
        msg += "\nProssime occupazioni:\n"
        code_block_content = ""
        for i, event in enumerate(status['next_events']):
            time_str = f"{event['start'].strftime('%H:%M')}-{event['end'].strftime('%H:%M')}"
            docenti_names = event.get('docenti', '')
            
            # Aggiungi separatore se non è il primo elemento
            if i > 0:
                code_block_content += "-----------\n"
            
            code_block_content += f"{time_str} {event['nome']}\n"
            if docenti_names:
                code_block_content += f"{docenti_names}\n"
            
        msg += f"```\n{code_block_content}```\n"
    
    # Aggiungi link alla fine (LA MAPPA + MAPPA)
    footer_links = []
    polo = aula.get('polo', 'fibonacci')
    polo_data = load_unified_json().get("polo", {}).get(polo, {})
    
    aula_id = aula.get("id")
    if not aula_id:
        aula_id = polo_data.get("id")
    if aula_id:
        footer_links.append(f"[LA MAPPA ↗](https://unipi.lamappa.org/{aula_id})")
    
    gmaps = polo_data.get("google_maps")
    amaps = polo_data.get("apple_maps")
    if gmaps:
        footer_links.append(f"[Google Maps ↗]({gmaps})")
    elif amaps:
        footer_links.append(f"[Apple Maps ↗]({amaps})")
    
    if footer_links:
        msg += "\n" + " • ".join(footer_links)
    
    return msg

def format_edificio_status(polo: str, edificio: str, events: List[Dict], now: datetime, time_filter: Optional[Dict] = None, biblio_hours: Optional[Dict] = None) -> str:
    """Formatta lo stato di tutte le aule di un edificio."""
    aule = get_aule_edificio(polo, edificio)
    polo_display = get_polo_display_name(polo)

    if not edificio or edificio == '?' or edificio.lower() == polo.lower():
        msg = f"*{polo_display}*\n"
    else:
        msg = f"*{get_edificio_display_name(polo, edificio)} - {polo_display}*\n"

    if time_filter:
        if time_filter['type'] == 'from':
            msg += f"Aule libere dalle {time_filter['start'].strftime('%H:%M')} — {now.strftime('%d/%m')}\n\n"
        else:
            msg += f"Aule libere {time_filter['start'].strftime('%H:%M')}–{time_filter['end'].strftime('%H:%M')} — {now.strftime('%d/%m')}\n\n"
    else:
        msg += f"Stato aule alle {now.strftime('%H:%M')} del {now.strftime('%d/%m')}\n\n"

    # Raggruppa per piano
    aule_per_piano = {}
    for aula in aule:
        piano = aula.get('piano', '0')
        if piano not in aule_per_piano:
            aule_per_piano[piano] = []
        aule_per_piano[piano].append(aula)

    if time_filter:
        end_time = time_filter.get('end') or now.replace(hour=23, minute=59, second=0, microsecond=0)
        any_free = False
        for piano in sorted(aule_per_piano.keys()):
            free_aule = [
                a for a in aule_per_piano[piano]
                if _has_live_status(a) and is_aula_free_in_period(a['nome'], events, time_filter['start'], end_time, polo=polo, edificio=edificio)
            ]
            if free_aule:
                msg += f"*Piano {piano}:*\n"
                for a in free_aule:
                    msg += f"{_aula_link_label(a)}\n"
                    any_free = True
                msg += "\n"
        if not any_free:
            msg += "_Nessuna aula libera per il periodo richiesto._\n"
        msg += BACK_HINT
    else:
        for piano in sorted(aule_per_piano.keys()):
            msg += f"*Piano {piano}:*\n"
            for aula in aule_per_piano[piano]:
                msg += _format_room_line(aula, events, now, polo, edificio, biblio_hours=biblio_hours)
            msg += "\n"
        msg += TIME_FILTER_HINT

    return msg

def format_piano_status(polo: str, edificio: str, piano: str, events: List[Dict], now: datetime, time_filter: Optional[Dict] = None, biblio_hours: Optional[Dict] = None) -> str:
    """Formatta lo stato di tutte le aule di un piano."""
    aule = get_aule_edificio(polo, edificio)
    aule = [a for a in aule if a.get('piano') == piano]
    polo_display = get_polo_display_name(polo)

    if not edificio or edificio == '?' or edificio.lower() == polo.lower():
        msg = f"*{polo_display} - Piano {piano}*\n"
    else:
        msg = f"*{polo_display} - {get_edificio_display_name(polo, edificio)} - Piano {piano}*\n"

    if time_filter:
        if time_filter['type'] == 'from':
            msg += f"Aule libere dalle {time_filter['start'].strftime('%H:%M')} — {now.strftime('%d/%m')}\n\n"
        else:
            msg += f"Aule libere {time_filter['start'].strftime('%H:%M')}–{time_filter['end'].strftime('%H:%M')} — {now.strftime('%d/%m')}\n\n"
    else:
        msg += f"Stato alle {now.strftime('%H:%M')} del {now.strftime('%d/%m')}\n\n"

    if time_filter:
        end_time = time_filter.get('end') or now.replace(hour=23, minute=59, second=0, microsecond=0)
        free_aule = [
            a for a in aule
            if _has_live_status(a) and is_aula_free_in_period(a['nome'], events, time_filter['start'], end_time, polo=polo, edificio=edificio)
        ]
        if free_aule:
            for a in free_aule:
                msg += f"{_aula_link_label(a)}\n"
        else:
            msg += "_Nessuna aula libera per il periodo richiesto._\n"
        msg += BACK_HINT
    else:
        for aula in aule:
            msg += _format_room_line(aula, events, now, polo, edificio, biblio_hours=biblio_hours)
        msg += TIME_FILTER_HINT

    return msg

def format_polo_status(polo: str, events: List[Dict], now: datetime, time_filter: Optional[Dict] = None, biblio_hours: Optional[Dict] = None) -> str:
    """Formatta lo stato di tutte le aule di un polo."""
    polo_display = get_polo_display_name(polo)
    msg = f"*{polo_display}*\n"

    if time_filter:
        if time_filter['type'] == 'from':
            msg += f"Aule libere dalle {time_filter['start'].strftime('%H:%M')} — {now.strftime('%d/%m')}\n\n"
        else:
            msg += f"Aule libere {time_filter['start'].strftime('%H:%M')}–{time_filter['end'].strftime('%H:%M')} — {now.strftime('%d/%m')}\n\n"
    else:
        msg += f"Stato aule alle {now.strftime('%H:%M')} del {now.strftime('%d/%m')}\n\n"

    edifici = get_edifici(polo)
    any_free = False

    if time_filter:
        end_time = time_filter.get('end') or now.replace(hour=23, minute=59, second=0, microsecond=0)
        for edificio in edifici:
            if edificio and edificio != '?' and edificio.lower() != polo.lower() and len(edifici) > 1:
                edificio_header = f"━━━ *{get_edificio_display_name(polo, edificio)}* ━━━\n"
            else:
                edificio_header = ""

            aule = get_aule_edificio(polo, edificio)
            aule_per_piano: Dict[str, List] = {}
            for aula in aule:
                p = aula.get('piano', '0')
                aule_per_piano.setdefault(p, []).append(aula)

            edificio_lines = ""
            for piano in sorted(aule_per_piano.keys()):
                piano_lines = ""
                for aula in aule_per_piano[piano]:
                    if _has_live_status(aula) and is_aula_free_in_period(aula['nome'], events, time_filter['start'], end_time, polo=polo, edificio=edificio):
                        piano_lines += f"{_aula_link_label(aula)}\n"
                        any_free = True
                if piano_lines:
                    edificio_lines += f"*Piano {piano}:*\n" + piano_lines + "\n"

            if edificio_lines:
                msg += edificio_header + edificio_lines

        if not any_free:
            msg += "_Nessuna aula libera per il periodo richiesto._\n"
        msg += BACK_HINT
    else:
        for edificio in edifici:
            if edificio and edificio != '?' and edificio.lower() != polo.lower() and len(edifici) > 1:
                msg += f"━━━ *{get_edificio_display_name(polo, edificio)}* ━━━\n"

            aule = get_aule_edificio(polo, edificio)

            # Raggruppa per piano
            aule_per_piano: Dict[str, List] = {}
            for aula in aule:
                piano = aula.get('piano', '0')
                aule_per_piano.setdefault(piano, []).append(aula)

            for piano in sorted(aule_per_piano.keys()):
                msg += f"*Piano {piano}:*\n"
                for aula in aule_per_piano[piano]:
                    msg += _format_room_line(aula, events, now, polo, edificio, short=True, biblio_hours=biblio_hours)
                msg += "\n"
        msg += TIME_FILTER_HINT

    # Aggiungi link alla fine
    polo_data = load_unified_json().get("polo", {}).get(polo, {})
    polo_id = polo_data.get("id")
    gmaps = polo_data.get("google_maps")
    amaps = polo_data.get("apple_maps")
    
    footer_links = []
    if polo_id:
        footer_links.append(f"[LA MAPPA ↗](https://unipi.lamappa.org/{polo_id})")
    if gmaps:
        footer_links.append(f"[Google Maps ↗]({gmaps})")
    elif amaps:
        footer_links.append(f"[Apple Maps ↗]({amaps})")
        
    if footer_links:
        msg += "\n\n" + " • ".join(footer_links)

    return msg

async def format_day_schedule(aula: Dict, events: List[Dict], target_date: datetime, show_title: bool = True) -> str:
    """Formatta il programma di una giornata specifica."""
    # Formato per giorni futuri/passati: Header + Programma
    text = format_aula_header(aula) + "\n"
    
    if show_title:
        # Formato per giorni futuri/passati: Header + Programma
        day_caps = WEEKDAYS_SHORT[target_date.weekday()]
        text += f"PROGRAMMA {day_caps} {target_date.strftime('%d/%m')}\n\n"
    
    # Recupera eventi del giorno
    start_of_day = target_date.replace(hour=0, minute=0, second=0)
    status_day = get_aula_status(aula['nome'], events, start_of_day, polo=aula.get('polo', 'fibonacci'), edificio=aula.get('edificio'))
    
    
    if not status_day['next_events'] and not status_day['current_event']:
            text += "Nessuna occupazione prevista.\n"
    else:
            all_events = status_day['next_events']
            if status_day['current_event']:
                all_events.insert(0, status_day['current_event'])
            
            code_block_content = ""
            for i, event in enumerate(all_events):
                time_str = f"{event['start'].strftime('%H:%M')}-{event['end'].strftime('%H:%M')}"
                docenti_names = event.get('docenti', '')
                
                # Divisore
                if i > 0:
                    code_block_content += "-----------\n"
                
                code_block_content += f"{time_str} {event['nome']}\n"
                if docenti_names:
                    code_block_content += f"{docenti_names}\n"
            
            text += f"```\n{code_block_content}```\n"
    
    # Aggiungi link alla fine
    polo = aula.get('polo', 'fibonacci')
    polo_data = load_unified_json().get("polo", {}).get(polo, {})
    
    footer_links = []
    aula_id = aula.get("id")
    if not aula_id:
        aula_id = polo_data.get("id")
    if aula_id:
        footer_links.append(f"[LA MAPPA ↗](https://unipi.lamappa.org/{aula_id})")
    
    gmaps = polo_data.get("google_maps")
    amaps = polo_data.get("apple_maps")
    if gmaps:
        footer_links.append(f"[Google Maps ↗]({gmaps})")
    elif amaps:
        footer_links.append(f"[Apple Maps ↗]({amaps})")
    
    if footer_links:
        text += "\n" + " • ".join(footer_links)
    
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
    "Scrivici su Telegram: @doveunipi\n"
    "<a href='https://github.com/plumkewe/dove-unipi/issues'>Apri una issue su GitHub</a>\n"
    "Scrivici su <a href='https://www.instagram.com/doveunipi/'>Instagram</a>"
)

# --- COMANDI STANDARD ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Load polos dynamically
    data = load_unified_json()
    polo_lines = []
    # Simplified list for start message
    polo_list_text = "• Fibonacci (+fib)\n• Ingegneria (+ing)\n• Carmignani (+car)\n• San Rossore (+sr)"

    text = (
        "<b>DOVE?UNIPI</b>\n\n"
        "Il bot per trovare aule e biblioteche dell'Università di Pisa.\n\n"
        "<b>Ricerca Inline</b>\n"
        "In qualsiasi chat, digita:\n"
        "<code>@doveunipibot nome aula</code>\n"
        "(es. <code>@doveunipibot N1</code> o <code>@doveunipibot C41</code>)\n\n"
        "<b>Posizione Polo</b>\n"
        "Digita il nome di un polo per ricevere i link a LA MAPPA UniPi, Google Maps e Apple Maps:\n"
        "<code>@doveunipibot nome polo</code>\n"
        "(es. <code>@doveunipibot fibonacci</code>, <code>@doveunipibot porta nuova</code>)\n\n"
        "<b>Filtri</b>\n"
        "Puoi filtrare la ricerca per polo specificando:\n"
        "• <b>+fib</b> per Fibonacci\n"
        "• <b>+ing</b> per Ingegneria\n"
        "• <b>+car</b> per Carmignani\n"
        "• <b>+sr</b> per San Rossore\n"
        "(es. <code>@doveunipibot B +ing</code>)\n\n"
        "<b>Cerca Biblioteca</b>\n"
        "Per cercare una biblioteca e vedere orari e info:\n"
        "<code>@doveunipibot b:nome biblioteca</code>\n\n"
        "<b>Stato Aula</b>\n"
        "Per vedere lo stato di un'aula:\n"
        "<code>@doveunipibot s:nome aula</code>\n"
        "Puoi aggiungere <code>+1</code>, <code>+2</code>... per i giorni successivi.\n\n"
        "<b>Stato Aula Interattivo</b>\n"
        "Per vedere lo stato con navigazione giorni:\n"
        "<code>@doveunipibot si:nome aula</code>\n\n"
        "<b>Occupazione Rapida</b>\n"
        "Premi il nome di un polo dai tasti in fondo alla chat per vedere subito l'occupazione di tutte le sue aule.\n\n"
        "<b>Filtra per Orario</b>\n"
        "Rispondi a un messaggio di occupazione con un orario per filtrare le aule:\n"
        "• <code>13:00</code> → solo aule libere da quell'ora a fine giornata\n"
        "• <code>13:00-15:00</code> → solo aule libere per l'intero intervallo\n\n"
        "<b>Comandi</b>\n"
        "/occupazione - Aule libere\n"
        "/biblioteche - Info biblioteche\n"
        "/links - Link utili\n"
        "/help - Guida dettagliata" +
        FEEDBACK_TEXT
    )
    
    keyboard = [
        [InlineKeyboardButton("Cerca aula", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("Cerca biblioteca", switch_inline_query_current_chat="b:")],
        [InlineKeyboardButton("Stato aula", switch_inline_query_current_chat="s:")],
        [InlineKeyboardButton("Stato interattivo", switch_inline_query_current_chat="si:")],
        [InlineKeyboardButton("Occupazione", callback_data="status:init")]
    ]
    
    await update.message.reply_sticker(
        sticker="CAACAgQAAxkBAAIZ8Gmcg5Xrs8mRCYW3UspAhShG3KyDAALvCQACSHnpULER21GjKvuQOgQ",
        reply_markup=build_polo_reply_keyboard()
    )
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
        f"Sito Web DOVE?UNIPI: {SITE_URL}\n\n"
        "Sito Web LA MAPPA UniPi: https://unipi.lamappa.org/app\n\n"
        "Instagram: https://instagram.com/unipilamappaorg\n\n"
        "Twitter: https://x.com/unipilamappaorg"
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
        "/biblioteche - Lista biblioteche e orari\n"
        "/occupazione - Mostra lo stato delle aule navigando per edifici\n"
        "/links - Link utili (GitHub, Sito, Social)\n"
        "/help - Mostra questo messaggio\n\n"
        "<b>Tasti Polo</b>\n"
        "In fondo alla chat trovi un tasto per ogni polo disponibile.\n"
        "Premendolo vedi subito l'occupazione di tutte le aule di quel polo, senza navigare i menu.\n\n"
        "<b>1. Ricerca Inline</b>\n"
        "Puoi cercare <b>Aule</b>, <b>Posizioni</b>, <b>Biblioteche</b> e <b>Uffici</b> direttamente in qualsiasi chat.\n\n"
        "Digita il nome del bot seguito dalla ricerca:\n"
        "Esempio:\n"
        "<code>@doveunipibot N1</code>\n"
        "Output:\n"
        "<pre>Polo Ingegneria › Edificio B › Piano T › Aula N1\nClicca per aprire su LA MAPPA ↗</pre>\n\n"
        "<b>2. Posizione Polo</b>\n"
        "Digita il nome di un polo per ricevere i link a Google Maps e Apple Maps:\n"
        "<code>@doveunipibot [nome polo]</code>\n"
        "Esempi: <code>@doveunipibot fibonacci</code>, <code>@doveunipibot porta nuova</code>\n\n"
        "<b>3. Filtri Polo</b>\n"
        "Se ottieni troppi risultati, puoi filtrare per polo:\n"
        "• <b>+fib</b>: Filtra per Fibonacci\n"
        "• <b>+ing</b>: Filtra per Ingegneria\n"
        "• <b>+car</b>: Filtra per Carmignani\n"
        "• <b>+sr</b>: Filtra per San Rossore\n"
        "Esempio: <code>@doveunipibot Aula B +ing</code> (cerca 'Aula B' solo a Ingegneria)\n\n"
        "<b>4. Verifica Stato Aula</b>\n"
        "Vedi se un'aula è libera o occupata:\n"
        "<code>@doveunipibot s:F</code>\n"
        "Per vedere i giorni successivi, aggiungi un numero:\n"
        "<code>@doveunipibot s:F +1</code> (domani)\n\n"
        "<b>Navigazione Interattiva:</b>\n"
        "<code>@doveunipibot si:F</code> (mostra tasti per cambiare giorno)\n\n"
        "<b>6. Cerca Biblioteca</b>\n"
        "Cerca una biblioteca per vedere informazioni e orari:\n"
        "<code>@doveunipibot b:Matematica</code>\n\n"
        "<b>8. Filtro Orario Occupazione</b>\n"
        "Dopo aver ricevuto un messaggio di occupazione (polo, edificio o piano), rispondi direttamente con:\n"
        "• <code>13:00</code> → mostra solo le aule libere da quell'ora fino a fine giornata\n"
        "• <code>13:00-15:00</code> → mostra solo le aule libere per l'intero intervallo specificato\n"
        "Funziona sui messaggi inviati tramite i tasti polo, il comando /occupazione o i bottoni TUTTI.\n\n"
        "<b>Pulsanti e Navigazione</b>\n"
        "<b>○</b>: Indietro / Menu Superiore\n"
        "<b>↺</b>: Aggiorna dati correnti\n"
        "<b>◀ ▶</b>: Cambia pagina o giorno\n\n"
        "I pulsanti si trovano sempre nella stessa posizione (es. 'Indietro' è sempre al centro, 'Aggiorna' sempre a destra).\n\n"
        "<b>Colori</b>\n"
        "I colori degli edifici e dello stato delle aule corrispondono esattamente a quelli visibili su LA MAPPA UniPi, per un'esperienza visiva coerente." +
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
    
    # Left Button: Back (Day - 1) solo se non siamo già ad oggi
    if offset > 0:
        row.append(InlineKeyboardButton("◀", callback_data=f"status:day_offset:{aula_id}:{offset-1}"))
    else:
        row.append(InlineKeyboardButton(" ", callback_data="status:noop"))
    
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
    """Crea la tastiera per navigazione 'Tutti' (avanti/indietro, back smart)."""
    row_nav = []
    
    # Left: Back (Day - 1) solo se non siamo già ad oggi
    if offset > 0:
        row_nav.append(InlineKeyboardButton("◀", callback_data=f"{current_callback_base}:{offset-1}"))
    else:
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

        text = f"*{get_polo_display_name(polo)}*\n\nSeleziona un edificio:"
        
        keyboard = [
            [InlineKeyboardButton("TUTTI", callback_data=f"status:tutti_polo:{polo}")]
        ]
        
        # Bottoni edifici (2 per riga)
        row = []
        for i, edificio in enumerate(edifici):
            display_name = get_edificio_display_name(polo, edificio)
            
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
        all_rooms = get_aule_polo(polo)
        biblio_hours = await _fetch_scope_biblio_hours(all_rooms, target_date)
        text = format_polo_status(polo, events, target_date, biblio_hours=biblio_hours)
        text = _safe_truncate(text)
        
        keyboard = get_smart_back_keyboard(offset, f"status:polo:{polo}", f"status:tutti_polo:{polo}")

        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        context.chat_data[f"occ_{query.message.message_id}"] = {
            'type': 'polo', 'polo': polo, 'edificio': None, 'piano': None,
            'target_date_iso': target_date.isoformat(), 'offset': offset,
        }

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
        status = get_aula_status(aula['nome'], events, target_date, polo=polo, edificio=aula.get('edificio'))
        
        # Formatta messaggio per il giorno specifico
        # Se offset == 0 usa formato standard, altrimenti formato programma
        if offset == 0:
            # Trova URL per link MAPPA
            aula_id = aula.get("id")
            if not aula_id:
                polo_data = load_unified_json().get("polo", {}).get(polo, {})
                aula_id = polo_data.get("id", "")
            dove_url = f"https://unipi.lamappa.org/{aula_id}" if aula_id else ""
            
            text = await format_single_aula_status(aula, status, target_date, dove_url)
        else:
            # Usa il nuovo helper
            text = await format_day_schedule(aula, events, target_date)
        
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
        all_rooms = get_aule_edificio(polo, edificio)
        biblio_hours = await _fetch_scope_biblio_hours(all_rooms, target_date)
        text = format_edificio_status(polo, edificio, events, target_date, biblio_hours=biblio_hours)
        text = _safe_truncate(text)
        
        keyboard = get_smart_back_keyboard(offset, f"status:edificio:{polo}:{edificio}", f"status:tutti_edificio:{polo}:{edificio}")

        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        context.chat_data[f"occ_{query.message.message_id}"] = {
            'type': 'edificio', 'polo': polo, 'edificio': edificio, 'piano': None,
            'target_date_iso': target_date.isoformat(), 'offset': offset,
        }

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
        all_rooms = get_aule_edificio(polo, edificio)
        biblio_hours = await _fetch_scope_biblio_hours(all_rooms, target_date)
        text = format_piano_status(polo, edificio, piano, events, target_date, biblio_hours=biblio_hours)
        text = _safe_truncate(text)
        
        keyboard = get_smart_back_keyboard(offset, f"status:piano:{polo}:{edificio}:{piano}", f"status:tutti_piano:{polo}:{edificio}:{piano}")

        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        context.chat_data[f"occ_{query.message.message_id}"] = {
            'type': 'piano', 'polo': polo, 'edificio': edificio, 'piano': piano,
            'target_date_iso': target_date.isoformat(), 'offset': offset,
        }

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
    
    # status:a:<aula_id> - Singola aula
    elif action == "a":
        aula_id = parts[2] if len(parts) > 2 else ""
        
        # Trova l'aula
        aule = get_all_aule()
        aula = None
        for a in aule:
            if a.get('id') == aula_id:
                aula = a
                break
        
        if not aula:
            await query.message.edit_text("Aula non trovata")
            return
            
        polo = aula.get('polo')
        edificio = aula.get('edificio')
        piano = aula.get('piano')
        
        events = await fetch_day_events_async(get_calendar_id(polo), now)
        status = get_aula_status(aula['nome'], events, now, polo=polo, edificio=edificio)
        
        # Trova URL per link MAPPA
        aula_id = aula.get("id")
        if not aula_id:
            polo_data = load_unified_json().get("polo", {}).get(polo, {})
            aula_id = polo_data.get("id", "")
        dove_url = f"https://unipi.lamappa.org/{aula_id}" if aula_id else ""
        
        text = await format_single_aula_status(aula, status, now, dove_url)
        
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
    
    if not edificio or edificio == '?' or normalize_short_code(polo) == normalize_short_code(edificio) or len(get_edifici(polo)) <= 1:
        text = f"*{get_polo_display_name(polo)}*\n\nSeleziona un piano:"
    else:
        text = f"*{get_polo_display_name(polo)} - {get_edificio_display_name(polo, edificio)}*\n\nSeleziona un piano:"
    
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
    
    if not edificio or edificio == '?' or normalize_short_code(polo) == normalize_short_code(edificio) or len(get_edifici(polo)) <= 1:
        text = f"*{get_polo_display_name(polo)} - Piano {piano}*\n\n"
    else:
        text = f"*{get_polo_display_name(polo)} - {get_edificio_display_name(polo, edificio)} - Piano {piano}*\n\n"
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
            callback_data=f"status:a:{aula_id}"
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

# --- TIME FILTER REPLY HANDLER ---
async def handle_time_filter_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles a user reply to an occupancy message with a time or time-range filter.
    e.g. replying "13:00" or "14:00-16:00" to a polo/edificio/piano status message.
    Edits the original occupancy message in place instead of sending a new one.
    """
    if not update.message or not update.message.reply_to_message:
        return
    if not update.message.text:
        return

    reply_text = update.message.text.strip()
    time_filter = parse_time_filter(reply_text)
    if not time_filter:
        return

    # The replied-to message must be from this bot
    bot_user = await context.bot.get_me()
    replied = update.message.reply_to_message
    if not replied.from_user or replied.from_user.id != bot_user.id:
        return

    # Look up occupancy context stored when the message was sent/edited
    occ_ctx = context.chat_data.get(f"occ_{replied.message_id}")
    if not occ_ctx:
        return

    polo = occ_ctx['polo']
    edificio = occ_ctx.get('edificio')
    piano = occ_ctx.get('piano')
    occ_type = occ_ctx['type']
    offset = occ_ctx.get('offset', 0)

    # Reconstruct the date the occupancy message referred to
    try:
        target_date = datetime.fromisoformat(occ_ctx['target_date_iso'])
        if target_date.tzinfo is None:
            target_date = TZ_ROME.localize(target_date)
    except Exception:
        target_date = datetime.now(TZ_ROME)

    # Apply the user's time to the original day (not necessarily today)
    def _apply_time(base: datetime, h: int, m: int) -> datetime:
        return base.replace(hour=h, minute=m, second=0, microsecond=0)

    tf_start = _apply_time(target_date, time_filter['start'].hour, time_filter['start'].minute)
    if time_filter['type'] == 'range':
        tf_end = _apply_time(target_date, time_filter['end'].hour, time_filter['end'].minute)
        adjusted_filter = {'type': 'range', 'start': tf_start, 'end': tf_end}
    else:
        # 'from': free from that hour to end of day
        tf_end = target_date.replace(hour=23, minute=59, second=0, microsecond=0)
        adjusted_filter = {'type': 'from', 'start': tf_start, 'end': tf_end}

    events = await fetch_day_events_async(get_calendar_id(polo), target_date)

    if occ_type == 'polo':
        text = format_polo_status(polo, events, target_date, time_filter=adjusted_filter)
        keyboard = get_smart_back_keyboard(offset, f"status:polo:{polo}", f"status:tutti_polo:{polo}")
    elif occ_type == 'edificio':
        text = format_edificio_status(polo, edificio, events, target_date, time_filter=adjusted_filter)
        keyboard = get_smart_back_keyboard(offset, f"status:edificio:{polo}:{edificio}", f"status:tutti_edificio:{polo}:{edificio}")
    elif occ_type == 'piano':
        text = format_piano_status(polo, edificio, piano, events, target_date, time_filter=adjusted_filter)
        keyboard = get_smart_back_keyboard(offset, f"status:piano:{polo}:{edificio}:{piano}", f"status:tutti_piano:{polo}:{edificio}:{piano}")
    else:
        return

    text = _safe_truncate(text)

    # Edit the original occupancy message in place
    try:
        await context.bot.edit_message_text(
            chat_id=replied.chat.id,
            message_id=replied.message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Errore edit time filter: {e}")
            return

    # Delete the user's reply (time input) to keep the chat clean
    try:
        await update.message.delete()
    except Exception:
        pass


# --- INLINE QUERY ---
async def handle_polo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i bottoni della reply keyboard: se il testo corrisponde al nome di un polo
    mostra l'occupazione del polo."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower().strip()
    data = load_unified_json()

    for polo_key, polo_data in data.get("polo", {}).items():
        keywords = [polo_key]
        nome_display = polo_data.get("nome", polo_key.capitalize())
        keywords.append(nome_display.lower())
        if "alias" in polo_data:
            keywords.extend([a.lower() for a in polo_data["alias"]])

        for kw in keywords:
            kw_clean = kw.replace("polo", "").strip()
            text_clean = text.replace("polo", "").strip()
            if text_clean == kw_clean:
                now = datetime.now(tz=pytz.timezone('Europe/Rome'))
                events = await fetch_day_events_async(get_calendar_id(polo_key), now)
                all_rooms = get_aule_polo(polo_key)
                biblio_hours = await _fetch_scope_biblio_hours(all_rooms, now)
                status_text = format_polo_status(polo_key, events, now, biblio_hours=biblio_hours)
                status_text = _safe_truncate(status_text)
                keyboard = get_smart_back_keyboard(0, f"status:polo:{polo_key}", f"status:tutti_polo:{polo_key}")
                sent = await update.message.reply_text(
                    status_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                context.chat_data[f"occ_{sent.message_id}"] = {
                    'type': 'polo', 'polo': polo_key, 'edificio': None, 'piano': None,
                    'target_date_iso': now.isoformat(), 'offset': 0,
                }
                return

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
        

        # GESTIONE b: PER RICERCA BIBLIOTECHE
    if query.startswith("b:"):
        bib_search = query[2:].strip()
        results = await search_biblioteca_inline(bib_search)
        if not results:
            no_results_button = InlineQueryResultsButton(text="Nessuna biblioteca trovata", start_parameter="empty")
            await update.inline_query.answer([], cache_time=0, button=no_results_button)
        else:
            await update.inline_query.answer(results[:50], cache_time=0)
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
            "title": "Sito Web DOVE?UNIPI",
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
        },
        {
            "id": "special_twitter",
            "type": "article",
            "title": "Twitter (X)",
            "description": "Seguici su X",
            "url": "https://x.com/unipilamappaorg",
            "thumb": INFO_ICON_URL,
            "keywords": ["twitter", "x", "social"]
        },
        {
            "id": "special_lamappa",
            "type": "article",
            "title": "LA MAPPA UniPi",
            "description": "L'app interattiva della mappa",
            "url": "https://unipi.lamappa.org/app",
            "thumb": MAP_ICON_URL,
            "keywords": ["mappa", "app", "sito", "lamappa"]
        }
    ]

    # SE LA QUERY È VUOTA: Istruzioni -> Mappa -> Link
    if not query:
        # A. Istruzioni (Mini-guida)
        instructions = [
            {
                "id": "inst_inline",
                "title": "Ricerca Inline",
                "desc": "<nome> (es. A1, Rossi)",
                "text": "*COME CERCARE UN'AULA O UNA PERSONA*\n\nVuoi trovare un'aula o il contatto di un membro del personale al volo?\nDigita nella chat:\n`@doveunipibot nome`\n\n_Esempio:_ `@doveunipibot A1` oppure `@doveunipibot Rossi`\n\nIl bot ti mostrerà subito le informazioni principali e dove si trova!"
            },
            {
                "id": "inst_s",
                "title": "Stato Aula",
                "desc": "s:<aula> (es. s:B, s:N1 +1)",
                "text": "*COME CONTROLLARE LO STATO DI UN'AULA*\n\nVuoi sapere se un'aula è libera o occupata oggi?\nDigita nella chat:\n`@doveunipibot s:nome_aula`\n\n_Esempio:_ `@doveunipibot s:B` oppure `@doveunipibot s:N1 +1` (per domani)\n\nIl bot ti fornirà gli orari di occupazione dell'aula per la giornata richiesta!"
            },
            {
                "id": "inst_si",
                "title": "Stato Interattivo",
                "desc": "si:<aula> (es. si:C, si:A1)",
                "text": "*COME NAVIGARE L'OCCUPAZIONE DI UN'AULA*\n\nVuoi vedere gli orari di un'aula e poterti spostare facilmente tra i giorni della settimana con dei comodi bottoni?\nDigita nella chat:\n`@doveunipibot si:nome_aula`\n\n_Esempio:_ `@doveunipibot si:C`\n\nIl bot ti mostrerà lo stato dell'aula e dei bottoni interattivi per scorrere i giorni!"
            },
            {
                "id": "inst_b",
                "title": "Cerca Biblioteca",
                "desc": "b:<nome> (es. b:Matematica)",
                "text": "*COME CERCARE UNA BIBLIOTECA*\n\nVuoi controllare gli orari di apertura o trovare informazioni su una biblioteca?\nDigita nella chat:\n`@doveunipibot b:nome_biblioteca`\n\n_Esempio:_ `@doveunipibot b:Matematica`\n\nIl bot ti fornirà tutti gli orari aggiornati della settimana per la biblioteca richiesta!"
            },
            {
                "id": "inst_filter",
                "title": "Filtra per Polo",
                "desc": "<query> +<polo> (es. A +fib, Aula 1 +car, B1 +sr)",
                "text": "*COME FILTRARE I RISULTATI PER POLO*\n\nStai ottenendo troppi risultati di aule o persone e vuoi restringere la ricerca a un polo specifico?\nAggiungi alla fine della tua ricerca il comando `+nome_polo` (o il suo prefisso).\n\nPoli disponibili:\n• `+fib` → Fibonacci\n• `+ing` → Ingegneria\n• `+car` → Carmignani\n• `+sr` → San Rossore\n\n_Esempio:_ `@doveunipibot A +fib`\n\nIl bot cercherà l'elemento esclusivamente all'interno del polo indicato!"
            },
            {
                "id": "inst_map",
                "title": "Posizione Polo",
                "desc": "Scrivi il nome del polo (es. fibonacci)",
                "text": "*COME TROVARE LA POSIZIONE DI UN POLO*\n\nNon sai dove si trova un polo dell'università?\nDigita semplicemente nella chat il nome del polo:\n\n_Esempio:_ `@doveunipibot fibonacci`\n\nIl bot ti invierà i link diretti a Google Maps e Apple Maps per raggiungere il polo!"
            },
            {
                "id": "inst_occupazione",
                "title": "Aule libere",
                "desc": "Usa /occupazione nella chat del bot per vedere lo status di tutte le aule",
                "text": "*COME TROVARE LE AULE LIBERE*\n\nSei alla disperata ricerca di un posto per studiare in questo momento?\nVai nella chat privata del bot e usa il comando apposito:\n\n_Comando:_ `/occupazione`\n\nIl bot controllerà in tempo reale e ti darà una lista delle aule miracolosamente libere ora!"
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
        # 0. CERCA MAPPE POLI E EDIFICI (Dynamic from unified.json)
        unified_data = load_unified_json()
        
        # --- PARSING PARAMETRO POLO (+param) ---
        parsed_query = parse_query_modifiers(query)
        polo_filter = parsed_query['polo_filter']
        query = parsed_query['clean_query']
        
        # Base URL per le immagini pubbliche.
        # Default: GitHub raw sul branch main. Override con env `MAPS_RAW_BASE_URL`.
        raw_repo_base = os.environ.get(
            "MAPS_RAW_BASE_URL",
            "https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/img/mappe/",
        )
        
        # Helper per processare un item (polo o edificio) e aggiungere risultati
        def process_map_item(item_key, item_data, parent_name=None):
            # Se siamo filtrati per polo, controlliamo se questo item appartiene al polo giusto
            # (Per i poli top-level, item_key è il polo_key. Per edifici, parent_name è il polo_key)
            if polo_filter:
                current_polo = parent_name if parent_name else item_key
                if current_polo != polo_filter:
                    return

            mappa_file = item_data.get("mappa")
            coords = item_data.get("coordinates") or {}
            has_lat_lng = bool(coords.get("lat") and coords.get("lng"))

            has_gmaps = bool(item_data.get("google_maps"))
            has_amaps = bool(item_data.get("apple_maps"))

            # Serve almeno le coordinate lat/lng, un file mappa, o dei link esterni
            if not has_lat_lng and not mappa_file and not has_gmaps and not has_amaps:
                return

            # Raccogli keywords (nome, alias, alternative_names)
            keywords = [item_key]
            nome_display = item_data.get("nome", item_key.capitalize())
            keywords.append(nome_display.lower())

            if "alias" in item_data:
                keywords.extend([a.lower() for a in item_data["alias"]])
            
            if "alternative_names" in item_data:
                keywords.extend([a.lower() for a in item_data["alternative_names"]])

            # Logic MATCH per Inline
            match_map = False
            
            # MOSTRA TUTTE LE MAPPE SE UTENTE CERCA SOLO "Mappa"
            if query in ["mappa", "mappe", "map", "maps"]:
                match_map = True

            # Se query contiene "mappa" e una keyword
            elif "mappa" in query:
                for kw in keywords:
                    kw_clean = kw.replace("polo", "").strip()
                    if kw_clean in query:
                        match_map = True
                        break
            else:
                # Se query scatta un match diretto con il nome o alias (ignora "polo " nella query solo per i confronti puliti)
                query_clean = query.replace("polo", "").strip()
                
                # Check 1: Match esatto su Keywords (es. "polo fibonacci", "fibonacci", "pn")
                if query in keywords:
                    match_map = True
                
                # Check 2: Match su query pulita (es. "fibonacci" matches "polo fibonacci" se tolgo polo)
                elif not match_map:
                    for kw in keywords:
                        kw_clean = kw.replace("polo", "").strip()
                        # Se l'utente scrive "fibonacci" e la keyword è "fibonacci" -> OK
                        if query_clean == kw_clean:
                            match_map = True
                            break
                        # Se l'utente scrive "porta nuova" e la keyword è "porta nuova" -> OK
                        if query_clean in kw_clean and len(query_clean) > 3:
                            match_map = True
                            break
                        # Se l'utente scrive "polo porta nuova" e keyword è "porta nuova" -> OK (già coperto da check 1 se keyword conteneva polo)

                # Check 3: Flessibilità per ricerche parziali forti (es "porta n")
                if not match_map and len(query_clean) > 3:
                     for kw in keywords:
                         if query_clean in kw:
                             match_map = True
                             break
            
            if match_map:
                # Costruisci caption
                caption_title = item_data.get("nome", item_key.capitalize())
                if parent_name and not caption_title.lower().startswith("polo"):
                     # Aggiungi contesto polo se è un edificio
                     caption_title = f"{caption_title} ({parent_name.capitalize()})"

                address = item_data.get("address", "")
                gmaps = item_data.get("google_maps", "")
                amaps = item_data.get("apple_maps", "")
                
                # Format: Nome \n Indirizzo \n Link
                caption = f"*{caption_title}*\n"
                if address:
                    caption += f"{address}\n\n"
                
                links_parts = []
                if gmaps:
                    links_parts.append(f"[Google Maps↗]({gmaps})")
                if amaps:
                    links_parts.append(f"[Apple Maps↗]({amaps})")
                
                if links_parts:
                    caption += "  ".join(links_parts)

                # Evita duplicati basati su ID
                res_id = f"map_{item_key}"
                if not any(r.id == res_id for r in results):
                    # Risultato testo con indirizzo e link mappe
                    results.append(
                        InlineQueryResultArticle(
                            id=res_id,
                            title=caption_title,
                            description=address or "Info e link mappe",
                            input_message_content=InputTextMessageContent(
                                message_text=caption,
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True,
                            ),
                            thumbnail_url=MAP_ICON_URL,
                            thumbnail_width=100,
                            thumbnail_height=100,
                        )
                    )

        # Loop su poli ed edifici
        for polo_key, polo_data in unified_data.get("polo", {}).items():
            # Processa il polo stesso
            process_map_item(polo_key, polo_data)
            
            # Processa edifici all'interno del polo
            if "edificio" in polo_data:
                for ed_key, ed_data in polo_data["edificio"].items():
                    process_map_item(ed_key, ed_data, parent_name=polo_key)

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
                if polo_filter and polo_filter.replace('_', ' ') not in description.lower():
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
                    clean_desc = description.split("\n")[0].strip()
                    if url:
                        final_text = f"{clean_desc} › {title}\n\nClicca per aprire su [LA MAPPA ↗]({url})"
                    else:
                        # Mostra comunque il percorso anche se non c'è il link
                        final_text = f"{clean_desc} › {title}"
                    
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
                
                # Professors have ids starting with 's_' in the search index
                is_professor = result_id.startswith('s_')
                
                # Match esatto
                result_title_lower = result_title.lower()
                parts = result_title_lower.split()
                last_word = parts[-1] if parts else ""
                
                title_exact = (result_title_lower == query) or (result_title_lower == f"aula {query}") or (last_word == query)
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
    filter_edificio = parsed.get('edificio_filter')
    aula_search = parsed['clean_query']
    
    now = datetime.now(TZ_ROME)
    target_date = now + timedelta(days=offset)
    fetch_day = target_date if offset > 0 else now

    # --- STEP 1: Trova le aule che matchano SENZA chiamate API ---
    aule = get_all_aule()
    items = get_data()

    matched_aule = []
    for aula in aule:
        # FILTER: Se c'è un filtro polo e l'aula non corrisponde, salta
        if filter_polo and aula.get('polo', '').lower() != filter_polo:
            continue
            
        # FILTER: Se c'è un filtro edificio e l'aula non corrisponde, salta
        if filter_edificio and aula.get('edificio', '').lower() != filter_edificio.lower():
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
            
            nome_parts = nome_lower.split()
            last_word = nome_parts[-1] if nome_parts else ""
            
            # Match esatto (incluso il caso in cui cerchi solo la lettera/numero dell'aula, es. "b2" per "Pia B2")
            if (nome_code == aula_search or 
                nome_lower == aula_search or 
                nome_lower == f"aula {aula_search}" or
                last_word == aula_search or
                aula.get("codice", "").lower().strip() == aula_search):
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

    # --- STEP 2: Fetch eventi SOLO per i poli delle aule trovate, IN PARALLELO ---
    needed_polos = set(aula.get('polo', 'fibonacci') for _, _, aula in matched_aule)

    async def _fetch_polo(polo_key):
        cid = get_calendar_id(polo_key)
        return polo_key, await fetch_day_events_async(cid, fetch_day)

    if needed_polos:
        fetched_pairs = await asyncio.gather(*[_fetch_polo(p) for p in needed_polos])
        events_by_polo = dict(fetched_pairs)
    else:
        events_by_polo = {}

    # --- STEP 3: Processa le aule ordinate ---
    for priority, nome_lower, aula in matched_aule:
            edificio = aula.get('edificio', '?').upper()
            piano = aula.get('piano', '?')
            polo = aula.get('polo', 'fibonacci')
            events = events_by_polo.get(polo, [])
            
            if offset > 0:
                # Per giorni futuri usiamo lo start of day per il calcolo status (per vedere eventi)
                check_time = target_date.replace(hour=0, minute=0, second=1)
                status = get_aula_status(aula['nome'], events, check_time, polo=polo, edificio=aula.get('edificio'))
            else:
                status = get_aula_status(aula['nome'], events, now, polo=polo, edificio=aula.get('edificio'))
            
            # --- LINK MAPPA ---
            aula_id = aula.get("id")
            if not aula_id:
                polo_data = load_unified_json().get("polo", {}).get(polo, {})
                aula_id = polo_data.get("id", "")
            dove_url = f"https://unipi.lamappa.org/{aula_id}" if aula_id else ""
            final_text_main = ""

            item = find_dove_item(items, aula.get("nome", ""), polo=polo)
            if item:
                # Prepara testo per il risultato "standard" (Punto 1)
                if dove_url:
                    description = item.get("description", "")
                    clean_desc = description.split("\n")[0].strip()
                    # Formato richiesto: Path › Name
                    final_text_main = f"{clean_desc} › {item.get('title', '')}\n\nClicca per aprire su [LA MAPPA ↗]({dove_url})"
                else:
                    raw_input = item.get("input_message_content", {})
                    final_text_main = raw_input.get("message_text", "")
            else:
                if len(get_edifici(polo)) <= 1:
                    final_text_main = f"Aula {aula['nome']} ({get_polo_display_name(polo)})"
                else:
                    edificio_display = get_edificio_display_name(polo, edificio, short=False)
                    final_text_main = f"Aula {aula['nome']} ({edificio_display})"

            # 1. Prima aggiungi il risultato ESATTAMENTE come la ricerca normale (se item esiste)
            if item:
                parse_mode_item = item.get("input_message_content", {}).get("parse_mode", "Markdown")
                # Use a unique ID combining name and polo to avoid duplicates if multiple polos match
                unique_pos_id = f"pos_{polo}_{aula.get('nome','id')}_{item.get('id', str(uuid.uuid4()))}"
                results.append(
                    InlineQueryResultArticle(
                        id=unique_pos_id,
                        title=item.get("title", aula['nome']),
                        description=item.get("description", f"{get_polo_display_name(polo)} › Piano {piano}" if len(get_edifici(polo)) <= 1 else f"{get_edificio_display_name(polo, edificio, short=False)} › Piano {piano}"),
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
                # Thumbnail verde per libera (CERCHIO)
                status_thumb = "https://ui-avatars.com/api/?name=X&background=8cacaa&color=8cacaa&rounded=true&size=100"
            else:
                busy_suffix = ""
                if status.get('current_event'):
                    busy_suffix = f" • {status['current_event']['nome'][:50]}"
                elif status.get('next_events'):
                    busy_suffix = f" • {status['next_events'][0]['nome'][:50]}"
                status_description = f"Occupata fino alle {status['busy_until'].strftime('%H:%M')}{busy_suffix}"
                # Thumbnail rosso per occupata (CERCHIO)
                status_thumb = "https://ui-avatars.com/api/?name=X&background=b04859&color=b04859&rounded=true&size=100"
            
            # Formatta messaggio status
            # UNICA LOGICA: Mostra sempre il programma completo del giorno, senza header
            # Questo soddisfa la richiesta "mi mostra tutte le lezioni di quel giorno senza scrivere occupata fino a..."
            status_msg = await format_day_schedule(aula, events, target_date, show_title=False)
            
            # Per descrizione e thumb manteniamo logica attuale (utile per anteprima)
            if offset > 0:
                 # Per i giorni futuri, descrizione adattata
                 if status['next_events'] or status['current_event']:
                     status_description = f"Programma del {target_date.strftime('%d/%m')} - Occupata"
                     # Thumbnail rosso se ci sono eventi (CERCHIO)
                     status_thumb = "https://ui-avatars.com/api/?name=X&background=b04859&color=b04859&rounded=true&size=100"
                 else:
                     status_description = f"Programma del {target_date.strftime('%d/%m')} - Libera"
                     status_thumb = "https://ui-avatars.com/api/?name=X&background=8cacaa&color=8cacaa&rounded=true&size=100"
                     
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
                # Thumbnail rosso per occupazioni future (CERCHIO)
                future_thumb = "https://ui-avatars.com/api/?name=X&background=b04859&color=b04859&rounded=true&size=100"
                
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


def format_biblio_single_message(lib: dict, events: list, week_offset: int, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    """Genera messaggio 'Simple' per biblioteca (status e schedule)."""
    nid = lib.get('nid')
    
    # 1. NAME
    text = f"<b>{lib.get('nome','')}</b>\n"
    
    # 2. CAP
    cap = lib.get('capienza')
    if cap:
            text += f"Capienza: {cap}\n"
            
    # determine current status (TODAY) - Solo se siamo nella settimana corrente
    now_str = now.strftime("%H:%M")
    today_iso = now.strftime("%Y-%m-%d")

    if week_offset == 0:
        # Filter events for today (ISO match)
        today_evs = [e for e in events if e.get('date') == today_iso]
        
        # Calc logic
        status_line = "CHIUSA"
        times_today = []
        for ev in today_evs:
            s, e = ev.get('start_time','').strip(), ev.get('end_time','').strip()
            if s and e: times_today.append((s,e))
        times_today.sort()
        
        if times_today:
            # Check open/closed
            is_open = False
            for start, end in times_today:
                if now_str >= start and now_str < end:
                    status_line = f"APERTA - chiude alle {end}"
                    is_open = True
                    break
            
            if not is_open:
                # Check if opening later
                for start, end in times_today:
                    if start > now_str:
                        status_line = f"CHIUSA - apre alle {start}"
                        break

        # 3. STATUS
        text += f"\n{status_line}\n\n"
    else:
        text += "\n" # Spacer
    
    # Schedule <pre>
    # Iterate Mon-Sun
    today_date = now.date()
    start_of_current_week = today_date - timedelta(days=today_date.weekday()) # Monday
    target_monday = start_of_current_week + timedelta(weeks=week_offset)

    schedule_lines = []
    for i in range(7):
        day_date = target_monday + timedelta(days=i)
        day_str = day_date.strftime("%Y-%m-%d")
        
        day_name = WEEKDAYS_SHORT[i] # LUN
        formatted_date = day_date.strftime("%d/%m")
        
        day_events = [e for e in events if e.get('date') == day_str]
        
        time_range = "Chiusa"
        if day_events:
            time_ranges = []
            for ev in day_events:
                start = ev.get('start_time','').strip()
                end = ev.get('end_time','').strip()
                if start and end:
                    time_ranges.append(f"{start}-{end}")
            if time_ranges:
                    time_range = ", ".join(time_ranges)
        
        # Pad day/date/time for alignment
        line_str = f"{day_name} {formatted_date} {time_range}"
        schedule_lines.append(line_str)
        
    # 4. SCHEDULE
    text += "<pre>" + "\n".join(schedule_lines) + "</pre>\n\n"
    
    # 5. LINKS
    links = []
    l_sito = lib.get('link_sito')
    if l_sito:
        links.append(f"<a href='{l_sito}'>SITO↗</a>")
        
    l_maps = lib.get('google maps')
    if l_maps:
        links.append(f"<a href='{l_maps}'>GOOGLE MAPS↗</a>")
        
    if links:
        text += "  ".join(links) + "\n\n"
        
    # Navigation Keyboard: Week Back, Back, Week Fwd, Refresh
    row_nav = []
    row_nav.append(InlineKeyboardButton("◀", callback_data=f"biblio:single:{nid}:{week_offset-1}"))
    
    if week_offset != 0:
            row_nav.append(InlineKeyboardButton("○", callback_data=f"biblio:single:{nid}:0"))
    else:
            row_nav.append(InlineKeyboardButton("○", callback_data="biblio:init"))

    row_nav.append(InlineKeyboardButton("▶", callback_data=f"biblio:single:{nid}:{week_offset+1}"))
    
    row_refresh = [
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton("↺", callback_data=f"biblio:single:{nid}:{week_offset}")
    ]
    
    full_kb = InlineKeyboardMarkup([row_nav, row_refresh])
    
    return text, full_kb


def format_biblio_rich_message(lib: dict, events: list, week_offset: int, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    """Genera messaggio dettagliato per biblioteca (ricerca inline e aggiornamenti)."""
    nid = lib.get('nid')
    
    # 1. INFO BASE
    nome = lib.get('nome', '')
    text = f"<b>{nome}</b>\n"
    
    cap = lib.get('capienza')
    if cap: text += f"Capienza: {cap}\n"
    
    email = lib.get('email')
    if isinstance(email, list): email = ", ".join(email)
    if email: text += f"Email: {email.strip()}\n"
    
    tel = lib.get('telefono')
    if isinstance(tel, list): tel = ", ".join(tel)
    if tel: text += f"Telefono: {tel.strip()}\n"
    
    fax = lib.get('fax')
    if isinstance(fax, list): fax = ", ".join(fax)
    if fax: text += f"Fax: {fax.strip()}\n"
    
    addr = lib.get('indirizzo')
    if isinstance(addr, list): addr = ", ".join(addr)
    if addr: text += f"Indirizzo: {addr.strip()}\n"
    
    # 2. STATUS LINE (TODAY) - Solo se siamo nella settimana e offset 0?
    # La logica originale single mostra lo status SOLO se week_offset == 0
    today_iso = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%H:%M")
    
    if week_offset == 0:
        # Filter events for today
        today_evs = [e for e in events if e.get('date') == today_iso]
        
        status_line = "CHIUSA"
        times_today = []
        for ev in today_evs:
            s, e = ev.get('start_time','').strip(), ev.get('end_time','').strip()
            if s and e: times_today.append((s,e))
        times_today.sort()
        
        if times_today:
            is_open = False
            for start, end in times_today:
                if now_str >= start and now_str < end:
                    status_line = f"APERTA - chiude alle {end}"
                    is_open = True
                    break
            
            if not is_open:
                for start, end in times_today:
                    if start > now_str:
                        status_line = f"CHIUSA - apre alle {start}"
                        break
        
        text += f"\n{status_line}\n\n"
    else:
        text += "\n"

    # 3. SCHEDULE
    # Calculate target monday
    today_date = now.date()
    start_of_current_week = today_date - timedelta(days=today_date.weekday())
    target_monday = start_of_current_week + timedelta(weeks=week_offset)
    
    schedule_lines = []
    for i in range(7):
        day_date = target_monday + timedelta(days=i)
        day_str = day_date.strftime("%Y-%m-%d")
        day_name = WEEKDAYS_SHORT[i]
        formatted_date = day_date.strftime("%d/%m")
        
        day_events = [e for e in events if e.get('date') == day_str]
        
        time_range = "Chiusa"
        if day_events:
            time_ranges = []
            for ev in day_events:
                start = ev.get('start_time','').strip()
                end = ev.get('end_time','').strip()
                if start and end:
                    time_ranges.append(f"{start}-{end}")
            if time_ranges:
                 time_range = ", ".join(time_ranges)
        
        schedule_lines.append(f"{day_name} {formatted_date} {time_range}")
        
    text += "<pre>" + "\n".join(schedule_lines) + "</pre>\n\n"
    
    # 4. LINKS
    links = []
    l_sito = lib.get('link_sito')
    if l_sito: links.append(f"<a href='{l_sito}'>SITO↗</a>")
    
    l_maps = lib.get('google maps')
    if l_maps: links.append(f"<a href='{l_maps}'>GOOGLE MAPS↗</a>")
    
    if links: text += "  ".join(links) + "\n\n"

    # KEYBOARD (biblio:detail)
    row_nav = []
    row_nav.append(InlineKeyboardButton("◀", callback_data=f"biblio:detail:{nid}:{week_offset-1}"))
    
    if week_offset != 0:
         row_nav.append(InlineKeyboardButton("○", callback_data=f"biblio:detail:{nid}:0"))
    else:
         # Se siamo a 0, il bottone "oggi" ricarica init? No, nel caso inline init non esiste.
         # Ma per coerenza con /biblioteche usiamo detail:0 anche qui
         row_nav.append(InlineKeyboardButton("○", callback_data=f"biblio:detail:{nid}:0"))

    row_nav.append(InlineKeyboardButton("▶", callback_data=f"biblio:detail:{nid}:{week_offset+1}"))
    
    row_refresh = [
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton(" ", callback_data="status:noop"),
        InlineKeyboardButton("↺", callback_data=f"biblio:detail:{nid}:{week_offset}")
    ]
    
    markup = InlineKeyboardMarkup([row_nav, row_refresh])
    return text, markup


async def search_biblioteca_inline(bib_search: str) -> list:
    """Cerca biblioteche e restituisce info + stato orari come risultati inline."""
    results = []
    biblioteche = load_biblioteche_json()
    if not biblioteche:
        return results

    now = datetime.now(TZ_ROME)

    # Filtriamo per bib_search per evitare di caricare tutto
    matched = []
    if bib_search:
        search_terms = bib_search.lower().split()
        for bib in biblioteche:
            name = bib.get('nome', '').lower()
            aliases = [a.lower() for a in bib.get('alias', [])]
            if all(term in name or any(term in alias for alias in aliases) for term in search_terms):
                matched.append(bib)
    else:
        matched = biblioteche

    # FETCH: Fetch weekly range instead of just today for the Schedule view
    today_date = now.date()
    start_week = today_date - timedelta(days=today_date.weekday())
    end_week = start_week + timedelta(days=6)
    
    dt_monday = datetime.combine(start_week, datetime.min.time())
    dt_sunday = datetime.combine(end_week, datetime.min.time())

    fetch_tasks = []
    for bib in matched:
        nid = bib.get('nid', '')
        if nid:
            fetch_tasks.append(fetch_sba_opening_hours_async(nid, dt_monday, dt_sunday))
        else:
            fut = asyncio.get_event_loop().create_future()
            fut.set_result([])
            fetch_tasks.append(fut)

    all_hours = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    for i, bib in enumerate(matched):
        nome = bib.get('nome', '')
        capienza = bib.get('capienza', 0)
        nid = bib.get('nid', '')
        bib_id = bib.get('id', str(uuid.uuid4()))

        hours_data = all_hours[i] if not isinstance(all_hours[i], Exception) else []

        # --- Result 1: Library info card (RICH INTERACTIVE) ---
        desc_parts = []
        if capienza and int(capienza) > 0:
            desc_parts.append(f"{capienza} posti")
            
        addr = bib.get('indirizzo')
        if isinstance(addr, list):
             addr = ", ".join(addr)
        if addr:
             desc_parts.append(addr.strip())
             
        info_description = "\n".join(desc_parts) if desc_parts else None
        
        # Generate Rich Text
        # Note: events=hours_data, week_offset=0
        if nid:
            rich_text, rich_markup = format_biblio_rich_message(bib, hours_data, 0, now)
        else:
            # Fallback text if no NID/API
            rich_text = f"*{nome}*\n{capienza} posti" if capienza else f"*{nome}*"
            rich_markup = None

        results.append(
            InlineQueryResultArticle(
                id=f"bib_info_{bib_id}",
                title=f"{nome} (Informazioni)",
                description=info_description,
                input_message_content=InputTextMessageContent(
                    message_text=rich_text,
                    parse_mode=ParseMode.HTML, # Switch to HTML for rich text
                    disable_web_page_preview=True
                ),
                thumbnail_url=LIBRARY_ICON_URL,
                thumbnail_width=100,
                thumbnail_height=100,
                reply_markup=rich_markup
            )
        )

        # --- Result 2: Current status card (Simplified check) ---
        # Uses get_biblio_status_string which expects events for target date (today) or logic to parse
        # get_biblio_status_string uses "events" list. The API returns list of {date, start, end}
        # get_biblio_status_string filters for `dt_view` (today) internaly if logic matches?
        # Let's check get_biblio_status_string logic again. 
        # It takes `events`. `fetch_sba` returns list of slots for requested days.
        # `get_biblio_status_string` takes `events` and filtered them by date??
        # get_biblio_status_string filters them already?
        # NO. existing `get_biblio_status_string` does NOT filter by date if it's not today.
        
        # We need to filter events for TODAY to pass to get_biblio_status_string for correct status line
        today_iso = now.strftime("%Y-%m-%d")
        today_events_only = [e for e in hours_data if e.get('date') == today_iso]

        if nid:
            status_line = get_biblio_status_string(nome, today_events_only, now)
            # GENERATE SIMPLE MESSAGE
            simple_text, simple_markup = format_biblio_single_message(bib, hours_data, 0, now)

            # Determine thumb color
            # Libera/Aperta se non è chiusa e non ha una dicitura "apre alle" senza essere già aperta
            is_open_or_future = " - chiude" in status_line or (" - chiusa" not in status_line and " - apre alle" not in status_line)
            if is_open_or_future:
                status_thumb = "https://ui-avatars.com/api/?name=X&background=8cacaa&color=8cacaa&rounded=true&size=100"
            else:
                status_thumb = "https://ui-avatars.com/api/?name=X&background=b04859&color=b04859&rounded=true&size=100"

            status_desc = status_line.split(" - ", 1)[1] if " - " in status_line else status_line
            
            # Capitalize first letter of description
            if status_desc:
                status_desc = status_desc[0].upper() + status_desc[1:]
            
            # Use simple_text as the message content
            
            results.append(
                InlineQueryResultArticle(
                    id=f"bib_status_{bib_id}",
                    title="STATO ATTUALE (Aggiornabile)",
                    description=status_desc,
                    input_message_content=InputTextMessageContent(
                        message_text=simple_text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    ),
                    thumbnail_url=status_thumb,
                    thumbnail_width=100,
                    thumbnail_height=100,
                    reply_markup=simple_markup
                )
            )

    return results


def get_biblio_status_string(name, events, dt_view):
    """
    Format generic line for TUTTE view:
    Nome - chiude alle HH:MM
    Nome - apre alle HH:MM
    Nome - chiusa
    """
    now = datetime.now(TZ_ROME)
    is_today = (dt_view.date() == now.date())
    
    if not is_today:
        # FUTURE/PAST DAYS
        times = []
        for ev in events:
            start = ev.get('start_time', '').strip()
            end = ev.get('end_time', '').strip()
            if start and end:
                times.append((start, end))
        
        if not times:
            return f"{name} - chiusa"
        
        # Sort by start
        times.sort()
        schedule_str = ", ".join([f"{s}-{e}" for s, e in times])
        return f"{name} - {schedule_str}"

    # TODAY
    if not events:
        return f"{name} - chiusa"

    times = []
    for ev in events:
        start = ev.get('start_time', '').strip()
        end = ev.get('end_time', '').strip()
        if start and end:
            times.append((start, end))

    if not times:
        return f"{name} - chiusa"

    times.sort()
    current_time_str = now.strftime("%H:%M")

    # Check status
    for start, end in times:
        # If interval hasn't started yet
        if current_time_str < start:
             return f"{name} - apre alle {start}"
        
        # If inside interval
        if current_time_str >= start and current_time_str < end:
             return f"{name} - chiude alle {end}"
    
    # Check if there is next opening?
    # Loop again to see if there is a FUTURE opening today
    # (The previous loop breaks on first future opening, so if we are here,
    # it means all intervals are either current (handled) or past (not handled yet)
    # wait.
    
    # Correct logic for TODAY:
    # 1. Is it OPEN right now?
    for start, end in times:
        if current_time_str >= start and current_time_str < end:
            return f"{name} - chiude alle {end}"
            
    # 2. Is it opening LATER today?
    # Find first start > current
    for start, end in times:
        if start > current_time_str:
            return f"{name} - apre alle {start}"
            
    # 3. Else Closed (finished for today)
    return f"{name} - chiusa"


def build_biblioteche_keyboard():
    libs = load_biblioteche_json()
    keyboard = []
    # Bottone TUTTE
    keyboard.append([InlineKeyboardButton("TUTTE", callback_data="biblio:tutte:0")])
    
    # Sort libs by name
    libs.sort(key=lambda x: x.get('nome', ''))
    
    # Grid 2xN
    row = []
    for lib in libs:
        name = lib.get('nome', 'N/A')
        # Limita lunghezza nome
        if len(name) > 20: 
            name = name[:18] + ".."
        nid = lib.get('nid')
        if not nid: continue
        
        row.append(InlineKeyboardButton(name, callback_data=f"biblio:single:{nid}:0"))
        
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    return InlineKeyboardMarkup(keyboard)

async def biblioteche_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /biblioteche - mostra lista biblioteche."""
    text = "<b>Biblioteche</b>\n\nSeleziona una biblioteca:"
    await update.message.reply_text(
        text,
        reply_markup=build_biblioteche_keyboard(),
        parse_mode=ParseMode.HTML
    )

async def biblio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    parts = data.split(":")
    action = parts[1]
    
    now = datetime.now(TZ_ROME)

    if action == "init":
        text = "<b>Biblioteche</b>\n\nSeleziona una biblioteca:"
        await query.message.edit_text(
            text,
            reply_markup=build_biblioteche_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return

    # biblio:tutte:<offset>
    if action == "tutte":
        try:
            offset = int(parts[2])
        except: offset = 0
        
        # Le biblioteche come /occupazione supportano navigazione giorni
        target_date = now + timedelta(days=offset)
        target_date_str = target_date.strftime("%d/%m/%Y")
        
        libs = load_biblioteche_json()
        libs.sort(key=lambda x: x.get('nome', ''))
        
        # Parallel fetch
        tasks = []
        valid_libs = []
        for lib in libs:
            nid = lib.get('nid')
            if nid:
                valid_libs.append(lib)
                tasks.append(fetch_sba_opening_hours_async(nid, target_date, target_date))
        
        if not tasks:
            await query.message.edit_text("Nessuna biblioteca configurata.")
            return

        results = await asyncio.gather(*tasks)
        
        # HEADER STILE /occupazione
        # "Stato aule alle {HH:MM} del {DD/MM}" (Use "Stato biblioteche" for clarity)
        header_time = now.strftime('%H:%M')
        header_date = target_date.strftime('%d/%m')
        
        text = f"<b>Stato biblioteche alle {header_time} del {header_date}</b>\n\n"
        
        for lib, res in zip(valid_libs, results):
            name = lib.get('nome', 'Unknown')
            line = get_biblio_status_string(name, res, target_date)
            text += f"{line}\n"
        
        if len(text) > 4096:
            cut = text.rfind('\n', 0, 4090)
            if cut == -1:
                cut = 4090
            text = text[:cut]
        
        # Navigation keyboard (smart back like occupazione)
        row_nav = []
        row_nav.append(InlineKeyboardButton(" ", callback_data="status:noop")) 
        
        # Smart button
        if offset != 0:
             row_nav.append(InlineKeyboardButton("○", callback_data="biblio:tutte:0"))
        else:
             row_nav.append(InlineKeyboardButton("○", callback_data="biblio:init"))

        row_nav.append(InlineKeyboardButton("▶", callback_data=f"biblio:tutte:{offset+1}"))
        
        row_refresh = [
            InlineKeyboardButton(" ", callback_data="status:noop"),
            InlineKeyboardButton(" ", callback_data="status:noop"),
            InlineKeyboardButton("↺", callback_data=f"biblio:tutte:{offset}")
        ]
        
        full_kb = InlineKeyboardMarkup([row_nav, row_refresh])
        
        try:
            await query.message.edit_text(text, reply_markup=full_kb, parse_mode=ParseMode.HTML)
        except Exception: 
            pass # ignore not modified

    # biblio:single:<nid>:<week_offset>
    elif action == "single":
        nid = parts[2]
        try:
            week_offset = int(parts[3])
        except: week_offset = 0
        
        # Find lib info
        libs = load_biblioteche_json()
        lib = next((l for l in libs if l.get('nid') == nid), None)
        if not lib:
            await query.answer("Biblioteca non trovata")
            return

        # Calculate week range Monday-Sunday based on current week + offset
        today = now.date()
        start_of_current_week = today - timedelta(days=today.weekday()) # Monday
        
        target_monday = start_of_current_week + timedelta(weeks=week_offset)
        target_sunday = target_monday + timedelta(days=6)
        
        # Fetch data for the whole week
        # Need datetime objects for function
        dt_monday = datetime.combine(target_monday, datetime.min.time())
        dt_sunday = datetime.combine(target_sunday, datetime.min.time())
        
        events = await fetch_sba_opening_hours_async(nid, dt_monday, dt_sunday)
        
        # --- BUILD MESSAGE ---
        text, full_kb = format_biblio_single_message(lib, events, week_offset, now)
        
        try:
            if query.message:
                await query.message.edit_text(text, reply_markup=full_kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            elif query.inline_message_id:
                await context.bot.edit_message_text(
                    text,
                    inline_message_id=query.inline_message_id,
                    reply_markup=full_kb,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        except Exception:
            pass

    # biblio:detail:<nid>:<week_offset> (RICH view for inline)
    elif action == "detail":
        nid = parts[2]
        try:
            week_offset = int(parts[3])
        except: week_offset = 0
        
        # Find lib info
        libs = load_biblioteche_json()
        lib = next((l for l in libs if l.get('nid') == nid), None)
        if not lib:
            await query.answer("Biblioteca non trovata")
            return

        # Calculate week diff
        today = now.date()
        start_of_current_week = today - timedelta(days=today.weekday())
        target_monday = start_of_current_week + timedelta(weeks=week_offset)
        target_sunday = target_monday + timedelta(days=6)
        
        dt_monday = datetime.combine(target_monday, datetime.min.time())
        dt_sunday = datetime.combine(target_sunday, datetime.min.time())
        
        events = await fetch_sba_opening_hours_async(nid, dt_monday, dt_sunday)
        
        text, markup = format_biblio_rich_message(lib, events, week_offset, now)
        
        try:
            if query.message:
                await query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            elif query.inline_message_id:
                await context.bot.edit_message_text(
                    text, 
                    inline_message_id=query.inline_message_id,
                    reply_markup=markup, 
                    parse_mode=ParseMode.HTML, 
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Error editing message: {e}")
            pass


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
            BotCommand("occupazione", "Aule libere"),
            BotCommand("biblioteche", "Orari biblioteche"),
            BotCommand("links", "Link utili"),
            BotCommand("help", "Guida all'uso"),
        ]
        await application.bot.set_my_commands(commands)
    
    app.post_init = post_init
    
    # Comandi
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("occupazione", occupazione_command))
    app.add_handler(CommandHandler("biblioteche", biblioteche_command))
    app.add_handler(CommandHandler("links", links_command))
    app.add_handler(CommandHandler("help", help_command))

    
    # Callback per bottoni
    app.add_handler(CallbackQueryHandler(biblio_callback, pattern="^biblio:"))
    app.add_handler(CallbackQueryHandler(status_callback))

    # Gestione messaggi testuali (Filtro orario occupazione + Mappe Poli)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.REPLY,
        handle_time_filter_reply
    ))
    # Group 1 ensures handle_polo_message also runs for reply messages that don't match the time filter
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_polo_message), group=1)
    
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