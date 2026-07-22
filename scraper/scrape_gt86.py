#!/usr/bin/env python3
"""Scarica gli annunci Toyota GT86 da AutoScout24.it e aggiorna lo storico giornaliero.

Pensato per girare via GitHub Actions (rete non filtrata). Scrive:
  docs/data/current.json    -> annunci attivi oggi (usato dalla dashboard)
  docs/data/history.json    -> database completo (attivi + rimossi) con storico prezzi
  docs/data/geocode_cache.json -> cache geocoding citta -> lat/lon
  docs/data/debug_last_page.html -> dump pagina se il parsing non trova nulla (debug)
"""
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.autoscout24.it/lst/toyota/gt86"
SEARCH_PARAMS = {
    "atype": "C",
    "cy": "I",       # Italia
    "damaged_listing": "exclude",
    "desc": "0",
    "sort": "standard",
    "source": "listpage_pagination",
    "ustate": "N,U",  # nuove e usate
    "size": "20",
}
MAX_PAGES = 15
REQUEST_DELAY_SEC = 1.5
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_DELAY_SEC = 1.1
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CURRENT_PATH = DATA_DIR / "current.json"
HISTORY_PATH = DATA_DIR / "history.json"
GEOCACHE_PATH = DATA_DIR / "geocode_cache.json"
DEBUG_HTML_PATH = DATA_DIR / "debug_last_page.html"
IMAGES_DIR = DATA_DIR / "images"

session = requests.Session()
session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_page(page: int) -> str:
    params = dict(SEARCH_PARAMS)
    params["page"] = str(page)
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_listing_arrays(node, found):
    """Cerca ricorsivamente nel JSON __NEXT_DATA__ liste di dict che sembrano annunci.

    Lo schema esatto di AutoScout24 puo' cambiare nel tempo: invece di un path
    fisso, cerchiamo array di oggetti che hanno contemporaneamente qualcosa
    che somiglia a un prezzo e qualcosa che somiglia a un chilometraggio/id.
    """
    if isinstance(node, dict):
        for value in node.values():
            find_listing_arrays(value, found)
    elif isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            sample_keys = set()
            for item in node[:3]:
                sample_keys |= set(k.lower() for k in item.keys())
            has_price = any("price" in k for k in sample_keys)
            has_id = any(k in sample_keys for k in ("id", "guid", "listingid"))
            if has_price and has_id:
                found.append(node)
        for item in node:
            find_listing_arrays(item, found)


def parse_listing(raw: dict):
    """Mappa un elemento dell'array risultati sullo schema reale di AutoScout24.it
    (verificato via --dump-schema): raw['price']['priceRaw'], raw['vehicle'],
    raw['location'], raw['seller'], raw['tracking']['firstRegistration']."""
    listing_id = str(raw.get("id") or "")
    if not listing_id:
        return None

    price = (raw.get("price") or {}).get("priceRaw")

    tracking = raw.get("tracking") or {}
    vehicle = raw.get("vehicle") or {}
    location = raw.get("location") or {}
    seller = raw.get("seller") or {}

    mileage = None
    mileage_raw = tracking.get("mileage") or vehicle.get("mileageInKm")
    if mileage_raw is not None:
        m = re.search(r"[\d.]+", str(mileage_raw).replace(".", "").replace(",", ""))
        # sopra rimuoviamo separatori delle migliaia prima di cercare le cifre
        if m:
            try:
                mileage = int(m.group())
            except ValueError:
                mileage = None

    year = None
    first_reg = tracking.get("firstRegistration")  # es. "11-2018"
    if first_reg:
        m = re.search(r"(19|20)\d{2}", str(first_reg))
        if m:
            year = int(m.group())

    slug_url = raw.get("url")
    if slug_url and slug_url.startswith("/"):
        slug_url = f"https://www.autoscout24.it{slug_url}"
    if not slug_url:
        slug_url = f"https://www.autoscout24.it/annunci/{listing_id}"

    model_bits = [vehicle.get("make"), vehicle.get("model"), vehicle.get("modelVersionInput")]
    title = " ".join(b for b in model_bits if b) or "Toyota GT86"

    seller_type_raw = seller.get("type") or ""
    seller_type = "private" if "private" in seller_type_raw.lower() else ("dealer" if seller_type_raw else None)

    images = raw.get("images") or []
    image = images[0] if images else None

    return {
        "id": listing_id,
        "title": title,
        "price": int(price) if price is not None else None,
        "currency": "EUR",
        "year": year,
        "mileage": mileage,
        "city": location.get("city"),
        "zip": location.get("zip"),
        "country": location.get("countryCode") or "IT",
        "seller_type": seller_type,
        "url": slug_url,
        "image": image,
        "transmission": vehicle.get("transmission"),
        "fuel": vehicle.get("fuel"),
    }


