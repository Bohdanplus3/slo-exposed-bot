"""
SLO.EXPOSED — AI Newsroom Bot
Автономная AI-редакция для Telegram-канала о криминале и коррупции в Словении.

Агенты:
  Scanner   → мониторит RSS и обычные сайты
  Writer    → переписывает новость в стиле SLO.EXPOSED
  Editor    → проверяет качество поста
  Publisher → публикует в Telegram канал

Управление:
  /start   — запустить мониторинг
  /stop    — остановить всё
  /pause   — пауза публикаций
  /resume  — возобновить публикации
  /queue   — посмотреть очередь
  /report  — статистика
  /test    — тестовый цикл
  /sources — список источников
  /help    — помощь
"""

import os
import re
import asyncio
import logging
import json
import hashlib
from html import unescape
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
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
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]   # e.g. @slo_exposed
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
MAX_POSTS_PER_DAY = int(os.environ.get("MAX_POSTS_PER_DAY", "8"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))
POST_LANGUAGE = os.environ.get("POST_LANGUAGE", "sl")


# ─── Sources ───────────────────────────────────────────────────────────────
RSS_SOURCES = [
    {"name": "Požareport", "url": "https://pozareport.si/?View=rss", "lang": "sl", "weight": 10},
    {"name": "RTV Slovenija", "url": "https://www.rtvslo.si/feeds/03.xml", "lang": "sl", "weight": 7},
]

PAGE_SOURCES = [
    {"name": "Necenzurirano", "url": "https://necenzurirano.si/", "lang": "sl", "weight": 10},
    {"name": "Siol Črna kronika", "url": "https://siol.net/novice/crna-kronika", "lang": "sl", "weight": 10},
]


# ─── Keywords ──────────────────────────────────────────────────────────────
KEYWORDS = [
    # Slovenian
    "korupcija", "kriminal", "afera", "obtožba", "preiskava", "zapor",
    "tihotapstvo", "pranje denarja", "davčna utaja", "podkupovanje",
    "nepotizem", "malverzacija", "sodba", "aretacija", "hišna preiskava",
    "tožilstvo", "policija", "sodišče", "škandal", "zloraba", "prisluhi",
    "lobiranje", "podkupnina", "ovadba", "aretiran", "obtožen",

    # English
    "corruption", "crime", "scandal", "arrest", "bribery", "fraud",
    "money laundering", "investigation", "indictment", "organized crime",
    "embezzlement",

    # Names / orgs often in SLO news
    "slovenia", "slovenski", "NLB", "SDH", "KPK", "Janša", "Golob",
    "Logar", "Vuković", "Klemenčič", "Počivalšek"
]


# ─── State ─────────────────────────────────────────────────────────────────
state = {
    "running": False,
    "paused": False,
    "seen_urls": set(),
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
async def claude(system: str, user: str, max_tokens: int = 800) -> str:
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        )
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"].strip()


