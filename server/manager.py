"""Lobby registry, token lookup, WebSocket delivery and the tick loop."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Optional

from fastapi import WebSocket

from .game import ENDED, LOBBY, GameError, Lobby, Outbox, Player, view_for

log = logging.getLogger("manhunt")

ENDED_TTL_S = 30 * 60
IDLE_TTL_S = 30 * 60
INGAME_IDLE_TTL_S = 2 * 3600
MAX_LOBBIES = 200
MAX_LOBBY_NAME = 32


def now() -> float:
    return time.time()


class Manager:
    def __init__(self) -> None:
        self.lobbies: dict[str, Lobby] = {}
        self.tokens: dict[str, tuple[str, str]] = {}         # token -> (lobby_id, player_id)
        self.sockets: dict[tuple[str, str], WebSocket] = {}  # (lobby_id, player_id) -> ws

    # ---------- REST-facing ----------
    def open_lobbies(self) -> list[dict]:
        return [l.summary() for l in self.lobbies.values() if l.phase == LOBBY]

    def create_lobby(self, lobby_name: str, nickname: str) -> tuple[Lobby, Player]:
        if len(self.lobbies) >= MAX_LOBBIES:
            raise GameError("Server is full, try again later")
        name = " ".join(str(lobby_name or "").split())[:MAX_LOBBY_NAME] or "Manhunt"
        t = now()
        lobby = Lobby(id=secrets.token_urlsafe(6), name=name, created_at=t, last_activity=t)
        player = lobby.add_player(nickname, t)
        self.lobbies[lobby.id] = lobby
        self.tokens[player.token] = (lobby.id, player.id)
        return lobby, player

    async def join_lobby(self, lobby_id: str, nickname: str) -> tuple[Lobby, Player]:
        lobby = self.lobbies.get(lobby_id)
        if lobby is None:
            raise GameError("Lobby not found")
        t = now()
        player = lobby.add_player(nickname, t)
        self.tokens[player.token] = (lobby.id, player.id)
        await self.deliver(lobby, [([p.id], view_for(lobby, p, t)) for p in lobby.players.values()])
        return lobby, player

    def resolve(self, token: str) -> Optional[tuple[Lobby, Player]]:
        ref = self.tokens.get(token or "")
        if not ref:
            return None
        lobby = self.lobbies.get(ref[0])
        player = lobby.players.get(ref[1]) if lobby else None
        return (lobby, player) if player else None

    # ---------- delivery ----------
    async def deliver(self, lobby: Lobby, outbox: Outbox) -> None:
        # Only the last "state" per player matters; drop earlier ones.
        last_state: dict[str, int] = {}
        for i, (recips, msg) in enumerate(outbox):
            if msg.get("t") == "state":
                for pid in recips:
                    last_state[pid] = i
        for i, (recips, msg) in enumerate(outbox):
            for pid in recips:
                if msg.get("t") == "state" and last_state.get(pid) != i:
                    continue
                ws = self.sockets.get((lobby.id, pid))
                if ws is None:
                    continue
                try:
                    await ws.send_json(msg)
                except Exception:  # socket died; the receive loop will clean up
                    pass

    async def send(self, ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    # ---------- websocket session ----------
    async def session(self, ws: WebSocket, token: str) -> None:
        await ws.accept()
        found = self.resolve(token)
        if not found:
            await ws.send_json({"t": "invalid_session"})
            await ws.close(code=4001)
            return
        lobby, player = found
        key = (lobby.id, player.id)
        old = self.sockets.get(key)
        if old is not None:
            try:
                await old.close(code=4002)
            except Exception:
                pass
        self.sockets[key] = ws
        player.connected = True
        t = now()
        lobby.last_activity = t
        await self.deliver(lobby, [([p.id], view_for(lobby, p, t)) for p in lobby.players.values()])
        try:
            while True:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    continue
                if await self.handle(ws, lobby, player, msg) == "left":
                    return
        except Exception:
            pass
        finally:
            if self.sockets.get(key) is ws:
                del self.sockets[key]
                if player.id in lobby.players:
                    player.connected = False
                    t = now()
                    await self.deliver(lobby, [([p.id], view_for(lobby, p, t)) for p in lobby.players.values()])

    async def handle(self, ws: WebSocket, lobby: Lobby, player: Player, msg: dict) -> Optional[str]:
        t = now()
        lobby.last_activity = t
        kind = msg.get("t")
        try:
            if kind == "ping":
                await self.send(ws, {"t": "pong", "server_time": t, "client_time": msg.get("client_time")})
            elif kind == "pos":
                ok, out = lobby.update_position(player.id, msg.get("lat"), msg.get("lon"), msg.get("acc"), t)
                await self.send(ws, {"t": "pos_ack", "accepted": ok})
                await self.deliver(lobby, out)
            elif kind == "set_team":
                await self.deliver(lobby, lobby.set_team(player.id, msg.get("player_id"), msg.get("team"), t))
            elif kind == "set_settings":
                await self.deliver(lobby, lobby.update_settings(player.id, msg.get("settings") or {}, t))
            elif kind == "kick":
                if player.id != lobby.host_id or lobby.phase != LOBBY:
                    raise GameError("Only the host can remove players in the lobby")
                await self.remove(lobby, str(msg.get("player_id")), notify="You were removed from the lobby")
            elif kind == "start":
                await self.deliver(lobby, lobby.start(player.id, t))
            elif kind == "catch_list":
                cands = lobby.catch_candidates(player.id, t)
                await self.send(ws, {"t": "catch_candidates", "hiders": [{"id": h.id, "name": h.name} for h in cands]})
            elif kind == "catch_request":
                await self.deliver(lobby, lobby.request_catch(player.id, str(msg.get("hider_id")), t))
            elif kind == "catch_reply":
                await self.deliver(lobby, lobby.reply_catch(player.id, str(msg.get("catch_id")), bool(msg.get("accept")), t))
            elif kind == "chat":
                await self.deliver(lobby, lobby.send_chat(player.id, str(msg.get("to")), msg.get("text"), t))
            elif kind == "leave":
                await self.remove(lobby, player.id)
                await self.send(ws, {"t": "left"})
                await ws.close()
                return "left"
            else:
                raise GameError("Unknown message")
        except GameError as e:
            await self.send(ws, {"t": "error", "msg": str(e)})
        return None

    async def remove(self, lobby: Lobby, pid: str, notify: Optional[str] = None) -> None:
        p = lobby.players.get(pid)
        if p is None:
            return
        self.tokens.pop(p.token, None)
        ws = self.sockets.pop((lobby.id, pid), None)
        out = lobby.remove_player(pid, now())
        if ws is not None and notify:
            await self.send(ws, {"t": "kicked", "msg": notify})
            try:
                await ws.close()
            except Exception:
                pass
        if not lobby.players:
            self.drop_lobby(lobby)
            return
        await self.deliver(lobby, out)

    def drop_lobby(self, lobby: Lobby) -> None:
        self.lobbies.pop(lobby.id, None)
        for p in lobby.players.values():
            self.tokens.pop(p.token, None)
            self.sockets.pop((lobby.id, p.id), None)

    # ---------- background loop ----------
    async def tick_forever(self) -> None:
        while True:
            try:
                await self.tick_once(now())
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(1)

    async def tick_once(self, t: float) -> None:
        for lobby in list(self.lobbies.values()):
            await self.deliver(lobby, lobby.tick(t))
            anyone = any(p.connected for p in lobby.players.values())
            idle = t - lobby.last_activity
            if (
                (lobby.phase == ENDED and t - (lobby.ended_at or t) > ENDED_TTL_S)
                or (lobby.phase == LOBBY and not anyone and idle > IDLE_TTL_S)
                or (not anyone and idle > INGAME_IDLE_TTL_S)
            ):
                self.drop_lobby(lobby)
