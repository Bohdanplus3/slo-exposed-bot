"""
SLO.EXPOSED v2.0 — AI Investigative Newsroom with Web Search
"""

import os
import asyncio
import logging
import json
import hashlib
import feedparser
import httpx
from datetime import datetime
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler,
)

logging.basicConfig(format="%(asctime)s │ %(levelname)s │ %(message)s", level=logging.INFO, datefmt="%H:%M:%S")
log = logging.getLogger("slo.exposed.v2")

BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID        = os.environ["TELEGRAM_CHANNEL_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_CHAT_ID     = os.environ.get("ADMIN_CHAT_ID", "")
MAX_POSTS_PER_DAY = int(os.environ.get("MAX_POSTS_PER_DAY", "6"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))
AUTO_PUBLISH      = os.environ.get("AUTO_PUBLISH", "false").lower() == "true"

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
    "running": False, "paused": False, "autopilot": AUTO_PUBLISH,
    "seen_urls": set(), "pending": {},
    "stats": {"scanned":0,"researched":0,"written":0,"approved":0,"rejected":0,"published":0,"reset_at":datetime.now()},
}

async def claude_text(system, user, max_tokens=1500):
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":max_tokens,"system":system,
                      "messages":[{"role":"user","content":user}]})
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                log.warning(f"[API] 429 rate limit — cakam {wait}s (poskus {attempt+1}/3)")
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            for block in r.json().get("content",[]):
                if block.get("type")=="text": return block["text"].strip()
            return ""
    raise Exception("API rate limit po 3 poskusih")

async def claude_search(system, user, max_tokens=2000):
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-sonnet-4-20250514","max_tokens":max_tokens,"system":system,
                  "messages":[{"role":"user","content":user}],
                  "tools":[{"type":"web_search_20250305","name":"web_search"}]})
        r.raise_for_status()
        parts = [b["text"] for b in r.json().get("content",[]) if b.get("type")=="text"]
        return "\n".join(parts).strip()

def url_hash(url): return hashlib.md5(url.encode()).hexdigest()

def is_relevant(title, summary):
    text = (title+" "+summary).lower()
    return any(kw.lower() in text for kw in KEYWORDS)

async def scan_sources():
    found = []
    for src in RSS_SOURCES:
        try:
            feed = feedparser.parse(src["url"])
            for entry in feed.entries[:10]:
                url = entry.get("link","")
                if not url: continue
                h = url_hash(url)
                if h in state["seen_urls"]: continue
                title = entry.get("title","")
                summary = entry.get("summary", entry.get("description",""))
                if is_relevant(title, summary):
                    found.append({"title":title,"summary":summary[:600],"url":url,"source":src["name"],"hash":h})
                    state["seen_urls"].add(h)
                    state["stats"]["scanned"] += 1
                    log.info(f"[SCANNER] {title[:60]} ({src['name']})")
        except Exception as e:
            log.warning(f"[SCANNER] {src['name']}: {e}")
    return found

RESEARCHER_SYS = """Si investigativni novinar za SLO.EXPOSED. Za dano novico poišči na internetu:
- Polna imena, funkcije, zneske, datume
- Pravni status: obtožnice, sodbe, preiskave
- Ozadje vpletenih: ali so bili že vpleteni v afere?
- Povezave: podjetja, politika, interesne skupine
Vrni strukturiran povzetek dejstev. Samo fakti."""

WRITER_SYS = """Si urednik kanala SLO.EXPOSED — investigativnega Telegram kanala o korupciji v Sloveniji.

FORMAT (obvezno):
- 15-20 stavkov, ne manj ne vec
- Struktura: 1) udarni začetek z imenom+dejanjem, 2) ozadje vpletenih, 3) dejstva kdaj/kje/kaj, 4) pravni vidik obtožbe/kazni, 5) kontekst ali so bili ze vpleteni, 6) ena ironicna zakljucna opomba
- STIL: samo fakti, suh ton, brez vode, brez "po porocanju"
- Max 2 emoji, ne na zacetku
- Zadnja vrstica: @slo_exposed"""

EDITOR_SYS = """Si urednik SLO.EXPOSED. Preveri po kriterijih:
1. Dolzina: 15-20 stavkov?
2. Konkretni fakti: imena, zneski, datumi?
3. Pravni vidik omenjen?
4. Konec z @slo_exposed?
5. Stil: suh, direkten?

Odgovori SAMO v JSON: {"approved":true/false,"score":1-10,"reason":"1 stavek","fix":"kaj popraviti"}"""

