# rss_watcher.py
import os, asyncio, json, time, re, urllib.parse as ul
from typing import Dict, Any, List, Optional

import feedparser
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from aiohttp import web

# ----------------- Config básica -----------------
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID     = os.environ.get("CHAT_ID", "").strip()
FEED_URL    = os.environ.get("FEED_URL", "https://nyaa.si/?page=rss")
SEEN_FILE   = os.environ.get("SEEN_FILE", "seen_nyaa.json")
POLL_EVERY  = int(os.environ.get("POLL_EVERY", "120"))
MAX_ITEMS   = int(os.environ.get("MAX_ITEMS", "50"))
PORT        = int(os.environ.get("PORT", "10000"))

# Filtros generales (ajústalos si quieres)
ONLY_TRUSTED = os.environ.get("ONLY_TRUSTED", "false").lower() in ("1","true","yes","on")
SKIP_REMAKES = os.environ.get("SKIP_REMAKES", "true").lower() in ("1","true","yes","on")
MIN_SEEDERS  = int(os.environ.get("MIN_SEEDERS", "0"))

# Palabras clave opcionales por título (vacío = no filtra por título)
TITLE_KEYWORDS = [k.strip() for k in os.environ.get("TITLE_KEYWORDS", "").split("|") if k.strip()]
TITLE_REGEX = re.compile("|".join([re.escape(k) for k in TITLE_KEYWORDS]), re.I) if TITLE_KEYWORDS else None

# ----------------- Filtros Multi (lo nuevo) -----------------
REQUIRE_MULTI_SUBS  = os.environ.get("REQUIRE_MULTI_SUBS", "false").lower() in ("1","true","yes","on")
REQUIRE_MULTI_AUDIO = os.environ.get("REQUIRE_MULTI_AUDIO", "false").lower() in ("1","true","yes","on")
REQUIRE_ANY_MULTI   = os.environ.get("REQUIRE_ANY_MULTI", "false").lower() in ("1","true","yes","on")

# Variaciones típicas que aparecen en títulos/descripciones
MULTISUB_PATTERNS = [
    r"multi[-\s_]?subs?\b", r"multi[-\s_]?subtitles?\b",
    r"multisubs?\b", r"multisub\b",
    r"dual[-\s_]?subs?\b",
    r"multi[-\s_]?lang(?:uage)?[-\s_]?subs?\b",
    r"multi[-\s_]?legendas?\b",           # PT/BR
    r"multi[-\s_]?sub\w*",                # variantes
    r"subs?:\s*multi",                    # "subs: multi"
]
MULTIAUDIO_PATTERNS = [
    r"multi[-\s_]?audio\b", r"dual[-\s_]?audio\b",
    r"2[-\s_]?audio\b", r"two[-\s_]?audio\b",
    r"audios?\s*múltiples?\b", r"audios?\s*multiples?\b",
    r"audio\s*multi\b", r"multi[-\s_]?lang(?:uage)?\b",  # a veces lo usan para audio
    r"eng\s*\+\s*(?:spa|esp|jpn|jap|por|ita|fre|fr)",    # ENG+SPA, etc.
    r"jpn\s*\+\s*eng", r"jap\s*\+\s*eng",
]

MULTISUB_RE = re.compile("|".join(MULTISUB_PATTERNS), re.I)
MULTIAUD_RE = re.compile("|".join(MULTIAUDIO_PATTERNS), re.I)

# ----------------- Utilidades -----------------
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
    # feedparser entrega tanto atributos como dict; cubrimos ambos
    return getattr(e, name, None) or e.get(name, default)

def build_magnet(infohash: str, title: str) -> str:
    return f"magnet:?xt=urn:btih:{infohash}&dn={ul.quote(title)}"

def has_multisubs_or_multiaudio(e: Any) -> tuple[bool,bool]:
    title = (entry_field(e, "title", "") or "")
    desc  = (entry_field(e, "description", "") or "")
    text  = f"{title}\n{desc}"
    return bool(MULTISUB_RE.search(text)), bool(MULTIAUD_RE.search(text))

def passes_filters(e: Any) -> bool:
    trusted = (entry_field(e, "nyaa_trusted", "No") == "Yes")
    remake  = (entry_field(e, "nyaa_remake", "No") == "Yes")
    seeders = int(entry_field(e, "nyaa_seeders", "0") or 0)
    if ONLY_TRUSTED and not trusted: return False
    if SKIP_REMAKES and remake:      return False
    if seeders < MIN_SEEDERS:        return False

    if TITLE_REGEX:
        title = entry_field(e, "title", "") or ""
        if not TITLE_REGEX.search(title): return False

    # ---- Filtro Multi requerido ----
    has_subs, has_audio = has_multisubs_or_multiaudio(e)
    if REQUIRE_MULTI_SUBS  and not has_subs:  return False
    if REQUIRE_MULTI_AUDIO and not has_audio: return False
    if REQUIRE_ANY_MULTI   and not (has_subs or has_audio): return False

    # Si no se exige nada, igualmente queremos "solo multi" → activa por defecto 'ANY' si ambos flags están en false?
    # El usuario pidió SOLO multi: activamos ANY si no definió nada explícitamente
    if not (REQUIRE_MULTI_SUBS or REQUIRE_MULTI_AUDIO or REQUIRE_ANY_MULTI):
        # Por defecto: solo notificar si tiene subs o audio multi
        if not (has_subs or has_audio): return False

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
        return

    has_subs, has_audio = has_multisubs_or_multiaudio(e)
    tags_line = []
    if has_subs:  tags_line.append("📝 MultiSubs")
    if has_audio: tags_line.append("🎧 MultiAudio")
    tags = " · ".join(tags_line) if tags_line else ""

    magnet = build_magnet(infohash, title)
    text = (
        f"<b>{title}</b>\n"
        f"📅 <i>{pub_date}</i>\n"
        f"📂 {cat} | 💾 {size}\n"
        f"🌱 {seeders} seeders · ⬇️ {leechers} leechers\n"
        f"✅ Trusted: {trusted} · ♻️ Remake: {remake}\n"
        f"{tags}\n\n"
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

    # Primer arranque: marcar actuales como vistos para evitar spam
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

# ----------------- Web /health para Render -----------------
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
        print(f"[Filtros] ONLY_TRUSTED={ONLY_TRUSTED} SKIP_REMAKES={SKIP_REMAKES} MIN_SEEDERS={MIN_SEEDERS}")
        print(f"[Multi] REQUIRE_MULTI_SUBS={REQUIRE_MULTI_SUBS} REQUIRE_MULTI_AUDIO={REQUIRE_MULTI_AUDIO} REQUIRE_ANY_MULTI={REQUIRE_ANY_MULTI}")
        await poll_loop(bot)  # bucle infinito
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
