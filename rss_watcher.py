# rss_watcher.py
import os, asyncio, json, time, re, urllib.parse as ul
from typing import Dict, Any, List, Optional, Tuple
from html import escape as hesc

import feedparser
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from aiohttp import web, ClientSession, ClientTimeout

# ----------------- Entorno -----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID   = os.environ.get("CHAT_ID", "").strip()

FEED_URL  = os.environ.get("FEED_URL", "https://nyaa.si/?page=rss").strip()

SEEN_FILE    = os.environ.get("SEEN_FILE", "seen_nyaa.json").strip()
POLL_EVERY   = int(os.environ.get("POLL_EVERY", "120"))
MAX_ITEMS    = int(os.environ.get("MAX_ITEMS", "80"))
PORT         = int(os.environ.get("PORT", "10000"))
BACKFILL_N   = int(os.environ.get("BACKFILL_N", "0"))
LOG_SKIPS    = os.environ.get("LOG_SKIPS", "false").lower() in ("1","true","yes","on")
STARTUP_PING = os.environ.get("STARTUP_PING", "true").lower() in ("1","true","yes","on")
CLEAR_SEEN   = os.environ.get("CLEAR_SEEN", "false").lower() in ("1","true","yes","on")

# Endpoint de prueba: /test?k=tu_secreto -> envía un mensaje de prueba
TEST_SECRET = os.environ.get("TEST_SECRET", "").strip()

# Filtros generales (solo si los activas)
ONLY_TRUSTED = os.environ.get("ONLY_TRUSTED", "false").lower() in ("1","true","yes","on")
SKIP_REMAKES = os.environ.get("SKIP_REMAKES", "false").lower() in ("1","true","yes","on")
MIN_SEEDERS  = int(os.environ.get("MIN_SEEDERS", "0"))

TITLE_KEYWORDS = [k.strip() for k in os.environ.get("TITLE_KEYWORDS", "").split("|") if k.strip()]
TITLE_REGEX = re.compile("|".join([re.escape(k) for k in TITLE_KEYWORDS]), re.I) if TITLE_KEYWORDS else None

# Filtros Multi (solo si los activas)
REQUIRE_MULTI_SUBS  = os.environ.get("REQUIRE_MULTI_SUBS", "false").lower() in ("1","true","yes","on")
REQUIRE_MULTI_AUDIO = os.environ.get("REQUIRE_MULTI_AUDIO", "false").lower() in ("1","true","yes","on")
REQUIRE_ANY_MULTI   = os.environ.get("REQUIRE_ANY_MULTI", "false").lower() in ("1","true","yes","on")

MULTISUB_PATTERNS = [
    r"multi[-\s_]?subs?\b", r"multi[-\s_]?subtitles?\b", r"multisubs?\b", r"multisub\b",
    r"dual[-\s_]?subs?\b", r"multi[-\s_]?lang(?:uage)?[-\s_]?subs?\b",
    r"multi[-\s_]?legendas?\b", r"subs?:\s*multi",
]
MULTIAUDIO_PATTERNS = [
    r"multi[-\s_]?audio\b", r"dual[-\s_]?audio\b", r"2[-\s_]?audio\b", r"two[-\s_]?audio\b",
    r"(?:eng|en)\s*\+\s*(?:spa|esp|es|jpn|jap|por|pt|ita|it|fra|fr)",
    r"(?:jpn|jap)\s*\+\s*(?:eng|en)",
]
MULTISUB_RE = re.compile("|".join(MULTISUB_PATTERNS), re.I)
MULTIAUD_RE = re.compile("|".join(MULTIAUDIO_PATTERNS), re.I)

# ----------------- Utils -----------------
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

def has_multisubs_or_multiaudio(e: Any) -> Tuple[bool, bool]:
    title = (entry_field(e, "title", "") or "")
    desc  = (entry_field(e, "description", "") or "")
    text  = f"{title}\n{desc}"
    return bool(MULTISUB_RE.search(text)), bool(MULTIAUD_RE.search(text))

def skip_log(e: Any, reason: str) -> bool:
    if LOG_SKIPS:
        t = (entry_field(e, "title", "") or "")[:140]
        print("SKIP:", t, "|", reason)
    return False

