
"""Weekly neurosurgery and neurocritical care digest: PubMed -> OpenAI -> Telegram."""

import os
import sys
import time
import random
import email.utils
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

import requests
from openai import OpenAI

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TELEGRAM_API = "https://api.telegram.org/bot"
TIMEOUT = 45
MAX_RETRIES = 6

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NeurosurgeryDigest/1.2 (academic literature digest)"})

QUERIES = {
    "Нейрохирургия": (
        '("Neurosurgical Procedures"[MeSH Terms] OR neurosurg*[Title/Abstract] '
        'OR "brain surgery"[Title/Abstract] OR "spine surgery"[Title/Abstract])'
    ),
    "Нейрореанимация": (
        '("Critical Care"[MeSH Terms] AND ("Brain Injuries"[MeSH Terms] '
        'OR "Subarachnoid Hemorrhage"[MeSH Terms] OR neurolog*[Title/Abstract])) '
        'OR neurocritical[Title/Abstract] OR "neurointensive care"[Title/Abstract] '
        'OR "intracranial pressure"[Title/Abstract]'
    ),
}


def pubmed_request(endpoint, params):
    """GET with exponential backoff for PubMed throttling/transient errors."""
    for attempt in range(MAX_RETRIES):
        time.sleep(0.6)
        try:
            response = SESSION.get(EUTILS + endpoint, params=params, timeout=TIMEOUT)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            delay = min(10 * 2 ** attempt, 120) + random.uniform(0, 2)
            print(f"PubMed connection error: {exc}; retry in {delay:.1f}s", flush=True)
            time.sleep(delay)
            continue

        if response.status_code in (429, 500, 502, 503, 504):
            if attempt == MAX_RETRIES - 1:
                response.raise_for_status()
            delay = min(15 * 2 ** attempt, 120)
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        retry_date = email.utils.parsedate_to_datetime(retry_after)
                        if retry_date.tzinfo is None:
                            retry_date = retry_date.replace(tzinfo=timezone.utc)
                        delay = max(delay, (retry_date - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        pass
            delay = min(max(delay, 0), 180) + random.uniform(0, 2)
            print(f"PubMed HTTP {response.status_code}; retry in {delay:.1f}s", flush=True)
            time.sleep(delay)
            continue

        response.raise_for_status()
        return response
    raise RuntimeError("PubMed request failed after retries")


def pubmed_ids(query, start, end, count=30):
    response = pubmed_request("esearch.fcgi", {
        "db": "pubmed", "term": query, "retmode": "json",
        "retmax": count, "sort": "pub date", "datetype": "pdat",
        "mindate": start, "maxdate": end,
    })
    return response.json()["esearchresult"]["idlist"]


def flatten(element):
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def fetch_articles(ids):
    if not ids:
        return []
    response = pubmed_request("efetch.fcgi", {
        "db": "pubmed", "id": ",".join(ids), "retmode": "xml",
    })
    root = ET.fromstring(response.content)
    articles = []
    for item in root.findall(".//PubmedArticle"):
        pmid = flatten(item.find("./MedlineCitation/PMID"))
        title = flatten(item.find(".//ArticleTitle"))
        abstract = flatten(item.find(".//Abstract"))
        journal = flatten(item.find(".//Journal/Title"))
        year = flatten(item.find(".//PubDate/Year")) or flatten(item.find(".//PubDate/MedlineDate"))
        doi = ""
        for identifier in item.findall(".//ArticleId"):
            if identifier.attrib.get("IdType") == "doi":
                doi = flatten(identifier)
                break
        if pmid and title and abstract:
            articles.append({
                "pmid": pmid, "title": title, "abstract": abstract[:5500],
                "journal": journal, "year": year, "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            })
    return articles


def select_articles():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=8)
    candidates = {}
    for area, query in QUERIES.items():
        print(f"Searching PubMed: {area}", flush=True)
        ids = pubmed_ids(query, start.isoformat(), end.isoformat())
        print(f"Found {len(ids)} IDs", flush=True)
        for article in fetch_articles(ids):
            pmid = article["pmid"]
            previous_areas = candidates.get(pmid, {}).get("areas", [])
            article["areas"] = sorted(set(previous_areas) | {area})
            candidates[pmid] = article
    selected = []
    used = set()
    for area in QUERIES:
        for article in candidates.values():
            if area in article["areas"] and article["pmid"] not in used:
                selected.append(article)
                used.add(article["pmid"])
                if len(selected) >= 24:
                    return selected, start, end
    return selected, start, end


def make_digest(articles, start, end):
    if not articles:
        return (f"НЕЙРОДАЙДЖЕСТ | {start} — {end}\n\n"
                "За указанный период подходящих публикаций с аннотациями не найдено.")

    sources = []
    for article in articles:
        sources.append(
            f"PMID: {article['pmid']}\n"
            f"Темы: {', '.join(article['areas'])}\n"
            f"Название: {article['title']}\n"
            f"Журнал: {article['journal']}\n"
            f"Год: {article['year']}\n"
            f"DOI: {article['doi'] or 'не указан'}\n"
            f"Ссылка: {article['url']}\n"
            f"Аннотация: {article['abstract']}"
        )

    instructions = (
        "Ты медицинский научный редактор, специализирующийся на нейрохирургии "
        "и нейрореанимации. Подготовь на русском языке еженедельный "
        "научно-клинический дайджест. Выбери 8–10 наиболее значимых "
        "публикаций, если качество позволяет, и охвати обе темы. "
        "Для каждой публикации укажи оригинальное название, журнал и год, "
        "ссылку на PubMed, дизайн, размер выборки (если известен), "
        "ключевые результаты с численными данными, ограничения, "
        "клиническую значимость и критическую оценку. "
        "Используй только предоставленные аннотации и метаданные. "
        "Не выдумывай статистику, авторов, результаты или DOI. "
        "Если вывод основан только на аннотации, прямо укажи это. "
        "Не делай категоричных клинических рекомендаций по одной работе. "
        "В конце добавь общий вывод недели, нерешённые вопросы и "
        "направления дальнейших исследований. Не добавляй слабые статьи "
        "ради достижения количества."
    )
    print("Generating digest with OpenAI...", flush=True)
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=120.0, max_retries=2)
    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"Период: {start} — {end}\n\n" + "\n\n".join(sources)},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty digest")
    return f"НЕЙРОДАЙДЖЕСТ | {start} — {end}\n\n{content}"


def split_messages(text, limit=3800):
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"{TELEGRAM_API}{token}/sendMessage"
    for number, part in enumerate(split_messages(text), start=1):
        response = SESSION.post(url, json={
            "chat_id": chat_id,
            "text": part,
            "disable_web_page_preview": True,
        }, timeout=TIMEOUT)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram delivery failed: {result.get('description')}")
        print(f"Telegram message {number} delivered", flush=True)
        time.sleep(0.5)


def main():
    for key in ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(key):
            raise RuntimeError(f"Missing required secret or variable: {key}")
    print("Starting Neurosurgery Digest...", flush=True)
    articles, start, end = select_articles()
    print(f"Collected {len(articles)} candidate articles", flush=True)
    digest = make_digest(articles, start, end)
    send_telegram(digest)
    print("SUCCESS: digest delivered to Telegram", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
