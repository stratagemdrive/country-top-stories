"""
build_country_headlines.py
==========================
Fetches up to 3 prominent headlines per country from NewsAPI.org,
covering a 7-day rolling window ending at the time of initialization.

Outputs: public/country_headlines.json

Format:
[
  {
    "country": "Russia",
    "iso2": "RU",
    "headlines": [
      {
        "title": "...",
        "url": "...",
        "source": "...",
        "publishedAt": "2026-05-08T23:54:00Z"
      },
      ...
    ],
    "lastUpdated": "2026-05-08T23:54:00Z"
  },
  ...
]

Dependencies (requirements.txt):
  requests
  python-dateutil
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from dateutil import parser as dtparser


# ─────────────────────────── CONFIG ───────────────────────────

NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
NEWSAPI_URL = "https://newsapi.org/v2/everything"

WINDOW_DAYS = 7          # look back 7 days from now
HEADLINES_PER_COUNTRY = 3
REQUEST_SLEEP = 0.25     # seconds between API calls (free-tier safe)
MAX_RETRIES = 3
RETRY_SLEEP = 2.0
TIMEOUT = 20
PAGE_SIZE = 20           # articles per request (keep low for free tier)

# ── 30 major English-language sources: UK, USA, Canada ──────────
SOURCES = ",".join([
    # USA
    "the-washington-post",
    "the-new-york-times",
    "cnn",
    "abc-news",
    "nbc-news",
    "cbs-news",
    "msnbc",
    "fox-news",
    "the-wall-street-journal",
    "axios",
    "politico",
    "bloomberg",
    "business-insider",
    "buzzfeed",
    "time",
    "newsweek",
    "the-hill",
    "npr",
    # UK
    "bbc-news",
    "the-guardian-uk",
    "the-telegraph",
    "the-times",
    "independent",
    "sky-news",
    "financial-times",
    "the-economist",
    "daily-mail",
    # Canada
    "cbc-news",
    "global-news",
    "the-globe-and-mail",
])

# ── Countries to track ─────────────────────────────────────────
COUNTRIES = [
    {"country": "Russia",       "iso2": "RU"},
    {"country": "India",        "iso2": "IN"},
    {"country": "Pakistan",     "iso2": "PK"},
    {"country": "China",        "iso2": "CN"},
    {"country": "United Kingdom","iso2": "GB"},
    {"country": "Germany",      "iso2": "DE"},
    {"country": "UAE",          "iso2": "AE"},
    {"country": "Saudi Arabia", "iso2": "SA"},
    {"country": "Israel",       "iso2": "IL"},
    {"country": "Palestine",    "iso2": "PS"},
    {"country": "Mexico",       "iso2": "MX"},
    {"country": "Brazil",       "iso2": "BR"},
    {"country": "Canada",       "iso2": "CA"},
    {"country": "Nigeria",      "iso2": "NG"},
    {"country": "Japan",        "iso2": "JP"},
    {"country": "Iran",         "iso2": "IR"},
    {"country": "Syria",        "iso2": "SY"},
    {"country": "France",       "iso2": "FR"},
    {"country": "Turkey",       "iso2": "TR"},
    {"country": "Venezuela",    "iso2": "VE"},
    {"country": "Vietnam",      "iso2": "VN"},
    {"country": "Taiwan",       "iso2": "TW"},
    {"country": "South Korea",  "iso2": "KR"},
    {"country": "North Korea",  "iso2": "KP"},
    {"country": "Indonesia",    "iso2": "ID"},
    {"country": "Myanmar",      "iso2": "MM"},
    {"country": "Armenia",      "iso2": "AM"},
    {"country": "Azerbaijan",   "iso2": "AZ"},
    {"country": "Morocco",      "iso2": "MA"},
    {"country": "Somalia",      "iso2": "SO"},
    {"country": "Yemen",        "iso2": "YE"},
    {"country": "Libya",        "iso2": "LY"},
    {"country": "Egypt",        "iso2": "EG"},
    {"country": "Algeria",      "iso2": "DZ"},
    {"country": "Argentina",    "iso2": "AR"},
    {"country": "Chile",        "iso2": "CL"},
    {"country": "Peru",         "iso2": "PE"},
    {"country": "Cuba",         "iso2": "CU"},
    {"country": "Colombia",     "iso2": "CO"},
    {"country": "Panama",       "iso2": "PA"},
    {"country": "El Salvador",  "iso2": "SV"},
    {"country": "Denmark",      "iso2": "DK"},
    {"country": "Sudan",        "iso2": "SD"},
    {"country": "Ukraine",      "iso2": "UA"},
]

# Search terms per country — expanded synonyms for better recall
COUNTRY_QUERY_MAP: Dict[str, str] = {
    "RU": "Russia OR Kremlin OR Moscow OR Putin",
    "IN": "India OR Modi OR New Delhi",
    "PK": "Pakistan OR Islamabad OR Pakistani",
    "CN": "China OR Beijing OR Xi Jinping OR CCP",
    "GB": "United Kingdom OR UK OR Britain OR London OR British",
    "DE": "Germany OR Berlin OR German",
    "AE": "UAE OR \"United Arab Emirates\" OR Dubai OR Abu Dhabi",
    "SA": "\"Saudi Arabia\" OR Riyadh OR MBS",
    "IL": "Israel OR Jerusalem OR Israeli OR IDF",
    "PS": "Palestine OR Gaza OR Palestinian OR West Bank",
    "MX": "Mexico OR Mexico City OR Mexican",
    "BR": "Brazil OR Brasilia OR Lula OR Brazilian",
    "CA": "Canada OR Ottawa OR Trudeau OR Canadian",
    "NG": "Nigeria OR Abuja OR Lagos OR Nigerian",
    "JP": "Japan OR Tokyo OR Japanese",
    "IR": "Iran OR Tehran OR Iranian",
    "SY": "Syria OR Damascus OR Syrian",
    "FR": "France OR Paris OR Macron OR French",
    "TR": "Turkey OR Ankara OR Erdogan OR Turkish",
    "VE": "Venezuela OR Caracas OR Maduro OR Venezuelan",
    "VN": "Vietnam OR Hanoi OR Vietnamese",
    "TW": "Taiwan OR Taipei OR Taiwanese",
    "KR": "\"South Korea\" OR Seoul OR Korean",
    "KP": "\"North Korea\" OR Pyongyang OR Kim Jong",
    "ID": "Indonesia OR Jakarta OR Indonesian",
    "MM": "Myanmar OR Burma OR Naypyidaw OR Burmese",
    "AM": "Armenia OR Yerevan OR Armenian",
    "AZ": "Azerbaijan OR Baku OR Azerbaijani",
    "MA": "Morocco OR Rabat OR Moroccan",
    "SO": "Somalia OR Mogadishu OR Somali",
    "YE": "Yemen OR Sanaa OR Yemeni OR Houthi",
    "LY": "Libya OR Tripoli OR Libyan",
    "EG": "Egypt OR Cairo OR Egyptian OR Sisi",
    "DZ": "Algeria OR Algiers OR Algerian",
    "AR": "Argentina OR Buenos Aires OR Milei OR Argentine",
    "CL": "Chile OR Santiago OR Chilean",
    "PE": "Peru OR Lima OR Peruvian",
    "CU": "Cuba OR Havana OR Cuban",
    "CO": "Colombia OR Bogota OR Colombian OR Petro",
    "PA": "Panama OR \"Panama Canal\" OR Panamanian",
    "SV": "\"El Salvador\" OR \"San Salvador\" OR Bukele OR Salvadoran",
    "DK": "Denmark OR Copenhagen OR Danish",
    "SD": "Sudan OR Khartoum OR Sudanese",
    "UA": "Ukraine OR Kyiv OR Zelenskyy OR Ukrainian",
}

# ── Headline cleaning ─────────────────────────────────────────
TITLE_PREFIXES_TO_DROP = [
    r"^watch( now)?:\s*",
    r"^live( now)?:\s*",
    r"^video:\s*",
    r"^analysis:\s*",
    r"^explainer:\s*",
    r"^opinion:\s*",
    r"^what to know:\s*",
    r"^fact check:\s*",
]
TITLE_SUFFIXES_TO_DROP = [
    r"\s*[-|]\s*bbc news\s*$",
    r"\s*[-|]\s*the guardian\s*$",
    r"\s*[-|]\s*reuters\s*$",
    r"\s*[-|]\s*cnn\s*$",
    r"\s*[-|]\s*npr\s*$",
    r"\s*[-|]\s*bloomberg\s*$",
    r"\s*[-|]\s*financial times\s*$",
]
TITLE_TRAILING_BRACKETS = [
    r"\s*\((?:video|watch|live|updated?|analysis|opinion)\)\s*$",
    r"\s*\[(?:video|watch|live|updated?|analysis|opinion)\]\s*$",
]

# Importance signals for ranking
IMPORTANCE_HINTS = [
    "war", "attack", "strike", "killed", "dead", "crisis", "conflict",
    "invasion", "sanction", "nuclear", "missile", "troops", "ceasefire",
    "summit", "deal", "agreement", "treaty", "election", "coup",
    "collapse", "escalat", "historic", "major", "urgent", "emergency",
    "explosion", "arrest", "sentenced", "protest", "crackdown",
]


# ─────────────────────────── HELPERS ──────────────────────────

def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def clean_headline(title: str) -> str:
    if not title:
        return ""
    t = title.strip()
    for pat in TITLE_PREFIXES_TO_DROP:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    for pat in TITLE_SUFFIXES_TO_DROP:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    for pat in TITLE_TRAILING_BRACKETS:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+[\|\-]\s*(?:news|newshour|world|international)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def canonicalize_url(url: str) -> str:
    try:
        u = urlparse(url)
        qs = parse_qs(u.query, keep_blank_values=True)
        drop_params = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid"}
        for p in list(qs.keys()):
            if p.lower() in drop_params:
                qs.pop(p, None)
        new_q = urlencode({k: v[0] for k, v in qs.items() if v})
        path = u.path.rstrip("/") or "/"
        return urlunparse((u.scheme, u.netloc, path, u.params, new_q, ""))
    except Exception:
        return url


def story_signature(title: str) -> str:
    """Normalised token fingerprint for deduplication."""
    t = _norm(title)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    stop = {
        "the","and","for","with","from","that","this","after","over","into",
        "says","say","said","will","could","would","should","amid","about",
        "new","more","than","they","their","its","his","her","your","our",
        "report","reports","update","latest","live","watch","has","have","been",
    }
    tokens = [x for x in t.split() if len(x) >= 3 and x not in stop]
    return " ".join(tokens[:10])


def importance_score(title: str) -> int:
    t = _norm(title)
    return sum(1 for h in IMPORTANCE_HINTS if h in t)


def parse_published_at(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        dt = dtparser.parse(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return raw


# ─────────────────────────── NEWSAPI ──────────────────────────

def newsapi_request(params: dict, retries: int = MAX_RETRIES) -> Optional[dict]:
    headers = {"X-Api-Key": NEWSAPI_KEY}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(NEWSAPI_URL, params=params, headers=headers, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                # Rate limited — back off
                wait = RETRY_SLEEP * attempt * 2
                print(f"  ⚠ Rate limited (429). Waiting {wait}s…")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                print(f"  ✗ Auth error {r.status_code} — check NEWSAPI_KEY")
                return None
            print(f"  ✗ HTTP {r.status_code} on attempt {attempt}")
        except requests.RequestException as e:
            print(f"  ✗ Request error attempt {attempt}: {e}")
        time.sleep(RETRY_SLEEP * attempt)
    return None


def fetch_country_articles(
    iso2: str,
    country_name: str,
    from_dt: datetime,
    to_dt: datetime,
) -> List[dict]:
    """
    Query NewsAPI for articles mentioning this country within the window.
    Returns raw article dicts from NewsAPI.
    """
    query = COUNTRY_QUERY_MAP.get(iso2, country_name)
    params = {
        "q": query,
        "sources": SOURCES,
        "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to":   to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "language": "en",
        "sortBy": "relevancy",
        "pageSize": PAGE_SIZE,
        "page": 1,
    }
    data = newsapi_request(params)
    if not data or data.get("status") != "ok":
        print(f"  ⚠ No data for {country_name} ({iso2}): {data.get('message','') if data else 'no response'}")
        return []
    return data.get("articles", [])


def select_top_headlines(articles: List[dict], n: int = HEADLINES_PER_COUNTRY) -> List[dict]:
    """
    Deduplicate and rank articles, returning the top n.
    Ranking: importance score (keyword density) + recency bonus.
    """
    seen_urls: set = set()
    seen_sigs: set = set()
    candidates: List[Tuple[float, dict]] = []

    for art in articles:
        raw_title = (art.get("title") or "").strip()
        url = canonicalize_url((art.get("url") or "").strip())
        source_name = (art.get("source") or {}).get("name") or ""
        published_raw = art.get("publishedAt")

        if not raw_title or not url:
            continue
        # NewsAPI sometimes returns "[Removed]" placeholders
        if "[removed]" in raw_title.lower() or "[removed]" in url.lower():
            continue

        title = clean_headline(raw_title)
        if not title:
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        sig = story_signature(title)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        # Compute score
        imp = importance_score(title)
        try:
            ts = dtparser.parse(published_raw).timestamp() if published_raw else 0.0
        except Exception:
            ts = 0.0

        # Normalise timestamp to a 0-1 recency bonus (within a 7-day window)
        recency = ts / (7 * 86400)  # small relative contribution
        score = (imp * 1_000_000) + recency

        candidates.append((score, {
            "title": title,
            "url": url,
            "source": source_name,
            "publishedAt": parse_published_at(published_raw),
        }))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in candidates[:n]]


# ─────────────────────────── MAIN ─────────────────────────────

def main() -> None:
    if not NEWSAPI_KEY:
        raise EnvironmentError(
            "NEWSAPI_KEY environment variable is not set. "
            "Add it as a GitHub Actions secret named NEWSAPI_KEY."
        )

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=WINDOW_DAYS)

    print(f"🕐 Run time : {now.isoformat()}")
    print(f"📅 Window  : {window_start.isoformat()} → {now.isoformat()}")
    print(f"🌍 Countries: {len(COUNTRIES)}")
    print()

    results: List[dict] = []

    for entry in COUNTRIES:
        iso2 = entry["iso2"]
        country_name = entry["country"]
        print(f"  Fetching: {country_name} ({iso2})…", end=" ", flush=True)

        articles = fetch_country_articles(iso2, country_name, window_start, now)
        headlines = select_top_headlines(articles)

        results.append({
            "country": country_name,
            "iso2": iso2,
            "headlines": headlines,
            "lastUpdated": now.isoformat().replace("+00:00", "Z"),
        })

        print(f"✓ {len(headlines)} headline(s)")
        time.sleep(REQUEST_SLEEP)

    out_dir = Path("public")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "country_headlines.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✅ Wrote {len(results)} countries → {out_path.resolve()}")


if __name__ == "__main__":
    main()