def passes_filters(e: Any) -> bool:
    # Por defecto NO se filtra nada.
    if ONLY_TRUSTED and entry_field(e, "nyaa_trusted", "No") != "Yes":
        return skip_log(e, "not trusted")
    if SKIP_REMAKES and entry_field(e, "nyaa_remake", "No") == "Yes":
        return skip_log(e, "remake")
    seeders = int(entry_field(e, "nyaa_seeders", "0") or 0)
    if seeders < MIN_SEEDERS:
        return skip_log(e, f"seeders<{MIN_SEEDERS}")
    if TITLE_REGEX:
        title = entry_field(e, "title", "") or ""
        if not TITLE_REGEX.search(title):
            return skip_log(e, "title regex")
    if REQUIRE_MULTI_SUBS or REQUIRE_MULTI_AUDIO or REQUIRE_ANY_MULTI:
        has_subs, has_audio = has_multisubs_or_multiaudio(e)
        if REQUIRE_MULTI_SUBS and not has_subs:       return skip_log(e, "need multi-subs")
        if REQUIRE_MULTI_AUDIO and not has_audio:     return skip_log(e, "need multi-audio")
        if REQUIRE_ANY_MULTI and not (has_subs or has_audio):
            return skip_log(e, "need any multi")
    return True

def make_keyboard(torrent_url: str, page_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Página",   url=page_url)],
        [InlineKeyboardButton("⬇️ .torrent", url=torrent_url)],
    ])

async def notify(bot: Bot, chat_id: str, e: Any) -> None:
    title       = entry_field(e, "title", "N/A") or "N/A"
    page_url    = entry_field(e, "guid", entry_field(e, "link", "")) or ""
    torrent_url = entry_field(e, "link", "") or ""
    pub_date    = entry_field(e, "published", entry_field(e, "pubDate", "")) or ""
    size        = entry_field(e, "nyaa_size", "?") or "?"
    seeders     = entry_field(e, "nyaa_seeders", "0") or "0"
    leechers    = entry_field(e, "nyaa_leechers", "0") or "0"
    trusted     = entry_field(e, "nyaa_trusted", "No") or "No"
    remake      = entry_field(e, "nyaa_remake", "No") or "No"
    cat         = entry_field(e, "nyaa_category", entry_field(e, "category", "")) or ""
    infohash    = entry_field(e, "nyaa_infohash", "") or ""

    if not infohash:
        print("SKIP (sin infohash):", title[:120])
        return

    magnet = build_magnet(infohash, title)

    # Escapar para HTML de Telegram
    t_title   = hesc(title)
    t_pubdate = hesc(pub_date)
    t_cat     = hesc(cat)
    t_size    = hesc(str(size))
    t_seed    = hesc(str(seeders))
    t_leech   = hesc(str(leechers))
    t_trusted = hesc(trusted)
    t_remake  = hesc(remake)
    t_magnet  = hesc(magnet)

    text = (
        f"<b>{t_title}</b>\n"
        f"📅 <i>{t_pubdate}</i>\n"
        f"📂 {t_cat} | 💾 {t_size}\n"
        f"🌱 {t_seed} seeders · ⬇️ {t_leech} leechers\n"
        f"✅ Trusted: {t_trusted} · ♻️ Remake: {t_remake}\n\n"
        f"<b>Magnet:</b>\n<code>{t_magnet}</code>\n"
    )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=make_keyboard(torrent_url, page_url),
    )

# ----------------- Fetch del feed -----------------
TIMEOUT = ClientTimeout(total=20)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NyaaWatcher/1.1; +https://render.com)",
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
}

async def fetch_entries() -> List[Any]:
    try:
        async with ClientSession(timeout=TIMEOUT, headers=HEADERS) as s:
            async with s.get(FEED_URL) as r:
                data = await r.read()
        d = feedparser.parse(data)
        entries = d.entries[:MAX_ITEMS] if d.entries else []
        print(f"[RSS] Fetched {len(entries)} entries")
        return entries
    except Exception as ex:
        print("Fetch RSS error:", ex)
        return []

# ----------------- Loop principal -----------------
def key_for(e: Any) -> str:
    guid = entry_field(e, "id", entry_field(e, "guid", entry_field(e, "link", ""))) or ""
    infohash = entry_field(e, "nyaa_infohash", "")
    return guid or infohash or (entry_field(e, "title", "") + "|" + entry_field(e, "published", ""))

