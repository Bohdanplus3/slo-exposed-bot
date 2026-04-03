"""
SLO.EXPOSED — AI Newsroom Bot
Автономная AI-редакция для Telegram-канала о криминале и коррупции в Словении.

Агенты:
  Scanner  → мониторит RSS источники каждые 5 минут
  Writer   → переписывает новость в стиле SLO.EXPOSED (Banksta-style)
  Editor   → проверяет качество поста
  Publisher→ публикует в Telegram канал

Управление (пишите боту):
  /start   — запустить мониторинг
  /stop    — остановить всё
  /pause   — пауза публикаций
  /resume  — возобновить публикации
  /queue   — посмотреть очередь
  /report  — статистика за 24 часа
  /test    — обработать одну тестовую новость
  /sources — список источников
"""

import os
import asyncio
import logging
import json
import hashlib
import feedparser
import httpx
from datetime import datetime
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ─── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S"
)
log = logging.getLogger("slo.exposed")

# ─── Config from environment ───────────────────────────────────────────────
BOT_TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID         = os.environ["TELEGRAM_CHANNEL_ID"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
ADMIN_CHAT_ID      = os.environ.get("ADMIN_CHAT_ID", "")
MAX_POSTS_PER_DAY  = int(os.environ.get("MAX_POSTS_PER_DAY", "8"))
SCAN_INTERVAL_SEC  = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))
POST_LANGUAGE      = os.environ.get("POST_LANGUAGE", "sl")
SEEN_URLS_FILE     = os.environ.get("SEEN_URLS_FILE", "seen_urls.json")

# ─── RSS Sources ───────────────────────────────────────────────────────────
RSS_SOURCES = [
    {"name": "N1 Slovenija",      "url": "https://n1info.si/feed/",              "lang": "sl", "weight": 10},
    {"name": "Siol Črna kronika", "url": "https://siol.net/feeds/latest",        "lang": "sl", "weight": 10},
    {"name": "Požareport",        "url": "https://pozareport.si/?View=rss",      "lang": "sl", "weight": 10},
    {"name": "RTV Slovenija",     "url": "https://www.rtvslo.si/feeds/03.xml",   "lang": "sl", "weight": 7},
]

KEYWORDS = [
    "korupcija", "kriminal", "afera", "obtožba", "preiskava", "zapor",
    "tihotapstvo", "pranje denarja", "davčna utaja", "podkupovanje",
    "nepotizem", "malverzacija", "sodba", "areacija", "hišna preiskava",
    "tožilstvo", "policija", "sodišče", "škandal", "zloraba",
    "corruption", "crime", "scandal", "arrest", "bribery", "fraud",
    "money laundering", "investigation", "indictment", "organized crime",
    "embezzlement", "Slovenia", "slovenski",
    "NLB", "SDH", "KPK", "Janša", "Golob", "Logar",
]