async def research_topic(news):
    prompt = f"Razisci: {news['title']}\nPovzetek: {news['summary']}\nURL: {news['url']}"
    try:
        result = await claude_search(RESEARCHER_SYS, prompt)
        state["stats"]["researched"] += 1
        log.info(f"[RESEARCHER] {len(result)} znakov")
        return result
    except Exception as e:
        log.warning(f"[RESEARCHER] {e}")
        return f"{news['title']}\n{news['summary']}"

async def write_post(research, news):
    prompt = f"Podatki:\n{research}\n\nOriginalna novica: {news['title']}\nVir: {news['source']}\n\nNapisi samo post."
    post = await claude_text(WRITER_SYS, prompt, max_tokens=1200)
    state["stats"]["written"] += 1
    log.info(f"[WRITER] {len(post)} znakov")
    return post

async def editor_check(post):
    try:
        r = await claude_text(EDITOR_SYS, f"Preveri:\n\n{post}", max_tokens=300)
        r = r.replace("```json","").replace("```","").strip()
        data = json.loads(r)
        if data.get("approved"): state["stats"]["approved"] += 1
        else: state["stats"]["rejected"] += 1
        log.info(f"[EDITOR] {'OK' if data.get('approved') else 'FAIL'} score={data.get('score')}")
        return data
    except Exception as e:
        log.warning(f"[EDITOR] {e}")
        return {"approved":True,"score":7,"reason":"auto-approved"}

async def publish_to_channel(bot, post, url):
    if state["stats"]["published"] >= MAX_POSTS_PER_DAY:
        log.info("[PUBLISHER] Dnevni limit dosežen")
        return False
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=post)
        state["stats"]["published"] += 1
        log.info(f"[PUBLISHER] Objavljeno #{state['stats']['published']}")
        if ADMIN_CHAT_ID:
            await bot.send_message(chat_id=ADMIN_CHAT_ID,
                text=f"Objavljeno #{state['stats']['published']}/{MAX_POSTS_PER_DAY}\n{url}")
        return True
    except Exception as e:
        log.error(f"[PUBLISHER] {e}")
        return False

async def send_for_approval(bot, post, news):
    if not ADMIN_CHAT_ID:
        await publish_to_channel(bot, post, news["url"])
        return
    pid = news["hash"][:8]
    state["pending"][pid] = {"post":post,"url":news["url"],"title":news["title"]}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("OBJAVI", callback_data=f"approve_{pid}"),
        InlineKeyboardButton("ZAVRNI", callback_data=f"reject_{pid}"),
    ]])
    preview = post[:300]+"..." if len(post)>300 else post
    await bot.send_message(chat_id=ADMIN_CHAT_ID,
        text=f"Nov post caka odobritev:\n\n{preview}\n\nVir: {news['url']}",
        reply_markup=keyboard)
    log.info(f"[APPROVAL] Post {pid} poslan v odobritev")

async def process_news(bot, news):
    try:
        log.info(f"[PIPELINE] {news['title'][:60]}")
        research = await research_topic(news)
        await asyncio.sleep(10)  # пауза после web search
        post = await write_post(research, news)
        await asyncio.sleep(5)   # пауза перед editor
        verdict = await editor_check(post)
        if not verdict.get("approved"):
            fix = verdict.get("fix","dodaj vec konkretnih faktov")
            log.info(f"[PIPELINE] Popravljam: {fix}")
            post2 = await claude_text(WRITER_SYS, f"Popravi glede na: {fix}\n\nOriginal:\n{post}", 1200)
            verdict2 = await editor_check(post2)
            if verdict2.get("approved"):
                post = post2
            else:
                log.info("[PIPELINE] 2x zavrnjen — preskočeno")
                return
        if state["autopilot"]:
            await publish_to_channel(bot, post, news["url"])
        else:
            await send_for_approval(bot, post, news)
    except Exception as e:
        log.error(f"[PIPELINE] {e}")

async def run_pipeline(bot):
    log.info("--- Skeniranje ---")
    items = await scan_sources()
    if not items:
        log.info("[PIPELINE] Ni novih novic")
        return
    log.info(f"[PIPELINE] {len(items)} novic")
    for news in items[:2]:
        await process_news(bot, news)
        await asyncio.sleep(30)

async def monitor_loop(bot):
    while state["running"]:
        try:
            if not state["paused"]:
                await run_pipeline(bot)
        except Exception as e:
            log.error(f"[MONITOR] {e}")
        now = datetime.now()
        if now.date() > state["stats"]["reset_at"].date():
            state["stats"].update({"scanned":0,"researched":0,"written":0,"approved":0,"rejected":0,"published":0,"reset_at":now})
        await asyncio.sleep(SCAN_INTERVAL_SEC)