# ─── Helpers ───────────────────────────────────────────────────────────────
def clean_text(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def url_hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    url = url.strip()
    if url.endswith("/"):
        return url[:-1]
    return url


def is_relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(kw.lower() in text for kw in KEYWORDS)


async def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sl,en;q=0.9,ru;q=0.8",
    }
    async with httpx.AsyncClient(
        timeout=25,
        follow_redirects=True,
        headers=headers
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


def same_domain(base_url: str, candidate_url: str) -> bool:
    try:
        return urlparse(base_url).netloc.replace("www.", "") == urlparse(candidate_url).netloc.replace("www.", "")
    except Exception:
        return False


def extract_candidate_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        if href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue

        full_url = urljoin(base_url, href)
        full_url = normalize_url(full_url)

        if not full_url.startswith("http"):
            continue
        if not same_domain(base_url, full_url):
            continue
        if full_url in seen:
            continue

        seen.add(full_url)
        links.append(full_url)

    return links


def looks_like_article(url: str, source_name: str) -> bool:
    u = url.lower()

    bad_parts = [
        "/tag/", "/tags/", "/rubrika/", "/category/", "/avtor/", "/author/",
        "/kontakt", "/about", "/info", "/search", "/isci", "/video", "/foto",
        "/podcast", "/login", "/prijava", "/narocnina", "/newsletter",
        "/vreme", "/sport", "/tv-spored", "/mnenja", "/forum"
    ]
    if any(part in u for part in bad_parts):
        return False

    if source_name == "Necenzurirano":
        return "/clanek/" in u

    if source_name == "Siol Črna kronika":
        return "/novice/" in u and len(u.split("/")) >= 5

    return True


def extract_page_title_summary(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    summary = ""

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = clean_text(og_title["content"])

    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc and og_desc.get("content"):
        summary = clean_text(og_desc["content"])

    if not summary:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            summary = clean_text(meta_desc["content"])

    if not summary:
        paragraphs = []
        for p in soup.find_all("p"):
            txt = clean_text(p.get_text(" ", strip=True))
            if len(txt) >= 80:
                paragraphs.append(txt)
        if paragraphs:
            summary = paragraphs[0]

    return title[:300], summary[:800]


# ─── AGENT 1: SCANNER ──────────────────────────────────────────────────────
async def scan_rss_sources() -> list[dict]:
    found = []

    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            entries = getattr(feed, "entries", [])[:10]

            log.info(f"[RSS] {source['name']}: найдено записей {len(entries)}")

            for entry in entries:
                url = normalize_url(entry.get("link", ""))
                if not url:
                    continue

                h = url_hash(url)
                if h in state["seen_urls"]:
                    continue

                title = clean_text(entry.get("title", ""))
                summary = clean_text(entry.get("summary", entry.get("description", "")))

                if not title:
                    continue

                if is_relevant(title, summary):
                    found.append({
                        "title": title,
                        "summary": summary[:800],
                        "url": url,
                        "source": source["name"],
                        "hash": h,
                    })
                    state["seen_urls"].add(h)
                    state["stats"]["scanned"] += 1
                    log.info(f"[RSS] Релевантная новость: {title[:80]} ({source['name']})")

        except Exception as e:
            log.warning(f"[RSS] Ошибка {source['name']}: {e}")

    return found


async def scan_page_sources() -> list[dict]:
    found = []

    for source in PAGE_SOURCES:
        try:
            html = await fetch_html(source["url"])
            links = extract_candidate_links(source["url"], html)

            log.info(f"[PAGE] {source['name']}: найдено ссылок {len(links)}")

            checked = 0
            for link in links:
                if checked >= 12:
                    break

                if not looks_like_article(link, source["name"]):
                    continue

                h = url_hash(link)
                if h in state["seen_urls"]:
                    continue

                try:
                    article_html = await fetch_html(link)
                    title, summary = extract_page_title_summary(article_html)

                    if not title:
                        continue

                    checked += 1

                    if is_relevant(title, summary):
                        found.append({
                            "title": title,
                            "summary": summary[:800],
                            "url": link,
                            "source": source["name"],
                            "hash": h,
                        })
                        state["seen_urls"].add(h)
                        state["stats"]["scanned"] += 1
                        log.info(f"[PAGE] Релевантная новость: {title[:80]} ({source['name']})")

                except Exception as e:
                    log.warning(f"[PAGE] Ошибка статьи {link}: {e}")

        except Exception as e:
            log.warning(f"[PAGE] Ошибка источника {source['name']}: {e}")

    return found


async def scan_sources() -> list[dict]:
    rss_items = await scan_rss_sources()
    page_items = await scan_page_sources()

    all_items = rss_items + page_items

    unique = []
    seen_hashes = set()
    for item in all_items:
        if item["hash"] in seen_hashes:
            continue
        seen_hashes.add(item["hash"])
        unique.append(item)

    return unique


# ─── AGENT 2: WRITER ───────────────────────────────────────────────────────
WRITER_SYSTEM = f"""
Ti si oster, pameten in zelo berljiv urednik Telegram kanala SLO.EXPOSED.
Pišeš v slovenščini, kratko, jasno, udarno, brez praznih fraz.

Navodila:
- Piši samo v jeziku: {POST_LANGUAGE}
- Napiši jedrnat Telegram post 400–900 znakov
- Začni z bistvom
- Izpostavi sum, afero, škandal, preiskavo ali sporni element
- Če ni konkretnih imen ali dejstev, to jasno omeni
- Ne izmišljaj ničesar
- Ne uporabljaj markdown oznak
- Dodaj na koncu podpis: @slo_exposed
"""


async def write_post(news: dict) -> str:
    user_prompt = f"""
Naslov: {news['title']}
Povzetek: {news['summary']}
Vir: {news['source']}
URL: {news['url']}

Napiši Telegram post za kanal SLO.EXPOSED.
"""
    post = await claude(WRITER_SYSTEM, user_prompt, max_tokens=500)
    post = post.strip()
    state["stats"]["written"] += 1
    log.info(f"[WRITER] Написан пост ({len(post)} символов)")
    return post


# ─── AGENT 3: EDITOR ───────────────────────────────────────────────────────
EDITOR_SYSTEM = """
Si izkušen urednik investigativnega Telegram kanala SLO.EXPOSED.

Naloga:
- oceni, ali je post dovolj konkreten, močan in objavljiv
- če manjka preveč dejstev, imen, datumov ali jasno jedro zgodbe, ga zavrni
- vrni samo JSON brez dodatnega besedila

Format:
{
  "approved": true/false,
  "score": 1-10,
  "reason": "kratka razlaga"
}
"""


async def editor_check(post: str) -> dict:
    raw = await claude(
        EDITOR_SYSTEM,
        f"Oceni ta Telegram post:\n\n{post}",
        max_tokens=200
    )

    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        parsed = json.loads(raw[start:end])
        return {
            "approved": bool(parsed.get("approved", False)),
            "score": int(parsed.get("score", 0)),
            "reason": str(parsed.get("reason", "Ni razloga"))
        }
    except Exception:
        return {
            "approved": False,
            "score": 0,
            "reason": f"Editor JSON parse error: {raw[:200]}"
        }


# ─── AGENT 4: PUBLISHER ────────────────────────────────────────────────────
async def publish_post(bot: Bot, post: str, url: str):
    final_text = f"{post}\n🔗 {url}"
    await bot.send_message(chat_id=CHANNEL_ID, text=final_text)
    state["stats"]["published"] += 1
    log.info("[PUBLISHER] Пост опубликован")

    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"✅ Опубликован пост #{state['stats']['published']}/{MAX_POSTS_PER_DAY}\n🔗 {url}"
            )
        except Exception as e:
            log.warning(f"[ADMIN] Не удалось отправить уведомление: {e}")


# ─── Main monitoring loop ──────────────────────────────────────────────────
def maybe_reset_stats():
    now = datetime.now()
    if now - state["stats"]["reset_at"] >= timedelta(days=1):
        state["stats"] = {
            "scanned": 0,
            "written": 0,
            "approved": 0,
            "rejected": 0,
            "published": 0,
            "reset_at": now,
        }
        log.info("[STATS] Суточная статистика сброшена")


async def process_news_item(bot: Bot, news: dict):
    log.info(f"[PIPELINE] Обработка: {news['title'][:80]}")

    try:
        post = await write_post(news)
        verdict = await editor_check(post)

        if verdict["approved"]:
            state["stats"]["approved"] += 1
            log.info(f"[EDITOR] Одобрено ({verdict['score']}/10): {verdict['reason']}")

            if state["paused"]:
                state["queue"].append({"post": post, "url": news["url"]})
                log.info("[QUEUE] Пост добавлен в очередь")
                return

            if state["stats"]["published"] >= MAX_POSTS_PER_DAY:
                state["queue"].append({"post": post, "url": news["url"]})
                log.info("[LIMIT] Достигнут дневной лимит, пост добавлен в очередь")
                return

            await publish_post(bot, post, news["url"])

        else:
            state["stats"]["rejected"] += 1
            log.info(f"[EDITOR] Отклонён ({verdict['score']}/10): {verdict['reason']}")

            if ADMIN_CHAT_ID:
                try:
                    await bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=(
                            "❌ Отклонён\n"
                            f"Score: {verdict['score']}/10\n"
                            f"Причина: {verdict['reason']}\n"
                            f"URL: {news['url']}"
                        )
                    )
                except Exception as e:
                    log.warning(f"[ADMIN] Ошибка отправки отклонения: {e}")

    except Exception as e:
        log.exception(f"[PIPELINE] Ошибка обработки новости: {e}")


