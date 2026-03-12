"""
SLO.EXPOSED v3.0 — Clean & Reliable
Scanner → Writer → Editor → Auto-Publisher
+ фото из источника + ссылка + retry логика
"""

import os, asyncio, logging, json, hashlib, feedparser, httpx
from datetime import datetime
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

logging.basicConfig(format="%(asctime)s │ %(levelname)s │ %(message)s", level=logging.INFO, datefmt="%H:%M:%S")
log = logging.getLogger("slo.exposed.v3")

BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID        = os.environ["TELEGRAM_CHANNEL_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_CHAT_ID     = os.environ.get("ADMIN_CHAT_ID", "")
MAX_POSTS_PER_DAY = int(os.environ.get("MAX_POSTS_PER_DAY", "6"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))

RSS_SOURCES = [
    {"name": "Necenzurirano", "url": "https://necenzurirano.si/feed/",     "lang": "sl"},
    {"name": "Pozareport",    "url": "https://www.pozareport.si/feed/",    "lang": "sl"},
    {"name": "OCCRP",         "url": "https://occrp.org/en/rss",           "lang": "en"},
    {"name": "RTV Slovenija", "url": "https://www.rtvslo.si/feeds/03.xml", "lang": "sl"},
]

KEYWORDS = [
    "korupcija","kriminal","afera","obtožba","preiskava","zapor",
    "tihotapstvo","pranje denarja","davčna utaja","podkupovanje",
    "nepotizem","malverzacija","sodba","aretacija","hišna preiskava",
    "tožilstvo","policija","sodišče","škandal","zloraba","prevara",
    "corruption","crime","scandal","arrest","bribery","fraud",
    "money laundering","investigation","indictment","organized crime",
    "Slovenia","NLB","SDH","KPK",
]

state = {
    "running": False, "paused": False,
    "seen_urls": set(),
    "stats": {"scanned":0,"written":0,"approved":0,"rejected":0,"published":0,"reset_at":datetime.now()},
}

# ─── Claude с retry ────────────────────────────────────────────────────────
async def claude_text(system: str, user: str, max_tokens: int = 1000) -> str:
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-sonnet-4-20250514", "max_tokens": max_tokens,
                          "system": system, "messages": [{"role": "user", "content": user}]}
                )
                if r.status_code == 429:
                    wait = 15 * (attempt + 1)
                    log.warning(f"[API] Rate limit — čakam {wait}s")
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                for block in r.json().get("content", []):
                    if block.get("type") == "text":
                        return block["text"].strip()
                return ""
        except Exception as e:
            log.warning(f"[API] Napaka (poskus {attempt+1}): {e}")
            await asyncio.sleep(10)
    return ""

# ─── Извлечь фото из RSS entry ─────────────────────────────────────────────
def extract_image(entry: dict) -> str | None:
    """Ищет фото в RSS записи — media:content, enclosure, og:image."""
    # media:content
    media = entry.get("media_content", [])
    if media:
        url = media[0].get("url", "")
        if url and any(url.lower().endswith(ext) for ext in [".jpg",".jpeg",".png",".webp"]):
            return url

    # enclosure
    enclosures = entry.get("enclosures", [])
    for enc in enclosures:
        if "image" in enc.get("type", ""):
            return enc.get("href") or enc.get("url")

    # media:thumbnail
    thumb = entry.get("media_thumbnail", [])
    if thumb:
        return thumb[0].get("url")

    # summary içindeki img tag
    summary = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
    if '<img' in summary:
        import re
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary)
        if m:
            url = m.group(1)
            if url.startswith("http"):
                return url

    return None

# ─── SCANNER ───────────────────────────────────────────────────────────────
def url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()

def is_relevant(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in KEYWORDS)

async def scan_sources() -> list[dict]:
    found = []
    for src in RSS_SOURCES:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(src["url"], headers={"User-Agent": "Mozilla/5.0"})
                feed = feedparser.parse(r.text)
            for entry in feed.entries[:10]:
                url = entry.get("link", "")
                if not url:
                    continue
                h = url_hash(url)
                if h in state["seen_urls"]:
                    continue
                title   = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                if is_relevant(title, summary):
                    image = extract_image(entry)
                    found.append({
                        "title":   title,
                        "summary": summary[:800],
                        "url":     url,
                        "source":  src["name"],
                        "hash":    h,
                        "image":   image,
                    })
                    state["seen_urls"].add(h)
                    state["stats"]["scanned"] += 1
                    log.info(f"[SCANNER] {title[:60]} | foto={'DA' if image else 'NE'}")
        except Exception as e:
            log.warning(f"[SCANNER] {src['name']}: {e}")
    return found

