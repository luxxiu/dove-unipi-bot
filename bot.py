import os
import json
import logging
import pytz
import asyncio
import requests
import hashlib
import time
import re
import urllib.request
from uuid import uuid4
from datetime import datetime
from typing import List, Dict, Any, Optional
from zoneinfo import ZoneInfo

# --- FIX per APScheduler < 3.10 su Python recenti ---
try:
    import apscheduler.util
    original_astimezone = apscheduler.util.astimezone
    def safe_astimezone(timezone):
        if timezone is None:
            return None
        try:
            return original_astimezone(timezone)
        except TypeError:
            if hasattr(timezone, 'key'):  # ZoneInfo
                return pytz.timezone(timezone.key)
            return pytz.utc
    apscheduler.util.astimezone = safe_astimezone
except ImportError:
    pass
# ----------------------------------------------------

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    Update,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters
)

# Configurazione Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Directory percorsi
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
AULE_GEOJSON_PATH = os.path.join(DATA_DIR, "aule2.geojson")

_asset_cache = {}

def get_asset_url(rel_path: str) -> str:
    """Restituisce l'URL GitHub Raw con cache-busting calcolato dall'hash MD5 del file locale."""
    fpath = os.path.join(ASSETS_DIR, rel_path)
    if os.path.isfile(fpath):
        mtime = os.path.getmtime(fpath)
        cached = _asset_cache.get(rel_path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(fpath, "rb") as f:
                h = hashlib.md5(f.read()).hexdigest()[:10]
        except Exception:
            h = str(int(mtime))
        url = f"https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/{rel_path}?v={h}"
        _asset_cache[rel_path] = (mtime, url)
        return url
    return f"https://raw.githubusercontent.com/luxxiu/dove-unipi-bot/main/assets/{rel_path}"

# Cache dati GeoJSON
_data_cache = None
_data_mtime = None

def load_aule_data() -> Dict[str, Any]:
    """Carica i dati da data/aule2.geojson con caching basato su mtime."""
    global _data_cache, _data_mtime
    try:
        mtime = os.path.getmtime(AULE_GEOJSON_PATH)
        if _data_cache is not None and _data_mtime == mtime:
            return _data_cache
        with open(AULE_GEOJSON_PATH, "r", encoding="utf-8") as f:
            _data_cache = json.load(f)
            _data_mtime = mtime
            logger.info(f"Caricati dati da {AULE_GEOJSON_PATH} (POIs: {len(_data_cache.get('pois', []))})")
            return _data_cache
    except Exception as e:
        logger.error(f"Errore durante il caricamento di {AULE_GEOJSON_PATH}: {e}")
        return {"pois": [], "piani": {}, "edifici": {}, "poli": {}}

class CustomInputRichMessageContent:
    """Rappresenta InputRichMessageContent per Telegram Bot API (Rich Message) nei risultati inline."""
    def __init__(self, blocks: list):
        self.blocks = blocks

    def to_dict(self, recursive=True):
        return {
            "rich_message": {
                "blocks": self.blocks
            }
        }


def is_same_polo_edificio(polo_name: str, edificio_name: str) -> bool:
    """Verifica se edificio e polo hanno lo stesso nome sostanziale."""
    if not edificio_name or not polo_name:
        return True
    p1 = polo_name.strip().lower()
    e1 = edificio_name.strip().lower()
    if p1 == e1:
        return True
    def clean(s: str) -> str:
        for prefix in ["polo ", "complesso ", "palazzo "]:
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s.strip()
    return clean(p1) == clean(e1)

def format_piano(piano: dict) -> str:
    """Formatta il livello del piano senza usare simboli di grado."""
    livello = piano.get("livello")
    if livello is None:
        return "Piano non specificato"
    if livello == 0:
        return "Piano Terra"
    elif livello == -1:
        return "Piano Seminterrato"
    return f"Piano {livello}"

def build_percorso(poi: dict, piano: dict, edificio: dict, polo: dict) -> str:
    """Costruisce il percorso nel formato: Polo › Edificio › Piano › Aula.
    Se edificio e polo hanno lo stesso nome, l'edificio non viene mostrato."""
    polo_name = (polo.get("nome") or "UniPi").strip()
    edificio_name = (edificio.get("nome") or "").strip()
    piano_str = format_piano(piano)
    aula_name = (poi.get("codice") or poi.get("nome") or "Aula").strip()

    parts = []
    if polo_name:
        parts.append(polo_name)
    if edificio_name and not is_same_polo_edificio(polo_name, edificio_name):
        parts.append(edificio_name)
    if piano_str:
        parts.append(piano_str)
    if aula_name:
        parts.append(aula_name)

    return " › ".join(parts)

# --- CINECA UP INTEGRATION & CACHE ---
_cineca_cache = {}  # (cal_id, date_str) -> (timestamp, list_of_events)
_CINECA_CACHE_TTL = 300  # 5 minuti

def extract_cineca_link_id(polo: dict, polo_id: str) -> Optional[str]:
    """Estrae l'id del calendario Cineca (24 caratteri hex) per il polo specificato."""
    if polo_id == "6501c860675557007eb417c0CUR":
        return "6501c860675557007eb417c0"
    cineca_link = polo.get("links", {}).get("cineca", "")
    match = re.search(r"linkCalendarioId=([a-zA-Z0-9]+)", cineca_link)
    if match:
        return match.group(1)[:24]
    return None

def fetch_cineca_events_sync(cal_id: str, date_str: str) -> list:
    """Effettua la chiamata POST a Cineca UP con retry automatico su errori server (50x)."""
    cache_key = (cal_id, date_str)
    now_ts = time.time()
    if cache_key in _cineca_cache:
        ts, cached_data = _cineca_cache[cache_key]
        if now_ts - ts < _CINECA_CACHE_TTL:
            return cached_data

    url = "https://apache.prod.up.cineca.it/api/Impegni/getImpegniCalendarioPubblico"
    payload = {
        "mostraImpegniAnnullati": True,
        "mostraIndisponibilitaTotali": False,
        "linkCalendarioId": cal_id,
        "clienteId": "628de8b9b63679f193b87046",
        "pianificazioneTemplate": False,
        "dataInizio": f"{date_str}T00:00:00",
        "dataFine": f"{date_str}T23:59:59"
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    _cineca_cache[cache_key] = (now_ts, data)
                    return data
        except Exception as e:
            if attempt == 0:
                time.sleep(0.4)
                continue
            logger.warning(f"Chiamata Cineca UP fallita per cal_id={cal_id}: {e}")
            break

    if cache_key in _cineca_cache:
        return _cineca_cache[cache_key][1]
    return []

async def get_polo_events_today(polo_id: str, now_dt: datetime) -> list:
    """Recupera in modo non bloccante gli eventi della giornata per il polo specificato."""
    data = load_aule_data()
    polo = data.get("poli", {}).get(polo_id, {})
    cal_id = extract_cineca_link_id(polo, polo_id)
    if not cal_id:
        return []
    date_str = now_dt.strftime("%Y-%m-%d")
    return await asyncio.to_thread(fetch_cineca_events_sync, cal_id, date_str)

def compute_aula_status(aula_id: str, aula_codice: str, events: list, now_dt: datetime) -> dict:
    """
    Calcola lo stato in tempo reale per un'aula applicando l'algoritmo di Interval Merging.
    Restituisce un dizionario con:
    - is_free: bool (se libera in questo istante)
    - status_text: str (es. 'Libera fino alle 11:30', 'Libera dalle 13:00 alle 15:30', 'Libera per tutta la giornata')
    """
    tz = ZoneInfo("Europe/Rome")
    codice_clean = (aula_codice or "").strip().upper()

    parsed = []
    for ev in events:
        matched = False
        for a in ev.get("aule", []):
            aid = a.get("id")
            acod = (a.get("codice") or "").strip().upper()
            if aid == aula_id or (codice_clean and acod == codice_clean):
                matched = True
                break
        if not matched:
            continue

        try:
            s_utc = datetime.fromisoformat(ev["dataInizio"].replace("Z", "+00:00"))
            e_utc = datetime.fromisoformat(ev["dataFine"].replace("Z", "+00:00"))
            s_loc = s_utc.astimezone(tz)
            e_loc = e_utc.astimezone(tz)
            if s_loc.date() == now_dt.date():
                parsed.append({"start": s_loc, "end": e_loc})
        except Exception:
            continue

    if not parsed:
        return {
            "is_free": True,
            "status_text": "Libera per tutta la giornata",
            "fino": None
        }

    parsed.sort(key=lambda x: x["start"])

    # Interval Merging: unione intervalli contigui o sovrapposti
    merged = []
    for it in parsed:
        if not merged:
            merged.append({"start": it["start"], "end": it["end"]})
        else:
            last = merged[-1]
            if it["start"] <= last["end"]:
                if it["end"] > last["end"]:
                    last["end"] = it["end"]
            else:
                merged.append({"start": it["start"], "end": it["end"]})

    # Controlla se occupata adesso
    current_idx = None
    for idx, it in enumerate(merged):
        if it["start"] <= now_dt < it["end"]:
            current_idx = idx
            break

    if current_idx is not None:
        curr = merged[current_idx]
        free_start_str = curr["end"].strftime("%H:%M")
        # Controlla se c'è un intervallo successivo con una finestra libera
        if current_idx + 1 < len(merged):
            next_it = merged[current_idx + 1]
            free_end_str = next_it["start"].strftime("%H:%M")
            if curr["end"] < next_it["start"]:
                return {
                    "is_free": False,
                    "status_text": f"Libera dalle {free_start_str} alle {free_end_str}",
                    "fino": free_start_str
                }
            else:
                return {
                    "is_free": False,
                    "status_text": f"Occupata fino alle {next_it['end'].strftime('%H:%M')}",
                    "fino": next_it["end"].strftime("%H:%M")
                }
        return {
            "is_free": False,
            "status_text": f"Libera dalle {free_start_str}",
            "fino": free_start_str
        }

    # Non occupata adesso: cerca il prossimo impegno odierno
    next_it = next((it for it in merged if it["start"] > now_dt), None)
    if next_it:
        next_start_str = next_it["start"].strftime("%H:%M")
        return {
            "is_free": True,
            "status_text": f"Libera fino alle {next_start_str}",
            "fino": next_start_str
        }

    # Tutti gli impegni odierni sono terminati
    return {
        "is_free": True,
        "status_text": "Libera per tutta la giornata",
        "fino": None
    }

def build_aula_rich_message(
    poi: dict,
    piano: dict,
    edificio: dict,
    polo: dict,
    percorso: str,
    schedule_info: dict = None
):
    """Costruisce i blocchi Rich Message per l'aula. Se schedule_info è presente, appende stato e orari."""
    codice = (poi.get("codice") or poi.get("nome") or "AULA").strip()
    cap = poi.get("capienza")
    if cap and cap > 0:
        header_text = f"{codice} | {cap} POSTI"
    else:
        header_text = codice.upper()

    aula_id = poi.get("id")

    blocks = [
        {
            "type": "heading",
            "size": 1,
            "text": header_text
        },
        {
            "type": "paragraph",
            "text": [
                {
                    "type": "bold",
                    "text": percorso
                }
            ]
        }
    ]

    tipo = poi.get("tipo")
    if tipo and tipo.lower() != "aula didattica":
        blocks.append({
            "type": "paragraph",
            "text": f"Tipologia: {tipo}"
        })

    # Se l'utente ha premuto ORARIO / AGGIORNA, appende la situazione attuale in quote e la tabella se presente
    if schedule_info is not None:
        st_text = schedule_info.get("status_text", "Libera per tutta la giornata")
        blocks.append({
            "type": "blockquote",
            "blocks": [
                {
                    "type": "paragraph",
                    "text": st_text
                }
            ]
        })

        table_cells = schedule_info.get("table_cells")
        if table_cells:
            blocks.append({
                "type": "table",
                "cells": table_cells
            })

    # Pulsanti: se schedule_info è presente, il tasto diventa AGGIORNA
    button_label = "AGGIORNA" if schedule_info is not None else "ORARIO"
    buttons = [
        {"text": button_label, "callback_data": f"sched|{aula_id}"},
        {"text": "LA MAPPA", "url": f"https://unipi.lamappa.org/{aula_id}"}
    ]
    gmaps_url = polo.get("links", {}).get("google_maps")
    if gmaps_url:
        buttons.append({"text": "GOOGLE MAPS", "url": gmaps_url})

    blocks.append({
        "type": "buttons",
        "align": "center",
        "buttons": buttons
    })

    return blocks

def build_aula_fallback(
    poi: dict,
    piano: dict,
    edificio: dict,
    polo: dict,
    percorso: str,
    schedule_info: dict = None
):
    """Costruisce testo e markup di fallback per client senza supporto Rich Message."""
    codice = (poi.get("codice") or poi.get("nome") or "AULA").strip()
    cap = poi.get("capienza")
    if cap and cap > 0:
        header_text = f"{codice} | {cap} POSTI"
    else:
        header_text = codice.upper()

    lines = [
        f"*{header_text}*\n",
        f"*{percorso}*"
    ]
    tipo = poi.get("tipo")
    if tipo and tipo.lower() != "aula didattica":
        lines.append(f"Tipologia: {tipo}")

    if schedule_info is not None:
        st_text = schedule_info.get("status_text", "Libera per tutta la giornata")
        lines.append(f"\n> {st_text}")
        events_list = schedule_info.get("events_list", [])
        if events_list:
            lines.append("")
            for item in events_list:
                lines.append(f"`{item['time']}` {item['materia']} - {item['docenti']}")

    aula_id = poi.get("id")
    button_label = "AGGIORNA" if schedule_info is not None else "ORARIO"
    fb_buttons = [
        [
            InlineKeyboardButton(button_label, callback_data=f"sched|{aula_id}"),
            InlineKeyboardButton("LA MAPPA", url=f"https://unipi.lamappa.org/{aula_id}")
        ]
    ]
    gmaps_url = polo.get("links", {}).get("google_maps")
    if gmaps_url:
        fb_buttons[0].append(InlineKeyboardButton("GOOGLE MAPS", url=gmaps_url))

    return "\n".join(lines), InlineKeyboardMarkup(fb_buttons)

def build_aula_card_by_id(aula_id: str):
    """Costruisce la scheda dell'aula tramite il suo id univoco."""
    data = load_aule_data()
    pois = data.get("pois", [])
    piani = data.get("piani", {})
    edifici = data.get("edifici", {})
    poli = data.get("poli", {})

    target_poi = next((p for p in pois if p.get("id") == aula_id), None)
    if not target_poi:
        return None, None, None

    piano = piani.get(target_poi.get("piano_id", ""), {})
    edificio = edifici.get(piano.get("edificio_id", ""), {})
    polo = poli.get(edificio.get("polo_id", ""), {})
    percorso = build_percorso(target_poi, piano, edificio, polo)
    blocks = build_aula_rich_message(target_poi, piano, edificio, polo, percorso)
    fb_text, fb_markup = build_aula_fallback(target_poi, piano, edificio, polo, percorso)
    return blocks, fb_text, fb_markup

async def build_aula_with_schedule(aula_id: str):
    """Costruisce la scheda dell'aula con aggiunta della tabella oraria e dello stato attuale."""
    data = load_aule_data()
    pois = data.get("pois", [])
    piani = data.get("piani", {})
    edifici = data.get("edifici", {})
    poli = data.get("poli", {})

    target_poi = next((p for p in pois if p.get("id") == aula_id), None)
    if not target_poi:
        return None, None, None

    piano = piani.get(target_poi.get("piano_id", ""), {})
    edificio = edifici.get(piano.get("edificio_id", ""), {})
    polo_id = edificio.get("polo_id", "")
    polo = poli.get(polo_id, {})
    percorso = build_percorso(target_poi, piano, edificio, polo)

    tz = ZoneInfo("Europe/Rome")
    now_dt = datetime.now(tz)
    events = await get_polo_events_today(polo_id, now_dt)

    codice = (target_poi.get("codice") or target_poi.get("nome") or "AULA").strip()
    status_info = compute_aula_status(aula_id, codice, events, now_dt)

    codice_clean = codice.upper()
    parsed = []
    for ev in events:
        matched = False
        for a in ev.get("aule", []):
            aid = a.get("id")
            acod = (a.get("codice") or "").strip().upper()
            if aid == aula_id or (codice_clean and acod == codice_clean):
                matched = True
                break
        if not matched:
            continue
        try:
            s_utc = datetime.fromisoformat(ev["dataInizio"].replace("Z", "+00:00"))
            e_utc = datetime.fromisoformat(ev["dataFine"].replace("Z", "+00:00"))
            s_loc = s_utc.astimezone(tz)
            e_loc = e_utc.astimezone(tz)
            if s_loc.date() == now_dt.date():
                parsed.append({
                    "start": s_loc,
                    "end": e_loc,
                    "nome": ev.get("nome") or "Attività Didattica",
                    "docenti": ev.get("docenti") or []
                })
        except Exception:
            continue

    parsed.sort(key=lambda x: x["start"])

    table_cells = None
    events_list = []
    if parsed:
        table_cells = [
            [
                {"text": "ORARIO", "is_header": True, "align": "center"},
                {"text": "MATERIA", "is_header": True, "align": "left"},
                {"text": "DOCENTE", "is_header": True, "align": "left"}
            ]
        ]
        for item in parsed:
            time_range = f"{item['start'].strftime('%H:%M')} – {item['end'].strftime('%H:%M')}"
            materia = item["nome"].strip()
            docs = []
            for d in item["docenti"]:
                n = d.get("nome", "").strip().capitalize()
                c = d.get("cognome", "").strip().capitalize()
                docs.append(f"{n[0]}. {c}" if n else c)
            docs_str = ", ".join(docs) if docs else "—"

            table_cells.append([
                {"text": time_range, "align": "center"},
                {"text": materia, "align": "left"},
                {"text": docs_str, "align": "left"}
            ])
            events_list.append({
                "time": time_range,
                "materia": materia,
                "docenti": docs_str
            })

    schedule_info = {
        "status_text": status_info["status_text"],
        "table_cells": table_cells,
        "events_list": events_list
    }

    blocks = build_aula_rich_message(target_poi, piano, edificio, polo, percorso, schedule_info=schedule_info)
    fb_text, fb_markup = build_aula_fallback(target_poi, piano, edificio, polo, percorso, schedule_info=schedule_info)
    return blocks, fb_text, fb_markup

def build_welcome_rich_message(is_group: bool = False):
    """Costruisce il Rich Message di benvenuto con stile diretto e pulito come su bot2.py."""
    if is_group:
        intro_text = "Per cercare le aule e le informazioni sui poli universitari utilizza le funzioni inline del bot."
    else:
        intro_text = "Per cercare un'aula digita il nome direttamente in qualsiasi chat oppure tocca il pulsante qui sotto."

    map_info_paragraph = {
        "type": "paragraph",
        "text": [
            "Per trovare le aule libere, controllare gli orari e gli avvisi delle biblioteche, uffici, erogatori d'acqua e tanti altri servizi dell'Ateneo, consulta ",
            {"type": "url", "text": "LA MAPPA UNIPI", "url": "https://unipi.lamappa.org"},
            "."
        ]
    }

    blocks = [
        {
            "type": "heading",
            "size": 1,
            "text": "DOVE?UNIPI BOT"
        },
        {
            "type": "paragraph",
            "text": intro_text
        },
        map_info_paragraph
    ]

    commands_items = [
        {"blocks": [{"type": "paragraph", "text": "/start - Messaggio di benvenuto"}]},
        {"blocks": [{"type": "paragraph", "text": "/links - Link utili e contatti"}]}
    ]
    commands_blocks = [
        {
            "type": "list",
            "items": commands_items
        }
    ]
    if is_group:
        commands_blocks.append({
            "type": "paragraph",
            "text": "I comandi nei gruppi sono visibili solo a te."
        })

    blocks.append({
        "type": "details",
        "summary": "Comandi disponibili",
        "blocks": commands_blocks
    })

    blocks.append({
        "type": "details",
        "summary": "Funzioni inline",
        "blocks": [
            {
                "type": "list",
                "items": [
                    {"blocks": [{"type": "paragraph", "text": "Ricerca aula o sala: @doveunipibot nome (es. @doveunipibot B1)"}]},
                    {"blocks": [{"type": "paragraph", "text": "Ricerca per polo: @doveunipibot polo (es. @doveunipibot Fibonacci)"}]}
                ]
            }
        ]
    })

    blocks.append({
        "type": "buttons",
        "align": "center",
        "buttons": [
            {
                "text": "CERCA AULA",
                "switch_inline_query_current_chat": ""
            },
            {
                "text": "LA MAPPA",
                "url": "https://unipi.lamappa.org"
            },
            {
                "text": "SEGUICI SU INSTAGRAM",
                "url": "https://www.instagram.com/unipilamappaorg"
            }
        ]
    })

    map_info_fb = "Per trovare le aule libere, controllare gli orari e gli avvisi delle biblioteche, uffici, erogatori d'acqua e tanti altri servizi, consulta [La Mappa UniPi](https://unipi.lamappa.org)."

    if is_group:
        fallback_text = (
            "*DOVE?UNIPI BOT*\n\n"
            f"{intro_text}\n\n"
            f"{map_info_fb}\n\n"
            "> *Comandi disponibili*\n"
            "> • /start - Messaggio di benvenuto\n"
            "> • /links - Link utili e contatti\n\n"
            "> I comandi nei gruppi sono visibili solo a te.\n\n"
            "> *Funzioni inline*\n"
            "> • Ricerca aula: `@doveunipibot B1`\n"
            "> • Ricerca per polo: `@doveunipibot Fibonacci`"
        )
    else:
        fallback_text = (
            "*DOVE?UNIPI BOT*\n\n"
            f"{intro_text}\n\n"
            f"{map_info_fb}\n\n"
            "> *Comandi disponibili*\n"
            "> • /start - Messaggio di benvenuto\n"
            "> • /links - Link utili e contatti\n\n"
            "> *Funzioni inline*\n"
            "> • Ricerca aula: `@doveunipibot B1`\n"
            "> • Ricerca per polo: `@doveunipibot Fibonacci`"
        )

    fallback_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("CERCA AULA", switch_inline_query_current_chat="")],
        [
            InlineKeyboardButton("LA MAPPA", url="https://unipi.lamappa.org"),
            InlineKeyboardButton("SEGUICI SU INSTAGRAM", url="https://www.instagram.com/unipilamappaorg")
        ]
    ])

    return blocks, fallback_text, fallback_markup

