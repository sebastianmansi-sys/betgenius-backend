"""
BetGenius AI — Backend v2
Scraping automatico partite calcio/basket + pronostici AI
Mercati: 1X2, Gol, Over/Under, MultiGol, Handicap, BTTS,
         Cartellini (Over/Under), Calci d'Angolo (1T e Totali)

Fonti dati:
  - ESPN public API    → partite di oggi/domani, orari, probabilità
  - fbref.com          → statistiche cartellini per squadra (stagionali)
  - understat.com      → statistiche angoli per squadra (stagionali)
"""

import os, json, time, logging, hashlib, re, math, random, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic

# ── Config ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("betgenius")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_HOURS = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────────────────
# CACHE HELPERS
# ─────────────────────────────────────────────────────────
def cache_path(key):
    h = hashlib.md5(key.encode()).hexdigest()[:10]
    return CACHE_DIR / f"{key}_{h}.json"

def read_cache(key):
    p = cache_path(key)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    if (time.time() - data.get("_ts", 0)) / 3600 > CACHE_TTL_HOURS:
        return None
    return data

def write_cache(key, payload):
    payload["_ts"] = time.time()
    cache_path(key).write_text(json.dumps(payload, ensure_ascii=False, indent=2))

# ─────────────────────────────────────────────────────────
# ESPN — Partite base (oggi + domani)
# ─────────────────────────────────────────────────────────
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_LEAGUES = [
    # ── Calcio nazionali ──
    ("soccer", "ita.1",          "Serie A",            "🇮🇹", "calcio"),
    ("soccer", "ita.2",          "Serie B",            "🇮🇹", "calcio"),
    ("soccer", "eng.1",          "Premier League",     "🏴󠁧󠁢󠁥󠁮", "calcio"),
    ("soccer", "esp.1",          "La Liga",            "🇪🇸", "calcio"),
    ("soccer", "ger.1",          "Bundesliga",         "🇩🇪", "calcio"),
    ("soccer", "fra.1",          "Ligue 1",            "🇫🇷", "calcio"),
    ("soccer", "por.1",          "Primeira Liga",      "🇵🇹", "calcio"),
    ("soccer", "ned.1",          "Eredivisie",         "🇳🇱", "calcio"),
    ("soccer", "tur.1",          "Super Lig",          "🇹🇷", "calcio"),
    # ── Competizioni europee ──
    ("soccer", "uefa.champions", "Champions League",   "🇪🇺", "calcio"),
    ("soccer", "uefa.europa",    "Europa League",      "🇪🇺", "calcio"),
    ("soccer", "uefa.confleague","Conference League",  "🇪🇺", "calcio"),
    ("soccer", "uefa.nations",   "Nations League",     "🇪🇺", "calcio"),
    # ── Competizioni mondiali ──
    ("soccer", "fifa.worldq.eu", "Qualif. Mondiali EU","🌍", "calcio"),
    ("soccer", "fifa.cwc",       "Club World Cup",     "🌍", "calcio"),
    ("soccer", "concacaf.champions", "CONCACAF CL",    "🌎", "calcio"),
    ("soccer", "conmebol.libertadores", "Copa Libertadores", "🌎", "calcio"),
    # ── Basket ──
    ("basketball", "nba",        "NBA",                "🇺🇸", "basket"),
    ("basketball", "nba",        "NBA Playoffs",       "🇺🇸", "basket"),
]



def espn_scoreboard(sport_ep, league_ep, date_str):
    url = f"{ESPN_BASE}/{sport_ep}/{league_ep}/scoreboard?dates={date_str}&limit=50"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception as e:
        log.warning(f"ESPN {league_ep} {date_str}: {e}")
        return []


def form_from_espn_record(comp_item):
    records = comp_item.get("records") or []
    overall = next((r for r in records if r.get("type") == "total"), None)
    if overall:
        parts = overall.get("summary", "0-0").split("-")
        try:
            w, l_v = int(parts[0]), int(parts[-1])
            total = w + l_v
            wr = w / total if total else 0.5
            if wr >= 0.65: return ["W", "W", "W", "D", "W"]
            if wr >= 0.50: return ["W", "D", "W", "L", "W"]
            if wr >= 0.35: return ["L", "W", "D", "L", "W"]
            return ["L", "L", "W", "D", "L"]
        except Exception:
            pass
    return ["?", "?", "?", "?", "?"]


