"""
build_country_headlines.py
==========================
Fetches up to 3 prominent headlines per country using RSS feeds only.
No API key required.  Runs continuously, polling every 3 hours.

Logic:
- Pulls from 30 major RSS feeds (UK, USA, Canada)
- 7-day rolling window; no article older than 7 days is ever surfaced
- For each country, scans all articles for mentions using name + synonym matching
- Title-primary matching: article must mention the country in its TITLE to qualify,
  OR have a strong match (multiple terms / full country name) in the title+summary
- Ranking priority (in order):
    1. UNIQUE stories — articles whose URL / story-signature have NOT appeared
       in the rolling archive yet (i.e. genuinely new to the feed)
    2. Within each tier, rank by importance-keyword density then recency
- Archive system:
    - public/archive/YYYY-MM-DD.jsonl  — one file per UTC day
    - Each line is a seen-article record: {url, sig, iso2, firstSeenAt}
    - On every run: load archive → prune entries > 7 days → merge new hits
    - Archive files older than 7 days are deleted automatically

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
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import feedparser
import requests
from dateutil import parser as dtparser


# ─────────────────────────── CONFIG ───────────────────────────

WINDOW_DAYS = 7              # max article age (days) — also archive retention
HEADLINES_PER_COUNTRY = 3
POLL_INTERVAL_HOURS = 3      # how often the continuous loop runs
TIMEOUT = 20
MAX_RETRIES = 3
RETRY_SLEEP = 1.2
FEED_SLEEP = 0.3             # polite delay between feed fetches

ARCHIVE_DIR = Path("public") / "archive"   # one .jsonl per UTC day

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
    # Original countries
    {"country": "Russia",               "iso2": "RU"},
    {"country": "India",                "iso2": "IN"},
    {"country": "Pakistan",             "iso2": "PK"},
    {"country": "China",                "iso2": "CN"},
    {"country": "United Kingdom",       "iso2": "GB"},
    {"country": "Germany",              "iso2": "DE"},
    {"country": "UAE",                  "iso2": "AE"},
    {"country": "Saudi Arabia",         "iso2": "SA"},
    {"country": "Israel",               "iso2": "IL"},
    {"country": "Palestine",            "iso2": "PS"},
    {"country": "Mexico",               "iso2": "MX"},
    {"country": "Brazil",               "iso2": "BR"},
    {"country": "Canada",               "iso2": "CA"},
    {"country": "Nigeria",              "iso2": "NG"},
    {"country": "Japan",                "iso2": "JP"},
    {"country": "Iran",                 "iso2": "IR"},
    {"country": "Syria",                "iso2": "SY"},
    {"country": "France",               "iso2": "FR"},
    {"country": "Turkey",               "iso2": "TR"},
    {"country": "Venezuela",            "iso2": "VE"},
    {"country": "Vietnam",              "iso2": "VN"},
    {"country": "Taiwan",               "iso2": "TW"},
    {"country": "South Korea",          "iso2": "KR"},
    {"country": "North Korea",          "iso2": "KP"},
    {"country": "Indonesia",            "iso2": "ID"},
    {"country": "Myanmar",              "iso2": "MM"},
    {"country": "Armenia",              "iso2": "AM"},
    {"country": "Azerbaijan",           "iso2": "AZ"},
    {"country": "Morocco",              "iso2": "MA"},
    {"country": "Somalia",              "iso2": "SO"},
    {"country": "Yemen",                "iso2": "YE"},
    {"country": "Libya",                "iso2": "LY"},
    {"country": "Egypt",                "iso2": "EG"},
    {"country": "Algeria",              "iso2": "DZ"},
    {"country": "Argentina",            "iso2": "AR"},
    {"country": "Chile",                "iso2": "CL"},
    {"country": "Peru",                 "iso2": "PE"},
    {"country": "Cuba",                 "iso2": "CU"},
    {"country": "Colombia",             "iso2": "CO"},
    {"country": "Panama",               "iso2": "PA"},
    {"country": "El Salvador",          "iso2": "SV"},
    {"country": "Denmark",              "iso2": "DK"},
    {"country": "Sudan",                "iso2": "SD"},
    {"country": "Ukraine",              "iso2": "UA"},
    # Newly added countries
    {"country": "Australia",            "iso2": "AU"},
    {"country": "Singapore",            "iso2": "SG"},
    {"country": "Philippines",          "iso2": "PH"},
    {"country": "Afghanistan",          "iso2": "AF"},
    {"country": "Iraq",                 "iso2": "IQ"},
    {"country": "Spain",                "iso2": "ES"},
    {"country": "Italy",                "iso2": "IT"},
    {"country": "Poland",               "iso2": "PL"},
    {"country": "Bolivia",              "iso2": "BO"},
    {"country": "New Zealand",          "iso2": "NZ"},
    {"country": "Portugal",             "iso2": "PT"},
    {"country": "Czech Republic",       "iso2": "CZ"},
    {"country": "Norway",               "iso2": "NO"},
    {"country": "Romania",              "iso2": "RO"},
    {"country": "Sweden",               "iso2": "SE"},
    {"country": "Hong Kong",            "iso2": "HK"},
    {"country": "Finland",              "iso2": "FI"},
    {"country": "Switzerland",          "iso2": "CH"},
    {"country": "Angola",               "iso2": "AO"},
    {"country": "South Africa",         "iso2": "ZA"},
    {"country": "Kenya",                "iso2": "KE"},
    {"country": "Oman",                 "iso2": "OM"},
    {"country": "Qatar",                "iso2": "QA"},
    {"country": "DRC",                  "iso2": "CD"},
    {"country": "Dominican Republic",   "iso2": "DO"},
    {"country": "Netherlands",          "iso2": "NL"},
    {"country": "Belgium",              "iso2": "BE"},
    {"country": "Malaysia",             "iso2": "MY"},
    {"country": "Guyana",               "iso2": "GY"},
    {"country": "Ireland",              "iso2": "IE"},
    {"country": "Austria",              "iso2": "AT"},
    {"country": "Belarus",              "iso2": "BY"},
    {"country": "Thailand",             "iso2": "TH"},
    {"country": "Cambodia",             "iso2": "KH"},
    {"country": "Laos",                 "iso2": "LA"},
    {"country": "Ecuador",              "iso2": "EC"},
    {"country": "Paraguay",             "iso2": "PY"},
    {"country": "Uruguay",              "iso2": "UY"},
    {"country": "Mali",                 "iso2": "ML"},
    {"country": "Botswana",             "iso2": "BW"},
    {"country": "Tanzania",             "iso2": "TZ"},
    {"country": "Madagascar",           "iso2": "MG"},
    {"country": "Turkmenistan",         "iso2": "TM"},
    {"country": "Kazakhstan",           "iso2": "KZ"},
    {"country": "Hungary",              "iso2": "HU"},
    {"country": "Serbia",               "iso2": "RS"},
    {"country": "Albania",              "iso2": "AL"},
    {"country": "Bulgaria",             "iso2": "BG"},
    {"country": "Moldova",              "iso2": "MD"},
    {"country": "Greece",               "iso2": "GR"},
    {"country": "Kosovo",               "iso2": "XK"},
    {"country": "Bahamas",              "iso2": "BS"},
    {"country": "Bangladesh",           "iso2": "BD"},
    {"country": "Nepal",                "iso2": "NP"},
    {"country": "Sri Lanka",            "iso2": "LK"},
    {"country": "Jordan",               "iso2": "JO"},
    {"country": "Lebanon",              "iso2": "LB"},
    {"country": "Kuwait",               "iso2": "KW"},
    {"country": "Bahrain",              "iso2": "BH"},
    {"country": "Tunisia",              "iso2": "TN"},
    {"country": "Ethiopia",             "iso2": "ET"},
    {"country": "Ghana",                "iso2": "GH"},
    {"country": "Ivory Coast",          "iso2": "CI"},
    {"country": "Senegal",              "iso2": "SN"},
    {"country": "Rwanda",               "iso2": "RW"},
    {"country": "Uganda",               "iso2": "UG"},
    {"country": "Zimbabwe",             "iso2": "ZW"},
    {"country": "Zambia",               "iso2": "ZM"},
    {"country": "Cameroon",             "iso2": "CM"},
    {"country": "Mozambique",           "iso2": "MZ"},
    {"country": "Burkina Faso",         "iso2": "BF"},
    {"country": "Niger",                "iso2": "NE"},
    {"country": "Chad",                 "iso2": "TD"},
    {"country": "Guinea",               "iso2": "GN"},
    {"country": "Uzbekistan",           "iso2": "UZ"},
    {"country": "Kyrgyzstan",           "iso2": "KG"},
    {"country": "Tajikistan",           "iso2": "TJ"},
    {"country": "Croatia",              "iso2": "HR"},
    {"country": "Slovakia",             "iso2": "SK"},
    {"country": "Slovenia",             "iso2": "SI"},
    {"country": "Lithuania",            "iso2": "LT"},
    {"country": "Latvia",               "iso2": "LV"},
    {"country": "Estonia",              "iso2": "EE"},
    {"country": "North Macedonia",      "iso2": "MK"},
    {"country": "Bosnia and Herzegovina","iso2": "BA"},
    {"country": "Montenegro",           "iso2": "ME"},
    {"country": "Guatemala",            "iso2": "GT"},
    {"country": "Honduras",             "iso2": "HN"},
    {"country": "Nicaragua",            "iso2": "NI"},
    {"country": "Costa Rica",           "iso2": "CR"},
    {"country": "Haiti",                "iso2": "HT"},
    {"country": "Trinidad and Tobago",  "iso2": "TT"},
    {"country": "Jamaica",              "iso2": "JM"},
    {"country": "South Sudan",          "iso2": "SS"},
    {"country": "Eritrea",              "iso2": "ER"},
    {"country": "Djibouti",             "iso2": "DJ"},
    {"country": "Mauritania",           "iso2": "MR"},
    {"country": "Liberia",              "iso2": "LR"},
    {"country": "Sierra Leone",         "iso2": "SL"},
    {"country": "Gabon",                "iso2": "GA"},
    {"country": "Congo",                "iso2": "CG"},
    {"country": "Namibia",              "iso2": "NA"},
    {"country": "Eswatini",             "iso2": "SZ"},
    {"country": "Lesotho",              "iso2": "LS"},
    {"country": "Malawi",               "iso2": "MW"},
    {"country": "Papua New Guinea",     "iso2": "PG"},
    {"country": "Mongolia",             "iso2": "MN"},
    {"country": "Brunei",               "iso2": "BN"},
    {"country": "Timor-Leste",          "iso2": "TL"},
    {"country": "Maldives",             "iso2": "MV"},
    {"country": "Bhutan",               "iso2": "BT"},
    {"country": "Georgia",              "iso2": "GE"},
    {"country": "Cyprus",               "iso2": "CY"},
    {"country": "Malta",                "iso2": "MT"},
    {"country": "Luxembourg",           "iso2": "LU"},
    {"country": "Iceland",              "iso2": "IS"},
]

# ── Country terms: (strong_terms, weak_terms) ──────────────────
COUNTRY_TERMS: Dict[str, Tuple[List[str], List[str]]] = {
    # ── Original entries ──
    "RU": (
        ["russia", "kremlin", "moscow", "putin"],
        ["russian"],
    ),
    "IN": (
        ["india", "new delhi", "modi"],
        ["indian"],
    ),
    "PK": (
        ["pakistan", "islamabad", "karachi"],
        ["pakistani"],
    ),
    "CN": (
        ["china", "beijing", "xi jinping", "shanghai", "chinese communist"],
        ["chinese", "ccp"],
    ),
    "GB": (
        ["united kingdom", "britain", "london", "england", "scotland", "wales",
         "westminster", "downing street"],
        ["british", "uk"],
    ),
    "DE": (
        ["germany", "berlin", "bundeswehr", "bundestag"],
        ["german"],
    ),
    "AE": (
        ["united arab emirates", "dubai", "abu dhabi"],
        ["uae", "emirati"],
    ),
    "SA": (
        ["saudi arabia", "riyadh", "bin salman"],
        ["saudi", "mbs"],
    ),
    "IL": (
        ["israel", "idf", "jerusalem", "tel aviv", "netanyahu"],
        ["israeli"],
    ),
    "PS": (
        ["palestine", "gaza", "west bank", "ramallah", "hamas"],
        ["palestinian"],
    ),
    "MX": (
        ["mexico", "mexico city"],
        ["mexican"],
    ),
    "BR": (
        ["brazil", "brasilia", "lula"],
        ["brazilian"],
    ),
    "CA": (
        ["canada", "ottawa", "toronto", "trudeau"],
        ["canadian"],
    ),
    "NG": (
        ["nigeria", "abuja", "lagos"],
        ["nigerian"],
    ),
    "JP": (
        ["japan", "tokyo"],
        ["japanese"],
    ),
    "IR": (
        ["iran", "tehran", "khamenei", "irgc"],
        ["iranian"],
    ),
    "SY": (
        ["syria", "damascus", "aleppo"],
        ["syrian"],
    ),
    "FR": (
        ["france", "paris", "macron", "élysée", "elysee"],
        ["french"],
    ),
    "TR": (
        ["turkey", "ankara", "erdogan"],
        ["turkish"],
    ),
    "VE": (
        ["venezuela", "caracas", "maduro"],
        ["venezuelan"],
    ),
    "VN": (
        ["vietnam", "hanoi", "ho chi minh"],
        ["vietnamese"],
    ),
    "TW": (
        ["taiwan", "taipei"],
        ["taiwanese"],
    ),
    "KR": (
        ["south korea", "seoul"],
        ["south korean"],
    ),
    "KP": (
        ["north korea", "pyongyang", "kim jong"],
        ["north korean"],
    ),
    "ID": (
        ["indonesia", "jakarta"],
        ["indonesian"],
    ),
    "MM": (
        ["myanmar", "naypyidaw", "burma"],
        ["burmese"],
    ),
    "AM": (
        ["armenia", "yerevan"],
        ["armenian"],
    ),
    "AZ": (
        ["azerbaijan", "baku"],
        ["azerbaijani"],
    ),
    "MA": (
        ["morocco", "rabat"],
        ["moroccan"],
    ),
    "SO": (
        ["somalia", "mogadishu", "somaliland"],
        ["somali"],
    ),
    "YE": (
        ["yemen", "sanaa", "houthis", "houthi"],
        ["yemeni"],
    ),
    "LY": (
        ["libya", "tripoli", "benghazi"],
        ["libyan"],
    ),
    "EG": (
        ["egypt", "cairo", "sisi"],
        ["egyptian"],
    ),
    "DZ": (
        ["algeria", "algiers"],
        ["algerian"],
    ),
    "AR": (
        ["argentina", "buenos aires", "milei"],
        ["argentine", "argentinian"],
    ),
    "CL": (
        ["chile", "santiago"],
        ["chilean"],
    ),
    "PE": (
        ["peru"],
        ["peruvian"],
    ),
    "CU": (
        ["cuba", "havana"],
        ["cuban"],
    ),
    "CO": (
        ["colombia", "bogota", "petro"],
        ["colombian"],
    ),
    "PA": (
        ["panama", "panama canal"],
        ["panamanian"],
    ),
    "SV": (
        ["el salvador", "san salvador", "bukele"],
        ["salvadoran"],
    ),
    "DK": (
        ["denmark", "copenhagen"],
        ["danish"],
    ),
    "SD": (
        ["sudan", "khartoum"],
        ["sudanese"],
    ),
    "UA": (
        ["ukraine", "kyiv", "kiev", "zelenskyy", "zelensky"],
        ["ukrainian"],
    ),
    # ── Newly added entries ──
    "AU": (
        ["australia", "canberra", "sydney", "melbourne"],
        ["australian"],
    ),
    "SG": (
        ["singapore"],
        ["singaporean"],
    ),
    "PH": (
        ["philippines", "manila", "marcos"],
        ["filipino", "philippine"],
    ),
    "AF": (
        ["afghanistan", "kabul", "taliban"],
        ["afghan"],
    ),
    "IQ": (
        ["iraq", "baghdad", "basra"],
        ["iraqi"],
    ),
    "ES": (
        ["spain", "madrid", "barcelona", "sanchez"],
        ["spanish"],
    ),
    "IT": (
        ["italy", "rome", "milan", "meloni"],
        ["italian"],
    ),
    "PL": (
        ["poland", "warsaw", "tusk"],
        ["polish"],
    ),
    "BO": (
        ["bolivia", "la paz", "sucre"],
        ["bolivian"],
    ),
    "NZ": (
        ["new zealand", "wellington", "auckland"],
        ["kiwi", "new zealander"],
    ),
    "PT": (
        ["portugal", "lisbon"],
        ["portuguese"],
    ),
    "CZ": (
        ["czech republic", "czechia", "prague"],
        ["czech"],
    ),
    "NO": (
        ["norway", "oslo"],
        ["norwegian"],
    ),
    "RO": (
        ["romania", "bucharest"],
        ["romanian"],
    ),
    "SE": (
        ["sweden", "stockholm"],
        ["swedish"],
    ),
    "HK": (
        ["hong kong"],
        ["hongkonger"],
    ),
    "FI": (
        ["finland", "helsinki"],
        ["finnish"],
    ),
    "CH": (
        ["switzerland", "bern", "zurich", "geneva"],
        ["swiss"],
    ),
    "AO": (
        ["angola", "luanda"],
        ["angolan"],
    ),
    "ZA": (
        ["south africa", "pretoria", "johannesburg", "cape town", "ramaphosa"],
        ["south african"],
    ),
    "KE": (
        ["kenya", "nairobi", "ruto"],
        ["kenyan"],
    ),
    "OM": (
        ["oman", "muscat"],
        ["omani"],
    ),
    "QA": (
        ["qatar", "doha"],
        ["qatari"],
    ),
    "CD": (
        ["democratic republic of the congo", "drc", "kinshasa", "congo-kinshasa"],
        ["congolese"],
    ),
    "DO": (
        ["dominican republic", "santo domingo"],
        ["dominican"],
    ),
    "NL": (
        ["netherlands", "amsterdam", "the hague", "schiphol"],
        ["dutch"],
    ),
    "BE": (
        ["belgium", "brussels", "antwerp"],
        ["belgian"],
    ),
    "MY": (
        ["malaysia", "kuala lumpur"],
        ["malaysian"],
    ),
    "GY": (
        ["guyana", "georgetown"],
        ["guyanese"],
    ),
    "IE": (
        ["ireland", "dublin"],
        ["irish"],
    ),
    "AT": (
        ["austria", "vienna"],
        ["austrian"],
    ),
    "BY": (
        ["belarus", "minsk", "lukashenko"],
        ["belarusian"],
    ),
    "TH": (
        ["thailand", "bangkok"],
        ["thai"],
    ),
    "KH": (
        ["cambodia", "phnom penh"],
        ["cambodian", "khmer"],
    ),
    "LA": (
        ["laos", "vientiane"],
        ["lao", "laotian"],
    ),
    "EC": (
        ["ecuador", "quito", "guayaquil", "noboa"],
        ["ecuadorian"],
    ),
    "PY": (
        ["paraguay", "asuncion"],
        ["paraguayan"],
    ),
    "UY": (
        ["uruguay", "montevideo"],
        ["uruguayan"],
    ),
    "ML": (
        ["mali", "bamako"],
        ["malian"],
    ),
    "BW": (
        ["botswana", "gaborone"],
        ["motswana", "batswana"],
    ),
    "TZ": (
        ["tanzania", "dar es salaam", "dodoma"],
        ["tanzanian"],
    ),
    "MG": (
        ["madagascar", "antananarivo"],
        ["malagasy"],
    ),
    "TM": (
        ["turkmenistan", "ashgabat"],
        ["turkmen"],
    ),
    "KZ": (
        ["kazakhstan", "astana", "almaty"],
        ["kazakh"],
    ),
    "HU": (
        ["hungary", "budapest", "orban"],
        ["hungarian"],
    ),
    "RS": (
        ["serbia", "belgrade", "vucic"],
        ["serbian"],
    ),
    "AL": (
        ["albania", "tirana"],
        ["albanian"],
    ),
    "BG": (
        ["bulgaria", "sofia"],
        ["bulgarian"],
    ),
    "MD": (
        ["moldova", "chisinau"],
        ["moldovan"],
    ),
    "GR": (
        ["greece", "athens", "thessaloniki"],
        ["greek"],
    ),
    "XK": (
        ["kosovo", "pristina"],
        ["kosovar"],
    ),
    "BS": (
        ["bahamas", "nassau"],
        ["bahamian"],
    ),
    "BD": (
        ["bangladesh", "dhaka"],
        ["bangladeshi"],
    ),
    "NP": (
        ["nepal", "kathmandu"],
        ["nepali", "nepalese"],
    ),
    "LK": (
        ["sri lanka", "colombo"],
        ["sri lankan"],
    ),
    "JO": (
        ["jordan", "amman", "king abdullah"],
        ["jordanian"],
    ),
    "LB": (
        ["lebanon", "beirut"],
        ["lebanese"],
    ),
    "KW": (
        ["kuwait", "kuwait city"],
        ["kuwaiti"],
    ),
    "BH": (
        ["bahrain", "manama"],
        ["bahraini"],
    ),
    "TN": (
        ["tunisia", "tunis", "saied"],
        ["tunisian"],
    ),
    "ET": (
        ["ethiopia", "addis ababa", "abiy"],
        ["ethiopian"],
    ),
    "GH": (
        ["ghana", "accra"],
        ["ghanaian"],
    ),
    "CI": (
        ["ivory coast", "cote d'ivoire", "abidjan", "yamoussoukro"],
        ["ivorian"],
    ),
    "SN": (
        ["senegal", "dakar"],
        ["senegalese"],
    ),
    "RW": (
        ["rwanda", "kigali", "kagame"],
        ["rwandan"],
    ),
    "UG": (
        ["uganda", "kampala", "museveni"],
        ["ugandan"],
    ),
    "ZW": (
        ["zimbabwe", "harare", "mnangagwa"],
        ["zimbabwean"],
    ),
    "ZM": (
        ["zambia", "lusaka"],
        ["zambian"],
    ),
    "CM": (
        ["cameroon", "yaounde", "douala"],
        ["cameroonian"],
    ),
    "MZ": (
        ["mozambique", "maputo"],
        ["mozambican"],
    ),
    "BF": (
        ["burkina faso", "ouagadougou"],
        ["burkinabe"],
    ),
    "NE": (
        ["niger", "niamey"],
        ["nigerien"],
    ),
    "TD": (
        ["chad", "ndjamena"],
        ["chadian"],
    ),
    "GN": (
        ["guinea", "conakry"],
        ["guinean"],
    ),
    "UZ": (
        ["uzbekistan", "tashkent"],
        ["uzbek"],
    ),
    "KG": (
        ["kyrgyzstan", "bishkek"],
        ["kyrgyz"],
    ),
    "TJ": (
        ["tajikistan", "dushanbe"],
        ["tajik"],
    ),
    "HR": (
        ["croatia", "zagreb"],
        ["croatian"],
    ),
    "SK": (
        ["slovakia", "bratislava", "fico"],
        ["slovak"],
    ),
    "SI": (
        ["slovenia", "ljubljana"],
        ["slovenian"],
    ),
    "LT": (
        ["lithuania", "vilnius"],
        ["lithuanian"],
    ),
    "LV": (
        ["latvia", "riga"],
        ["latvian"],
    ),
    "EE": (
        ["estonia", "tallinn"],
        ["estonian"],
    ),
    "MK": (
        ["north macedonia", "skopje"],
        ["macedonian"],
    ),
    "BA": (
        ["bosnia", "sarajevo", "herzegovina"],
        ["bosnian"],
    ),
    "ME": (
        ["montenegro", "podgorica"],
        ["montenegrin"],
    ),
    "GT": (
        ["guatemala", "guatemala city"],
        ["guatemalan"],
    ),
    "HN": (
        ["honduras", "tegucigalpa"],
        ["honduran"],
    ),
    "NI": (
        ["nicaragua", "managua", "ortega"],
        ["nicaraguan"],
    ),
    "CR": (
        ["costa rica", "san jose"],
        ["costa rican"],
    ),
    "HT": (
        ["haiti", "port-au-prince"],
        ["haitian"],
    ),
    "TT": (
        ["trinidad and tobago", "port of spain"],
        ["trinidadian", "tobagonian"],
    ),
    "JM": (
        ["jamaica", "kingston"],
        ["jamaican"],
    ),
    "SS": (
        ["south sudan", "juba"],
        ["south sudanese"],
    ),
    "ER": (
        ["eritrea", "asmara"],
        ["eritrean"],
    ),
    "DJ": (
        ["djibouti"],
        ["djiboutian"],
    ),
    "MR": (
        ["mauritania", "nouakchott"],
        ["mauritanian"],
    ),
    "LR": (
        ["liberia", "monrovia"],
        ["liberian"],
    ),
    "SL": (
        ["sierra leone", "freetown"],
        ["sierra leonean"],
    ),
    "GA": (
        ["gabon", "libreville"],
        ["gabonese"],
    ),
    "CG": (
        ["republic of the congo", "brazzaville", "congo-brazzaville"],
        ["congolese"],
    ),
    "NA": (
        ["namibia", "windhoek"],
        ["namibian"],
    ),
    "SZ": (
        ["eswatini", "swaziland", "mbabane"],
        ["swazi"],
    ),
    "LS": (
        ["lesotho", "maseru"],
        ["basotho", "lesothan"],
    ),
    "MW": (
        ["malawi", "lilongwe", "blantyre"],
        ["malawian"],
    ),
    "PG": (
        ["papua new guinea", "port moresby"],
        ["papuan"],
    ),
    "MN": (
        ["mongolia", "ulaanbaatar"],
        ["mongolian"],
    ),
    "BN": (
        ["brunei", "bandar seri begawan"],
        ["bruneian"],
    ),
    "TL": (
        ["timor-leste", "east timor", "dili"],
        ["timorese"],
    ),
    "MV": (
        ["maldives", "male"],
        ["maldivian"],
    ),
    "BT": (
        ["bhutan", "thimphu"],
        ["bhutanese"],
    ),
    "GE": (
        ["georgia", "tbilisi"],
        ["georgian"],
    ),
    "CY": (
        ["cyprus", "nicosia"],
        ["cypriot"],
    ),
    "MT": (
        ["malta", "valletta"],
        ["maltese"],
    ),
    "LU": (
        ["luxembourg"],
        ["luxembourgish"],
    ),
    "IS": (
        ["iceland", "reykjavik"],
        ["icelandic", "icelander"],
    ),
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


def _term_in_text(term: str, text: str) -> bool:
    """
    Whole-word / whole-phrase boundary check so that e.g. "iran" doesn't
    match inside "tirane" or "uk" doesn't match inside "truck".
    """
    pattern = r"(?<![a-z])" + re.escape(term) + r"(?![a-z])"
    return bool(re.search(pattern, text))


def article_mentions_country(title: str, summary: str, iso2: str) -> bool:
    """
    Returns True only when the article is genuinely about this country.

    Matching rules (in priority order):
    1. A STRONG term appears in the TITLE                     → MATCH
    2. A STRONG term appears in the SUMMARY **and** at least
       one strong or weak term also appears in the TITLE       → MATCH
    3. A WEAK term appears in the TITLE **and** a second
       strong term also appears anywhere in title+summary      → MATCH
    4. Anything else                                           → NO MATCH
    """
    if iso2 not in COUNTRY_TERMS:
        return False

    strong_terms, weak_terms = COUNTRY_TERMS[iso2]
    norm_title   = _norm(title)
    norm_summary = _norm(summary)

    # Rule 1: strong hit in title
    for t in strong_terms:
        if _term_in_text(t, norm_title):
            return True

    # Rule 2: strong hit in summary + corroboration in title
    summary_strong_hit = any(_term_in_text(t, norm_summary) for t in strong_terms)
    if summary_strong_hit:
        title_any_hit = (
            any(_term_in_text(t, norm_title) for t in strong_terms) or
            any(_term_in_text(t, norm_title) for t in weak_terms)
        )
        if title_any_hit:
            return True

    # Rule 3: weak hit in title + a strong term somewhere in the full text
    title_weak_hit = any(_term_in_text(t, norm_title) for t in weak_terms)
    if title_weak_hit:
        full_text = norm_title + " " + norm_summary
        if any(_term_in_text(t, full_text) for t in strong_terms):
            return True

    return False


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
    seen_urls: Set[str],
    seen_sigs: Set[str],
    n: int = HEADLINES_PER_COUNTRY,
) -> List[dict]:
    """
    Filter articles mentioning this country, deduplicate, rank, return top n.

    Ranking priority:
      1. Articles whose URL AND story-signature are both absent from the archive
         (genuinely new — never surfaced before in the 7-day window).
      2. Articles already in the archive but still within the 7-day window
         (shown only when there aren't enough fresh ones).
      Within each tier: higher importance score first, then more recent.
    """
    matching = [a for a in articles if article_mentions_country(a["title"], a["summary"], iso2)]

    local_urls: set = set()
    local_sigs: set = set()
    fresh: List[Tuple[float, dict]] = []
    repeat: List[Tuple[float, dict]] = []

    for a in matching:
        if a["url"] in local_urls:
            continue
        local_urls.add(a["url"])

        sig = story_signature(a["title"])
        if sig in local_sigs:
            continue
        local_sigs.add(sig)

        imp = importance_score(a["title"])
        recency = a["_ts"] / (7 * 86400)
        score = (imp * 1_000_000) + recency

        is_new = (a["url"] not in seen_urls) and (sig not in seen_sigs)
        if is_new:
            fresh.append((score, a))
        else:
            repeat.append((score, a))

    fresh.sort(key=lambda x: x[0], reverse=True)
    repeat.sort(key=lambda x: x[0], reverse=True)
    ranked = fresh + repeat

    out = []
    for _, a in ranked[:n]:
        out.append({
            "title": a["title"],
            "url": a["url"],
            "source": a["source"],
            "publishedAt": a["publishedAt"],
        })
    return out


# ─────────────────────────── ARCHIVE ──────────────────────────

def _archive_file(dt: datetime) -> Path:
    return ARCHIVE_DIR / f"{dt.strftime('%Y-%m-%d')}.jsonl"


def load_archive(now: datetime) -> tuple[Set[str], Set[str]]:
    """
    Read all archive .jsonl files within the retention window.
    Returns (seen_urls, seen_sigs) — sets of strings already surfaced.
    Deletes any archive files older than WINDOW_DAYS.
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    seen_urls: Set[str] = set()
    seen_sigs: Set[str] = set()

    for path in sorted(ARCHIVE_DIR.glob("*.jsonl")):
        # Parse date from filename
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if file_date < cutoff:
            path.unlink(missing_ok=True)
            print(f"  🗑  Pruned old archive: {path.name}")
            continue

        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                seen_urls.add(rec.get("url", ""))
                seen_sigs.add(rec.get("sig", ""))
        except Exception as e:
            print(f"  ⚠️  Could not read archive {path.name}: {e}")

    return seen_urls, seen_sigs


def append_to_archive(articles: List[dict], now: datetime) -> None:
    """
    Append newly surfaced article records to today's archive file.
    Each record: {url, sig, firstSeenAt}
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = _archive_file(now)
    ts = now.isoformat().replace("+00:00", "Z")
    lines = []
    for a in articles:
        rec = {
            "url": a["url"],
            "sig": story_signature(a["title"]),
            "firstSeenAt": ts,
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    if lines:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ─────────────────────────── MAIN ─────────────────────────────

def run_once() -> None:
    """Execute a single fetch-match-rank-write cycle."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)

    print(f"\n{'─'*60}")
    print(f"🕐 Run time : {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"📅 Window  : last {WINDOW_DAYS} days")
    print(f"🌍 Countries: {len(COUNTRIES)}  |  📰 Feeds: {len(RSS_FEEDS)}")
    print()

    # ── 1. Load archive ──────────────────────────────────────
    print("── Loading archive ──")
    seen_urls, seen_sigs = load_archive(now)
    print(f"  📦 Archive: {len(seen_urls)} known URLs, {len(seen_sigs)} known story signatures")
    print()

    # ── 2. Fetch feeds ───────────────────────────────────────
    print("── Fetching RSS feeds ──")
    all_articles = fetch_all_articles(cutoff)
    print(f"\n✓ Total articles in window: {len(all_articles)}")
    print()

    # ── 3. Match & rank ──────────────────────────────────────
    print("── Matching articles to countries ──")
    results: List[dict] = []
    last_updated = now.isoformat().replace("+00:00", "Z")
    all_surfaced: List[dict] = []

    for entry in COUNTRIES:
        iso2 = entry["iso2"]
        country_name = entry["country"]
        headlines = select_top_for_country(
            all_articles, iso2, seen_urls, seen_sigs
        )
        results.append({
            "country": country_name,
            "iso2": iso2,
            "headlines": headlines,
            "lastUpdated": last_updated,
        })

        fresh_count = sum(
            1 for h in headlines
            if h["url"] not in seen_urls and story_signature(h["title"]) not in seen_sigs
        )
        label = f"({fresh_count} new)" if fresh_count else ""
        print(f"  {country_name} ({iso2}): {len(headlines)} headline(s) {label}".rstrip())

        all_surfaced.extend(headlines)

    # ── 4. Persist output ────────────────────────────────────
    out_dir = Path("public")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "country_headlines.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── 5. Update archive with newly surfaced articles ───────
    new_articles = [
        a for a in all_surfaced
        if a["url"] not in seen_urls and story_signature(a["title"]) not in seen_sigs
    ]
    append_to_archive(new_articles, now)

    print(f"\n✅ Wrote {len(results)} countries → {out_path.resolve()}")
    print(f"📁 Archived {len(new_articles)} new article(s) → {_archive_file(now).name}")


def main() -> None:
    """
    Continuous loop: run once immediately, then every POLL_INTERVAL_HOURS hours.
    Pass --once as a CLI flag to run a single cycle and exit (useful for cron/CI).
    """
    single_shot = "--once" in sys.argv

    if single_shot:
        run_once()
        return

    print(f"🔄 Scheduler started — polling every {POLL_INTERVAL_HOURS}h")
    print("   (pass --once to run a single cycle and exit)\n")

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"\n❌ Run failed: {e}")

        next_run = datetime.now(timezone.utc) + timedelta(hours=POLL_INTERVAL_HOURS)
        print(f"\n⏭  Next run: {next_run.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"   Sleeping {POLL_INTERVAL_HOURS}h …")
        time.sleep(POLL_INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