def build_links_rich_message():
    """Costruisce il Rich Message per il comando /links."""
    blocks = [
        {
            "type": "heading",
            "size": 1,
            "text": "LINK UTILI"
        },
        {
            "type": "list",
            "items": [
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": [
                                "Instagram: ",
                                {"type": "url", "text": "@unipilamappaorg", "url": "https://www.instagram.com/unipilamappaorg"}
                            ]
                        }
                    ]
                },
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": [
                                "GitHub: ",
                                {"type": "url", "text": "luxxiu/dove-unipi-bot", "url": "https://github.com/luxxiu/dove-unipi-bot"}
                            ]
                        }
                    ]
                },
                {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": [
                                "Mappa interattiva: ",
                                {"type": "url", "text": "unipi.lamappa.org", "url": "https://unipi.lamappa.org"}
                            ]
                        }
                    ]
                }
            ]
        },
        {
            "type": "buttons",
            "align": "center",
            "buttons": [
                {"text": "SEGUICI SU INSTAGRAM", "url": "https://www.instagram.com/unipilamappaorg"},
                {"text": "REPOSITORY GITHUB", "url": "https://github.com/luxxiu/dove-unipi-bot"}
            ]
        }
    ]

    fallback_text = (
        "*LINK UTILI*\n\n"
        "• [Instagram: @unipilamappaorg](https://www.instagram.com/unipilamappaorg)\n"
        "• [GitHub: luxxiu/dove-unipi-bot](https://github.com/luxxiu/dove-unipi-bot)\n"
        "• [Mappa interattiva](https://unipi.lamappa.org)"
    )
    fallback_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("SEGUICI SU INSTAGRAM", url="https://www.instagram.com/unipilamappaorg")],
        [InlineKeyboardButton("REPOSITORY GITHUB", url="https://github.com/luxxiu/dove-unipi-bot")]
    ])
    return blocks, fallback_text, fallback_markup

