# rss_watcher.py
import os, asyncio, json, time, re, urllib.parse as ul
from typing import Dict, Any, List, Optional

import feedparser
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from aiohttp import web

# ----------------- Configuración por entorno -----------------
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID     = os.environ.get("CHAT_ID", "").strip()

# RSS general (todo Nyaa). Cambia si quieres algo más específico.
FEED_URL    = os.environ.get("FEED_URL", "https://nyaa.si/?page=rss").strip()

SEEN_FILE   = os.environ.get("SEEN_FILE", "seen_nyaa.json").strip()
POLL_EVERY  = int(os.environ.get("POLL_EVERY", "120"))   # segundos
MAX_ITEMS   = int(os.environ.get("MAX_ITEMS", "80"))     # cuantos items leer del feed
PORT        = int(os.environ.get("PORT", "10000"))

# Al arrancar: cuántos últimos enviar inmediatamente (0 = ninguno)
BACKFILL_N  = int(os.environ.get("BACKFILL_N", "0"))

# Logs de descartes (útil para depurar si activas filtros)
LOG_SKIPS   = os.environ.get("LOG_SKIPS", "false").lower() in ("1","true","yes","on")

# Filtros generales (solo se aplican si tú los activas)
ONLY_TRUSTED = os.environ.get("ONLY_TRUSTED", "false").lower() in ("1","true","yes","on")
SKIP_REMAKES = os.environ.get("SKIP_REMAKES", "false").lower() in ("1","true","yes","on")
MIN_SEEDERS  = int(os.environ.get("MIN_SEEDERS", "0"))

# Regex por título opcional (cadena tipo: "Naruto|One Piece")
TITLE_KEYWORDS = [k.strip() for k in os.environ.get("TITLE_KEYWORDS", "").split("|") if k.strip()]
TITLE_REGEX = re.compile("|".join([re.escape(k) for k in TITLE_KEYWORDS]), re.I) if TITLE_KEYWORDS else None

# ---------- Filtros Multi (solo si los activas explícitamente) ----------
REQUIRE_MULTI_SUBS  = os.environ.get("REQUIRE_MULTI_SUBS", "false").lower() in ("1","true","yes","on")
REQUIRE_MULTI_AUDIO = os.environ.get("REQUIRE_MULTI_AUDIO", "false").lower() in ("1","true","yes","on")
REQUIRE_ANY_MULTI   = os.environ.get("REQUIRE_ANY_MULTI", "false").lower() in ("1","true","yes","on")

