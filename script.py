import json
import os
import requests

# Carica i dati dal file rooms.json
def load_rooms_data(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        return json.load(file)

# Funzione per normalizzare il codice breve
def normalize_short_code(value):
    return value.strip().lower().replace(" ", "") if value else ""

# Funzione per ottenere il codice breve di una stanza
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

# Genera short links per ogni aula, dipartimento, sala, laboratorio, persona
def generate_short_links(data, base_url):
    structured_links = []
    id_counter = 1

    # Tipi di stanze idonee per i short link
    eligible_types = {'aula', 'dipartimento', 'laboratorio', 'sala', 'biblioteca', 'studio', 'persona'}

    for building, building_data in data.get('polo', {}).get('fibonacci', {}).get('edificio', {}).items():
        for floor, rooms in building_data.get('piano', {}).items():
            for room in rooms:
                room_type = room.get('type')
                
                # Check eligibility dealing with both string and list types
                is_eligible = False
                room_types_list = []
                if isinstance(room_type, list):
                    room_types_list = room_type
                else:
                    room_types_list = [room_type]
                
                is_eligible = any(t in eligible_types for t in room_types_list)
                
                if not is_eligible:
                    continue

                # Handle Persona Type Specifically
                if 'persona' in room_types_list:
                    # Use 'ricerca' field for person name
                    person_name = room.get('ricerca', '')
                    
                    if not person_name or not person_name.strip():
                        continue
                    
                    # Use the normalized person name as the short code
                    # This creates links like ?p=fibonacci&c=bacciudavide
                    normalized_code = normalize_short_code(person_name)
                    if not normalized_code:
                        continue
                        
                    short_link = f"{base_url}?p=fibonacci&c={normalized_code}"
                        
                    # Costruisci la descrizione base
                    floor_label = "Piano Terra" if floor == "0" else f"Piano {floor}"
                    
                    room_alias = ""
                    aliases = room.get('alias', [])
                    if aliases and len(aliases) > 0:
                        room_alias = aliases[0]
                    
                    room_ref = room_alias if room_alias else room.get('room', '')
                    
                    description = f"Polo Fibonacci › Edificio {building.upper()} › {floor_label}"
                    
                    # Aggiungi la stanza solo se presente
                    if room_ref:
                         description += f" › Stanza {room_ref}"
                    
                    # Aggiungi la categoria (ruolo) se presente
                    categoria = room.get('categoria')
                    if categoria:
                        if isinstance(categoria, list):
                            categoria_text = ', '.join(categoria)
                        else:
                            categoria_text = str(categoria)
                        description += f"\n{categoria_text}"

                    # Aggiungi keyword
                    keywords = list(aliases) if isinstance(aliases, list) else []
                        
                    structured_links.append({
                        "type": "article",
                        "id": str(id_counter),
                        "title": person_name,
                        "keywords": keywords,
                        "description": description,
                        "input_message_content": {
                            "message_text": f"[{person_name}]({short_link})",
                            "parse_mode": "Markdown"
                        }
                    })
                    id_counter += 1
                    
                    # Continue to next room after processing persona
                    # Persona entries are typically distinct
                    if len(room_types_list) == 1 and room_types_list[0] == 'persona':
                        continue

                # Standard logic for non-person or mixed types (e.g. Aulas)
                # But we should skip if we already handled it as persona? 
                # If a room has multiple types including persona, we generated persona links above.
                # Now we want to correct generate the room link if it's also an aula.
                
                # Check directly if it's one of the other types
                other_types = [t for t in room_types_list if t != 'persona']
                if not other_types:
                    continue
                    
                # Re-check eligibility for other types
                if not any(t in eligible_types for t in other_types):
                     continue

                resolved_code = get_room_short_code(room)
                if not resolved_code:
                    continue

                short_link = f"{base_url}?p=fibonacci&c={normalize_short_code(resolved_code)}"
                
                # Costruisci la descrizione base
                floor_label = "Piano Terra" if floor == "0" else f"Piano {floor}"
                description = f"Polo Fibonacci › Edificio {building.upper()} › {floor_label}"
                
                # Aggiungi capienza solo se presente
                capacity = room.get('capienza')
                if capacity:
                    description += f"\nCapienza: {capacity}"

                # Estrai gli alias come keywords
                keywords = room.get('alias', [])
                if not isinstance(keywords, list):
                    keywords = []

                structured_links.append({
                    "type": "article",
                    "id": str(id_counter),
                    "title": room.get('nome', 'Unknown Room'),
                    "keywords": keywords,  # Aggiungi gli alias come keywords
                    "description": description,
                    "input_message_content": {
                        "message_text": f"[{room.get('nome', 'Unknown Room')}]({short_link})",
                        "parse_mode": "Markdown"
                    }
                })
                id_counter += 1

    return structured_links

# Salva i dati strutturati in un file
def save_structured_links(structured_links, output_file):
    with open(output_file, 'w', encoding='utf-8') as file:
        json.dump(structured_links, file, indent=4, ensure_ascii=False)

# URL del file unified.json
rooms_url = "https://raw.githubusercontent.com/plumkewe/dove-unipi/refs/heads/main/data/unified.json"

# URL base per i link
base_url = "https://plumkewe.github.io/dove-unipi/"

# Percorso del file di output
output_file_path = 'data.json'

print(f"Scaricando i dati da {rooms_url}...")
try:
    response = requests.get(rooms_url)
    response.raise_for_status()
    data = response.json()
    print("Dati scaricati con successo.")
except Exception as e:
    print(f"Errore durante il download dei dati: {e}")
    exit(1)

# Genera i dati strutturati
structured_links = generate_short_links(data, base_url)

# Salva i dati strutturati in un file
save_structured_links(structured_links, output_file_path)

print(f"Dati strutturati generati e salvati in {output_file_path}")