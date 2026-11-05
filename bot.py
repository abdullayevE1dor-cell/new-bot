"""Telegram news channel bot: reads world-news RSS feeds, writes a short
Uzbek analysis with an LLM, and posts it to a Telegram channel."""
import html
import json
import os
import re
import sys
import time

import feedparser
import requests

FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://rss.dw.com/rdf/rss-en-world",
    "https://www.theguardian.com/world/rss",
]
STATE_FILE = "posted.json"
MAX_POSTS = int(os.getenv("MAX_POSTS", "2"))
IMAGE_MODE = os.getenv("IMAGE_MODE", "preview")
PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
MODEL = os.getenv(
    "LLM_MODEL",
    "gemini-2.5-flash-lite" if PROVIDER == "gemini" else "claude-haiku-5-5",
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL = os.environ["CHANNEL"]
API_KEY = os.environ["GEMINI_API_KEY" if PROVIDER == "gemini" else "ANTHROPIC_API_KEY"]

PROMPT = """Sen xalqaro yangiliklar tahlilchisisan. Quyidagi yangilik ma'lumoti
asosida O'ZBEK tilida (lotin yozuvi) qisqa post tayyorla.
Qoidalar:
- Faqat berilgan ma'lumotdan foydalan, hech qanday fakt, raqam yoki iqtibosni o'ylab topma.
- Maqolani ko'chirma, o'z so'zlaring bilan yoz.
- Ma'lumot yetarli bo'lmasa, tahlilni umumiy va ehtiyotkor yoz.
Faqat JSON qaytar: {{"title": "...", "summary": "2-3 gap", "analysis": "Nima uchun muhim, 1-2 gap"}}

Sarlavha: {title}
Qisqacha mazmun: {summary}
Manba: {source}
"""


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_state(links):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(links[-500:], f, ensure_ascii=False, indent=1)


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch_items(seen):
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print("feed error", url, e)
            continue
        source = feed.feed.get("title", url)
        for e in feed.entries[:10]:
            link = e.get("link")
            if not link or link in seen:
                continue
            items.append({
                "title": clean(e.get("title")),
                "summary": clean(e.get("summary"))[:1200],
                "link": link,
                "source": source,
                "ts": e.get("published_parsed") or e.get("updated_parsed"),
            })
    items.sort(key=lambda i: i["ts"] or (0,), reverse=True)
    return items


class RateLimited(Exception):
    pass


def ask_llm(prompt):
    if PROVIDER == "gemini":
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            headers={"x-goog-api-key": API_KEY, "content-type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=60,
        )
        if r.status_code == 429:
            raise RateLimited("Gemini free limit reached")
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 700,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    if r.status_code == 429:
        raise RateLimited("Claude rate limit reached")
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def analyze(item):
    text = ask_llm(PROMPT.format(**item))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)
  def og_image(link):
    try:
        page = requests.get(link, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).text
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
        return m.group(1) if m else None
    except Exception:
        return None


def build_text(item, a):
    e = html.escape
    return (f"<b>{e(a['title'])}</b>\n\n{e(a['summary'])}\n\n"
            f"<b>Tahlil:</b> {e(a['analysis'])}\n\n"
            f"Manba: <a href=\"{e(item['link'])}\">{e(item['source'])}</a>")


def send(item, a):
    text = build_text(item, a)
    base = f"https://api.telegram.org/bot{TOKEN}"
    if IMAGE_MODE == "photo":
        img = og_image(item["link"])
        if img and len(text) <= 1024:
            r = requests.post(f"{base}/sendPhoto", timeout=30, data={
                "chat_id": CHANNEL, "photo": img, "caption": text, "parse_mode": "HTML"})
            if r.ok:
                return
    payload = {"chat_id": CHANNEL, "text": text, "parse_mode": "HTML"}
    if IMAGE_MODE == "none":
        payload["link_preview_options"] = {"is_disabled": True}
    else:
        payload["link_preview_options"] = {
            "url": item["link"], "show_above_text": True, "prefer_large_media": True}
    r = requests.post(f"{base}/sendMessage", json=payload, timeout=30)
    r.raise_for_status()


def main():
    seen = load_state()
    posted = 0
    for item in fetch_items(set(seen)):
        if posted >= MAX_POSTS:
            break
        try:
            a = analyze(item)
            send(item, a)
            seen.append(item["link"])
            posted += 1
            print("posted:", item["title"])
            time.sleep(8)
        except RateLimited as e:
            print("stop:", e, file=sys.stderr)
            break
        except Exception as e:
            print("skip:", item["title"], "->", e, file=sys.stderr)
    save_state(seen)


if name == "main":
    main()