def _get_ephemeral_id(message):
    """Estrae l'ephemeral_message_id da un oggetto Message."""
    eph_id = getattr(message, "ephemeral_message_id", None)
    if eph_id:
        return eph_id
    api_kw = getattr(message, "api_kwargs", {}) or {}
    return api_kw.get("ephemeral_message_id")

async def send_rich_message_or_fallback(bot, chat_id: int, blocks: list, fallback_text: str, fallback_markup = None, user_id: int = None, is_group: bool = False):
    """Invia un Rich Message con fallback automatico a testo standard."""
    has_rich_buttons = any(b.get("type") == "buttons" for b in blocks)
    rich_payload = {
        "chat_id": chat_id,
        "rich_message": {
            "blocks": blocks
        }
    }
    if not has_rich_buttons and fallback_markup:
        rich_payload["reply_markup"] = fallback_markup.to_dict() if hasattr(fallback_markup, "to_dict") else fallback_markup
    if user_id:
        rich_payload["ephemeral_message_parameters"] = {
            "receiver_user_id": user_id
        }
    try:
        await bot._post("sendRichMessage", data=rich_payload)
    except Exception as e:
        logger.warning(f"sendRichMessage fallito ({e}), invio fallback standard.")
        send_kwargs = {}
        if user_id:
            send_kwargs["api_kwargs"] = {
                "ephemeral_message_parameters": {
                    "receiver_user_id": user_id
                }
            }
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=fallback_text,
                reply_markup=fallback_markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                **send_kwargs
            )
        except Exception as e2:
            if is_group and user_id:
                logger.warning(f"Fallback effimero fallito ({e2}), messaggio non inviato nel gruppo.")
            else:
                raise