async def monitor_loop(bot: Bot):
    log.info("[LOOP] Мониторинг запущен")

    while state["running"]:
        try:
            maybe_reset_stats()

            found = await scan_sources()
            log.info(f"[SCAN] Новых релевантных новостей: {len(found)}")

            for news in found:
                if not state["running"]:
                    break
                await process_news_item(bot, news)
                await asyncio.sleep(5)

        except Exception as e:
            log.exception(f"[LOOP] Критическая ошибка цикла: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SEC)

    log.info("[LOOP] Мониторинг остановлен")


# ─── BOT COMMANDS ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        await update.message.reply_text("⚡ Редакция уже работает.")
        return

    state["running"] = True
    state["paused"] = False

    all_source_names = [s["name"] for s in RSS_SOURCES] + [s["name"] for s in PAGE_SOURCES]

    await update.message.reply_text(
        "◎ SLO.EXPOSED запущен\n\n"
        f"Сканирую: {', '.join(all_source_names)}\n"
        f"Интервал: каждые {SCAN_INTERVAL_SEC // 60} мин\n"
        f"Лимит: {MAX_POSTS_PER_DAY} постов/день\n"
        f"Канал: {CHANNEL_ID}\n\n"
        "Для остановки: /stop"
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
        "Посты накапливаются в очереди. /resume для возобновления."
    )
    log.info("[CMD] /pause")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    q = state["queue"][:]
    await update.message.reply_text(f"▶️ Публикации возобновлены. В очереди: {len(q)} постов.")

    for item in q:
        if state["stats"]["published"] >= MAX_POSTS_PER_DAY:
            break
        await publish_post(ctx.bot, item["post"], item["url"])
        if item in state["queue"]:
            state["queue"].remove(item)
        await asyncio.sleep(10)

    log.info("[CMD] /resume")


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = state["queue"]
    if not q:
        await update.message.reply_text("📭 Очередь пуста.")
        return

    text = f"📋 В очереди: {len(q)} постов\n\n"
    for i, item in enumerate(q[:5], 1):
        snippet = item["post"][:80].replace("*", "")
        text += f"{i}. {snippet}…\n\n"

    await update.message.reply_text(text)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = state["stats"]
    since = s["reset_at"].strftime("%d.%m %H:%M")
    text = (
        f"📊 Отчёт SLO.EXPOSED (с {since})\n\n"
        f"◎ Просканировано новостей: {s['scanned']}\n"
        f"◈ Написано постов: {s['written']}\n"
        f"◐ Одобрено: {s['approved']} · Отклонено: {s['rejected']}\n"
        f"◉ Опубликовано: {s['published']}/{MAX_POSTS_PER_DAY}\n\n"
        f"Статус: {'🟢 Работает' if state['running'] else '🔴 Остановлен'}"
        f"{' ⏸ Пауза' if state['paused'] else ''}"
    )
    await update.message.reply_text(text)


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Запускаю тестовый цикл...")

    test_news = {
        "title": "Bivši župan obtožen korupcije pri javnih naročilih",
        "summary": (
            "Tožilstvo je vložilo obtožnico zoper bivšega župana zaradi domnevnega jemanja "
            "podkupnin pri dodeljevanju javnih naročil v vrednosti 1,2 milijona evrov. "
            "Preiskava je trajala 14 mesecev."
        ),
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
            f"Вердикт Editor: {status}\n"
            f"Score: {verdict.get('score')}/10\n"
            f"Причина: {verdict.get('reason')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка теста: {e}")


async def cmd_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = "📡 Активные источники:\n\n"

    text += "RSS:\n"
    for s in RSS_SOURCES:
        text += f"◦ {s['name']} ({s['lang'].upper()})\n"

    text += "\nSites:\n"
    for s in PAGE_SOURCES:
        text += f"◦ {s['name']} ({s['lang'].upper()})\n"

    text += f"\nКлючевых слов: {len(KEYWORDS)}"
    await update.message.reply_text(text)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "SLO.EXPOSED Bot\n\n"
        "/start — запустить редакцию\n"
        "/stop — остановить всё\n"
        "/pause — пауза публикаций\n"
        "/resume — возобновить\n"
        "/queue — посмотреть очередь\n"
        "/report — статистика\n"
        "/test — тестовый цикл\n"
        "/sources — список источников\n"
        "/help — помощь"
    )


# ─── ENTRY POINT ───────────────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("  SLO.EXPOSED AI Newsroom Bot")
    log.info("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("help", cmd_help))

    log.info("Бот запущен. Ожидание команды /start...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
