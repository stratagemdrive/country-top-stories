"""
build_country_headlines.py
==========================
Fetches up to 3 prominent headlines per country using RSS feeds only.
No API key required.

Logic:
- Pulls from 30 major RSS feeds (UK, USA, Canada)
- 7-day rolling window ending at runtime
- For each country, scans all articles for mentions using name + synonym matching
- Ranks by importance keyword density + source diversity + recency
- Outputs top 3 unique headlines per country

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
      }
    ],
    "lastUpdated": "2026-05-08T23:54:00Z"
  },
  ...
]

Dependencies (requirements.txt):
  feedparser
  requests
  python-dateutil
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import feedparser
import requests
from dateutil import parser as dtparser


# ─────────────────────────── CONFIG ───────────────────────────

WINDOW_DAYS = 7
HEADLINES_PER_COUNTRY = 3
TIMEOUT = 20
MAX_RETRIES = 3
RETRY_SLEEP = 1.2
FEED_SLEEP = 0.3   # polite delay between feed fetches

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── 30 major RSS feeds: USA, UK, Canada ───────────────────────
RSS_FEEDS: Dict[str, str] = {
    # USA
    "BBC News":            "https://feeds.bbci.co.uk/news/world/rss.xml",
    "NPR World":           "https://www.npr.org/rss/rss.php?id=1004",
    "PBS NewsHour":        "https://www.pbs.org/newshour/feeds/rss/world",
    "CNN":                 "http://rss.cnn.com/rss/edition_world.rss",
    "ABC News":            "https://abcnews.go.com/abcnews/internationalheadlines",
    "CBS News":            "https://www.cbsnews.com/latest/rss/world",
    "NBC News":            "https://feeds.nbcnews.com/nbcnews/public/world",
    "Fox News World":      "https://moxie.foxnews.com/google-publisher/world.xml",
    "Axios World":         "https://api.axios.com/feed/world",
    "Time Magazine":       "https://time.com/feed/",
    "Newsweek":            "https://www.newsweek.com/rss",
    "Bloomberg":           "https://feeds.bloomberg.com/politics/news.rss",
    "Business Insider":    "https://feeds.businessinsider.com/custom/all",
    "The Hill":            "https://thehill.com/homenews/feed/",
    "Politico":            "https://rss.politico.com/politics-news.xml",
    # UK
    "The Guardian":        "https://www.theguardian.com/world/rss",
    "The Telegraph":       "https://www.telegraph.co.uk/rss.xml",
    "The Independent":     "https://www.independent.co.uk/news/world/rss",
    "Sky News":            "https://feeds.skynews.com/feeds/rss/world.xml",
    "The Economist":       "https://www.economist.com/the-world-this-week/rss.xml",
    "Financial Times":     "https://www.ft.com/world?format=rss",
    "Daily Mail World":    "https://www.dailymail.co.uk/news/worldnews/index.rss",
    "Evening Standard":    "https://www.standard.co.uk/rss",
    "The Times UK":        "https://www.thetimes.co.uk/rss/world",
    "Al Jazeera":          "https://www.aljazeera.com/xml/rss/all.xml",
    # Canada
    "CBC News World":      "https://rss.cbc.ca/lineup/world.xml",
    "Global News":         "https://globalnews.ca/world/feed/",
    "Toronto Star":        "https://www.thestar.com/content/thestar/feed.RSSManagerServlet.articles.topstories.rss",
    "National Post":       "https://nationalpost.com/feed/",
    "CTV News":            "https://www.ctvnews.ca/rss/ctvnews-ca-world-public-rss-1.822289",
}

# ── Countries to track ─────────────────────────────────────────
COUNTRIES = [
    {"country": "Russia",        "iso2": "RU"},
    {"country": "India",         "iso2": "IN"},
    {"country": "Pakistan",      "iso2": "PK"},
    {"country": "China",         "iso2": "CN"},
    {"country": "United Kingdom","iso2": "GB"},
    {"country": "Germany",       "iso2": "DE"},
    {"country": "UAE",           "iso2": "AE"},
    {"country": "Saudi Arabia",  "iso2": "SA"},
    {"country": "Israel",        "iso2": "IL"},
    {"country": "Palestine",     "iso2": "PS"},
    {"country": "Mexico",        "iso2": "MX"},
    {"country": "Brazil",        "iso2": "BR"},
    {"country": "Canada",        "iso2": "CA"},
    {"country": "Nigeria",       "iso2": "NG"},
    {"country": "Japan",         "iso2": "JP"},
    {"country": "Iran",          "iso2": "IR"},
    {"country": "Syria",         "iso2": "SY"},
    {"country": "France",        "iso2": "FR"},
    {"country": "Turkey",        "iso2": "TR"},
    {"country": "Venezuela",     "iso2": "VE"},
    {"country": "Vietnam",       "iso2": "VN"},
    {"country": "Taiwan",        "iso2": "TW"},
    {"country": "South Korea",   "iso2": "KR"},
    {"country": "North Korea",   "iso2": "KP"},
    {"country": "Indonesia",     "iso2": "ID"},
    {"country": "Myanmar",       "iso2": "MM"},
    {"country": "Armenia",       "iso2": "AM"},
    {"country": "Azerbaijan",    "iso2": "AZ"},
    {"country": "Morocco",       "iso2": "MA"},
    {"country": "Somalia",       "iso2": "SO"},
    {"country": "Yemen",         "iso2": "YE"},
    {"country": "Libya",         "iso2": "LY"},
    {"country": "Egypt",         "iso2": "EG"},
    {"country": "Algeria",       "iso2": "DZ"},
    {"country": "Argentina",     "iso2": "AR"},
    {"country": "Chile",         "iso2": "CL"},
    {"country": "Peru",          "iso2": "PE"},
    {"country": "Cuba",          "iso2": "CU"},
    {"country": "Colombia",      "iso2": "CO"},
    {"country": "Panama",        "iso2": "PA"},
    {"country": "El Salvador",   "iso2": "SV"},
    {"country": "Denmark",       "iso2": "DK"},
    {"country": "Sudan",         "iso2": "SD"},
    {"country": "Ukraine",       "iso2": "UA"},
]

# Synonyms used to match articles to a country
COUNTRY_TERMS: Dict[str, List[str]] = {
    "RU": ["russia", "russian", "kremlin", "moscow", "putin"],
    "IN": ["india", "indian", "modi", "new delhi", "delhi"],
    "PK": ["pakistan", "pakistani", "islamabad", "karachi"],
    "CN": ["china", "chinese", "beijing", "xi jinping", "ccp", "shanghai"],
    "GB": ["united kingdom", "britain", "british", "london", "england", "scotland", "wales"],
    "DE": ["germany", "german", "berlin", "bundeswehr"],
    "AE": ["uae", "united arab emirates", "dubai", "abu dhabi", "emirati"],
    "SA": ["saudi arabia", "saudi", "riyadh", "mbs", "bin salman"],
    "IL": ["israel", "israeli", "idf", "jerusalem", "tel aviv", "netanyahu"],
    "PS": ["palestine", "palestinian", "gaza", "west bank", "hamas", "ramallah"],
    "MX": ["mexico", "mexican", "mexico city"],
    "BR": ["brazil", "brazilian", "brasilia", "lula"],
    "CA": ["canada", "canadian", "ottawa", "trudeau", "toronto"],
    "NG": ["nigeria", "nigerian", "abuja", "lagos"],
    "JP": ["japan", "japanese", "tokyo"],
    "IR": ["iran", "iranian", "tehran", "khamenei", "irgc"],
    "SY": ["syria", "syrian", "damascus", "aleppo"],
    "FR": ["france", "french", "paris", "macron"],
    "TR": ["turkey", "turkish", "ankara", "erdogan"],
    "VE": ["venezuela", "venezuelan", "caracas", "maduro"],
    "VN": ["vietnam", "vietnamese", "hanoi"],
    "TW": ["taiwan", "taiwanese", "taipei"],
    "KR": ["south korea", "south korean", "seoul"],
    "KP": ["north korea", "north korean", "pyongyang", "kim jong"],
    "ID": ["indonesia", "indonesian", "jakarta"],
    "MM": ["myanmar", "burmese", "burma", "naypyidaw"],
    "AM": ["armenia", "armenian", "yerevan"],
    "AZ": ["azerbaijan", "azerbaijani", "baku"],
    "MA": ["morocco", "moroccan", "rabat"],
    "SO": ["somalia", "somali", "mogadishu"],
    "YE": ["yemen", "yemeni", "sanaa", "houthi", "houthis"],
    "LY": ["libya", "libyan", "tripoli", "benghazi"],
    "EG": ["egypt", "egyptian", "cairo", "sisi"],
    "DZ": ["algeria", "algerian", "algiers"],
    "AR": ["argentina", "argentine", "argentinian", "buenos aires", "milei"],
    "CL": ["chile", "chilean", "santiago"],
    "PE": ["peru", "peruvian", "lima"],
    "CU": ["cuba", "cuban", "havana"],
    "CO": ["colombia", "colombian", "bogota", "petro"],
    "PA": ["panama", "panamanian", "panama canal"],
    "SV": ["el salvador", "salvadoran", "san salvador", "bukele"],
    "DK": ["denmark", "danish", "copenhagen"],
    "SD": ["sudan", "sudanese", "khartoum"],
    "UA": ["ukraine", "ukrainian", "kyiv", "kiev", "zelenskyy", "zelensky"],
}

# Importance signals for ranking
IMPORTANCE_HINTS = [
    "war", "attack", "strike", "killed", "dead", "crisis", "conflict",
    "invasion", "sanction", "nuclear", "missile", "troops", "ceasefire",
    "summit", "deal", "agreement", "treaty", "election", "coup",
    "collapse", "escalat", "historic", "major", "urgent", "emergency",
    "explosion", "arrest", "sentenced", "protest", "crackdown",
    "offensive", "battle", "siege", "casualt", "civilian",
]

# ── Headline cleaning ──────────────────────────────────────────
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
    r"\s*[-|]\s*pbs newshour?\s*$",
    r"\s*[-|]\s*sky news\s*$",
    r"\s*[-|]\s*al jazeera\s*$",
    r"\s*[-|]\s*cbc news\s*$",
]
TITLE_TRAILING_BRACKETS = [
    r"\s*\((?:video|watch|live|updated?|analysis|opinion)\)\s*$",
    r"\s*\[(?:video|watch|live|updated?|analysis|opinion)\]\s*$",
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
        "was","are","were","not","but","also","just","how","who","why","what",
    }
    tokens = [x for x in t.split() if len(x) >= 3 and x not in stop]
    return " ".join(tokens[:10])


def parse_dt(entry: dict) -> Optional[datetime]:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(k)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for k in ("published", "updated", "created"):
        v = entry.get(k)
        if v:
            try:
                dt = dtparser.parse(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    return None


def importance_score(title: str) -> int:
    t = _norm(title)
    return sum(1 for h in IMPORTANCE_HINTS if h in t)


def article_mentions_country(title: str, summary: str, iso2: str) -> bool:
    """Return True if the article text mentions any synonym for this country."""
    text = _norm(f"{title} {summary}")
    return any(term in text for term in COUNTRY_TERMS.get(iso2, []))


# ─────────────────────────── RSS FETCHING ─────────────────────

def fetch_text(url: str) -> Optional[str]:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = sess.get(url, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r.text
        except requests.RequestException:
            pass
        time.sleep(RETRY_SLEEP * attempt)
    return None


def fetch_all_articles(cutoff: datetime) -> List[dict]:
    """
    Pull all RSS feeds and return every article published after cutoff.
    Each article dict: title, url, source, publishedAt (ISO string), summary, _ts (float)
    """
    all_articles: List[dict] = []

    for source_name, feed_url in RSS_FEEDS.items():
        print(f"  📡 {source_name}…", end=" ", flush=True)
        txt = fetch_text(feed_url)
        if not txt:
            print("✗ failed")
            time.sleep(FEED_SLEEP)
            continue

        d = feedparser.parse(txt)
        count = 0
        for e in getattr(d, "entries", []):
            raw_title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not raw_title or not link:
                continue
            if "[removed]" in raw_title.lower():
                continue

            title = clean_headline(raw_title)
            if not title:
                continue

            url = canonicalize_url(link)
            dt = parse_dt(e)

            # Skip articles outside our window
            if dt and dt < cutoff:
                continue

            summary = _norm(e.get("summary") or e.get("description") or "")
            ts = dt.timestamp() if dt else 0.0
            pub_str = dt.isoformat().replace("+00:00", "Z") if dt else None

            all_articles.append({
                "title": title,
                "url": url,
                "source": source_name,
                "publishedAt": pub_str,
                "summary": summary,
                "_ts": ts,
            })
            count += 1

        print(f"✓ {count} article(s) in window")
        time.sleep(FEED_SLEEP)

    return all_articles


# ─────────────────────────── RANKING ──────────────────────────

def select_top_for_country(
    articles: List[dict],
    iso2: str,
    n: int = HEADLINES_PER_COUNTRY,
) -> List[dict]:
    """
    Filter articles mentioning this country, deduplicate, rank, return top n.
    """
    matching = [a for a in articles if article_mentions_country(a["title"], a["summary"], iso2)]

    seen_urls: set = set()
    seen_sigs: set = set()
    candidates: List[Tuple[float, dict]] = []

    for a in matching:
        if a["url"] in seen_urls:
            continue
        seen_urls.add(a["url"])

        sig = story_signature(a["title"])
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        imp = importance_score(a["title"])
        recency = a["_ts"] / (7 * 86400)
        score = (imp * 1_000_000) + recency

        candidates.append((score, a))

    candidates.sort(key=lambda x: x[0], reverse=True)

    out = []
    for _, a in candidates[:n]:
        out.append({
            "title": a["title"],
            "url": a["url"],
            "source": a["source"],
            "publishedAt": a["publishedAt"],
        })
    return out


# ─────────────────────────── MAIN ─────────────────────────────

def main() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)

    print(f"🕐 Run time : {now.isoformat()}")
    print(f"📅 Window  : {cutoff.isoformat()} → {now.isoformat()}")
    print(f"🌍 Countries: {len(COUNTRIES)}")
    print(f"📰 Feeds    : {len(RSS_FEEDS)}")
    print()
    print("── Fetching RSS feeds ──")

    all_articles = fetch_all_articles(cutoff)
    print(f"\n✓ Total articles in window: {len(all_articles)}")
    print()
    print("── Matching articles to countries ──")

    results: List[dict] = []
    last_updated = now.isoformat().replace("+00:00", "Z")

    for entry in COUNTRIES:
        iso2 = entry["iso2"]
        country_name = entry["country"]
        headlines = select_top_for_country(all_articles, iso2)
        results.append({
            "country": country_name,
            "iso2": iso2,
            "headlines": headlines,
            "lastUpdated": last_updated,
        })
        print(f"  {country_name} ({iso2}): {len(headlines)} headline(s)")

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