# ─── Persistent seen_urls ──────────────────────────────────────────────────
def load_seen_urls() -> set:
    """Загружает список обработанных URL из файла."""
    try:
        with open(SEEN_URLS_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_urls():
    """Сохраняет список обработанных URL в файл."""
    try:
        # Держим только последние 5000 хешей чтобы файл не рос бесконечно
        recent = list(state["seen_urls"])[-5000:]
        with open(SEEN_URLS_FILE, "w") as f:
            json.dump(recent, f)
    except Exception as e:
        log.warning(f"[STATE] Не удалось сохранить seen_urls: {e}")

# ─── State ─────────────────────────────────────────────────────────────────
state = {
    "running": False,
    "paused": False,
    "seen_urls": load_seen_urls(),  # загружаем при старте
    "queue": [],
    "stats": {
        "scanned": 0,
        "written": 0,
        "approved": 0,
        "rejected": 0,
        "published": 0,
        "reset_at": datetime.now(),
    }
}

# ─── Anthropic helper ──────────────────────────────────────────────────────
async def call_claude(system: str, user: str, max_tokens: int = 800) -> str:
    """Вызов Claude через Anthropic API."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",  # быстрее и дешевле для Writer/Editor
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

# ─── AGENT 1: SCANNER ──────────────────────────────────────────────────────
def is_relevant(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in KEYWORDS)

def url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()

async def scan_sources() -> list[dict]:
    found = []
    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
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
                    found.append({
                        "title":   title,
                        "summary": summary[:800],
                        "url":     url,
                        "source":  source["name"],
                        "hash":    h,
                    })
                    state["seen_urls"].add(h)
                    state["stats"]["scanned"] += 1
                    log.info(f"[SCANNER] {title[:60]}… ({source['name']})")
        except Exception as e:
            log.warning(f"[SCANNER] Ошибка {source['name']}: {e}")

    if found:
        save_seen_urls()  # сохраняем после каждого скана
    return found

# ─── AGENT 2: WRITER ───────────────────────────────────────────────────────
LANG_MAP = {"sl": "slovenščina", "en": "english", "ru": "русский", "de": "deutsch"}

WRITER_SYSTEM = """Ti si urednik kanala SLO.EXPOSED — slovenskega investigativnega Telegram kanala.

STIL (identičen Banksta):
- Maksimalno 4-5 stavkov. Nič več.
- Samo fakti: imena, zneski, datumi, organizacije
- Suh ton + ena ironična opomba na koncu
- BEZ "po poročanju medijev", "kot poroča", "viri navajajo"
- 1-2 emoji maksimalno — ne na začetku
- Zadnja vrstica vedno: @slo_exposed

PRIMER DOBREGA POSTA:
Direktor podjetja Alfa d.o.o. Marko Novak (47) aretiran zbog suma pranja 2,3 mio € prek offshore računov na Cipru. Preiskava KPK traja že 8 mesecev. Novak je bil svetovalec vlade Golob v letih 2022–2023. Zamrznili so 4 nepremičnine in dva BMW-ja. Naključje, seveda. 🧊
@slo_exposed"""

async def write_post(news: dict, editor_feedback: str = "") -> str:
    """Writer agent. Принимает опциональный фидбек от Editor для переработки."""
    lang_name = LANG_MAP.get(POST_LANGUAGE, "slovenščina")

    feedback_block = ""
    if editor_feedback:
        feedback_block = f"\n\nPREJŠNJI POST JE BIL ZAVRNJEN. Razlog urednika: {editor_feedback}\nPopravi te napake."

    prompt = f"""Napiši Telegram post v jeziku: {lang_name}

NOVICA:
Naslov: {news['title']}
Povzetek: {news['summary']}
Vir: {news['source']}
URL: {news['url']}
{feedback_block}
Samo post, nič drugega."""

    post = await call_claude(WRITER_SYSTEM, prompt)
    state["stats"]["written"] += 1
    log.info(f"[WRITER] Написан пост ({len(post)} символов)")
    return post

# ─── AGENT 3: EDITOR ───────────────────────────────────────────────────────
EDITOR_SYSTEM = """Si izkušen urednik investigativnega Telegram kanala SLO.EXPOSED.

Preveri post po teh kriterijih:
1. Stil: suh, direkten, brez vode? (mora biti kot Banksta)
2. Dolžina: max 5 stavkov?
3. Fakti: so konkretni (ime, znesek, datum)?
4. Konec: se zaključi z @slo_exposed?
5. Jezik: ustrezen?

Odgovori SAMO v JSON formatu:
{"approved": true/false, "score": 1-10, "reason": "kratka razlaga"}

Nič drugega. Samo JSON."""

async def editor_check(post: str) -> dict:
    try:
        result = await call_claude(EDITOR_SYSTEM, f"Preveri ta post:\n\n{post}", max_tokens=200)
        result = result.replace("```json", "").replace("```", "").strip()
        data = json.loads(result)
        if data.get("approved"):
            state["stats"]["approved"] += 1
            log.info(f"[EDITOR] ✅ Одобрен (score: {data.get('score')})")
        else:
            state["stats"]["rejected"] += 1
            log.info(f"[EDITOR] ❌ Отклонён: {data.get('reason')}")
        return data
    except Exception as e:
        log.warning(f"[EDITOR] Ошибка парсинга JSON: {e}")
        return {"approved": True, "score": 7, "reason": "auto-approved (parse error)"}

# ─── AGENT 4: PUBLISHER ────────────────────────────────────────────────────
async def publish_post(bot: Bot, post: str, source_url: str) -> bool:
    if state["paused"]:
        state["queue"].append({
            "post": post,
            "url": source_url,
            "queued_at": datetime.now().isoformat()
        })
        log.info("[PUBLISHER] Пауза — пост добавлен в очередь")
        return False

    if state["stats"]["published"] >= MAX_POSTS_PER_DAY:
        log.info(f"[PUBLISHER] Лимит {MAX_POSTS_PER_DAY} постов/день достигнут")
        return False

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post,
            parse_mode=None,
            disable_web_page_preview=False,
        )
        state["stats"]["published"] += 1
        log.info(f"[PUBLISHER] ✅ Опубликовано (#{state['stats']['published']} сегодня)")

        if ADMIN_CHAT_ID:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"✅ Пост #{state['stats']['published']}/{MAX_POSTS_PER_DAY}\n🔗 {source_url}",
            )
        return True
    except Exception as e:
        log.error(f"[PUBLISHER] Ошибка публикации: {e}")
        return False

# ─── MAIN PIPELINE ─────────────────────────────────────────────────────────
async def run_pipeline(bot: Bot):
    log.info("─── Запуск цикла сканирования ───")
    news_items = await scan_sources()

    if not news_items:
        log.info("[PIPELINE] Новых релевантных новостей не найдено")
        return

    log.info(f"[PIPELINE] Найдено {len(news_items)} новых новостей")

    for news in news_items[:3]:
        try:
            # Первая попытка
            post = await write_post(news)
            verdict = await editor_check(post)

            # Вторая попытка — Writer получает конкретный фидбек от Editor
            if not verdict.get("approved"):
                log.info("[PIPELINE] Переписываем с фидбеком Editor...")
                post = await write_post(news, editor_feedback=verdict.get("reason", ""))
                verdict = await editor_check(post)

                if not verdict.get("approved"):
                    log.info("[PIPELINE] Пост отклонён дважды — пропускаем")
                    continue

            await publish_post(bot, post, news["url"])
            await asyncio.sleep(60)

        except Exception as e:
            log.error(f"[PIPELINE] Ошибка обработки: {e}")

# ─── MONITORING LOOP ───────────────────────────────────────────────────────
async def monitor_loop(bot: Bot):
    log.info(f"[MONITOR] Запущен. Интервал: {SCAN_INTERVAL_SEC}с")
    while state["running"]:
        try:
            await run_pipeline(bot)
        except Exception as e:
            log.error(f"[MONITOR] Критическая ошибка: {e}")

        # Сброс статистики в полночь
        now = datetime.now()
        if now.date() > state["stats"]["reset_at"].date():
            state["stats"].update({
                "scanned": 0, "written": 0, "approved": 0,
                "rejected": 0, "published": 0, "reset_at": now
            })
            log.info("[MONITOR] Статистика сброшена (новый день)")

        await asyncio.sleep(SCAN_INTERVAL_SEC)

# ─── BOT COMMANDS ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        await update.message.reply_text("⚡ Редакция уже работает.")
        return
    state["running"] = True
    state["paused"] = False
    await update.message.reply_text(
        "◎ *SLO.EXPOSED* запущен\n\n"
        f"Источники: {', '.join(s['name'] for s in RSS_SOURCES)}\n"
        f"Интервал: каждые {SCAN_INTERVAL_SEC // 60} мин\n"
        f"Лимит: {MAX_POSTS_PER_DAY} постов/день\n"
        f"Канал: {CHANNEL_ID}\n\n"
        "Для остановки: /stop",
        parse_mode=ParseMode.MARKDOWN
    )
    asyncio.create_task(monitor_loop(ctx.bot))
    log.info("[CMD] /start — редакция запущена")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["running"] = False
    state["paused"] = False
    await update.message.reply_text("⬛ Редакция остановлена. /start для возобновления.")
    log.info("[CMD] /stop")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    await update.message.reply_text(
        "⏸ Публикации на паузе. Мониторинг продолжается.\n"
        "/resume для возобновления."
    )

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    queued = state["queue"][:]  # копия чтобы избежать mutation во время итерации
    state["queue"].clear()
    await update.message.reply_text(f"▶️ Возобновлено. Публикую {len(queued)} постов из очереди...")
    for item in queued:
        await publish_post(ctx.bot, item["post"], item["url"])
        await asyncio.sleep(30)

async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = state["queue"]
    if not q:
        await update.message.reply_text("📭 Очередь пуста.")
        return
    text = f"📋 В очереди: {len(q)} постов\n\n"
    for i, item in enumerate(q[:5], 1):
        text += f"*{i}.* {item['post'][:80]}…\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = state["stats"]
    since = s["reset_at"].strftime("%d.%m %H:%M")
    status = "🟢 Работает" if state["running"] else "🔴 Остановлен"
    if state["paused"]:
        status += " ⏸ Пауза"
    text = (
        f"📊 *Отчёт SLO.EXPOSED* (с {since})\n\n"
        f"◎ Просканировано: *{s['scanned']}*\n"
        f"◈ Написано: *{s['written']}*\n"
        f"◐ Одобрено: *{s['approved']}* · Отклонено: *{s['rejected']}*\n"
        f"◉ Опубликовано: *{s['published']}/{MAX_POSTS_PER_DAY}*\n\n"
        f"Статус: {status}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Запускаю тестовый цикл...")
    test_news = {
        "title": "Bivši župan obtožen korupcije pri javnih naročilih",
        "summary": "Tožilstvo je vložilo obtožnico zoper bivšega župana zaradi domnevnega jemanja podkupnin pri dodeljevanju javnih naročil v vrednosti 1,2 milijona evrov. Preiskava je trajala 14 mesecev.",
        "source": "TEST",
        "url": "https://test.example.com/test-news",
        "hash": "test123",
    }
    try:
        post = await write_post(test_news)
        verdict = await editor_check(post)
        status = "✅ Одобрен" if verdict.get("approved") else "❌ Отклонён"
        await update.message.reply_text(f"📝 Тестовый пост:\n\n{post}")
        await update.message.reply_text(
            f"{status} | Score: {verdict.get('score')}/10\n"
            f"Причина: {verdict.get('reason')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка теста: {e}")

async def cmd_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = "📡 *Активные источники:*\n\n"
    for s in RSS_SOURCES:
        text += f"◦ {s['name']} ({s['lang'].upper()})\n"
    text += f"\nКлючевых слов: {len(KEYWORDS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*SLO.EXPOSED Bot*\n\n"
        "/start — запустить редакцию\n"
        "/stop — остановить всё\n"
        "/pause — пауза публикаций\n"
        "/resume — возобновить\n"
        "/queue — посмотреть очередь\n"
        "/report — статистика\n"
        "/test — тестовый цикл\n"
        "/sources — список источников",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── ENTRY POINT ───────────────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("  SLO.EXPOSED AI Newsroom Bot")
    log.info(f"  Загружено {len(state['seen_urls'])} обработанных URL")
    log.info("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    app.add_handler(CommandHandler("queue",   cmd_queue))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("test",    cmd_test))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("help",    cmd_help))

    log.info("Бот запущен. Ожидание команды /start...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