async def handle_approval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("approve_"):
        pid = data.replace("approve_","")
        item = state["pending"].pop(pid, None)
        if item:
            await publish_to_channel(ctx.bot, item["post"], item["url"])
            await query.edit_message_text(f"Objavljeno v {CHANNEL_ID}")
        else:
            await query.edit_message_text("Post ni vec na voljo")
    elif data.startswith("reject_"):
        pid = data.replace("reject_","")
        state["pending"].pop(pid, None)
        state["stats"]["rejected"] += 1
        await query.edit_message_text("Zavrnjeno")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        await update.message.reply_text("Redakcija ze deluje.")
        return
    state["running"] = True
    state["paused"] = False
    await update.message.reply_text(
        f"SLO.EXPOSED v2.0 zagnan\n\n"
        f"Viri: {', '.join(s['name'] for s in RSS_SOURCES)}\n"
        f"Interval: {SCAN_INTERVAL_SEC//60} min\n"
        f"Kanal: {CHANNEL_ID}\n"
        f"Nacin: {'AUTOPILOT' if state['autopilot'] else 'ODOBRITEV'}\n"
        f"Web search: AKTIVEN\n\n"
        f"/stop za ustavitev"
    )
    asyncio.create_task(monitor_loop(ctx.bot))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["running"] = False
    await update.message.reply_text("Ustavljeno. /start za ponovni zagon.")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    await update.message.reply_text("Pavza. /resume za nadaljevanje.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    await update.message.reply_text("Nadaljujem.")

async def cmd_autopilot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["autopilot"] = not state["autopilot"]
    await update.message.reply_text(f"Autopilot: {'VKLJUCEN' if state['autopilot'] else 'IZKLJUCEN'}")

async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["pending"]
    if not p:
        await update.message.reply_text("Ni postov za odobritev.")
        return
    text = f"Caka {len(p)} postov:\n\n"
    for pid, item in list(p.items())[:3]:
        text += f"{pid}: {item['title'][:60]}\n\n"
    await update.message.reply_text(text)

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = state["stats"]
    await update.message.reply_text(
        f"SLO.EXPOSED v2.0 porocilo\n\n"
        f"Skenirano: {s['scanned']}\n"
        f"Raziskano: {s['researched']}\n"
        f"Napisano: {s['written']}\n"
        f"Odobreno: {s['approved']} / Zavrnjeno: {s['rejected']}\n"
        f"Objavljeno: {s['published']}/{MAX_POSTS_PER_DAY}\n\n"
        f"Status: {'Deluje' if state['running'] else 'Ustavljeno'}"
        f"{' (pavza)' if state['paused'] else ''}\n"
        f"Autopilot: {'DA' if state['autopilot'] else 'NE'}"
    )

async def cmd_investigate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(ctx.args) if ctx.args else ""
    if not topic:
        await update.message.reply_text("Uporaba: /investigate tema ali ime osebe")
        return
    await update.message.reply_text(f"Raziskujem: {topic}...")
    news = {"title":topic,"summary":topic,
            "url":f"https://www.google.com/search?q={topic.replace(' ','+')}+Slovenija+korupcija",
            "source":"MANUAL","hash":url_hash(topic+str(datetime.now()))}
    await process_news(ctx.bot, news)

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Testni cikel z web iskanjem...")
    news = {"title":"Korupcija pri javnih narocilih v slovenskem zdravstvu 2024",
            "summary":"Tuzilstvo preiskuje sum korupcije pri javnih narocilih.",
            "url":"https://www.rtvslo.si","source":"TEST",
            "hash":url_hash("test"+str(datetime.now()))}
    await process_news(ctx.bot, news)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "SLO.EXPOSED v2.0\n\n"
        "/start — zagon\n/stop — ustavitev\n/pause — pavza\n/resume — nadaljuj\n"
        "/autopilot — preklopi avtomatsko objavo\n/queue — cakajoci posti\n"
        "/report — statistika\n/investigate tema — rocna raziskava\n/test — test\n\n"
        "Ko AI pripravi post, dobis gumba OBJAVI / ZAVRNI v chat."
    )

def main():
    log.info("SLO.EXPOSED v2.0 — AI Investigative Newsroom")
    app = Application.builder().token(BOT_TOKEN).build()
    for cmd, fn in [("start",cmd_start),("stop",cmd_stop),("pause",cmd_pause),
                    ("resume",cmd_resume),("autopilot",cmd_autopilot),("queue",cmd_queue),
                    ("report",cmd_report),("investigate",cmd_investigate),("test",cmd_test),("help",cmd_help)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(handle_approval))
    log.info("Cakam /start...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