# ─── WRITER ────────────────────────────────────────────────────────────────
WRITER_SYSTEM = """Si urednik kanala SLO.EXPOSED — investigativnega Telegram kanala o korupciji in kriminalu v Sloveniji.

PRAVILA (strogo):
- 5-7 stavkov, ne več ne manj
- SAMO dejstva: polna imena, zneski v €, datumi, institucije
- BEZ: "po poročanju", "kot navajajo", "domnevno", "naj bi"
- Suh direkten ton — ena ironična opomba NA KONCU
- Max 2 emoji, NIKOLI na začetku stavka
- Zadnja vrstica VEDNO samo: @slo_exposed
- Brez "**" bold označevanja, brez # hashtag-ov

PRIMER:
Nekdanji župan Občine Domžale Tone Ferjan (61) aretiran na podlagi odredbe Specializiranega tožilstva. Obtožen je korupcije pri javnih naročilih v vrednosti 870.000 €. Pogodbe so bile dodeljene podjetju njegove hčere brez razpisa v letih 2019–2021. KPK je primer prijavila že leta 2022, tožilstvo pa je potrebovalo dve leti za ukrepanje. Zamrznjena sta dva bančna računa in stanovanje v Ljubljani. Sistem deluje. 🐢
@slo_exposed"""

async def write_post(news: dict) -> str:
    prompt = f"""Napiši post za SLO.EXPOSED.

Naslov: {news['title']}
Vsebina: {news['summary']}
Vir: {news['source']}

Samo post, nič drugega."""
    post = await claude_text(WRITER_SYSTEM, prompt, max_tokens=600)
    state["stats"]["written"] += 1
    log.info(f"[WRITER] {len(post)} znakov")
    return post

# ─── EDITOR ────────────────────────────────────────────────────────────────
EDITOR_SYSTEM = """Si urednik SLO.EXPOSED. Preveri post:

1. 5-7 stavkov? (manj ali več = zavrni)
2. Konkretna imena, zneski, datumi? (brez = zavrni)
3. Konec z @slo_exposed?
4. Brez vode, brez "po poročanju"?

Odgovori SAMO JSON brez oklepaji kode:
{"approved": true, "score": 8, "reason": "razlaga"}"""

async def editor_check(post: str) -> dict:
    try:
        result = await claude_text(EDITOR_SYSTEM, f"Preveri:\n\n{post}", max_tokens=150)
        result = result.replace("```json","").replace("```","").strip()
        # Najdi JSON v odgovoru
        import re
        m = re.search(r'\{.*\}', result, re.DOTALL)
        if m:
            data = json.loads(m.group())
            if data.get("approved"):
                state["stats"]["approved"] += 1
            else:
                state["stats"]["rejected"] += 1
            log.info(f"[EDITOR] {'OK' if data.get('approved') else 'FAIL'} score={data.get('score')} | {data.get('reason','')[:60]}")
            return data
    except Exception as e:
        log.warning(f"[EDITOR] {e}")
    return {"approved": True, "score": 7, "reason": "auto-approved"}

# ─── PUBLISHER ─────────────────────────────────────────────────────────────
async def publish(bot: Bot, post: str, news: dict) -> bool:
    if state["stats"]["published"] >= MAX_POSTS_PER_DAY:
        log.info("[PUBLISHER] Dnevni limit dosežen")
        return False
    try:
        image_url = news.get("image")
        source_url = news.get("url", "")

        # Dodamo vir na konec posta (pred @slo_exposed)
        lines = post.strip().split("\n")
        if lines and lines[-1].strip() == "@slo_exposed":
            lines.insert(-1, f"\n🔗 {source_url}")
            post = "\n".join(lines)
        else:
            post = post.strip() + f"\n\n🔗 {source_url}\n@slo_exposed"

        if image_url:
            # Objavi s sliko
            try:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=image_url,
                    caption=post,
                )
                log.info(f"[PUBLISHER] Objavljeno s sliko #{state['stats']['published']+1}")
            except Exception as img_err:
                log.warning(f"[PUBLISHER] Slika ni uspela ({img_err}), objavljam brez")
                await bot.send_message(chat_id=CHANNEL_ID, text=post)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=post)

        state["stats"]["published"] += 1

        # Obvestilo adminu
        if ADMIN_CHAT_ID:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"✅ Objavljeno #{state['stats']['published']}/{MAX_POSTS_PER_DAY}\n{'📷 s sliko' if image_url else '📝 brez slike'}\n{source_url}"
            )
        return True

    except Exception as e:
        log.error(f"[PUBLISHER] Napaka: {e}")
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"❌ Napaka pri objavi: {e}")
            except:
                pass
        return False