def parse_next_data(html: str):
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return []
    try:
        data = json.loads(script.string)
    except json.JSONDecodeError:
        return []

    arrays = []
    find_listing_arrays(data, arrays)
    if not arrays:
        return []
    # prendi l'array piu' numeroso plausibile (di solito e' quello dei risultati)
    arrays.sort(key=len, reverse=True)
    best = arrays[0]

    parsed = []
    for raw in best:
        item = parse_listing(raw)
        if item:
            parsed.append(item)
    return parsed


def scrape_all_listings():
    """Scarica tutte le pagine dei risultati.

    Importante: la pagina si ferma solo quando un risultato torna vuoto o
    piu' corto della page size (= ultima pagina), MAI quando una pagina non
    contiene id "nuovi". Con sort=standard l'ordinamento di AutoScout24 puo'
    spostare leggermente gli annunci (specialmente quelli sponsorizzati dei
    concessionari) tra una richiesta e l'altra della stessa scansione: se ci
    si fermasse al primo "0 nuovi" si rischia di saltare pagine successive
    che contengono annunci reali mai visti, facendoli risultare erroneamente
    "rimossi/venduti" il giorno dopo.
    """
    page_size = int(SEARCH_PARAMS["size"])
    all_listings = {}
    for page in range(1, MAX_PAGES + 1):
        html = fetch_page(page)
        listings = parse_next_data(html)
        if not listings:
            if page == 1:
                save_json_html_debug(html)
            break
        for item in listings:
            all_listings.setdefault(item["id"], item)
        if len(listings) < page_size:
            break
        time.sleep(REQUEST_DELAY_SEC)
    return list(all_listings.values())


def save_json_html_debug(html: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_HTML_PATH.write_text(html, encoding="utf-8")


def geocode(city, zipcode, country, cache: dict):
    if not city and not zipcode:
        return None, None
    key = f"{zipcode or ''}|{city or ''}|{country or 'IT'}".strip("|").lower()
    if key in cache:
        entry = cache[key]
        return entry.get("lat"), entry.get("lon")

    query = ", ".join(p for p in [zipcode, city, "Italia" if country == "IT" else country] if p)
    try:
        resp = session.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "gt86-tracker/1.0 (personal use)"},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError):
        results = []
    time.sleep(NOMINATIM_DELAY_SEC)

    if results:
        lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
        cache[key] = {"lat": lat, "lon": lon}
        return lat, lon
    cache[key] = {"lat": None, "lon": None}
    return None, None


def verify_listing_still_active(listing_id: str, url: str) -> bool:
    """Verifica diretta se un annuncio dato per assente dalla lista paginata sia
    davvero sparito, aprendo la sua pagina invece di fidarsi solo dell'elenco.

    La lista risultati di AutoScout24 (sort=standard) puo' riordinarsi durante
    la scansione stessa (annunci sponsorizzati che si spostano tra una pagina e
    l'altra), quindi un annuncio ancora online puo' non comparire in nessuna
    pagina fetchata pur essendo ancora in vendita. Controllare direttamente la
    pagina dell'annuncio elimina questa fonte di falsi positivi.

    In caso di dubbio (errore di rete, risposta ambigua) ritorna True: meglio
    un giorno di ritardo nel segnare "rimosso" che un falso "venduto"."""
    if not url:
        return True
    try:
        resp = session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException:
        return True
    if resp.status_code >= 400:
        return False
    if "/annunci/" not in resp.url:
        # redirect fuori dalla pagina annuncio (es. verso i risultati di ricerca)
        return False
    return listing_id in resp.url or listing_id in resp.text


def download_cover_image(listing_id: str, image_url: str | None) -> str | None:
    """Scarica la foto di copertina e la salva nel repo (docs/data/images/), cosi'
    la dashboard non dipende dal CDN di AutoScout24 (bloccato da alcuni filtri di
    rete/firewall aziendali). Ritorna il path relativo a docs/, o l'URL originale
    come fallback se il download fallisce. Non riscarica se il file esiste gia'."""
    if not image_url:
        return None

    filename = f"{listing_id}.webp"
    local_path = IMAGES_DIR / filename
    relative_path = f"data/images/{filename}"
    if local_path.exists():
        return relative_path

    try:
        resp = session.get(image_url, timeout=20)
        resp.raise_for_status()
        if not resp.headers.get("Content-Type", "").startswith("image/"):
            return image_url
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(resp.content)
        return relative_path
    except requests.RequestException:
        return image_url


