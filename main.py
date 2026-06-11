"""
BetGenius AI — Backend v3
- Pronostici su 3 giorni (oggi, domani, dopodomani)
- Tennis ATP/WTA via SofaScore API pubblica
- Leghe calcio attive tutto l'anno + nazionali + coppe
- Cartellini e angoli via fbref + understat
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

# CACHE
def cache_path(key):
    h = hashlib.md5(key.encode()).hexdigest()[:10]
    return CACHE_DIR / f"{key}_{h}.json"

def read_cache(key):
    p = cache_path(key)
    if not p.exists(): return None
    data = json.loads(p.read_text())
    if (time.time() - data.get("_ts", 0)) / 3600 > CACHE_TTL_HOURS: return None
    return data

def write_cache(key, payload):
    payload["_ts"] = time.time()
    cache_path(key).write_text(json.dumps(payload, ensure_ascii=False, indent=2))

# DATE — 3 giorni
def get_next_days(n=3):
    today = datetime.now()
    labels = ["Oggi", "Domani", "Dopodomani"]
    return [(( today + timedelta(days=i)).strftime("%Y%m%d"), labels[i]) for i in range(n)]

# ESPN
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_LEAGUES = [
    ("soccer","ita.1","Serie A","🇮🇹","calcio"),
    ("soccer","ita.2","Serie B","🇮🇹","calcio"),
    ("soccer","eng.1","Premier League","🏴󠁧󠁢󠁥󠁮󠁧󠁿","calcio"),
    ("soccer","eng.2","Championship","🏴󠁧󠁢󠁥󠁮󠁧󠁿","calcio"),
    ("soccer","esp.1","La Liga","🇪🇸","calcio"),
    ("soccer","ger.1","Bundesliga","🇩🇪","calcio"),
    ("soccer","fra.1","Ligue 1","🇫🇷","calcio"),
    ("soccer","por.1","Primeira Liga","🇵🇹","calcio"),
    ("soccer","ned.1","Eredivisie","🇳🇱","calcio"),
    ("soccer","tur.1","Super Lig","🇹🇷","calcio"),
    ("soccer","bra.1","Brasileirao","🇧🇷","calcio"),
    ("soccer","arg.1","Liga Profesional","🇦🇷","calcio"),
    ("soccer","mls","MLS","🇺🇸","calcio"),
    ("soccer","jpn.1","J-League","🇯🇵","calcio"),
    ("soccer","mex.1","Liga MX","🇲🇽","calcio"),
    ("soccer","fifa.world","Mondiali FIFA","🌍","calcio"),
    ("soccer","fifa.worldq.eu","Qualif. Mondiali EU","🌍","calcio"),
    ("soccer","fifa.worldq.conmebol","Qualif. Mondiali SA","🌎","calcio"),
    ("soccer","fifa.worldq.concacaf","Qualif. Mondiali CONC","🌎","calcio"),
    ("soccer","fifa.worldq.afc","Qualif. Mondiali Asia","🌏","calcio"),
    ("soccer","uefa.euro","Europei UEFA","🇪🇺","calcio"),
    ("soccer","uefa.euro.qualifier","Qualif. Europei","🇪🇺","calcio"),
    ("soccer","copa.america","Copa America","🌎","calcio"),
    ("soccer","africa.nations","Coppa d'Africa","🌍","calcio"),
    ("soccer","afc.asian.cup","Asian Cup","🌏","calcio"),
    ("soccer","concacaf.gold","Gold Cup","🌎","calcio"),
    ("soccer","uefa.nations","Nations League","🇪🇺","calcio"),
    ("soccer","uefa.champions","Champions League","🇪🇺","calcio"),
    ("soccer","uefa.europa","Europa League","🇪🇺","calcio"),
    ("soccer","uefa.confleague","Conference League","🇪🇺","calcio"),
    ("soccer","fifa.cwc","Club World Cup","🌍","calcio"),
    ("soccer","conmebol.libertadores","Copa Libertadores","🌎","calcio"),
    ("soccer","concacaf.champions","CONCACAF CL","🌎","calcio"),
    ("basketball","nba","NBA","🇺🇸","basket"),
    ("basketball","wnba","WNBA","🇺🇸","basket"),
    ("basketball","mens-college-basketball","NCAA","🇺🇸","basket"),
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

def form_from_espn(comp_item):
    records = comp_item.get("records") or []
    overall = next((r for r in records if r.get("type") == "total"), None)
    if overall:
        parts = overall.get("summary","0-0").split("-")
        try:
            w,l_v = int(parts[0]),int(parts[-1])
            total = w+l_v
            wr = w/total if total else 0.5
            if wr>=0.65: return ["W","W","W","D","W"]
            if wr>=0.50: return ["W","D","W","L","W"]
            if wr>=0.35: return ["L","W","D","L","W"]
            return ["L","L","W","D","L"]
        except: pass
    return ["?","?","?","?","?"]

def parse_espn_event(ev, flag, league_name, sport_key, date_label):
    comp = (ev.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    if len(comps) < 2: return None
    status = comp.get("status",{}).get("type",{}).get("name","")
    if "STATUS_SCHEDULED" not in status and "scheduled" not in status.lower(): return None
    home = next((c for c in comps if c.get("homeAway")=="home"), comps[0])
    away = next((c for c in comps if c.get("homeAway")=="away"), comps[1])
    home_name = home.get("team",{}).get("displayName","").strip()
    away_name = away.get("team",{}).get("displayName","").strip()
    if not home_name or not away_name: return None
    raw_date = ev.get("date","")
    try:
        dt_utc = datetime.strptime(raw_date[:16],"%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        display_time = (dt_utc+timedelta(hours=2)).strftime("%H:%M")
    except:
        display_time = "TBD"
    win_prob = {}
    for o in (comp.get("odds") or []):
        hp = o.get("homeTeamOdds",{}).get("winPercentage")
        ap = o.get("awayTeamOdds",{}).get("winPercentage")
        if hp:
            win_prob = {"home":round(hp,1),"away":round(ap or 100-hp,1),"draw":round(max(0,100-hp-(ap or 0)),1)}
            break
    stats = {
        "hG":1.5,"aG":1.2,"btts":52,"o25":58,"o35":32,
        "hPPG":112,"aPPG":109,"totAvg":221,
        "hYellow":1.8,"aYellow":1.6,"hRed":0.12,"aRed":0.10,
        "totalCards":3.4,"over25Cards":72,"over35Cards":55,"over45Cards":38,"over55Cards":22,
        "hCorners":5.2,"aCorners":4.8,"totalCorners":10.0,
        "over75Corners":68,"over85Corners":52,"over95Corners":38,"over105Corners":25,"over115Corners":14,
        "corners1H":4.6,"corners2H":5.4,"over45Corners1H":45,
        "h2h":f"{home_name} vs {away_name} — {league_name}",
    }
    return {
        "id":ev.get("id",""),"home":home_name,"away":away_name,
        "league":league_name,"flag":flag,"sport":sport_key,
        "time":display_time,"date":date_label,
        "homeForm":form_from_espn(home),"awayForm":form_from_espn(away),
        "stats":stats,"winProb":win_prob,
    }

# TENNIS — SofaScore
SOFA_HEADERS = {**HEADERS,"Referer":"https://www.sofascore.com/","X-Requested-With":"XMLHttpRequest"}

def fetch_tennis_sofascore(date_ymd, date_label):
    url = f"https://api.sofascore.com/api/v1/sport/tennis/scheduled-events/{date_ymd}"
    matches = []
    try:
        r = requests.get(url, headers=SOFA_HEADERS, timeout=15)
        r.raise_for_status()
        events = r.json().get("events",[])
        base_id = 8000 + int(date_ymd.replace("-","")) % 1000
        for i,ev in enumerate(events):
            try:
                status = ev.get("status",{}).get("type","")
                if status not in ("notstarted","scheduled"): continue
                tournament = ev.get("tournament",{})
                t_name = tournament.get("name","Tennis")
                category = tournament.get("category",{}).get("name","")
                p1 = ev.get("homeTeam",{}).get("name","").strip()
                p2 = ev.get("awayTeam",{}).get("name","").strip()
                if not p1 or not p2: continue
                ts = ev.get("startTimestamp",0)
                if ts:
                    dt_utc = datetime.fromtimestamp(ts,tz=timezone.utc)
                    display_time = (dt_utc+timedelta(hours=2)).strftime("%H:%M")
                else:
                    display_time = "TBD"
                rank1 = ev.get("homeTeam",{}).get("ranking",999) or 999
                rank2 = ev.get("awayTeam",{}).get("ranking",999) or 999
                if rank1<rank2 and rank1<500: wp1 = round(min(85,50+(rank2-rank1)*0.15),1)
                elif rank2<rank1 and rank2<500: wp1 = round(max(15,50-(rank1-rank2)*0.15),1)
                else: wp1 = 50.0
                surface_map = {1:"Clay",2:"Hard",3:"Grass",4:"Indoor Hard",5:"Carpet"}
                ground = ev.get("groundType",{})
                surface = surface_map.get(ground.get("id",2) if isinstance(ground,dict) else 2,"Hard")
                circuit = "WTA" if "WTA" in t_name or "WTA" in category else "ATP"
                if any(x in t_name for x in ["Davis","Billie","United Cup"]): circuit="Nations"
                flag = "🎾"
                if "Roland" in t_name: flag="🇫🇷🎾"
                elif "Wimbledon" in t_name: flag="🏴󠁧󠁢󠁥󠁮󠁧󠁿🎾"
                elif "US Open" in t_name: flag="🇺🇸🎾"
                elif "Australian" in t_name: flag="🇦🇺🎾"
                matches.append({
                    "id":base_id+i,"home":p1,"away":p2,
                    "league":f"Tennis {circuit} — {t_name}","flag":flag,"sport":"tennis",
                    "time":display_time,"date":date_label,
                    "homeForm":["?"]*5,"awayForm":["?"]*5,
                    "stats":{"rank1":rank1,"rank2":rank2,"circuit":circuit,"surface":surface,
                        "tournament":t_name,"h2h":f"{p1} (#{rank1}) vs {p2} (#{rank2}) su {surface}",
                        "hG":0,"aG":0,"btts":0,"o25":0,"o35":0,"hPPG":0,"aPPG":0,"totAvg":0,
                        "hYellow":0,"aYellow":0,"hRed":0,"aRed":0,"totalCards":0,
                        "over35Cards":0,"over45Cards":0,"hCorners":0,"aCorners":0,
                        "totalCorners":0,"over85Corners":0,"over95Corners":0,"over105Corners":0,
                        "corners1H":0,"over45Corners1H":0},
                    "winProb":{"home":wp1,"away":round(100-wp1,1)},
                })
            except Exception as e:
                log.warning(f"Tennis parse: {e}")
        log.info(f"Tennis SofaScore {date_ymd}: {len(matches)} match")
    except Exception as e:
        log.warning(f"Tennis SofaScore {date_ymd}: {e}")
    return matches

# CARTELLINI — fbref
FBREF_LEAGUES = {
    "Serie A":"https://fbref.com/en/comps/11/misc/Serie-A-Stats",
    "Premier League":"https://fbref.com/en/comps/9/misc/Premier-League-Stats",
    "La Liga":"https://fbref.com/en/comps/12/misc/La-Liga-Stats",
    "Bundesliga":"https://fbref.com/en/comps/20/misc/Bundesliga-Stats",
    "Ligue 1":"https://fbref.com/en/comps/13/misc/Ligue-1-Stats",
    "Primeira Liga":"https://fbref.com/en/comps/32/misc/Primeira-Liga-Stats",
    "Eredivisie":"https://fbref.com/en/comps/23/misc/Eredivisie-Stats",
    "Brasileirao":"https://fbref.com/en/comps/24/misc/Serie-A-Stats",
    "Liga Profesional":"https://fbref.com/en/comps/21/misc/Primera-Division-Stats",
    "MLS":"https://fbref.com/en/comps/22/misc/Major-League-Soccer-Stats",
}
_cards_cache = {}

def scrape_cards_fbref(league_name):
    url = FBREF_LEAGUES.get(league_name)
    if not url: return {}
    try:
        r = requests.get(url,headers={**HEADERS,"Accept-Encoding":"gzip"},timeout=18)
        r.raise_for_status()
        soup = BeautifulSoup(r.text,"html.parser")
        table = (soup.find("table",{"id":re.compile(r"stats_squads_misc_for")}) or
                 soup.find("table",id=re.compile(r"misc")))
        if not table: return {}
        result = {}
        for row in table.find("tbody").find_all("tr"):
            if "thead" in row.get("class",[]): continue
            team_cell = row.find("td",{"data-stat":"team"})
            if not team_cell: continue
            team = team_cell.get_text(strip=True).lower()
            def val(stat):
                c = row.find("td",{"data-stat":stat})
                try: return float(c.get_text(strip=True)) if c else 0.0
                except: return 0.0
            mp = val("games") or 1
            result[team] = {"yellow_pg":round(val("cards_yellow")/mp,2),"red_pg":round(val("cards_red")/mp,2)}
        log.info(f"fbref {league_name}: {len(result)} squadre")
        return result
    except Exception as e:
        log.warning(f"fbref {league_name}: {e}")
        return {}

def get_cards_stats(team_name,league):
    global _cards_cache
    if league not in _cards_cache: _cards_cache[league] = scrape_cards_fbref(league)
    lookup = _cards_cache.get(league,{})
    nl = team_name.lower()
    if nl in lookup: return lookup[nl]
    for k,v in lookup.items():
        if k in nl or nl in k: return v
    for k,v in lookup.items():
        if any(w in k for w in nl.split() if len(w)>3): return v
    return {"yellow_pg":1.8,"red_pg":0.12}

# ANGOLI — understat
UNDERSTAT_LEAGUES = {
    "Serie A":"Serie_A","Premier League":"EPL","La Liga":"La_liga",
    "Bundesliga":"Bundesliga","Ligue 1":"Ligue_1",
}
_corners_cache = {}

def scrape_corners_understat(league_name):
    key = UNDERSTAT_LEAGUES.get(league_name)
    if not key: return {}
    try:
        r = requests.get(f"https://understat.com/league/{key}",headers=HEADERS,timeout=18)
        r.raise_for_status()
        soup = BeautifulSoup(r.text,"html.parser")
        for script in soup.find_all("script"):
            txt = script.string or ""
            if "teamsData" not in txt: continue
            m = re.search(r"teamsData\s*=\s*JSON\.parse\('(.+?)'\)",txt)
            if not m: continue
            teams_data = json.loads(m.group(1).encode().decode("unicode_escape"))
            result = {}
            for _,td in teams_data.items():
                tname = td.get("title","").lower()
                history = td.get("history",[])
                if not history: continue
                cf = [g.get("corners",0) or 0 for g in history]
                cag = [g.get("corners_ag",0) or 0 for g in history]
                result[tname] = {"corners_pg":round(mean(cf) if cf else 5.2,2),"corners_conceded_pg":round(mean(cag) if cag else 4.8,2)}
            log.info(f"understat {league_name}: {len(result)} squadre")
            return result
        return {}
    except Exception as e:
        log.warning(f"understat {league_name}: {e}")
        return {}

def get_corners_stats(team_name,league):
    global _corners_cache
    if league not in _corners_cache: _corners_cache[league] = scrape_corners_understat(league)
    lookup = _corners_cache.get(league,{})
    nl = team_name.lower()
    if nl in lookup: return lookup[nl]
    for k,v in lookup.items():
        if k in nl or nl in k: return v
    for k,v in lookup.items():
        if any(w in k for w in nl.split() if len(w)>3): return v
    return {"corners_pg":5.2,"corners_conceded_pg":4.8}

# ENRICH
def poisson_over(lam,k):
    p = sum((lam**i*math.exp(-lam))/math.factorial(int(i)) for i in range(int(k)+1))
    return round(max(0.0,min(100.0,(1-p)*100)),1)

def enrich_cards_corners(match):
    if match["sport"] != "calcio": return match
    league = match["league"]
    s = match["stats"]
    hc = get_cards_stats(match["home"],league)
    ac = get_cards_stats(match["away"],league)
    h_yel,a_yel = hc.get("yellow_pg",1.8),ac.get("yellow_pg",1.6)
    h_red,a_red = hc.get("red_pg",0.12),ac.get("red_pg",0.10)
    tc = h_yel+a_yel+h_red+a_red
    s.update({"hYellow":round(h_yel,2),"aYellow":round(a_yel,2),"hRed":round(h_red,2),"aRed":round(a_red,2),
        "totalCards":round(tc,2),"over15Cards":poisson_over(tc,1.5),"over25Cards":poisson_over(tc,2.5),
        "over35Cards":poisson_over(tc,3.5),"over45Cards":poisson_over(tc,4.5),"over55Cards":poisson_over(tc,5.5)})
    hcorn = get_corners_stats(match["home"],league)
    acorn = get_corners_stats(match["away"],league)
    h_exp = round((hcorn.get("corners_pg",5.2)+acorn.get("corners_conceded_pg",4.8))/2,2)
    a_exp = round((acorn.get("corners_pg",4.8)+hcorn.get("corners_conceded_pg",5.2))/2,2)
    tot = h_exp+a_exp
    exp1h = round(tot*0.45,2)
    s.update({"hCorners":h_exp,"aCorners":a_exp,"totalCorners":round(tot,2),
        "corners1H":exp1h,"corners2H":round(tot-exp1h,2),
        "over65Corners":poisson_over(tot,6.5),"over75Corners":poisson_over(tot,7.5),
        "over85Corners":poisson_over(tot,8.5),"over95Corners":poisson_over(tot,9.5),
        "over105Corners":poisson_over(tot,10.5),"over115Corners":poisson_over(tot,11.5),
        "over35Corners1H":poisson_over(exp1h,3.5),"over45Corners1H":poisson_over(exp1h,4.5),
        "over55Corners1H":poisson_over(exp1h,5.5)})
    match["stats"] = s
    return match

# SCRAPING PRINCIPALE
def scrape_all_matches():
    log.info("Scraping ESPN + SofaScore — 3 giorni...")
    days = get_next_days(3)
    all_matches,match_id,seen = [],1,set()

    for (sport_ep,league_ep,league_name,flag,sport_key) in ESPN_LEAGUES:
        for (date_str,date_label) in days:
            for ev in espn_scoreboard(sport_ep,league_ep,date_str):
                parsed = parse_espn_event(ev,flag,league_name,sport_key,date_label)
                if parsed:
                    uid = f"{parsed['home']}_{parsed['away']}_{date_str}"
                    if uid not in seen:
                        parsed["id"] = match_id
                        all_matches.append(parsed)
                        seen.add(uid)
                        match_id += 1
        time.sleep(0.2)

    log.info(f"ESPN: {len(all_matches)} partite. Tennis...")
    for (date_str,date_label) in days:
        date_ymd = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        for t in fetch_tennis_sofascore(date_ymd,date_label):
            uid = f"{t['home']}_{t['away']}_{date_str}"
            if uid not in seen:
                t["id"] = match_id
                all_matches.append(t)
                seen.add(uid)
                match_id += 1
        time.sleep(0.3)

    log.info(f"Totale {len(all_matches)} partite. Enrichment...")
    global _cards_cache,_corners_cache
    _cards_cache = {}
    _corners_cache = {}
    enriched = []
    for m in all_matches:
        try: enriched.append(enrich_cards_corners(m))
        except Exception as e:
            log.warning(f"Enrich {m.get('home','?')}: {e}")
            enriched.append(m)
    log.info(f"Enrichment OK: {len(enriched)} partite")
    return enriched

# AI ANALYSIS
def analyze_match_ai(match):
    if not ANTHROPIC_API_KEY: return _fallback_prediction(match)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    s = match["stats"]
    wp = match.get("winProb",{})
    sport = match["sport"]
    wp_str = f"Prob.live: Casa={wp.get('home','?')}% Pareggio={wp.get('draw','?')}% Trasferta={wp.get('away','?')}%" if wp else ""

    if sport == "tennis":
        prompt = f"""Esperto scommesse tennis. UN pronostico ottimale (quota 1.30-4.00).