async def safe_edit_message(bot, query, text: str = None, rich_blocks: list = None, reply_markup = None, parse_mode = ParseMode.MARKDOWN):
    """Modifica un messaggio supportando messaggi effimeri nei gruppi, inline o standard."""
    is_group = bool(query.message and query.message.chat.type in ("group", "supergroup"))
    eph_id = _get_ephemeral_id(query.message) if query.message else None

    def is_not_modified(err: Exception) -> bool:
        return "Message is not modified" in str(err) or "message is not modified" in str(err)

    has_rich_buttons = any(b.get("type") == "buttons" for b in rich_blocks) if rich_blocks else False

    if rich_blocks is not None:
        if query.inline_message_id:
            try:
                rich_payload = {
                    "inline_message_id": query.inline_message_id,
                    "rich_message": {"blocks": rich_blocks}
                }
                if has_rich_buttons:
                    rich_payload["reply_markup"] = {"inline_keyboard": []}
                elif reply_markup:
                    rich_payload["reply_markup"] = reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup
                await bot._post("editMessageText", data=rich_payload)
                return
            except Exception as e:
                if is_not_modified(e):
                    return
                logger.warning(f"Rich inline edit fallito ({e}), fallback a text.")
                if text:
                    try:
                        await bot.edit_message_text(
                            inline_message_id=query.inline_message_id,
                            text=text,
                            reply_markup=reply_markup,
                            parse_mode=parse_mode,
                            disable_web_page_preview=True
                        )
                    except Exception as e_fb:
                        if not is_not_modified(e_fb):
                            logger.warning(f"Fallback inline edit fallito: {e_fb}")
                return

        if is_group and eph_id:
            eph_payload = {
                "chat_id": query.message.chat_id,
                "receiver_user_id": query.from_user.id,
                "ephemeral_message_id": eph_id,
                "rich_message": {"blocks": rich_blocks}
            }
            if has_rich_buttons:
                eph_payload["reply_markup"] = {"inline_keyboard": []}
            elif reply_markup:
                eph_payload["reply_markup"] = reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup
            try:
                await bot._post("editEphemeralMessageText", data=eph_payload)
                return
            except Exception as e:
                if is_not_modified(e):
                    return
                logger.info(f"editEphemeralMessageText fallito ({e}), provo editMessageText standard.")

        try:
            rich_payload = {
                "chat_id": query.message.chat_id,
                "message_id": query.message.message_id,
                "rich_message": {"blocks": rich_blocks}
            }
            if has_rich_buttons:
                rich_payload["reply_markup"] = {"inline_keyboard": []}
            elif reply_markup:
                rich_payload["reply_markup"] = reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup
            await bot._post("editMessageText", data=rich_payload)
            return
        except Exception as e:
            if is_not_modified(e):
                return
            logger.warning(f"Rich message edit fallito ({e}), provo fallback testuale.")

    # Fallback standard
    if text:
        try:
            if query.inline_message_id:
                await bot.edit_message_text(
                    inline_message_id=query.inline_message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True
                )
            elif is_group and eph_id:
                try:
                    await bot._post(
                        "editEphemeralMessageText",
                        data={
                            "chat_id": query.message.chat_id,
                            "receiver_user_id": query.from_user.id,
                            "ephemeral_message_id": eph_id,
                            "text": text,
                            "parse_mode": "Markdown",
                            "reply_markup": reply_markup.to_dict() if reply_markup else None
                        }
                    )
                except Exception:
                    await bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True
                    )
            else:
                await bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True
                )
        except Exception as e:
            if not is_not_modified(e):
                logger.warning(f"Fallback edit text fallito: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce il comando /start (effimero se evocato in gruppo)."""
    chat = update.effective_chat
    user = update.effective_user
    is_private = chat.type == "private"
    is_group = chat.type in ("group", "supergroup")
    user_id = user.id if (user and is_group) else None

    # In chat privata: invia lo sticker rimuovendo eventuali tastiere residue
    if is_private:
        try:
            await update.message.reply_sticker(
                "CAACAgQAAxkBAAIsdmrwleejJt4LPp0y_zy_33augvCGAALnHQACrUeIU3yQdcOSIi3TPQQ",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logger.warning(f"Invio sticker fallito: {e}")

    blocks, fallback_text, fallback_markup = build_welcome_rich_message(is_group=is_group)
    await send_rich_message_or_fallback(
        bot=context.bot,
        chat_id=chat.id,
        blocks=blocks,
        fallback_text=fallback_text,
        fallback_markup=fallback_markup,
        user_id=user_id,
        is_group=is_group
    )

async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce il comando /links. Mostra i link utili con Rich Message (effimero se in gruppo)."""
    chat = update.effective_chat
    user = update.effective_user
    is_group = chat.type in ("group", "supergroup")
    user_id = user.id if (user and is_group) else None

    blocks, fallback_text, fallback_markup = build_links_rich_message()
    await send_rich_message_or_fallback(
        bot=context.bot,
        chat_id=chat.id,
        blocks=blocks,
        fallback_text=fallback_text,
        fallback_markup=fallback_markup,
        user_id=user_id,
        is_group=is_group
    )

async def handle_group_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce l'evocazione del bot nei gruppi inviando un messaggio di benvenuto effimero."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    blocks, fallback_text, fallback_markup = build_welcome_rich_message(is_group=True)
    await send_rich_message_or_fallback(
        bot=context.bot,
        chat_id=chat.id,
        blocks=blocks,
        fallback_text=fallback_text,
        fallback_markup=fallback_markup,
        user_id=user.id,
        is_group=True
    )

def search_aule(query: str, pois: list, piani: dict, edifici: dict, poli: dict) -> list:
    """Esegue una ricerca per rilevanza tra le aule e le sale."""
    query_clean = query.strip().lower()
    tokens = query_clean.split()

    scored_results = []
    for poi in pois:
        piano = piani.get(poi.get("piano_id", ""), {})
        edificio = edifici.get(piano.get("edificio_id", ""), {})
        polo = poli.get(edificio.get("polo_id", ""), {})

        codice = (poi.get("codice") or "").strip().lower()
        nome = (poi.get("nome") or "").strip().lower()
        tipo = (poi.get("tipo") or "").strip().lower()
        ed_nome = (edificio.get("nome") or "").strip().lower()
        polo_nome = (polo.get("nome") or "").strip().lower()
        aliases = [a.lower() for a in polo.get("alias", [])]

        searchable_corpus = f"{codice} {nome} {tipo} {ed_nome} {polo_nome} {' '.join(aliases)}"

        if not all(t in searchable_corpus for t in tokens):
            continue

        score = 0
        if codice == query_clean:
            score += 130
        elif nome == query_clean:
            score += 120
        elif codice.endswith(f" {query_clean}") or codice.startswith(f"{query_clean} "):
            score += 100
        elif any(part == query_clean for part in codice.split()):
            score += 95
        elif query_clean in codice:
            score += 80
        elif query_clean in nome:
            score += 70
        elif any(query_clean == a for a in aliases):
            score += 65
        elif query_clean in polo_nome:
            score += 60
        elif query_clean in ed_nome:
            score += 50
        else:
            score += 30

        scored_results.append((score, poi, piano, edificio, polo))

    scored_results.sort(
        key=lambda item: (
            -item[0],
            item[4].get("nome", "").lower(),
            item[1].get("codice", "").lower()
        )
    )

    return [item[1:] for item in scored_results]

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce la ricerca inline di aule e sale da data/aule2.geojson con Rich Messages."""
    raw_query = update.inline_query.query or ""
    query = raw_query.strip()
    results = []

    data = load_aule_data()
    pois = data.get("pois", [])
    piani = data.get("piani", {})
    edifici = data.get("edifici", {})
    poli = data.get("poli", {})

    thumb_map = get_asset_url("icons/map.png")
    thumb_info = get_asset_url("icons/info.png")
    thumb_insta = get_asset_url("icons/instagram.png")
    thumb_github = get_asset_url("icons/github.png")

    # --- CASO 1: QUERY VUOTA -> Mostra SOLO SUGGERIMENTI ---
    if not query:
        # 1. Suggerimento unico di ricerca con icona info.png
        results.append(
            InlineQueryResultArticle(
                id="sugg_guide",
                title="Come cercare un'aula",
                description="Digita il nome dell'aula o del polo per iniziare...",
                thumbnail_url=thumb_info,
                thumbnail_width=48,
                thumbnail_height=48,
                input_message_content=CustomInputRichMessageContent([
                    {"type": "heading", "size": 1, "text": "COME CERCARE LE AULE"},
                    {
                        "type": "paragraph",
                        "text": "Per trovare un'aula o una sala dell'Università di Pisa, digita in chat il tag del bot seguito dal nome o dal polo."
                    },
                    {
                        "type": "list",
                        "items": [
                            {"blocks": [{"type": "paragraph", "text": "@doveunipibot B1"}]},
                            {"blocks": [{"type": "paragraph", "text": "@doveunipibot Fibonacci"}]},
                            {"blocks": [{"type": "paragraph", "text": "@doveunipibot Carmignani"}]},
                            {"blocks": [{"type": "paragraph", "text": "@doveunipibot Magna"}]}
                        ]
                    },
                    {
                        "type": "buttons",
                        "align": "center",
                        "buttons": [
                            {"text": "PROVA SUBITO (B1)", "switch_inline_query_current_chat": "B1"}
                        ]
                    }
                ])
            )
        )

        # 2. Voce Instagram come su bot2.py
        results.append(
            InlineQueryResultArticle(
                id="sugg_instagram",
                title="Seguici su Instagram",
                description="@unipilamappaorg",
                thumbnail_url=thumb_insta,
                thumbnail_width=48,
                thumbnail_height=48,
                input_message_content=CustomInputRichMessageContent([
                    {"type": "heading", "size": 1, "text": "SEGUICI SU INSTAGRAM"},
                    {
                        "type": "paragraph",
                        "text": "Segui il profilo @unipilamappaorg per rimanere sempre aggiornato su aule, mappe e servizi universitari!"
                    },
                    {
                        "type": "buttons",
                        "align": "center",
                        "buttons": [
                            {"text": "APRI INSTAGRAM", "url": "https://www.instagram.com/unipilamappaorg"}
                        ]
                    }
                ])
            )
        )

        # 3. Voce GitHub come su bot2.py
        results.append(
            InlineQueryResultArticle(
                id="sugg_github",
                title="Repository GitHub",
                description="luxxiu/dove-unipi-bot",
                thumbnail_url=thumb_github,
                thumbnail_width=48,
                thumbnail_height=48,
                input_message_content=CustomInputRichMessageContent([
                    {"type": "heading", "size": 1, "text": "REPOSITORY GITHUB"},
                    {
                        "type": "paragraph",
                        "text": "Il bot è open source! Visita la repository ufficiale su GitHub per consultare il codice o lasciare una stella al progetto."
                    },
                    {
                        "type": "buttons",
                        "align": "center",
                        "buttons": [
                            {"text": "APRI REPOSITORY", "url": "https://github.com/luxxiu/dove-unipi-bot"}
                        ]
                    }
                ])
            )
        )

        await update.inline_query.answer(results, cache_time=0)
        return

        await update.inline_query.answer(results, cache_time=0)
        return

    # --- CASO 2: QUERY CON TESTO -> Ricerca aule/sale ---
    matches = search_aule(query, pois, piani, edifici, poli)

    for poi, piano, edificio, polo in matches[:50]:
        codice = (poi.get("codice") or poi.get("nome") or "AULA").strip()
        cap = poi.get("capienza")
        if cap and cap > 0:
            title_text = f"{codice} | {cap} posti"
        else:
            title_text = codice

        percorso = build_percorso(poi, piano, edificio, polo)
        blocks = build_aula_rich_message(poi, piano, edificio, polo, percorso)

        edificio_id = piano.get("edificio_id", "")
        if edificio_id and os.path.isfile(os.path.join(ASSETS_DIR, "icons", "edifici", f"{edificio_id}.png")):
            thumb_aula = get_asset_url(f"icons/edifici/{edificio_id}.png")
        else:
            thumb_aula = thumb_map

        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=title_text,
                description=percorso,
                thumbnail_url=thumb_aula,
                thumbnail_width=48,
                thumbnail_height=48,
                input_message_content=CustomInputRichMessageContent(blocks)
            )
        )

    button = None
    if not results:
        button = InlineQueryResultsButton(text="Nessuna aula trovata", start_parameter="help")

    await update.inline_query.answer(results, cache_time=5, button=button)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce i callback query dai bottoni inline (orari e aggiornamenti aule)."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    data = (query.data or "").split("|")
    action = data[0]

    if action == "sched":
        # Formato: sched|aula_id -> appende la tabella oraria e trasforma il pulsante in AGGIORNA
        aula_id = data[1]
        blocks, fallback_text, fallback_markup = await build_aula_with_schedule(aula_id)
        if blocks:
            await safe_edit_message(context.bot, query, rich_blocks=blocks, text=fallback_text, reply_markup=fallback_markup)
        return