async def fetch_new(bot: Bot, chat_id: str, seen: Dict[str, float]) -> int:
    entries = await fetch_entries()
    new_count = 0
    for e in reversed(entries):  # antiguo -> nuevo
        k = key_for(e)
        if not k or k in seen:          continue
        if not passes_filters(e):       continue
        try:
            await notify(bot, chat_id, e)
            seen[k] = time.time()
            new_count += 1
        except Exception as ex:
            print("notify error:", ex)
    if len(seen) > 2000:
        pruned = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:1200])
        seen.clear(); seen.update(pruned)
    return new_count

async def poll_loop(bot: Bot) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Faltan BOT_TOKEN o CHAT_ID"); return

    seen = load_seen(SEEN_FILE)

    if CLEAR_SEEN:
        print("[Init] CLEAR_SEEN=true -> limpiando historial")
        seen.clear()
        save_seen(SEEN_FILE, seen)

    # Arranque: backfill opcional o marcar existentes como vistos
    try:
        entries = await fetch_entries()
        print(f"[Init] entries={len(entries)} BACKFILL_N={BACKFILL_N}")
        if BACKFILL_N > 0:
            enviados = 0
            for e in entries[-BACKFILL_N:]:
                k = key_for(e)
                if not k or k in seen: continue
                if not passes_filters(e): continue
                try:
                    await notify(bot, CHAT_ID, e)
                    seen[k] = time.time()
                    enviados += 1
                except Exception as ex:
                    print("notify(backfill) error:", ex)
            print(f"[Init] backfill enviados={enviados}")
            save_seen(SEEN_FILE, seen)
        else:
            for e in entries:
                k = key_for(e)
                if k: seen.setdefault(k, time.time())
            save_seen(SEEN_FILE, seen)
    except Exception as ex:
        print("Init scan error:", ex)

    while True:
        try:
            new_n = await fetch_new(bot, CHAT_ID, seen)
            if new_n:
                print(f"[RSS] Enviados {new_n} nuevos")
                save_seen(SEEN_FILE, seen)
        except Exception as ex:
            print("Error en fetch_new:", ex)
        await asyncio.sleep(POLL_EVERY)

# ----------------- Web (health + test) -----------------
async def health(_): return web.Response(text="ok")

async def test_handler(request: web.Request):
    if not TEST_SECRET:
        return web.Response(status=400, text="No TEST_SECRET set")
    if request.query.get("k") != TEST_SECRET:
        return web.Response(status=403, text="forbidden")
    try:
        bot = request.app["bot"]
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"✅ Test OK\nWatching: {FEED_URL}",
            disable_web_page_preview=True,
        )
        return web.Response(text="sent")
    except Exception as ex:
        return web.Response(status=500, text=f"send error: {ex}")

async def start_web(bot: Bot) -> web.AppRunner:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/health", health)
    app.router.add_get("/test", test_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Servidor web en :{PORT} (/health, /test)")
    return runner

# ----------------- Main -----------------
async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Define BOT_TOKEN y CHAT_ID en variables de entorno.")
    bot = Bot(token=BOT_TOKEN)

    runner = await start_web(bot)

    if STARTUP_PING:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=f"🚀 Nyaa watcher iniciado.\nFeed: {FEED_URL}")
        except Exception as ex:
            print("Startup ping error:", ex)

    print(f"[Watcher] FEED_URL={FEED_URL}")
    print(f"[General] ONLY_TRUSTED={ONLY_TRUSTED} SKIP_REMAKES={SKIP_REMAKES} MIN_SEEDERS={MIN_SEEDERS}")
    print(f"[Title regex] {TITLE_REGEX.pattern if TITLE_REGEX else '(none)'}")
    print(f"[Multi] REQUIRE_MULTI_SUBS={REQUIRE_MULTI_SUBS} REQUIRE_MULTI_AUDIO={REQUIRE_MULTI_AUDIO} REQUIRE_ANY_MULTI={REQUIRE_ANY_MULTI}")
    print(f"[Backfill] BACKFILL_N={BACKFILL_N} | POLL_EVERY={POLL_EVERY}s | MAX_ITEMS={MAX_ITEMS} | CLEAR_SEEN={CLEAR_SEEN}")

    try:
        await poll_loop(bot)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
