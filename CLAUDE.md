# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A GPS "manhunt" game for phones. Players pick a lobby, and the host splits them into hiders and seekers.
- Hiders see the other hiders on a map, refreshed on an interval, plus the distance to the nearest seeker.
- Seekers see the other seekers on a map, plus a compass bearing and distance to the nearest hider.
- A catch is started by the seeker and confirmed by the hider. The caught hider joins the seekers.

The stack is a Python FastAPI server and a vanilla JS + Leaflet frontend, with no build step.

## Commands

The Python virtualenv lives in `.venv/` (Windows paths shown).

```bash
.venv/Scripts/pip install -r requirements-dev.txt          # deps (fastapi, uvicorn, pytest, httpx)
.venv/Scripts/python -m uvicorn server.main:app --reload    # dev server on http://localhost:8000
.venv/Scripts/python -m pytest                              # all tests
.venv/Scripts/python -m pytest tests/test_game.py::test_time_limit_hiders_win   # single test
node --check static/app.js                                  # quick JS syntax check (no linter configured)
```

**Manual testing:** open several tabs at `http://localhost:8000/?sim=1`.
- Sim mode replaces GPS with map taps.
- It stores the session in `sessionStorage` rather than `localStorage`, so each tab is a separate player.
- Without `?sim=1`, the tabs share one session and the same real position.
- To iterate quickly, set the head start to 0 and use short intervals in the lobby settings.

**Phones:** geolocation and the compass only work over HTTPS, so use `cloudflared tunnel --url http://localhost:8000` or deploy via `render.yaml`.

## Architecture

**Core rule:** the server is the only holder of positions. Clients must never receive the other team's coordinates.
- Seekers get only `{bearing_deg, distance_m}`.
- Hiders get only the nearest-seeker `distance_m`.
- `tests/test_game.py::assert_no_leaks` checks this. Keep it passing when adding messages.
- The one deliberate exception is the `ended` phase. Then `all_map`, with every player's position, goes to everyone every `ENDED_MAP_INTERVAL_S` so people can regroup, and chat is allowed between any players.

### Server layers (`server/`)

- **`game.py`: pure logic, with no I/O or asyncio.**
  - `Lobby` methods take an explicit `now` timestamp.
  - Every mutating method returns an **outbox**: a list of `(recipient_player_ids, message_dict)`. Add new behaviour here as an outbox-returning method, not by sending from the manager.
  - **`view_for(lobby, player, now)` is the single place that decides what a player may see**. It builds the full `state` message.
  - Players store the *last broadcast* compass and seeker-distance values (`last_compass`, `last_seeker_distance`). Lobbies store the last map snapshots (`last_hider_map`, `last_seeker_map`). Because `view_for` returns these stored values, reconnecting cannot be used to get fresher information than the interval allows.
- **`manager.py`: the transport layer.**
  - Holds the lobby registry, a `token -> (lobby_id, player_id)` map, and the sockets keyed by `(lobby_id, player_id)`.
  - `deliver()` sends an outbox. When one outbox holds several `state` messages for the same player, it keeps only the last.
  - `handle()` dispatches client messages and turns a `GameError` into an `{"t":"error"}` reply to the sender.
  - `tick_forever()` runs every 1 s. It calls `Lobby.tick()` and removes ended or idle lobbies.
- **`main.py`:**
  - REST endpoints for listing, creating and joining lobbies. Create and join return a token.
  - `/ws?token=…` carries everything else.
  - Static files are mounted at `/`, and the mount must stay last.

### Game state

All state is in memory and lives in a single process. Restarting the server ends running games, and running more than one instance would break the game.

**Phases:** `lobby → head_start → active → ended`.
- Timers are absolute epoch seconds: `active_at` and `ends_at`.
- Periodic broadcasts (`hider_map`, `seeker_map`, `seeker_distance`, `compass`) are scheduled through `Lobby.next_due` using the per-lobby `Settings` intervals.
- The compass is only sent during `active`.
- **Win rules** live in `Lobby.check_end`, which returns `(winner, winner_id)`. `survival_at` is `active_at + survival_time_s`, capped at the time limit.
  - All hiders caught → `seeker`.
  - Past `survival_at` with exactly one hider left → `last_hider`, with `winner_id` set to that player.
  - `ends_at` reached with 2+ hiders → `hider` (a team win).

### WebSocket protocol

JSON messages, with the type in field `t`.
- The client to server types are matched in `Manager.handle`.
- Server to client types are produced in `game.py` and handled in the `onMessage` switch in `static/app.js`.
- `state` is the full authoritative snapshot. The client re-renders its whole screen from it.

### Frontend (`static/`)

- Plain IIFE modules loaded in order: `geo.js` (GPS, sim mode, compass heading, wake lock), then `map.js` (Leaflet wrapper), then `app.js` (screens, socket, rendering).
- `app.js` picks the screen from `state.lobby.phase`.
- The client measures its clock offset from `ping`/`pong`, so countdowns use server time (`serverNow()`).
- Compass heading:
  - iOS uses `webkitCompassHeading` and needs `DeviceOrientationEvent.requestPermission()` called from a user tap.
  - Android uses `deviceorientationabsolute`, with heading = `360 - alpha`.
- Leaflet is loaded from cdnjs and pinned to 1.9.4.
