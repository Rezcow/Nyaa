import os, time, json, threading, hashlib
from datetime import datetime
from flask import Flask, jsonify
import requests
import feedparser

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()
FEED_URL  = os.getenv("FEED_URL", "https://nyaa.si/?page=rss").strip()

POLL_EVERY = int(os.getenv("POLL_EVERY", "180"))
BACKFILL_N = int(os.getenv("BACKFILL_N", "5"))
STATE_FILE = "seen.json"

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Faltan BOT_TOKEN o CHAT_ID en variables de entorno.")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

session = requests.Session()
session.headers.update({
    # evitar bloqueos de Nyaa/Cloudflare
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
})

def log(msg):
    print(f"[{datetime.utcnow().isoformat()}Z] {msg}", flush=True)

def load_seen():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen)), f)
    except Exception as e:
        log(f"Error guardando {STATE_FILE}: {e}")

def fetch_feed():
    """Descarga el RSS con headers propios y lo pasa a feedparser."""
    try:
        resp = session.get(FEED_URL, timeout=25)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo:
            log(f"feedparser.bozo=True (posible error en RSS): {parsed.bozo_exception}")
        return parsed
    except Exception as e:
        log(f"Error al descargar feed: {e}")
        return None

def entry_id(e):
    """ID estable por entry: usa guid si hay; si no, hash del título+link."""
    guid = e.get("id") or e.get("guid")
    if guid:
        return str(guid)
    raw = f"{e.get('title','')}|{e.get('link','')}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()

def to_magnet(e):
    # feedparser convierte <nyaa:infoHash> -> e['nyaa_infohash']
    ih = (
        e.get("nyaa_infohash") or
        e.get("nyaa:infohash") or
        # algunos feeds incluyen hash en description; último intento:
        None
    )
    if ih:
        ih = str(ih).strip()
        return f"magnet:?xt=urn:btih:{ih}"
    return None

def fmt_entry(e):
    title = e.get("title", "(sin título)")
    view  = e.get("link") or e.get("guid") or ""
    size  = e.get("nyaa_size") or e.get("nyaa:size") or ""
    cat   = e.get("nyaa_category") or e.get("nyaa:category") or ""
    magnet = to_magnet(e) or "(sin magnet en feed)"
    lines = [
        f"🆕 <b>{title}</b>",
        f"📂 <i>{cat}</i>  •  💾 {size}".strip(),
        f"🧲 <code>{magnet}</code>",
    ]
    if view:
        lines.append(f"🔗 <a href=\"{view}\">Ver en Nyaa</a>")
    return "\n".join([l for l in lines if l])

def send_tg(text):
    try:
        r = session.post(
            TG_API,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        log(f"Error enviando a Telegram: {e}")
        return False

def announce_start():
    send_tg(f"🧑‍🚀📺 Nyaa watcher iniciado.\nFeed: <a href=\"{FEED_URL}\">{FEED_URL}</a>")

def process_entries(entries, seen, mode):
    """mode='backfill' o 'live' solo para log."""
    sent = 0
    # ordenar por fecha asc para enviar en orden cronológico
    def sort_key(e):
        return e.get("published_parsed") or e.get("updated_parsed") or 0
    for e in sorted(entries, key=sort_key):
        eid = entry_id(e)
        if eid in seen:
            continue
        text = fmt_entry(e)
        ok = send_tg(text)
        if ok:
            sent += 1
            seen.add(eid)
        time.sleep(0.8)  # para no pegarle tan rápido a Telegram
    if sent:
        save_seen(seen)
    log(f"{mode}: enviados {sent} nuevos.")

def poll_loop():
    seen = load_seen()
    announce_start()

    # BACKFILL en arranque
    parsed = fetch_feed()
    if parsed and parsed.entries:
        log(f"Arranque: feed con {len(parsed.entries)} items.")
        if BACKFILL_N > 0 and not seen:
            backfill = parsed.entries[:BACKFILL_N]
            log(f"Backfill de {len(backfill)} items.")
            process_entries(backfill, seen, mode="backfill")

    while True:
        parsed = fetch_feed()
        if parsed is None:
            time.sleep(POLL_EVERY)
            continue

        total = len(parsed.entries or [])
        log(f"Poll: feed trae {total} items.")
        if total:
            # En modo “live” enviamos los que aún no estén en seen
            process_entries(parsed.entries, seen, mode="live")
        time.sleep(POLL_EVERY)

# --------- Flask health ---------
app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify(ok=True, feed=FEED_URL), 200

def run_watcher():
    poll_loop()

if __name__ == "__main__":
    # thread para el watcher + servidor web para Render/uptime
    t = threading.Thread(target=run_watcher, daemon=True)
    t.start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