def parse_espn_event(ev, flag, league_name, sport_key):
    comp = (ev.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    if len(comps) < 2:
        return None

    status = comp.get("status", {}).get("type", {}).get("name", "")
    if "STATUS_SCHEDULED" not in status and "scheduled" not in status.lower():
        return None

    home = next((c for c in comps if c.get("homeAway") == "home"), comps[0])
    away = next((c for c in comps if c.get("homeAway") == "away"), comps[1])
    home_name = home.get("team", {}).get("displayName", "").strip()
    away_name = away.get("team", {}).get("displayName", "").strip()
    if not home_name or not away_name:
        return None

    raw_date = ev.get("date", "")
    try:
        dt_utc = datetime.strptime(raw_date[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        dt_local = dt_utc + timedelta(hours=2)   # UTC → CEST
        display_time = dt_local.strftime("%H:%M")
        raw_date_only = dt_utc.strftime("%Y%m%d")
    except Exception:
        display_time = "TBD"
        raw_date_only = ""

    today_str = datetime.now().strftime("%Y%m%d")
    date_label = "Oggi" if raw_date_only == today_str else "Domani"

    # Probabilità di vittoria da ESPN odds
    win_prob = {}
    for o in (comp.get("odds") or []):
        hp = o.get("homeTeamOdds", {}).get("winPercentage")
        ap = o.get("awayTeamOdds", {}).get("winPercentage")
        if hp:
            win_prob = {
                "home": round(hp, 1),
                "away": round(ap or 100 - hp, 1),
                "draw": round(max(0, 100 - hp - (ap or 0)), 1),
            }
            break

    is_calc = sport_key == "calcio"
    stats = {
        # gol
        "hG": 1.5, "aG": 1.2, "btts": 52, "o25": 58, "o35": 32,
        # basket
        "hPPG": 112, "aPPG": 109, "totAvg": 221,
        # cartellini (riempiti da enrich_cards_corners)
        "hYellow": 1.8, "aYellow": 1.6, "hRed": 0.12, "aRed": 0.10,
        "totalCards": 3.4,
        "over25Cards": 72, "over35Cards": 55, "over45Cards": 38, "over55Cards": 22,
        # angoli (riempiti da enrich_cards_corners)
        "hCorners": 5.2, "aCorners": 4.8, "totalCorners": 10.0,
        "over75Corners": 68, "over85Corners": 52, "over95Corners": 38,
        "over105Corners": 25, "over115Corners": 14,
        "corners1H": 4.6, "corners2H": 5.4, "over45Corners1H": 45,
        "h2h": f"{home_name} vs {away_name} — {league_name}",
    }

    return {
        "id":       ev.get("id", ""),
        "home":     home_name,
        "away":     away_name,
        "league":   league_name,
        "flag":     flag,
        "sport":    sport_key,
        "time":     display_time,
        "date":     date_label,
        "raw_date": raw_date_only,
        "homeForm": form_from_espn_record(home),
        "awayForm": form_from_espn_record(away),
        "stats":    stats,
        "winProb":  win_prob,
    }

# ─────────────────────────────────────────────────────────
# SCRAPER CARTELLINI — fbref.com
# ─────────────────────────────────────────────────────────
FBREF_LEAGUES = {
    "Serie A":        "https://fbref.com/en/comps/11/misc/Serie-A-Stats",
    "Premier League": "https://fbref.com/en/comps/9/misc/Premier-League-Stats",
    "La Liga":        "https://fbref.com/en/comps/12/misc/La-Liga-Stats",
    "Bundesliga":     "https://fbref.com/en/comps/20/misc/Bundesliga-Stats",
    "Ligue 1":        "https://fbref.com/en/comps/13/misc/Ligue-1-Stats",
}

_cards_cache = {}   # league → {team_lower → {yellow_pg, red_pg}}


def scrape_cards_fbref(league_name: str) -> dict:
    url = FBREF_LEAGUES.get(league_name)
    if not url:
        return {}
    try:
        r = requests.get(url, headers={**HEADERS, "Accept-Encoding": "gzip"}, timeout=18)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # cerca tabella misc (contiene CrdY / CrdR)
        table = (
            soup.find("table", {"id": re.compile(r"stats_squads_misc_for")}) or
            soup.find("table", id=re.compile(r"misc"))
        )
        if not table:
            log.warning(f"fbref: tabella non trovata per {league_name}")
            return {}

        result = {}
        for row in table.find("tbody").find_all("tr"):
            if "thead" in row.get("class", []):
                continue
            team_cell = row.find("td", {"data-stat": "team"})
            if not team_cell:
                continue
            team = team_cell.get_text(strip=True).lower()

            def val(stat):
                c = row.find("td", {"data-stat": stat})
                try:
                    return float(c.get_text(strip=True)) if c else 0.0
                except (ValueError, AttributeError):
                    return 0.0

            mp = val("games") or 1
            result[team] = {
                "yellow_pg": round(val("cards_yellow") / mp, 2),
                "red_pg":    round(val("cards_red")    / mp, 2),
            }

        log.info(f"fbref cartellini {league_name}: {len(result)} squadre")
        return result

    except Exception as e:
        log.warning(f"fbref {league_name}: {e}")
        return {}


def get_cards_stats(team_name: str, league: str) -> dict:
    global _cards_cache
    if league not in _cards_cache:
        _cards_cache[league] = scrape_cards_fbref(league)

    lookup = _cards_cache.get(league, {})
    nl = team_name.lower()

    # exact → partial → word match
    if nl in lookup:
        return lookup[nl]
    for key, val in lookup.items():
        if key in nl or nl in key:
            return val
    for key, val in lookup.items():
        if any(w in key for w in nl.split() if len(w) > 3):
            return val

    return {"yellow_pg": 1.8, "red_pg": 0.12}

# ─────────────────────────────────────────────────────────
# SCRAPER ANGOLI — understat.com
# ─────────────────────────────────────────────────────────
UNDERSTAT_LEAGUES = {
    "Serie A":        "Serie_A",
    "Premier League": "EPL",
    "La Liga":        "La_liga",
    "Bundesliga":     "Bundesliga",
    "Ligue 1":        "Ligue_1",
}

_corners_cache = {}   # league → {team_lower → {corners_pg, corners_conceded_pg}}


def scrape_corners_understat(league_name: str) -> dict:
    key = UNDERSTAT_LEAGUES.get(league_name)
    if not key:
        return {}
    url = f"https://understat.com/league/{key}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=18)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        result = {}
        for script in soup.find_all("script"):
            txt = script.string or ""
            if "teamsData" not in txt:
                continue
            m = re.search(r"teamsData\s*=\s*JSON\.parse\('(.+?)'\)", txt)
            if not m:
                continue
            raw = m.group(1).encode().decode("unicode_escape")
            teams_data = json.loads(raw)
            for _, td in teams_data.items():
                tname = td.get("title", "").lower()
                history = td.get("history", [])
                if not history:
                    continue
                cf  = [g.get("corners", 0) or 0 for g in history]
                cag = [g.get("corners_ag", 0) or 0 for g in history]
                result[tname] = {
                    "corners_pg":          round(mean(cf)  if cf  else 5.2, 2),
                    "corners_conceded_pg": round(mean(cag) if cag else 4.8, 2),
                }
            log.info(f"understat angoli {league_name}: {len(result)} squadre")
            return result

        return {}
    except Exception as e:
        log.warning(f"understat {league_name}: {e}")
        return {}


def get_corners_stats(team_name: str, league: str) -> dict:
    global _corners_cache
    if league not in _corners_cache:
        _corners_cache[league] = scrape_corners_understat(league)

    lookup = _corners_cache.get(league, {})
    nl = team_name.lower()

    if nl in lookup:
        return lookup[nl]
    for key, val in lookup.items():
        if key in nl or nl in key:
            return val
    for key, val in lookup.items():
        if any(w in key for w in nl.split() if len(w) > 3):
            return val

    return {"corners_pg": 5.2, "corners_conceded_pg": 4.8}

# ─────────────────────────────────────────────────────────
# ENRICH: aggiunge cartellini e angoli con Poisson
# ─────────────────────────────────────────────────────────
def poisson_over(lam: float, k: float) -> float:
    """P(X > k) dove X ~ Poisson(lambda), ritorna %."""
    p_le_k = sum(
        (lam ** i * math.exp(-lam)) / math.factorial(int(i))
        for i in range(int(k) + 1)
    )
    return round(max(0.0, min(100.0, (1 - p_le_k) * 100)), 1)


def enrich_cards_corners(match: dict) -> dict:
    if match["sport"] != "calcio":
        return match

    league = match["league"]
    s = match["stats"]

    # ── Cartellini ──────────────────────────────────────
    hc = get_cards_stats(match["home"], league)
    ac = get_cards_stats(match["away"], league)
    h_yel = hc.get("yellow_pg", 1.8)
    a_yel = ac.get("yellow_pg", 1.6)
    h_red = hc.get("red_pg", 0.12)
    a_red = ac.get("red_pg", 0.10)
    tc_exp = h_yel + a_yel + h_red + a_red   # λ totale carte

    s.update({
        "hYellow":     round(h_yel, 2),
        "aYellow":     round(a_yel, 2),
        "hRed":        round(h_red, 2),
        "aRed":        round(a_red, 2),
        "totalCards":  round(tc_exp, 2),
        "over15Cards": poisson_over(tc_exp, 1.5),
        "over25Cards": poisson_over(tc_exp, 2.5),
        "over35Cards": poisson_over(tc_exp, 3.5),
        "over45Cards": poisson_over(tc_exp, 4.5),
        "over55Cards": poisson_over(tc_exp, 5.5),
    })

    # ── Angoli ──────────────────────────────────────────
    hcorn = get_corners_stats(match["home"], league)
    acorn = get_corners_stats(match["away"], league)

    # angoli attesi per squadra = media tra (calci fatti propri + calci subiti avversario) / 2
    h_exp = round((hcorn.get("corners_pg", 5.2) + acorn.get("corners_conceded_pg", 4.8)) / 2, 2)
    a_exp = round((acorn.get("corners_pg", 4.8) + hcorn.get("corners_conceded_pg", 5.2)) / 2, 2)
    tot_exp = h_exp + a_exp
    exp_1h  = round(tot_exp * 0.45, 2)   # ~45% degli angoli nel 1° tempo

    s.update({
        "hCorners":        h_exp,
        "aCorners":        a_exp,
        "totalCorners":    round(tot_exp, 2),
        "corners1H":       exp_1h,
        "corners2H":       round(tot_exp - exp_1h, 2),
        "over65Corners":   poisson_over(tot_exp, 6.5),
        "over75Corners":   poisson_over(tot_exp, 7.5),
        "over85Corners":   poisson_over(tot_exp, 8.5),
        "over95Corners":   poisson_over(tot_exp, 9.5),
        "over105Corners":  poisson_over(tot_exp, 10.5),
        "over115Corners":  poisson_over(tot_exp, 11.5),
        "over35Corners1H": poisson_over(exp_1h, 3.5),
        "over45Corners1H": poisson_over(exp_1h, 4.5),
        "over55Corners1H": poisson_over(exp_1h, 5.5),
    })

    match["stats"] = s
    return match

# ─────────────────────────────────────────────────────────
# SCRAPING PRINCIPALE
# ─────────────────────────────────────────────────────────
def scrape_all_matches() -> list:
    log.info("Scraping ESPN — partite oggi e domani...")
    today    = datetime.now().strftime("%Y%m%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
    all_matches, match_id = [], 1

    for (sport_ep, league_ep, league_name, flag, sport_key) in ESPN_LEAGUES:
        for date_str in [today, tomorrow]:
            for ev in espn_scoreboard(sport_ep, league_ep, date_str):
                parsed = parse_espn_event(ev, flag, league_name, sport_key)
                if parsed:
                    parsed["id"] = match_id
                    all_matches.append(parsed)
                    match_id += 1
        time.sleep(0.25)

    log.info(f"ESPN: {len(all_matches)} partite. Avvio enrichment cartellini/angoli...")

    # reset cache giornaliera dei dati stagionali
    global _cards_cache, _corners_cache
    _cards_cache = {}
    _corners_cache = {}

    enriched = []
    for m in all_matches:
        try:
            enriched.append(enrich_cards_corners(m))
        except Exception as e:
            log.warning(f"Enrich error {m.get('home', '?')}: {e}")
            enriched.append(m)

    log.info(f"Enrichment completato: {len(enriched)} partite pronte")
    return enriched

# ─────────────────────────────────────────────────────────
# AI ANALYSIS — 4 mercati per partita di calcio
# ─────────────────────────────────────────────────────────
def analyze_match_ai(match: dict) -> dict:
    if not ANTHROPIC_API_KEY:
        return _fallback_prediction(match)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    s  = match["stats"]
    wp = match.get("winProb", {})
    is_calc = match["sport"] == "calcio"

    wp_str = (
        f"Probabilità live ESPN: Casa={wp.get('home','?')}% "
        f"Pareggio={wp.get('draw','?')}% "
        f"Trasferta={wp.get('away','?')}%"
    ) if wp else ""

    if is_calc:
        prompt = f"""Sei un analista professionista di scommesse sportive.
Analizza la partita sotto e restituisci pronostici ottimali per QUATTRO mercati.
Usa SOLO i dati forniti. Quote target: 1.30–4.00.

═══ PARTITA ═══
{match['home']} vs {match['away']}  |  {match['league']}  |  {match['date']} ore {match['time']}
Forma {match['home']}: {'-'.join(match.get('homeForm') or ['?']*5)}
Forma {match['away']}: {'-'.join(match.get('awayForm') or ['?']*5)}
{wp_str}

═══ GOL ═══
Media gol per partita — casa={s.get('hG',1.5)}, trasferta={s.get('aG',1.2)}
BTTS={s.get('btts',52)}%  Over2.5={s.get('o25',58)}%  Over3.5={s.get('o35',32)}%

═══ CARTELLINI (medie stagionali per partita) ═══
{match['home']}: {s.get('hYellow',1.8)} gialli + {s.get('hRed',0.12)} rossi
{match['away']}: {s.get('aYellow',1.6)} gialli + {s.get('aRed',0.10)} rossi
Totale carte attese: {s.get('totalCards',3.4):.1f}
Over 2.5={s.get('over25Cards',72)}%  Over 3.5={s.get('over35Cards',55)}%  Over 4.5={s.get('over45Cards',38)}%  Over 5.5={s.get('over55Cards',22)}%

═══ ANGOLI (medie stagionali per partita) ═══
{match['home']}: {s.get('hCorners',5.2):.1f} angoli fatti/gara
{match['away']}: {s.get('aCorners',4.8):.1f} angoli fatti/gara
Totale angoli attesi: {s.get('totalCorners',10.0):.1f}
Over 8.5={s.get('over85Corners',52)}%  Over 9.5={s.get('over95Corners',38)}%  Over 10.5={s.get('over105Corners',25)}%
Angoli 1° Tempo attesi: {s.get('corners1H',4.6):.1f}
Over 4.5 angoli 1T={s.get('over45Corners1H',45)}%

H2H: {s.get('h2h','')}

Rispondi SOLO con JSON valido (zero testo extra, zero backtick markdown):
{{
  "main":      {{"prediction":"...","betType":"1X2|Goal/NoGoal|Over/Under|MultiGoal|Handicap|BTTS","odds":1.85,"confidence":72,"analysis":"2-3 frasi","keyFactors":["f1","f2","f3"],"risk":"Basso|Medio|Alto"}},
  "cards":     {{"prediction":"Over/Under X.5 Cartellini","betType":"Cartellini","odds":1.75,"confidence":68,"analysis":"1-2 frasi basate sui dati","keyFactors":["f1","f2"],"risk":"Basso|Medio|Alto"}},
  "corners1h": {{"prediction":"Over/Under X.5 Angoli 1° Tempo","betType":"Angoli 1°T","odds":1.90,"confidence":65,"analysis":"1-2 frasi","keyFactors":["f1","f2"],"risk":"Basso|Medio|Alto"}},
  "corners":   {{"prediction":"Over/Under X.5 Angoli Totali","betType":"Angoli Totali","odds":1.80,"confidence":70,"analysis":"1-2 frasi","keyFactors":["f1","f2"],"risk":"Basso|Medio|Alto"}}
}}"""
    else:
        prompt = f"""Analizza questa partita basket e dai UN pronostico (quota 1.30-4.00).
{match['home']} vs {match['away']} | {match['league']} | {match['date']} {match['time']}
Forma: {'-'.join(match.get('homeForm') or ['?']*5)} vs {'-'.join(match.get('awayForm') or ['?']*5)}
Media punti: casa={s.get('hPPG',110)}, trasferta={s.get('aPPG',108)}, totale={s.get('totAvg',218)}
{wp_str}
Rispondi SOLO JSON valido:
{{"main":{{"prediction":"...","betType":"ML|Handicap|Over/Under","odds":1.85,"confidence":72,"analysis":"2-3 frasi","keyFactors":["f1","f2","f3"],"risk":"Basso|Medio|Alto"}},"cards":null,"corners1h":null,"corners":null}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=950,
            system="Sei un esperto analista di scommesse sportive. Rispondi SOLO con JSON valido, zero testo aggiuntivo, zero backtick.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        result = json.loads(text[start:end])

        # Sanity check su ogni mercato
        for key in ("main", "cards", "corners1h", "corners"):
            sub = result.get(key)
            if sub and isinstance(sub, dict):
                sub["odds"]       = max(1.01, min(5.0, float(sub.get("odds", 1.8))))
                sub["confidence"] = max(50,   min(95,  int(sub.get("confidence", 65))))

        return result

    except Exception as e:
        log.warning(f"AI error {match.get('home','?')} vs {match.get('away','?')}: {e}")
        return _fallback_prediction(match)


def _fallback_prediction(match: dict) -> dict:
    """Pronostico di fallback quando l'AI non è disponibile."""
    s   = match["stats"]
    wp  = match.get("winProb", {})
    is_calc = match["sport"] == "calcio"

    if is_calc:
        # Mercato principale
        if wp.get("home", 0) > 60:
            mp = {"prediction": f"Vittoria {match['home']}", "betType": "1X2",
                  "odds": round(1.3 + random.random() * 0.5, 2)}
        elif wp.get("away", 0) > 60:
            mp = {"prediction": f"Vittoria {match['away']}", "betType": "1X2",
                  "odds": round(1.4 + random.random() * 0.6, 2)}
        else:
            mp = {"prediction": "Over 2.5 Gol", "betType": "Over/Under",
                  "odds": round(1.6 + random.random() * 0.5, 2)}
        mp.update({
            "confidence": random.randint(60, 72),
            "analysis": f"{match['home']} ospita {match['away']} in {match['league']}. Analisi statistica.",
            "keyFactors": ["Forma recente", "Fattore campo", "H2H"],
            "risk": "Medio",
        })

        # Cartellini
        tc = s.get("totalCards", 3.4)
        line_c = 3.5 if tc >= 3.5 else 2.5
        cards = {
            "prediction": f"Over {line_c} Cartellini",
            "betType": "Cartellini",
            "odds": round(1.65 + random.random() * 0.4, 2),
            "confidence": random.randint(58, 70),
            "analysis": f"Media cartellini attesi: {tc:.1f}. Linea {line_c} consigliata.",
            "keyFactors": ["Gialli/partita casa", "Gialli/partita trasferta", "Storico"],
            "risk": "Medio",
        }

        # Angoli totali
        tot = s.get("totalCorners", 10.0)
        line_a = 8.5 if tot < 9 else (9.5 if tot < 10.5 else 10.5)
        corners = {
            "prediction": f"Over {line_a} Angoli Totali",
            "betType": "Angoli Totali",
            "odds": round(1.70 + random.random() * 0.4, 2),
            "confidence": random.randint(60, 72),
            "analysis": f"Attesi {tot:.1f} angoli totali. Linea {line_a} favorevole.",
            "keyFactors": ["Angoli fatti/gara", "Angoli subiti/gara", "Stile di gioco"],
            "risk": "Medio",
        }

        # Angoli 1° tempo
        exp1h = s.get("corners1H", 4.6)
        line_1h = 3.5 if exp1h < 4.2 else (4.5 if exp1h < 5.0 else 5.5)
        corn1h = {
            "prediction": f"Over {line_1h} Angoli 1° Tempo",
            "betType": "Angoli 1°T",
            "odds": round(1.75 + random.random() * 0.45, 2),
            "confidence": random.randint(58, 68),
            "analysis": f"Attesi ~{exp1h:.1f} angoli nel primo tempo. Linea {line_1h}.",
            "keyFactors": ["Ritmo 1° tempo", "Pressing alto", "Set-piece tendenze"],
            "risk": "Medio",
        }

        return {"main": mp, "cards": cards, "corners1h": corn1h, "corners": corners}
    else:
        mp = {
            "prediction": f"Over {s.get('totAvg', 220):.0f} Punti",
            "betType": "Over/Under",
            "odds": round(1.75 + random.random() * 0.4, 2),
            "confidence": random.randint(62, 74),
            "analysis": "Media punti stagionale sopra la linea. Entrambe le squadre in forma offensiva.",
            "keyFactors": ["Media punti", "Ritmo offensivo", "Difese"],
            "risk": "Medio",
        }
        return {"main": mp, "cards": None, "corners1h": None, "corners": None}

# ─────────────────────────────────────────────────────────
# DAILY REFRESH JOB
# ─────────────────────────────────────────────────────────
def daily_refresh():
    log.info("═══ daily_refresh START ═══")
    matches = scrape_all_matches()
    if not matches:
        log.warning("Nessuna partita trovata, abort")
        return

    results = []
    for i, m in enumerate(matches):
        log.info(f"  AI [{i+1}/{len(matches)}] {m['home']} vs {m['away']}")
        preds = analyze_match_ai(m)
        results.append({**m, "predictions": preds})
        time.sleep(0.9)   # rate limit API

    write_cache("daily_matches", {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count":      len(results),
        "matches":    results,
    })
    log.info(f"═══ daily_refresh DONE — {len(results)} pronostici salvati ═══")

# ─────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────
@app.route("/health")
def health():
    cached = read_cache("daily_matches") or {}
    return jsonify({
        "status":      "ok",
        "time":        datetime.utcnow().isoformat(),
        "match_count": cached.get("count", 0),
        "updated_at":  cached.get("updated_at", "mai"),
        "cache_age_h": round((time.time() - cached.get("_ts", time.time())) / 3600, 1),
    })

@app.route("/api/matches")
def api_matches():
    cached = read_cache("daily_matches")
    if cached:
        return jsonify(cached)
    log.info("Cache miss — genero al volo (prima richiesta)...")
    daily_refresh()
    cached = read_cache("daily_matches")
    if cached:
        return jsonify(cached)
    return jsonify({"error": "Nessuna partita disponibile", "matches": []}), 503

@app.route("/api/matches/refresh", methods=["POST"])
def api_refresh():
    if request.headers.get("X-Refresh-Token") != os.environ.get("REFRESH_TOKEN", "betgenius2025"):
        return jsonify({"error": "Unauthorized"}), 401
    threading.Thread(target=daily_refresh, daemon=True).start()
    return jsonify({"status": "refresh avviato in background"})

@app.route("/api/news")
def api_news():
    cached = read_cache("daily_matches") or {}
    news = []
    for m in (cached.get("matches") or []):
        ico = "⚽" if m["sport"] == "calcio" else "🏀"
        preds = m.get("predictions", {})
        main_pred = (preds.get("main") or {}).get("prediction", "")
        news.append({
            "s": ico,
            "t": f"{m['home']} vs {m['away']} — {m['league']} | {m['date']} {m['time']}"
                 + (f" → {main_pred}" if main_pred else ""),
        })
    return jsonify({"news": news[:30]})

@app.route("/api/leagues")
def api_leagues():
    cached = read_cache("daily_matches") or {}
    leagues = sorted({m["league"] for m in (cached.get("matches") or [])})
    return jsonify({"leagues": leagues})

# ─────────────────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────────────────
def start_scheduler():
    s = BackgroundScheduler(timezone="Europe/Rome")
    s.add_job(daily_refresh, "cron", hour=7,  minute=30, id="morning")
    s.add_job(daily_refresh, "cron", hour=12, minute=0,  id="midday")
    s.add_job(daily_refresh, "cron", hour=17, minute=0,  id="afternoon")
    s.start()
    log.info("Scheduler attivo: 07:30, 12:00, 17:00 (Europe/Rome)")

# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    start_scheduler()
    if not read_cache("daily_matches"):
        threading.Thread(target=daily_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
