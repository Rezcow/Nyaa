import os, time, threading, logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote_plus
import requests
import feedparser

# ---- Config ----
BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = os.getenv("CHAT_ID", "").strip()
FEED_URL    = os.getenv("FEED_URL", "https://nyaa.si/?page=rss&c=0_0&f=0").strip()
POLL_EVERY  = int(os.getenv("POLL_EVERY", "180"))
BACKFILL_N  = int(os.getenv("BACKFILL_N", "5"))
PORT        = int(os.getenv("PORT", "10000"))
USER_AGENT  = os.getenv("USER_AGENT", "Mozilla/5.0 (rss-watcher)")

assert BOT_TOKEN and CHAT_ID, "Faltan BOT_TOKEN y/o CHAT_ID"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
seen_ids = set()

# ---- Health endpoint (sin Flask) ----
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()
def start_health_server():
    HTTPServer(("0.0.0.0", PORT), Health).serve_forever()
threading.Thread(target=start_health_server, daemon=True).start()

# ---- Utilidades ----
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

def fetch_feed(url: str):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return feedparser.parse(r.content)

def tg_send(text: str):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = session.post(api, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)
    if not resp.ok:
        logging.error("Telegram error %s: %s", resp.status_code, resp.text)

def build_message(entry):
    title = entry.get("title", "(sin título)")
    guid  = entry.get("id") or entry.get("guid")
    torrent_link = entry.get("link")  # en Nyaa apunta al .torrent
    size  = entry.get("nyaa_size") or entry.get("size")
    cat   = entry.get("nyaa_category") or entry.get("category")
    # infohash (Nyaa lo expone en namespace nyaa)
    infohash = entry.get("nyaa_infohash") or getattr(entry, "nyaa_infohash", None)

    magnet = ""
    if infohash:
        magnet = f"magnet:?xt=urn:btih:{infohash}&dn={quote_plus(title)}"

    lines = [
        f"🆕 <b>{title}</b>",
        f"📂 <b>Categoría:</b> {cat}" if cat else "",
        f"📦 <b>Tamaño:</b> {size}" if size else "",
        f"🔗 <a href=\"{guid}\">Vista</a>" if guid else "",
        f"🧲 <a href=\"{magnet}\">Magnet</a>" if magnet else "",
        f"📥 <a href=\"{torrent_link}\">.torrent</a>" if torrent_link else "",
    ]
    return "\n".join([x for x in lines if x])

def process_entries(entries, announce_prefix=""):
    # orden ascendente para que lleguen en el mismo orden del feed
    entries_sorted = sorted(
        entries, key=lambda e: e.get("published_parsed") or e.get("updated_parsed") or 0
    )
    count = 0
    for e in entries_sorted:
        eid = e.get("id") or e.get("guid") or e.get("link")
        if not eid or eid in seen_ids:
            continue
        msg = build_message(e)
        tg_send(msg)
        seen_ids.add(eid)
        count += 1
        time.sleep(0.3)  # evitar flood
    if count:
        logging.info("%s enviados %d ítems.", announce_prefix, count)

def main():
    tg_send(f"🚀 Nyaa watcher iniciado.\nFeed: {FEED_URL}")
    logging.info("Arrancando watcher. Feed=%s", FEED_URL)

    # Primer fetch + backfill
    try:
        feed = fetch_feed(FEED_URL)
        entries = feed.entries or []
        logging.info("Feed inicial trae %d ítems.", len(entries))
        if BACKFILL_N > 0 and entries:
            process_entries(entries[:BACKFILL_N], announce_prefix="Backfill")
    except Exception as e:
        logging.exception("Error inicial: %s", e)

    # Loop
    while True:
        try:
            feed = fetch_feed(FEED_URL)
            entries = feed.entries or []
            # Nuevos = los que no estén en seen_ids
            fresh = [e for e in entries if (e.get("id") or e.get("guid") or e.get("link")) not in seen_ids]
            logging.info("Poll: feed trae %d, nuevos %d", len(entries), len(fresh))
            process_entries(fresh, announce_prefix="Live")
        except Exception as e:
            logging.exception("Error en poll: %s", e)
        time.sleep(POLL_EVERY)

if __name__ == "__main__":
    main()