async def post_init(application: Application) -> None:
    """Inizializza i comandi del bot: standard in privato, effimeri nei gruppi."""
    try:
        await application.bot.set_my_commands(
            commands=[
                BotCommand("start", "Messaggio di benvenuto"),
                BotCommand("links", "Link utili e contatti")
            ],
            scope=BotCommandScopeAllPrivateChats()
        )
        try:
            await application.bot._post(
                "setMyCommands",
                data={
                    "commands": [
                        {"command": "start", "description": "Messaggio di benvenuto", "is_ephemeral": True},
                        {"command": "links", "description": "Link utili e contatti", "is_ephemeral": True}
                    ],
                    "scope": {"type": "all_group_chats"}
                }
            )
        except Exception as e_eph:
            logger.warning(f"Comandi effimeri per gruppi non supportati ({e_eph}), rimuovo comandi gruppi.")
            await application.bot.set_my_commands(
                commands=[],
                scope=BotCommandScopeAllGroupChats()
            )
    except Exception as e:
        logger.warning(f"Configurazione set_my_commands fallita: {e}")

async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    """Pinga il server per evitare che vada in sleep su Render."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        try:
            logger.info(f"Pinging {url}...")
            await asyncio.to_thread(requests.get, url, timeout=10)
        except Exception as e:
            logger.error(f"Ping fallito: {e}")

def main() -> None:
    """Avvia il bot."""
    token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN")

    if not token:
        logger.error("Errore: La variabile d'ambiente TELEGRAM_TOKEN (o BOT_TOKEN) non è impostata.")
        print("Per favore imposta la variabile d'ambiente TELEGRAM_TOKEN o BOT_TOKEN.")
        return

    application = Application.builder().token(token).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("links", links_command))

    # Risposta effimera alle menzioni o evocazioni del bot nei gruppi
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.Entity("mention") | filters.Regex(r"(?i)@doveunipibot")),
        handle_group_mention
    ))

    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(InlineQueryHandler(inline_query))

    # Configurazione Webhook (per Render) o Polling (locale)
    PORT = int(os.environ.get("PORT", "8443"))
    WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

    if WEBHOOK_URL:
        logger.info(f"Avvio in modalità WEBHOOK su porta {PORT}")
        if application.job_queue:
            application.job_queue.run_repeating(self_ping, interval=840, first=60)
        else:
            logger.error("JobQueue non disponibile! Il self-ping non funzionerà.")

        try:
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=token,
                webhook_url=f"{WEBHOOK_URL}/{token}"
            )
        except Exception as e:
            logger.critical(f"Errore critico durante l'avvio del webhook: {e}")
            raise e
    else:
        logger.info("Avvio in modalità POLLING")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
