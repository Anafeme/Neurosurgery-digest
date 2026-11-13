"""Weekly clinical literature digest: PubMed -> OpenAI -> Telegram."""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

import requests
from openai import OpenAI

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TG = "https://api.telegram.org/bot"
TIMEOUT = 35
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NeuroDigestBot/1.0 (academic literature digest)"})

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

def pubmed_ids(query, start, end, count=30):
     for attempt in range(6):
        response = SESSION.get(
            EUTILS + "efetch.fcgi",
            params={
                "db": "pubmed",
                "id": ",".join(ids),
                "retmode": "xml",
            },
            timeout=TIMEOUT,
        )
        if response.status_code in (429, 500, 502, 503, 504):
            if attempt < 5:
                time.sleep(min(15 * (2 ** attempt), 120))
                continue
        response.raise_for_status()
        break

    return response.json()["esearchresult"]["idlist"]

def flatten(element):
    return " ".join("".join(element.itertext()).split()) if element is not None else ""

def fetch_articles(ids):
    if not ids:
        return []
    response = SESSION.post(EUTILS + "efetch.fcgi", data={
        "db": "pubmed", "id": ",".join(ids), "retmode": "xml"
    }, timeout=TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    out = []
    for article in root.findall(".//PubmedArticle"):
        pmid = flatten(article.find("./MedlineCitation/PMID"))
        title = flatten(article.find(".//ArticleTitle"))
        abstract = flatten(article.find(".//Abstract"))
        journal = flatten(article.find(".//Journal/Title"))
        year = flatten(article.find(".//PubDate/Year")) or flatten(article.find(".//PubDate/MedlineDate"))
        doi = ""
        for aid in article.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi":
                doi = flatten(aid)
                break
        if pmid and title and abstract:
            out.append({"pmid": pmid, "title": title, "abstract": abstract[:5500],
                        "journal": journal, "year": year, "doi": doi,
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"})
    return out

def select_articles():
    # Publication-date filtering can miss recently indexed articles; report window explicitly.
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=8)
    candidates = {}
    for area, query in QUERIES.items():
        ids = pubmed_ids(query, start.isoformat(), end.isoformat())
        for a in fetch_articles(ids):
            a["areas"] = sorted(set(candidates.get(a["pmid"], {}).get("areas", [])) | {area})
            candidates[a["pmid"]] = a
        time.sleep(0.4)
    # Balance categories; model makes final quality selection.
    groups = {area: [a for a in candidates.values() if area in a["areas"]]
              for area in QUERIES}
    selected, used = [], set()
    for area in QUERIES:
        for a in groups[area][:12]:
            if a["pmid"] not in used:
                selected.append(a)
                used.add(a["pmid"])
    return selected[:24], start, end

def make_digest(articles, start, end):
    if not articles:
        return f"Научный дайджест: {start} — {end}\n\nВ PubMed не найдено подходящих публикаций с аннотациями за указанный период."
    source = "\n\n".join(
        f"PMID {a['pmid']} | Темы: {', '.join(a['areas'])}\n"
        f"Название: {a['title']}\nЖурнал: {a['journal']}\n"
        f"Год: {a['year']}\nDOI: {a['doi'] or 'не указан'}\n"
        f"Ссылка: {a['url']}\nАннотация: {a['abstract']}"
        for a in articles
    )
    instructions = (
        "Ты медицинский научный редактор. Подготовь на РУССКОМ языке "
        "клинический дайджест по нейрохирургии и нейрореанимации. "
        "Выбери до 10 (предпочтительно 8–10, только если качество позволяет) "
        "наиболее значимых РАЗНЫХ статей из списка, обеспечивая охват обеих тем. "
        "Используй ТОЛЬКО предоставленные метаданные и аннотации. "
        "Для каждой: оригинальное название, PMID-ссылка, дизайн/выборка "
        "(если сообщены), основные результаты с числами (только если они есть), "
        "ограничения, клиническая значимость и критическая оценка. "
        "Если полный текст недоступен, прямо укажи, что оценка основана "
        "только на аннотации. Не придумывай авторов, статистику, исходы или DOI. "
        "Не называй аннотацию полноценной экспертизой риска систематической ошибки. "
        "Не делай категоричных лечебных рекомендаций по одному исследованию. "
        "В конце общий вывод недели и нерешённые вопросы. "
        "Если подходящих качественных работ меньше 8, не заполняй список "
        "слабыми работами искусственно."
    )
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=120.0)
    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"Период публикации: {start} — {end}\n\n{source}"}
        ],
        temperature=0.2,
    )
    return f"НЕЙРОДАЙДЖЕСТ | {start} — {end}\n\n" + (response.choices[0].message.content or "")

def chunks(text, limit=3800):
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
    chat = os.environ["TELEGRAM_CHAT_ID"]
    for part in chunks(text):
        response = SESSION.post(
            f"{TG}{token}/sendMessage",
            json={"chat_id": chat, "text": part, "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram delivery failed: " + str(data.get("description")))
        time.sleep(0.5)

def main():
    for key in ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(key):
            raise RuntimeError(f"Missing required secret or variable: {key}")
    articles, start, end = select_articles()
    digest = make_digest(articles, start, end)
    send_telegram(digest)
    print(f"Delivered digest; {len(articles)} candidate abstracts; {start} to {end}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
