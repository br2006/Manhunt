"""Pure game logic: no networking, no asyncio.

Every mutating Lobby method returns an *outbox*: a list of ``(recipients, message)``
tuples, where ``recipients`` is a list of player ids. The transport layer
(manager.py) just delivers them. Visibility rules live here and nowhere else.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field, fields
from typing import Optional

from .geo import bearing_deg, haversine_m

LOBBY, HEAD_START, ACTIVE, ENDED = "lobby", "head_start", "active", "ended"
HIDER, SEEKER = "hider", "seeker"
LAST_HIDER = "last_hider"  # winner value when one hider wins alone (see Lobby.winner_id)

STALE_FIX_S = 600          # fixes older than this are ignored for nearest/catch logic
ENDED_MAP_INTERVAL_S = 10  # after the game, everyone's position is shared this often
MAX_ACCURACY_M = 150       # worse GPS readings are rejected
MAX_NAME_LEN = 24
MAX_CHAT_LEN = 200
CHAT_RATE_WINDOW_S = 10    # at most CHAT_RATE_MAX messages per window per player
CHAT_RATE_MAX = 5

Outbox = list[tuple[list[str], dict]]


class GameError(Exception):
    """A client did something it is not allowed to; message is shown to them."""


@dataclass
class Settings:
    min_players: int = 3
    head_start_s: int = 300
    time_limit_s: int = 3600              # hard limit: 2+ hiders still free -> hiders win
    survival_time_s: int = 1800           # after this, the last remaining hider wins alone
    hider_map_interval_s: int = 120
    seeker_map_interval_s: int = 30
    compass_interval_s: int = 10
    hider_dist_interval_s: int = 30
    catch_radius_m: int = 75
    catch_confirm_timeout_s: int = 30

    LIMITS = {
        "min_players": (2, 50),
        "head_start_s": (0, 3600),
        "time_limit_s": (60, 6 * 3600),
        "survival_time_s": (60, 6 * 3600),
        "hider_map_interval_s": (1, 1800),
        "seeker_map_interval_s": (1, 1800),
        "compass_interval_s": (1, 1800),
        "hider_dist_interval_s": (1, 1800),
        "catch_radius_m": (5, 1000),
        "catch_confirm_timeout_s": (5, 300),
    }

    def update(self, data: dict) -> None:
        for f in fields(self):
            if f.name in data:
                try:
                    value = int(data[f.name])
                except (TypeError, ValueError):
                    raise GameError(f"Invalid value for {f.name}")
                lo, hi = self.LIMITS[f.name]
                setattr(self, f.name, max(lo, min(hi, value)))

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class Player:
    id: str
    token: str
    name: str
    joined_at: float
    team: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    accuracy_m: Optional[float] = None
    last_fix_at: Optional[float] = None
    connected: bool = False
    caught_at: Optional[float] = None
    caught_by: Optional[str] = None
    # Last values actually broadcast to this player, so a reconnect cannot be
    # used to get fresher information than the interval allows.
    last_compass: Optional[dict] = None
    last_seeker_distance: Optional[dict] = None
    chat_times: list[float] = field(default_factory=list)

    def has_fix(self, now: float) -> bool:
        return self.last_fix_at is not None and now - self.last_fix_at <= STALE_FIX_S

    def fix_age(self, now: float) -> Optional[int]:
        return None if self.last_fix_at is None else int(now - self.last_fix_at)


@dataclass
class PendingCatch:
    id: str
    seeker_id: str
    hider_id: str
    expires_at: float


def nearest(origin: Player, candidates: list[Player], now: float):
    """Return (player, distance_m, bearing_deg) of the nearest candidate with a fresh fix."""
    if not origin.has_fix(now):
        return None
    best = None
    for c in candidates:
        if c.id == origin.id or not c.has_fix(now):
            continue
        d = haversine_m(origin.lat, origin.lon, c.lat, c.lon)
        if best is None or d < best[1]:
            best = (c, d)
    if best is None:
        return None
    p, d = best
    return p, d, bearing_deg(origin.lat, origin.lon, p.lat, p.lon)


def clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())[:MAX_NAME_LEN]
    if not name:
        raise GameError("Please enter a nickname")
    return name


@dataclass
class Lobby:
    id: str
    name: str
    created_at: float
    host_id: Optional[str] = None
    players: dict[str, Player] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)
    phase: str = LOBBY
    started_at: Optional[float] = None
    active_at: Optional[float] = None
    ends_at: Optional[float] = None
    ended_at: Optional[float] = None
    winner: Optional[str] = None          # SEEKER, HIDER (team) or LAST_HIDER (winner_id)
    winner_id: Optional[str] = None
    survival_at: Optional[float] = None
    survival_announced: bool = False
    last_all_map: list[dict] = field(default_factory=list)
    last_all_map_at: Optional[float] = None
    pending: dict[str, PendingCatch] = field(default_factory=dict)
    next_due: dict[str, float] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    last_hider_map: list[dict] = field(default_factory=list)
    last_seeker_map: list[dict] = field(default_factory=list)
    last_hider_map_at: Optional[float] = None
    last_seeker_map_at: Optional[float] = None
    last_activity: float = 0.0

    # ---------- helpers ----------
    def team(self, team: str) -> list[Player]:
        return [p for p in self.players.values() if p.team == team]

    def ids(self, team: Optional[str] = None) -> list[str]:
        return [p.id for p in self.players.values() if team is None or p.team == team]

    def get(self, pid: str) -> Player:
        p = self.players.get(pid)
        if p is None:
            raise GameError("Unknown player")
        return p

    def _require_host(self, actor_id: str) -> None:
        if actor_id != self.host_id:
            raise GameError("Only the host can do that")

    def _require_phase(self, *phases: str) -> None:
        if self.phase not in phases:
            raise GameError("Not allowed right now")

    def summary(self) -> dict:
        host = self.players.get(self.host_id)
        return {
            "id": self.id,
            "name": self.name,
            "host": host.name if host else None,
            "players": len(self.players),
            "min_players": self.settings.min_players,
            "phase": self.phase,
        }

    def _states(self, now: float) -> Outbox:
        return [([p.id], view_for(self, p, now)) for p in self.players.values()]

    # ---------- lobby phase ----------
    def add_player(self, name: str, now: float) -> Player:
        self._require_phase(LOBBY)
        name = clean_name(name)
        if any(p.name.lower() == name.lower() for p in self.players.values()):
            raise GameError("That nickname is taken in this lobby")
        p = Player(id=secrets.token_hex(4), token=secrets.token_urlsafe(24), name=name, joined_at=now, team=HIDER)
        self.players[p.id] = p
        if self.host_id is None:
            self.host_id = p.id
        self.last_activity = now
        return p

    def remove_player(self, pid: str, now: float) -> Outbox:
        p = self.players.pop(pid, None)
        if p is None:
            return []
        for cid, c in list(self.pending.items()):
            if pid in (c.seeker_id, c.hider_id):
                del self.pending[cid]
        if self.host_id == pid:
            remaining = sorted(self.players.values(), key=lambda x: x.joined_at)
            self.host_id = remaining[0].id if remaining else None
        out: Outbox = []
        if self.phase in (HEAD_START, ACTIVE):
            self.events.append({"t": "left", "name": p.name, "team": p.team, "at": now})
            out += self._maybe_end(now)
        return out + self._states(now)

    def set_team(self, actor_id: str, pid: str, team: Optional[str], now: float) -> Outbox:
        self._require_host(actor_id)
        self._require_phase(LOBBY)
        if team not in (HIDER, SEEKER, None):
            raise GameError("Invalid team")
        self.get(pid).team = team
        return self._states(now)

    def update_settings(self, actor_id: str, data: dict, now: float) -> Outbox:
        self._require_host(actor_id)
        self._require_phase(LOBBY)
        self.settings.update(data)
        return self._states(now)

    def start_problem(self, now: float) -> Optional[str]:
        """Why the game cannot start, or None if it can."""
        if self.phase != LOBBY:
            return "Game already started"
        if len(self.players) < self.settings.min_players:
            return f"Need at least {self.settings.min_players} players"
        if any(p.team is None for p in self.players.values()):
            return "Assign every player to a team"
        if not self.team(SEEKER):
            return "Need at least one seeker"
        if not self.team(HIDER):
            return "Need at least one hider"
        missing = [p.name for p in self.players.values() if not p.has_fix(now)]
        if missing:
            return "Waiting for GPS: " + ", ".join(missing)
        return None

    def start(self, actor_id: str, now: float) -> Outbox:
        self._require_host(actor_id)
        problem = self.start_problem(now)
        if problem:
            raise GameError(problem)
        s = self.settings
        self.phase = HEAD_START if s.head_start_s > 0 else ACTIVE
        self.started_at = now
        self.active_at = now + s.head_start_s
        self.ends_at = self.active_at + s.time_limit_s
        self.survival_at = self.active_at + min(s.survival_time_s, s.time_limit_s)
        self.next_due = {k: now for k in ("hider_map", "hider_dist", "seeker_map", "compass")}
        out = self._states(now)
        return out + self.tick(now)

    # ---------- positions ----------
    def update_position(self, pid: str, lat, lon, acc, now: float) -> tuple[bool, Outbox]:
        p = self.get(pid)
        try:
            lat, lon = float(lat), float(lon)
            acc = float(acc) if acc is not None else 0.0
        except (TypeError, ValueError):
            raise GameError("Bad position")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise GameError("Bad position")
        if acc > MAX_ACCURACY_M:
            return False, []
        had_fix = p.has_fix(now)
        p.lat, p.lon, p.accuracy_m, p.last_fix_at = lat, lon, acc, now
        self.last_activity = now
        # In the lobby, the roster shows a GPS tick; refresh everyone when it first appears.
        if self.phase == LOBBY and not had_fix:
            return True, self._states(now)
        return True, []

    # ---------- per-team payloads ----------
    def _map_payload(self, team: str, now: float) -> list[dict]:
        return [
            {"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon, "fix_age_s": p.fix_age(now)}
            for p in self.team(team)
            if p.last_fix_at is not None
        ]

    def _compass_for(self, seeker: Player, now: float) -> dict:
        n = nearest(seeker, self.team(HIDER), now)
        if n is None:
            return {"available": False, "at": now}
        hider, dist, bearing = n
        return {
            "available": True,
            "bearing_deg": round(bearing, 1),
            "distance_m": round(dist),
            "fix_age_s": hider.fix_age(now),
            "at": now,
        }

    def _seeker_distance_for(self, hider: Player, now: float) -> dict:
        n = nearest(hider, self.team(SEEKER), now)
        if n is None:
            return {"available": False, "at": now}
        seeker, dist, _ = n
        return {"available": True, "distance_m": round(dist), "fix_age_s": seeker.fix_age(now), "at": now}

    def _send_compass(self, seeker: Player, now: float) -> Outbox:
        seeker.last_compass = self._compass_for(seeker, now)
        return [([seeker.id], {"t": "compass", **seeker.last_compass})]

    # ---------- tick ----------
    def _due(self, kind: str, interval: int, now: float) -> bool:
        if now >= self.next_due.get(kind, float("inf")):
            self.next_due[kind] = now + interval
            return True
        return False

    def _all_map_payload(self, now: float) -> list[dict]:
        """Everyone's position: only ever sent once the game is over."""
        return [
            {"id": p.id, "name": p.name, "team": p.team, "lat": p.lat, "lon": p.lon, "fix_age_s": p.fix_age(now)}
            for p in self.players.values()
            if p.last_fix_at is not None
        ]

    def _refresh_all_map(self, now: float) -> Outbox:
        self.last_all_map = self._all_map_payload(now)
        self.last_all_map_at = now
        self.next_due["all_map"] = now + ENDED_MAP_INTERVAL_S
        return [(self.ids(), {"t": "all_map", "players": self.last_all_map, "at": now})]

    def tick(self, now: float) -> Outbox:
        if self.phase == ENDED:
            if now >= self.next_due.get("all_map", 0):
                return self._refresh_all_map(now)
            return []
        if self.phase not in (HEAD_START, ACTIVE):
            return []
        out: Outbox = []
        s = self.settings

        if self.phase == HEAD_START and now >= self.active_at:
            self.phase = ACTIVE
            self.next_due["compass"] = now
            out += [(self.ids(), {"t": "phase", "phase": ACTIVE})]
            out += self._states(now)

        for cid, c in list(self.pending.items()):
            if now >= c.expires_at:
                del self.pending[cid]
                out.append(([c.seeker_id], {"t": "catch_result", "ok": False, "reason": "No response from hider"}))
                out.append(([c.hider_id], {"t": "catch_cancelled", "catch_id": cid}))

        out += self._maybe_end(now)
        if self.phase == ENDED:
            return out

        if self.survival_reached(now) and not self.survival_announced:
            self.survival_announced = True
            out.append((self.ids(), {"t": "survival_reached", "hiders_left": len(self.team(HIDER))}))

        if self._due("hider_map", s.hider_map_interval_s, now):
            self.last_hider_map = self._map_payload(HIDER, now)
            self.last_hider_map_at = now
            out.append((self.ids(HIDER), {"t": "hider_map", "players": self.last_hider_map, "at": now}))
        if self._due("seeker_map", s.seeker_map_interval_s, now):
            self.last_seeker_map = self._map_payload(SEEKER, now)
            self.last_seeker_map_at = now
            out.append((self.ids(SEEKER), {"t": "seeker_map", "players": self.last_seeker_map, "at": now}))
        if self._due("hider_dist", s.hider_dist_interval_s, now):
            for h in self.team(HIDER):
                h.last_seeker_distance = self._seeker_distance_for(h, now)
                out.append(([h.id], {"t": "seeker_distance", **h.last_seeker_distance}))
        if self.phase == ACTIVE and self._due("compass", s.compass_interval_s, now):
            for sk in self.team(SEEKER):
                out += self._send_compass(sk, now)
        return out

    # ---------- catching ----------
    def catch_candidates(self, seeker_id: str, now: float) -> list[Player]:
        seeker = self.get(seeker_id)
        if seeker.team != SEEKER:
            raise GameError("Only seekers can catch")
        if self.phase != ACTIVE:
            raise GameError("Catching isn't allowed yet")
        if not seeker.has_fix(now):
            raise GameError("Your GPS position is missing")
        r = self.settings.catch_radius_m
        result = []
        for h in self.team(HIDER):
            if h.has_fix(now):
                d = haversine_m(seeker.lat, seeker.lon, h.lat, h.lon)
                if d <= r:
                    result.append((d, h))
        return [h for _, h in sorted(result, key=lambda x: x[0])]

    def request_catch(self, seeker_id: str, hider_id: str, now: float) -> Outbox:
        candidates = self.catch_candidates(seeker_id, now)
        hider = next((h for h in candidates if h.id == hider_id), None)
        if hider is None:
            raise GameError("That hider isn't within catch range")
        if any(c.hider_id == hider_id for c in self.pending.values()):
            raise GameError("A catch for that hider is already pending")
        if any(c.seeker_id == seeker_id for c in self.pending.values()):
            raise GameError("You already have a catch pending")
        c = PendingCatch(
            id=secrets.token_hex(4),
            seeker_id=seeker_id,
            hider_id=hider_id,
            expires_at=now + self.settings.catch_confirm_timeout_s,
        )
        self.pending[c.id] = c
        seeker = self.players[seeker_id]
        return [
            ([hider_id], {"t": "catch_prompt", "catch_id": c.id, "seeker_name": seeker.name, "expires_at": c.expires_at}),
            ([seeker_id], {"t": "catch_waiting", "hider_name": hider.name, "expires_at": c.expires_at}),
        ]

    def reply_catch(self, hider_id: str, catch_id: str, accept: bool, now: float) -> Outbox:
        c = self.pending.get(catch_id)
        if c is None or c.hider_id != hider_id:
            raise GameError("That catch request has expired")
        del self.pending[catch_id]
        hider = self.players[hider_id]
        seeker = self.players.get(c.seeker_id)
        if not accept:
            return [([c.seeker_id], {"t": "catch_result", "ok": False, "reason": f"{hider.name} denied the catch"})]

        hider.team = SEEKER
        hider.caught_at = now
        hider.caught_by = seeker.name if seeker else "?"
        hider.last_seeker_distance = None
        # Cancel any other pending catches involving the new seeker.
        for cid, other in list(self.pending.items()):
            if other.hider_id == hider_id:
                del self.pending[cid]
        self.events.append({"t": "caught", "hider": hider.name, "seeker": hider.caught_by, "at": now})
        self.last_hider_map = [e for e in self.last_hider_map if e["id"] != hider_id]
        self.last_seeker_map = self._map_payload(SEEKER, now)
        self.last_seeker_map_at = now

        out: Outbox = [
            (self.ids(), {"t": "player_caught", "hider": hider.name, "seeker": hider.caught_by}),
            ([c.seeker_id], {"t": "catch_result", "ok": True, "hider_name": hider.name}),
        ]
        out += self._maybe_end(now)
        out += self._states(now)
        if self.phase == ACTIVE:
            out += self._send_compass(hider, now)
        return out

    # ---------- team chat ----------
    def send_chat(self, sender_id: str, to_id: str, text, now: float) -> Outbox:
        """Direct message (emoji or text) to a player currently on the same team."""
        self._require_phase(HEAD_START, ACTIVE, ENDED)
        sender = self.get(sender_id)
        target = self.players.get(to_id)
        if target is None or target.id == sender.id:
            raise GameError("Unknown recipient")
        if self.phase != ENDED and target.team != sender.team:  # after the game anyone can talk
            raise GameError("You can only message your own team")
        text = " ".join(str(text or "").split())[:MAX_CHAT_LEN]
        if not text:
            raise GameError("Message is empty")
        sender.chat_times = [t for t in sender.chat_times if now - t < CHAT_RATE_WINDOW_S]
        if len(sender.chat_times) >= CHAT_RATE_MAX:
            raise GameError("Slow down: too many messages")
        sender.chat_times.append(now)
        self.last_activity = now
        return [
            ([target.id], {"t": "chat", "from_id": sender.id, "from_name": sender.name, "text": text, "at": now}),
            ([sender.id], {"t": "chat_sent", "to_name": target.name, "text": text}),
        ]

    # ---------- ending ----------
    def survival_reached(self, now: float) -> bool:
        return self.phase == ACTIVE and self.survival_at is not None and now >= self.survival_at

    def check_end(self, now: float) -> Optional[tuple[str, Optional[str]]]:
        """Return (winner, winner_player_id) or None.

        - all hiders caught (before the survival time)       -> seekers
        - past the survival time with exactly one hider left -> that hider alone
        - hard time limit reached with 2+ hiders still free  -> hiders (team)
        """
        if self.phase not in (HEAD_START, ACTIVE):
            return None
        hiders = self.team(HIDER)
        if not hiders:
            return SEEKER, None
        if not self.team(SEEKER):
            return HIDER, None
        time_up = self.ends_at is not None and now >= self.ends_at
        if len(hiders) == 1 and (self.survival_reached(now) or time_up):
            return LAST_HIDER, hiders[0].id
        if time_up:
            return HIDER, None
        return None

    def _maybe_end(self, now: float) -> Outbox:
        result = self.check_end(now)
        if result is None:
            return []
        self.phase = ENDED
        self.winner, self.winner_id = result
        self.ended_at = now
        self.pending.clear()
        self._refresh_all_map(now)  # stored in last_all_map, so the states below include it
        win = self.players.get(self.winner_id) if self.winner_id else None
        msg = {"t": "game_over", "winner": self.winner, "winner_id": self.winner_id, "winner_name": win.name if win else None}
        return [(self.ids(), msg)] + self._states(now)