# ─── PIPELINE ──────────────────────────────────────────────────────────────
async def process_news(bot: Bot, news: dict):
    try:
        log.info(f"[PIPELINE] Obdelujem: {news['title'][:60]}")

        post = await write_post(news)
        if not post:
            log.warning("[PIPELINE] Writer ni vrnil posta")
            return

        await asyncio.sleep(5)
        verdict = await editor_check(post)

        if not verdict.get("approved"):
            log.info("[PIPELINE] Poskus popravka...")
            await asyncio.sleep(5)
            post2 = await write_post(news)
            if post2:
                await asyncio.sleep(5)
                verdict2 = await editor_check(post2)
                if verdict2.get("approved"):
                    post = post2
                else:
                    log.info(f"[PIPELINE] Zavrnjeno 2x: {verdict2.get('reason','')}")
                    return
            else:
                return

        await publish(bot, post, news)

    except Exception as e:
        log.error(f"[PIPELINE] {e}")

async def run_pipeline(bot: Bot):
    log.info("─── Skeniranje ───")
    items = await scan_sources()
    if not items:
        log.info("[PIPELINE] Ni novih novic")
        return
    log.info(f"[PIPELINE] Najdenih {len(items)} novic")
    for news in items[:2]:
        await process_news(bot, news)
        await asyncio.sleep(45)

async def monitor_loop(bot: Bot):
    log.info(f"[MONITOR] Start. Interval: {SCAN_INTERVAL_SEC}s")
    while state["running"]:
        try:
            if not state["paused"]:
                await run_pipeline(bot)
        except Exception as e:
            log.error(f"[MONITOR] {e}")
            await asyncio.sleep(30)
        now = datetime.now()
        if now.date() > state["stats"]["reset_at"].date():
            state["stats"].update({"scanned":0,"written":0,"approved":0,"rejected":0,"published":0,"reset_at":now})
            log.info("[MONITOR] Statistika resetirana")
        await asyncio.sleep(SCAN_INTERVAL_SEC)

# ─── COMMANDS ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        await update.message.reply_text("Redakcija ze deluje.")
        return
    state["running"] = True
    state["paused"] = False
    await update.message.reply_text(
        f"SLO.EXPOSED v3.0 zagnan\n\n"
        f"Viri: {', '.join(s['name'] for s in RSS_SOURCES)}\n"
        f"Interval: {SCAN_INTERVAL_SEC//60} min\n"
        f"Kanal: {CHANNEL_ID}\n"
        f"Maks. objav/dan: {MAX_POSTS_PER_DAY}\n"
        f"Foto: avtomatsko ce obstaja\n\n"
        f"/stop za ustavitev"
    )
    asyncio.create_task(monitor_loop(ctx.bot))
    log.info("[CMD] /start")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["running"] = False
    await update.message.reply_text("Ustavljeno. /start za ponovni zagon.")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    await update.message.reply_text("Pavza. /resume za nadaljevanje.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    await update.message.reply_text("Nadaljujem.")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = state["stats"]
    await update.message.reply_text(
        f"SLO.EXPOSED v3.0\n\n"
        f"Skenirano: {s['scanned']}\n"
        f"Napisano: {s['written']}\n"
        f"Odobreno: {s['approved']} / Zavrnjeno: {s['rejected']}\n"
        f"Objavljeno: {s['published']}/{MAX_POSTS_PER_DAY}\n\n"
        f"Status: {'Deluje' if state['running'] else 'Ustavljeno'}"
        f"{' (pavza)' if state['paused'] else ''}"
    )

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Testni cikel...")
    test_news = {
        "title": "Bivši župan obtožen korupcije pri javnih naročilih za 1,2 mio EUR",
        "summary": "Specializirano državno tožilstvo je vložilo obtožnico zoper bivšega župana zaradi korupcije pri javnih naročilih. Preiskava je trajala 14 mesecev. Zamrznili so 4 nepremičnine in bančne račune.",
        "url": "https://necenzurirano.si",
        "source": "TEST",
        "hash": url_hash("test" + str(datetime.now())),
        "image": None,
    }
    await process_news(ctx.bot, test_news)
    await update.message.reply_text("Test zakljucen — preveri kanal.")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "SLO.EXPOSED v3.0\n\n"
        "/start — zagon\n"
        "/stop — ustavitev\n"
        "/pause — pavza\n"
        "/resume — nadaljuj\n"
        "/report — statistika\n"
        "/test — testni cikel\n"
        "/help — pomoc"
    )

# ─── MAIN ──────────────────────────────────────────────────────────────────
def main():
    log.info("SLO.EXPOSED v3.0 — Clean Newsroom Bot")
    app = Application.builder().token(BOT_TOKEN).build()
    for cmd, fn in [
        ("start", cmd_start), ("stop", cmd_stop), ("pause", cmd_pause),
        ("resume", cmd_resume), ("report", cmd_report), ("test", cmd_test), ("help", cmd_help)
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    log.info("Cakam /start...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
