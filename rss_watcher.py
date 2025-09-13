import os, asyncio, json, time, re, urllib.parse as ul
from typing import Dict, Any, List, Optional

import feedparser
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

# --- Config básica ---
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID     = os.environ.get("CHAT_ID", "").strip()
FEED_URL    = os.environ.get("FEED_URL", "https://nyaa.si/?page=rss&c=1_2&f=2")  # Ej: English-translated + trusted
SEEN_FILE   = os.environ.get("SEEN_FILE", "seen_nyaa.json")
POLL_EVERY  = int(os.environ.get("POLL_EVERY", "120"))
MAX_ITEMS   = int(os.environ.get("MAX_ITEMS", "50"))
PORT        = int(os.environ.get("PORT", "10000"))  # Render asigna PORT

ONLY_TRUSTED = True
SKIP_REMAKES = True
MIN_SEEDERS  = 0
TITLE_KEYWORDS = []  # Ej: ["Hikaru ga Shinda Natsu", "Sono Bisque Doll"]
TITLE_REGEX = re.compile("|".join([re.escape(k) for k in TITLE_KEYWORDS]), re.I) if TITLE_KEYWORDS else None

# --- Utilidades ---
def load_seen(path: str) -> Dict[str, float]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_seen(path: str, data: Dict[str, float]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def entry_field(e: Any, name: str, default: Optional[str] = None) -> Optional[str]:
    return getattr(e, name, None) or e.get(name, default)

def build_magnet(infohash: str, title: str) -> str:
    return f"magnet:?xt=urn:btih:{infohash}&dn={ul.quote(title)}"

def passes_filters(e: Any) -> bool:
    trusted = (entry_field(e, "nyaa_trusted", "No") == "Yes")
    remake  = (entry_field(e, "nyaa_remake", "No") == "Yes")
    seeders = int(entry_field(e, "nyaa_seeders", "0") or 0)
    if ONLY_TRUSTED and not trusted: return False
    if SKIP_REMAKES and remake:      return False
    if seeders < MIN_SEEDERS:        return False
    title = entry_field(e, "title", "") or ""
    if TITLE_REGEX and not TITLE_REGEX.search(title): return False
    return True

def make_keyboard(torrent_url: str, page_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Página", url=page_url)],
        [InlineKeyboardButton("⬇️ .torrent", url=torrent_url)],
    ])

async def notify(bot: Bot, chat_id: str, e: Any) -> None:
    title       = entry_field(e, "title", "N/A")
    page_url    = entry_field(e, "guid", entry_field(e, "link", ""))
    torrent_url = entry_field(e, "link", "")
    pub_date    = entry_field(e, "published", entry_field(e, "pubDate", ""))
    size        = entry_field(e, "nyaa_size", "?")
    seeders     = entry_field(e, "nyaa_seeders", "0")
    leechers    = entry_field(e, "nyaa_leechers", "0")
    trusted     = entry_field(e, "nyaa_trusted", "No")
    remake      = entry_field(e, "nyaa_remake", "No")
    cat         = entry_field(e, "nyaa_category", entry_field(e, "category", "")) or ""
    infohash    = entry_field(e, "nyaa_infohash", "")

    if not infohash:
        return  # sin hash no hay magnet

    magnet = build_magnet(infohash, title)
    text = (
        f"<b>{title}</b>\n"
        f"📅 <i>{pub_date}</i>\n"
        f"📂 {cat} | 💾 {size}\n"
        f"🌱 {seeders} seeders · ⬇️ {leechers} leechers\n"
        f"✅ Trusted: {trusted} · ♻️ Remake: {remake}\n\n"
        f"<b>Magnet:</b>\n<code>{magnet}</code>\n"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                           disable_web_page_preview=True, reply_markup=make_keyboard(torrent_url, page_url))

async def fetch_new(bot: Bot, chat_id: str, seen: Dict[str, float]) -> int:
    d = feedparser.parse(FEED_URL)
    entries: List[Any] = d.entries[:MAX_ITEMS] if d.entries else []
    new_count = 0
    for e in reversed(entries):  # del más antiguo al más nuevo
        guid = entry_field(e, "id", entry_field(e, "guid", entry_field(e, "link", ""))) or ""
        infohash = entry_field(e, "nyaa_infohash", "")
        key = guid or infohash or (entry_field(e, "title", "") + "|" + entry_field(e, "published", ""))
        if not key or key in seen:     continue
        if not infohash:               continue
        if not passes_filters(e):      continue
        await notify(bot, chat_id, e)
        seen[key] = time.time()
        new_count += 1
    # Limpiar memoria
    if len(seen) > 500:
        pruned = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:500])
        seen.clear(); seen.update(pruned)
    return new_count

async def poll_loop(bot: Bot) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Faltan BOT_TOKEN o CHAT_ID"); return
    seen = load_seen(SEEN_FILE)
    # Marcar actuales como vistos al iniciar (evita spam)
    try:
        d = feedparser.parse(FEED_URL)
        for e in (d.entries or [])[:MAX_ITEMS]:
            guid = entry_field(e, "id", entry_field(e, "guid", entry_field(e, "link", ""))) or ""
            infohash = entry_field(e, "nyaa_infohash", "")
            key = guid or infohash or (entry_field(e, "title", "") + "|" + entry_field(e, "published", ""))
            if key: seen.setdefault(key, time.time())
        save_seen(SEEN_FILE, seen)
    except Exception as ex:
        print("Init scan error:", ex)

    while True:
        try:
            new_n = await fetch_new(bot, CHAT_ID, seen)
            if new_n: save_seen(SEEN_FILE, seen)
        except Exception as ex:
            print("Error en fetch_new:", ex)
        await asyncio.sleep(POLL_EVERY)

# --- Servidor web para Render Free (mantener despierto con UptimeRobot) ---
from aiohttp import web
async def health(_): return web.Response(text="ok")

async def start_web() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Servidor web /health en :{PORT}")
    return runner

async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Define BOT_TOKEN y CHAT_ID en variables de entorno.")
    runner = await start_web()
    try:
        bot = Bot(token=BOT_TOKEN)
        print(f"[RSS-Watcher] Vigilando: {FEED_URL}")
        await poll_loop(bot)  # no termina
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