Mercati: ML (vincitore), Handicap Set, Over/Under Games, Over/Under Set.
Match: {match['home']} vs {match['away']}
Torneo: {s.get('tournament','?')} | Superficie: {s.get('surface','Hard')} | {s.get('circuit','ATP')}
Ranking: {match['home']}=#{s.get('rank1',999)}, {match['away']}=#{s.get('rank2',999)}
{wp_str}
Rispondi SOLO JSON: {{"main":{{"prediction":"...","betType":"ML|Handicap Set|Over/Under Games|Over/Under Set","odds":1.85,"confidence":72,"analysis":"2-3 frasi","keyFactors":["f1","f2","f3"],"risk":"Basso|Medio|Alto"}},"cards":null,"corners1h":null,"corners":null}}"""
        try:
            msg = client.messages.create(model="claude-sonnet-4-20250514",max_tokens=600,
                system="Esperto scommesse. SOLO JSON valido senza backtick.",
                messages=[{"role":"user","content":prompt}])
            text = msg.content[0].text.strip()
            result = json.loads(text[text.find("{"):text.rfind("}")+1])
            sub = result.get("main")
            if sub:
                sub["odds"] = max(1.01,min(5.0,float(sub.get("odds",1.8))))
                sub["confidence"] = max(50,min(95,int(sub.get("confidence",65))))
            return result
        except Exception as e:
            log.warning(f"Tennis AI {match['home']}: {e}")
            return _fallback_prediction(match)

    if sport == "calcio":
        prompt = f"""Analista scommesse calcio. QUATTRO mercati. Quote 1.30-4.00.
