# Manhunt

Real-world hide & seek on phones.

- **Hiders** see the other hiders on a map (refreshed every ~2 min) and the distance to the nearest seeker.
- **Seekers** see the other seekers on a map, plus a compass arrow and distance to the nearest hider.
- A seeker taps **Caught someone!** when within the catch radius. The hider confirms on their phone and then becomes a seeker.
- **Seekers win** if every hider is caught before the **survival time**.
- After the survival time, when only one hider is left, that **hider wins alone** and the game ends.
- If **2 or more hiders** are still free at the **hard time limit**, the hiders win as a team.
- Both times are lobby settings, counted from the end of the head start.

The server is the only thing that knows every position. Each phone receives only what its team may see: seekers get a bearing and distance, never hider coordinates.

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Windows (use .venv/bin/pip on macOS/Linux)
.venv/Scripts/python -m uvicorn server.main:app --reload
```

### Desk testing (sim mode)

Open several tabs at <http://localhost:8000/?sim=1>. In sim mode:
- each tab is a separate player;
- the GPS is fake: tap the map to move yourself.

To test quickly, set short intervals and a 0-minute head start in the lobby settings.

### Tests

```bash
.venv/Scripts/python -m pytest
```

## Play on phones

Geolocation and the compass require **HTTPS**. Two ways to get it:

- **Quick test**: `cloudflared tunnel --url http://localhost:8000`, then open the printed `https://…trycloudflare.com` URL on each phone.
- **Deploy**: push this folder to GitHub and create a Render "Blueprint" from `render.yaml`. Any host that runs `uvicorn server.main:app --host 0.0.0.0 --port $PORT` as a **single instance** works.

Tips for players:
- Keep the page open with the screen on. The app asks for a wake lock, but locking the phone or switching apps stops GPS updates.
- On iPhone, tap **Enable location & compass** in the lobby. This grants compass access.
- Game state lives in memory, so restarting or redeploying the server ends running games.

## Layout

```
server/geo.py      haversine distance + bearing
server/game.py     pure game logic, phases, visibility rules (view_for)
server/manager.py  lobbies, tokens, websocket delivery, 1 s tick loop
server/main.py     FastAPI routes + /ws + static files
static/            single-page frontend (vanilla JS + Leaflet)
tests/             pytest: geometry, game rules, end-to-end websocket game
```