# ---------- views ----------
def roster(lobby: Lobby, now: float) -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "team": p.team,
            "connected": p.connected,
            "has_fix": p.has_fix(now),
            "is_host": p.id == lobby.host_id,
            "caught_by": p.caught_by,
            "caught_at": p.caught_at,
        }
        for p in sorted(lobby.players.values(), key=lambda x: x.joined_at)
    ]


def view_for(lobby: Lobby, player: Player, now: float) -> dict:
    """Full personal state. The ONLY place that decides what a player may see."""
    view = {
        "t": "state",
        "server_time": now,
        "lobby": {
            "id": lobby.id,
            "name": lobby.name,
            "phase": lobby.phase,
            "host_id": lobby.host_id,
            "settings": lobby.settings.to_dict(),
            "started_at": lobby.started_at,
            "active_at": lobby.active_at,
            "ends_at": lobby.ends_at,
            "survival_at": lobby.survival_at,
            "ended_at": lobby.ended_at,
            "winner": lobby.winner,
            "winner_id": lobby.winner_id,
            "events": lobby.events,
            "start_problem": lobby.start_problem(now) if lobby.phase == LOBBY else None,
        },
        "roster": roster(lobby, now),
        "you": {"id": player.id, "name": player.name, "team": player.team, "is_host": player.id == lobby.host_id},
    }
    in_game = lobby.phase in (HEAD_START, ACTIVE)
    if in_game and player.team == HIDER:
        hider_ids = set(lobby.ids(HIDER))
        view["hider_map"] = [e for e in lobby.last_hider_map if e["id"] in hider_ids]
        view["hider_map_at"] = lobby.last_hider_map_at
        view["seeker_distance"] = player.last_seeker_distance
        prompt = next((c for c in lobby.pending.values() if c.hider_id == player.id), None)
        if prompt:
            seeker = lobby.players.get(prompt.seeker_id)
            view["catch_prompt"] = {
                "catch_id": prompt.id,
                "seeker_name": seeker.name if seeker else "?",
                "expires_at": prompt.expires_at,
            }
    elif in_game and player.team == SEEKER:
        view["seeker_map"] = lobby.last_seeker_map
        view["seeker_map_at"] = lobby.last_seeker_map_at
        view["compass"] = player.last_compass if lobby.phase == ACTIVE else None
    return view