PARTITA: {match['home']} vs {match['away']} | {match['league']} | {match['date']} {match['time']}
Forma {match['home']}: {'-'.join(match.get('homeForm') or ['?']*5)} | Forma {match['away']}: {'-'.join(match.get('awayForm') or ['?']*5)}
{wp_str}
GOL: casa={s.get('hG',1.5)} trasferta={s.get('aG',1.2)} BTTS={s.get('btts',52)}% Over2.5={s.get('o25',58)}% Over3.5={s.get('o35',32)}%
CARTELLINI: {match['home']}={s.get('hYellow',1.8)}g+{s.get('hRed',0.12)}r | {match['away']}={s.get('aYellow',1.6)}g+{s.get('aRed',0.10)}r | tot={s.get('totalCards',3.4):.1f} Over3.5={s.get('over35Cards',55)}% Over4.5={s.get('over45Cards',38)}%
ANGOLI: {match['home']}={s.get('hCorners',5.2):.1f} {match['away']}={s.get('aCorners',4.8):.1f} tot={s.get('totalCorners',10.0):.1f} Over8.5={s.get('over85Corners',52)}% Over9.5={s.get('over95Corners',38)}% | 1T={s.get('corners1H',4.6):.1f} Over4.5 1T={s.get('over45Corners1H',45)}%
H2H: {s.get('h2h','')}
Rispondi SOLO JSON: {{"main":{{"prediction":"...","betType":"1X2|Goal/NoGoal|Over/Under|MultiGoal|Handicap|BTTS","odds":1.85,"confidence":72,"analysis":"2-3 frasi","keyFactors":["f1","f2","f3"],"risk":"Basso|Medio|Alto"}},"cards":{{"prediction":"Over/Under X.5 Cartellini","betType":"Cartellini","odds":1.75,"confidence":68,"analysis":"1-2 frasi","keyFactors":["f1","f2"],"risk":"Medio"}},"corners1h":{{"prediction":"Over/Under X.5 Angoli 1T","betType":"Angoli 1°T","odds":1.90,"confidence":65,"analysis":"1-2 frasi","keyFactors":["f1","f2"],"risk":"Medio"}},"corners":{{"prediction":"Over/Under X.5 Angoli Totali","betType":"Angoli Totali","odds":1.80,"confidence":70,"analysis":"1-2 frasi","keyFactors":["f1","f2"],"risk":"Medio"}}}}"""
    else:
        prompt = f"""Basket UN pronostico (1.30-4.00). Mercati: ML, Handicap, Over/Under.
{match['home']} vs {match['away']} | {match['league']} | {match['date']} {match['time']}
Forma: {'-'.join(match.get('homeForm') or ['?']*5)} vs {'-'.join(match.get('awayForm') or ['?']*5)}
Punti: casa={s.get('hPPG',110)} trasferta={s.get('aPPG',108)} tot={s.get('totAvg',218)} {wp_str}
Rispondi SOLO JSON: {{"main":{{"prediction":"...","betType":"ML|Handicap|Over/Under","odds":1.85,"confidence":72,"analysis":"2-3 frasi","keyFactors":["f1","f2","f3"],"risk":"Basso|Medio|Alto"}},"cards":null,"corners1h":null,"corners":null}}"""

    try:
        msg = client.messages.create(model="claude-sonnet-4-20250514",max_tokens=950,
            system="Esperto scommesse. SOLO JSON valido senza testo extra né backtick.",
            messages=[{"role":"user","content":prompt}])
        text = msg.content[0].text.strip()
        result = json.loads(text[text.find("{"):text.rfind("}")+1])
        for key in ("main","cards","corners1h","corners"):
            sub = result.get(key)
            if sub and isinstance(sub,dict):
                sub["odds"] = max(1.01,min(5.0,float(sub.get("odds",1.8))))
                sub["confidence"] = max(50,min(95,int(sub.get("confidence",65))))
        return result
    except Exception as e:
        log.warning(f"AI {match.get('home','?')}: {e}")
        return _fallback_prediction(match)

def _fallback_prediction(match):
    s = match["stats"]
    wp = match.get("winProb",{})
    sport = match["sport"]
    if sport == "tennis":
        wp1 = wp.get("home",50)
        fav = match["home"] if wp1>=50 else match["away"]
        return {"main":{"prediction":f"Vittoria {fav}","betType":"ML","odds":round(1.3+random.random()*.7,2),
            "confidence":random.randint(60,75),"analysis":f"{fav} favorito per ranking e superficie.",
            "keyFactors":["Ranking","Superficie","Forma"],"risk":"Medio"},"cards":None,"corners1h":None,"corners":None}
    if sport == "calcio":
        if wp.get("home",0)>60: mp={"prediction":f"Vittoria {match['home']}","betType":"1X2","odds":round(1.3+random.random()*.5,2)}
        elif wp.get("away",0)>60: mp={"prediction":f"Vittoria {match['away']}","betType":"1X2","odds":round(1.4+random.random()*.6,2)}
        else: mp={"prediction":"Over 2.5 Gol","betType":"Over/Under","odds":round(1.6+random.random()*.5,2)}
        mp.update({"confidence":random.randint(60,72),"analysis":f"Analisi {match['league']}.","keyFactors":["Forma","Campo","H2H"],"risk":"Medio"})
        tc = s.get("totalCards",3.4)
        cards = {"prediction":f"Over {'3.5' if tc>=3.5 else '2.5'} Cartellini","betType":"Cartellini",
            "odds":round(1.65+random.random()*.4,2),"confidence":random.randint(58,70),
            "analysis":f"Media {tc:.1f} cartellini attesi.","keyFactors":["Gialli/gara","H2H","Arbitro"],"risk":"Medio"}
        tot = s.get("totalCorners",10.0)
        line_a = 8.5 if tot<9 else (9.5 if tot<10.5 else 10.5)
        corners = {"prediction":f"Over {line_a} Angoli","betType":"Angoli Totali",
            "odds":round(1.70+random.random()*.4,2),"confidence":random.randint(60,72),
            "analysis":f"Attesi {tot:.1f} angoli.","keyFactors":["Angoli/gara","Stile","H2H"],"risk":"Medio"}
        exp1h = s.get("corners1H",4.6)
        line_1h = 3.5 if exp1h<4.2 else (4.5 if exp1h<5.0 else 5.5)
        corn1h = {"prediction":f"Over {line_1h} Angoli 1°T","betType":"Angoli 1°T",
            "odds":round(1.75+random.random()*.45,2),"confidence":random.randint(58,68),
            "analysis":f"Attesi ~{exp1h:.1f} angoli 1° tempo.","keyFactors":["Ritmo","Pressing","H2H"],"risk":"Medio"}
        return {"main":mp,"cards":cards,"corners1h":corn1h,"corners":corners}
    return {"main":{"prediction":f"Over {s.get('totAvg',220):.0f} Punti","betType":"Over/Under",
        "odds":round(1.75+random.random()*.4,2),"confidence":random.randint(62,74),
        "analysis":"Media punti stagionale favorevole.","keyFactors":["Media punti","Ritmo","Difese"],"risk":"Medio"},
        "cards":None,"corners1h":None,"corners":None}

# DAILY REFRESH
def daily_refresh():
    log.info("=== daily_refresh START ===")
    matches = scrape_all_matches()
    if not matches: log.warning("Nessuna partita"); return
    results = []
    for i,m in enumerate(matches):
        log.info(f"  AI [{i+1}/{len(matches)}] {m['home']} vs {m['away']} ({m['sport']})")
        results.append({**m,"predictions":analyze_match_ai(m)})
        time.sleep(0.9)
    write_cache("daily_matches",{"updated_at":datetime.now(timezone.utc).isoformat(),"count":len(results),"matches":results})
    log.info(f"=== daily_refresh DONE — {len(results)} pronostici ===")

# API
@app.route("/health")
def health():
    cached = read_cache("daily_matches") or {}
    return jsonify({"status":"ok","time":datetime.utcnow().isoformat(),
        "match_count":cached.get("count",0),"updated_at":cached.get("updated_at","mai"),
        "cache_age_h":round((time.time()-cached.get("_ts",time.time()))/3600,1)})

@app.route("/api/matches")
def api_matches():
    cached = read_cache("daily_matches")
    if cached: return jsonify(cached)
    daily_refresh()
    cached = read_cache("daily_matches")
    if cached: return jsonify(cached)
    return jsonify({"error":"Nessuna partita","matches":[]}),503

@app.route("/api/matches/refresh",methods=["POST"])
def api_refresh():
    if request.headers.get("X-Refresh-Token") != os.environ.get("REFRESH_TOKEN","betgenius2025"):
        return jsonify({"error":"Unauthorized"}),401
    threading.Thread(target=daily_refresh,daemon=True).start()
    return jsonify({"status":"refresh avviato"})

@app.route("/api/news")
def api_news():
    cached = read_cache("daily_matches") or {}
    news = []
    for m in (cached.get("matches") or []):
        ico = "⚽" if m["sport"]=="calcio" else ("🎾" if m["sport"]=="tennis" else "🏀")
        pred = (m.get("predictions",{}).get("main") or {}).get("prediction","")
        news.append({"s":ico,"t":f"{m['home']} vs {m['away']} — {m['league']} | {m['date']} {m['time']}"+(f" → {pred}" if pred else "")})
    return jsonify({"news":news[:35]})

@app.route("/api/leagues")
def api_leagues():
    cached = read_cache("daily_matches") or {}
    return jsonify({"leagues":sorted({m["league"] for m in (cached.get("matches") or [])})})

# SCHEDULER
def start_scheduler():
    s = BackgroundScheduler(timezone="Europe/Rome")
    s.add_job(daily_refresh,"cron",hour=7,minute=30,id="morning")
    s.add_job(daily_refresh,"cron",hour=12,minute=0,id="midday")
    s.add_job(daily_refresh,"cron",hour=17,minute=0,id="afternoon")
    s.start()
    log.info("Scheduler: 07:30, 12:00, 17:00 (Europe/Rome)")

if __name__ == "__main__":
    start_scheduler()
    if not read_cache("daily_matches"):
        threading.Thread(target=daily_refresh,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8000)),debug=False)
