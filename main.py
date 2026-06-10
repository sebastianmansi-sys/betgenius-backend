import os
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
from datetime import datetime, timedelta
import anthropic
import openai
import json
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="BetGenius AI Backend")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ==================== PAGINA BENVENUTA ====================
@app.get("/")
async def root():
    return {
        "message": "✅ BetGenius AI Backend è attivo!",
        "status": "online",
        "endpoints": {
            "Calcio": "/matches/football",
            "Basket": "/matches/basket",
            "Tennis": "/matches/tennis",
            "Live": "/matches/live",
            "Pronostici AI": "POST /ai/predictions"
        },
        "info": "Prova ad aprire /matches/football"
    }
# ==================== CHIAVI API ====================
FOOTBALL_API_KEY = os.getenv('FOOTBALL_API_KEY')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GROK_API_KEY = os.getenv('GROK_API_KEY')
SPORTS_API_KEY = os.getenv('SPORTS_API_KEY')

# Client AI (prova Claude → OpenAI → Grok)
def get_ai_client():
    if ANTHROPIC_API_KEY:
        import anthropic
        return "anthropic", anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    elif OPENAI_API_KEY:
        return "openai", openai.OpenAI(api_key=OPENAI_API_KEY)
    elif GROK_API_KEY:
        return "grok", openai.OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
    return None, None

FOOTBALL_HEADERS = {'X-Auth-Token': FOOTBALL_API_KEY} if FOOTBALL_API_KEY else {}
SPORTS_HEADERS = {'x-apisports-key': SPORTS_API_KEY} if SPORTS_API_KEY else {}

def get_dates(days=3):
    return [(datetime.now() + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)]

# ==================== MATCHES ====================
@app.get("/matches/football")
async def get_football_matches(competition: str = Query(None)):
    dates = get_dates()
    all_matches = []
    try:
        for date in dates:
            url = f"https://api.football-data.org/v4/matches?dateFrom={date}&dateTo={date}"
            if competition:
                url += f"&competitions={competition}"
            resp = requests.get(url, headers=FOOTBALL_HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get('matches', []):
                    all_matches.append({
                        "id": str(m.get('id')),
                        "date": m.get('utcDate'),
                        "competition": m.get('competition', {}).get('name', 'N/A'),
                        "homeTeam": {"name": m.get('homeTeam', {}).get('name', 'TBD'), "emoji": "⚽"},
                        "awayTeam": {"name": m.get('awayTeam', {}).get('name', 'TBD'), "emoji": "⚽"},
                        "status": m.get('status', {}).get('status', 'SCHEDULED'),
                        "sport": "football"
                    })
        return all_matches[:60]
    except Exception as e:
        return {"error": str(e), "matches": []}

@app.get("/matches/basket")
async def get_basket_matches():
    dates = get_dates()
    all_matches = []
    try:
        for date in dates:
            url = f"https://v1.basketball.api-sports.io/games?date={date}"
            resp = requests.get(url, headers=SPORTS_HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get('response', []):
                    all_matches.append({
                        "id": str(m.get('id')),
                        "date": m.get('date'),
                        "competition": m.get('league', {}).get('name', 'NBA'),
                        "homeTeam": {"name": m.get('teams', {}).get('home', {}).get('name', 'TBD'), "emoji": "🏀"},
                        "awayTeam": {"name": m.get('teams', {}).get('away', {}).get('name', 'TBD'), "emoji": "🏀"},
                        "status": m.get('status', {}).get('short', 'NS'),
                        "sport": "basket"
                    })
        return all_matches[:50]
    except Exception as e:
        return {"error": str(e), "matches": []}

@app.get("/matches/tennis")
async def get_tennis_matches():
    dates = get_dates()
    all_matches = []
    try:
        for date in dates:
            url = f"https://v1.tennis.api-sports.io/fixtures?date={date}"
            resp = requests.get(url, headers=SPORTS_HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get('response', []):
                    players = m.get('players', [])
                    all_matches.append({
                        "id": str(m.get('id')),
                        "date": m.get('date'),
                        "competition": m.get('tournament', {}).get('name', 'ATP/WTA'),
                        "homeTeam": {"name": players[0].get('name', 'TBD') if players else 'TBD', "emoji": "🎾"},
                        "awayTeam": {"name": players[1].get('name', 'TBD') if len(players) > 1 else 'TBD', "emoji": "🎾"},
                        "status": m.get('status', {}).get('short', 'NS'),
                        "sport": "tennis"
                    })
        return all_matches[:40]
    except Exception as e:
        return {"error": str(e), "matches": []}

# ==================== LIVE ====================
@app.get("/matches/live")
async def get_live_matches():
    live = []
    try:
        # Calcio Live
        resp = requests.get("https://api.football-data.org/v4/matches?status=LIVE", headers=FOOTBALL_HEADERS, timeout=10)
        if resp.status_code == 200:
            for m in resp.json().get('matches', []):
                live.append({
                    "sport": "football",
                    "homeTeam": m.get('homeTeam', {}).get('name'),
                    "awayTeam": m.get('awayTeam', {}).get('name'),
                    "score": "LIVE",
                    "status": "LIVE"
                })
    except:
        pass
    return {"live_matches": live, "count": len(live)}

# ==================== STATISTICHE NBA ====================
@app.get("/stats/basket/{game_id}")
async def get_basket_game_stats(game_id: int):
    try:
        url = f"https://v1.basketball.api-sports.io/games/statistics?id={game_id}"
        resp = requests.get(url, headers=SPORTS_HEADERS, timeout=15)
        return {"game_id": game_id, "success": True, "statistics": resp.json().get('response', [])}
    except Exception as e:
        return {"error": str(e)}

# ==================== PRONOSTICI AI ====================
@app.post("/ai/predictions")
async def generate_predictions(request: dict):
    matches = request.get("matches", [])
    sport = request.get("sport", "football")

    prompt = f"""
Sei un esperto di scommesse sportive. Analizza queste partite di {sport.upper()} e genera pronostici realistici e ben motivati.

Partite: {json.dumps(matches, ensure_ascii=False)}

Restituisci **SOLO** un array JSON con oggetti:
- "match"
- "pronostico"
- "confidenza" (numero tra 68 e 92)
- "motivazione" (1-2 frasi)

Rispondi esclusivamente con JSON valido.
"""

    provider, client = get_ai_client()
    if not client:
        return {"error": "Nessuna chiave AI configurata (ANTHROPIC / OPENAI / GROK)"}

    try:
        if provider == "anthropic":
            message = client.messages.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=1500,
                temperature=0.65,
                messages=[{"role": "user", "content": prompt}]
            )
            content = message.content[0].text
        else:
            response = client.chat.completions.create(
                model="gpt-4o" if provider == "openai" else "grok-4",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.65
            )
            content = response.choices[0].message.content

        try:
            predictions = json.loads(content)
        except:
            predictions = content

        return {
            "success": True,
            "provider": provider,
            "predictions": predictions,
            "message": f"Pronostici generati con {provider.upper()}"
        }
    except Exception as e:
        return {"error": str(e), "provider": provider}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)