MULTISUB_PATTERNS = [
    r"multi[-\s_]?subs?\b", r"multi[-\s_]?subtitles?\b",
    r"multisubs?\b", r"multisub\b", r"dual[-\s_]?subs?\b",
    r"multi[-\s_]?lang(?:uage)?[-\s_]?subs?\b",
    r"multi[-\s_]?legendas?\b", r"multi[-\s_]?sub\w*", r"subs?:\s*multi",
]
MULTIAUDIO_PATTERNS = [
    r"multi[-\s_]?audio\b", r"dual[-\s_]?audio\b",
    r"2[-\s_]?audio\b", r"two[-\s_]?audio\b",
    r"audios?\s*múltiples?\b", r"audios?\s*multiples?\b",
    r"audio\s*multi\b",
    # a veces usan "ENG + SPA", etc.
    r"eng\s*\+\s*(?:spa|esp|jpn|jap|por|ita|fre|fr)",
    r"(?:jpn|jap)\s*\+\s*eng",
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
    # feedparser entrega atributos tanto vía objeto como dict
    return getattr(e, name, None) or e.get(name, default)

def build_magnet(infohash: str, title: str) -> str:
    return f"magnet:?xt=urn:btih:{infohash}&dn={ul.quote(title)}"

def has_multisubs_or_multiaudio(e: Any) -> tuple[bool, bool]:
    title = (entry_field(e, "title", "") or "")
    desc  = (entry_field(e, "description", "") or "")
    text  = f"{title}\n{desc}"
    return bool(MULTISUB_RE.search(text)), bool(MULTIAUD_RE.search(text))

def passes_filters(e: Any) -> bool:
    """
    Por defecto NO se filtra nada: se envía TODO.
    Solo se aplican filtros si tú activas variables de entorno.
    """
    def skip(reason: str) -> bool:
        if LOG_SKIPS:
            t = (entry_field(e, "title", "") or "")[:140]
            print("SKIP:", t, "|", reason)
        return False

    # Filtros generales opcionales
    if ONLY_TRUSTED and entry_field(e, "nyaa_trusted", "No") != "Yes":
        return skip("not trusted")
    if SKIP_REMAKES and entry_field(e, "nyaa_remake", "No") == "Yes":
        return skip("remake")
    seeders = int(entry_field(e, "nyaa_seeders", "0") or 0)
    if seeders < MIN_SEEDERS:
        return skip(f"seeders<{MIN_SEEDERS}")

    if TITLE_REGEX:
        title = entry_field(e, "title", "") or ""
        if not TITLE_REGEX.search(title):
            return skip("title regex")

    # Filtros Multi solo si tú los activas
    if REQUIRE_MULTI_SUBS or REQUIRE_MULTI_AUDIO or REQUIRE_ANY_MULTI:
        has_subs, has_audio = has_multisubs_or_multiaudio(e)
        if REQUIRE_MULTI_SUBS and not has_subs:       return skip("need multi-subs")
        if REQUIRE_MULTI_AUDIO and not has_audio:     return skip("need multi-audio")
        if REQUIRE_ANY_MULTI and not (has_subs or has_audio):
            return skip("need any multi")

    return True

def make_keyboard(torrent_url: str, page_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Página",   url=page_url)],
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

    magnet = build_magnet(infohash, title)
    text = (
        f"<b>{title}</b>\n"
        f"📅 <i>{pub_date}</i>\n"
        f"📂 {cat} | 💾 {size}\n"
        f"🌱 {seeders} seeders · ⬇️ {leechers} leechers\n"
        f"✅ Trusted: {trusted} · ♻️ Remake: {remake}\n\n"
        f"<b>Magnet:</b>\n<code>{magnet}</code>\n"
    )
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=make_keyboard(torrent_url, page_url),
    )

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

    # Limpiar memoria para que el archivo no crezca infinito
    if len(seen) > 1000:
        pruned = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:1000])
        seen.clear(); seen.update(pruned)
    return new_count

async def poll_loop(bot: Bot) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Faltan BOT_TOKEN o CHAT_ID"); return

    seen = load_seen(SEEN_FILE)

    # Arranque: backfill opcional o marcar existentes como vistos
    try:
        d = feedparser.parse(FEED_URL)
        entries = d.entries[:MAX_ITEMS] if d.entries else []
        if BACKFILL_N > 0:
            for e in entries[-BACKFILL_N:]:
                guid = entry_field(e, "id", entry_field(e, "guid", entry_field(e, "link", ""))) or ""
                infohash = entry_field(e, "nyaa_infohash", "")
                key = guid or infohash or (entry_field(e, "title", "") + "|" + entry_field(e, "published", ""))
                if not key or key in seen or not infohash:
                    continue
                if not passes_filters(e):
                    continue
                await notify(bot, CHAT_ID, e)
                seen[key] = time.time()
            save_seen(SEEN_FILE, seen)
        else:
            for e in entries:
                guid = entry_field(e, "id", entry_field(e, "guid", entry_field(e, "link", ""))) or ""
                infohash = entry_field(e, "nyaa_infohash", "")
                key = guid or infohash or (entry_field(e, "title", "") + "|" + entry_field(e, "published", ""))
                if key:
                    seen.setdefault(key, time.time())
            save_seen(SEEN_FILE, seen)
    except Exception as ex:
        print("Init scan error:", ex)

    # Bucle principal
    while True:
        try:
            new_n = await fetch_new(bot, CHAT_ID, seen)
            if new_n: save_seen(SEEN_FILE, seen)
        except Exception as ex:
            print("Error en fetch_new:", ex)
        await asyncio.sleep(POLL_EVERY)

# ----------------- Web /health (Render) -----------------
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
        print(f"[General] ONLY_TRUSTED={ONLY_TRUSTED} SKIP_REMAKES={SKIP_REMAKES} MIN_SEEDERS={MIN_SEEDERS}")
        print(f"[Title regex] {TITLE_REGEX.pattern if TITLE_REGEX else '(none)'}")
        print(f"[Multi] REQUIRE_MULTI_SUBS={REQUIRE_MULTI_SUBS} REQUIRE_MULTI_AUDIO={REQUIRE_MULTI_AUDIO} REQUIRE_ANY_MULTI={REQUIRE_ANY_MULTI}")
        print(f"[Backfill] BACKFILL_N={BACKFILL_N}")
        await poll_loop(bot)  # loop infinito
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