def dump_schema():
    """Stampa la struttura del primo annuncio trovato in pagina 1, per debug
    quando lo schema JSON di AutoScout24 cambia e i campi vanno rimappati."""
    html = fetch_page(1)
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        print("NESSUN __NEXT_DATA__ TROVATO")
        save_json_html_debug(html)
        return
    data = json.loads(script.string)
    arrays = []
    find_listing_arrays(data, arrays)
    print(f"Array candidati trovati: {len(arrays)} (lunghezze: {[len(a) for a in arrays]})")
    if arrays:
        arrays.sort(key=len, reverse=True)
        print("--- Primo elemento dell'array piu' numeroso ---")
        print(json.dumps(arrays[0][0], ensure_ascii=False, indent=2))


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()

    scraped = scrape_all_listings()
    scraped_ids = {item["id"] for item in scraped}

    history = load_json(HISTORY_PATH, {"listings": {}, "daily_snapshots": []})
    geocache = load_json(GEOCACHE_PATH, {})

    history_listings = history.setdefault("listings", {})

    new_ids, removed_ids, reappeared_ids = [], [], []

    for item in scraped:
        lat, lon = geocode(item["city"], item["zip"], item["country"], geocache)
        item["lat"] = lat
        item["lon"] = lon
        item["image"] = download_cover_image(item["id"], item["image"])

        existing = history_listings.get(item["id"])
        if existing is None:
            item["first_seen"] = today
            item["last_seen"] = today
            item["status"] = "active"
            item["price_history"] = [{"date": today, "price": item["price"]}]
            history_listings[item["id"]] = item
            new_ids.append(item["id"])
        else:
            if existing.get("status") == "removed":
                reappeared_ids.append(item["id"])
            was_price = existing.get("price")
            price_history = existing.get("price_history", [])
            if item["price"] is not None and item["price"] != was_price:
                price_history.append({"date": today, "price": item["price"]})
            existing.update(item)
            existing["last_seen"] = today
            existing["status"] = "active"
            existing["price_history"] = price_history
            existing.pop("removed_on", None)
            history_listings[item["id"]] = existing

    # Non trovato nella lista paginata non basta per dire "rimosso": la lista
    # puo' riordinarsi durante la scansione stessa (vedi nota in
    # verify_listing_still_active) e far sparire un annuncio ancora online da
    # tutte le pagine fetchate. Prima di segnarlo rimosso, verifico
    # direttamente la sua pagina.
    confirmed_active_ids = set(scraped_ids)
    for listing_id, existing in history_listings.items():
        if listing_id in scraped_ids or existing.get("status") != "active":
            continue
        if verify_listing_still_active(listing_id, existing.get("url", "")):
            existing["last_seen"] = today
            confirmed_active_ids.add(listing_id)
        else:
            existing["status"] = "removed"
            existing["removed_on"] = today
            removed_ids.append(listing_id)
        time.sleep(REQUEST_DELAY_SEC)

    active_prices = sorted(
        p
        for lid in confirmed_active_ids
        if (p := history_listings[lid].get("price")) is not None
    )
    median_price = active_prices[len(active_prices) // 2] if active_prices else None

    history["daily_snapshots"] = history.get("daily_snapshots", [])
    # se e' gia' girata una scansione oggi, sostituisci lo snapshot di oggi
    # invece di accumularne uno per ogni run manuale della stessa giornata
    history["daily_snapshots"] = [s for s in history["daily_snapshots"] if s.get("date") != today]
    history["daily_snapshots"].append(
        {
            "date": today,
            "count": len(confirmed_active_ids),
            "median_price": median_price,
            "new_ids": new_ids,
            "removed_ids": removed_ids,
            "reappeared_ids": reappeared_ids,
        }
    )
    # tieni al massimo 365 snapshot giornaliere
    history["daily_snapshots"] = history["daily_snapshots"][-365:]

    active_listings = []
    for listing_id in confirmed_active_ids:
        entry = dict(history_listings[listing_id])
        try:
            first_seen = datetime.strptime(entry["first_seen"], "%Y-%m-%d")
            entry["days_listed"] = (datetime.strptime(today, "%Y-%m-%d") - first_seen).days + 1
        except (KeyError, ValueError):
            entry["days_listed"] = None
        active_listings.append(entry)
    active_listings.sort(key=lambda x: (x["price"] is None, x["price"]))

    # "Nuovi/rimossi oggi" per la dashboard = tutta l'attivita' della giornata
    # letta da history_listings, non solo il delta dell'ultima scansione: con
    # il tasto "Aggiorna" si puo' rilanciare piu' volte lo stesso giorno, e un
    # run successivo senza novita' azzererebbe altrimenti il conteggio anche
    # se qualcosa era stato aggiunto/rimosso in una scansione precedente dello
    # stesso giorno.
    new_ids_today = [lid for lid, e in history_listings.items() if e.get("first_seen") == today]
    removed_ids_today = [
        lid for lid, e in history_listings.items() if e.get("status") == "removed" and e.get("removed_on") == today
    ]

    current = {
        "updated_at": now_iso,
        "count": len(active_listings),
        "new_ids": new_ids_today,
        "removed_ids": removed_ids_today,
        "listings": active_listings,
    }

    save_json(CURRENT_PATH, current)
    save_json(HISTORY_PATH, history)
    save_json(GEOCACHE_PATH, geocache)

    print(f"OK: {len(active_listings)} annunci attivi, {len(new_ids)} nuovi, {len(removed_ids)} rimossi oggi.")


if __name__ == "__main__":
    try:
        if "--dump-schema" in sys.argv:
            dump_schema()
        else:
            main()
    except requests.RequestException as exc:
        print(f"Errore di rete durante lo scraping: {exc}", file=sys.stderr)
        sys.exit(1)
