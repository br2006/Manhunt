"""FastAPI entrypoint: REST for lobby list/create/join, one WebSocket for everything else."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .game import GameError
from .manager import Manager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

manager = Manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(manager.tick_forever())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Manhunt", lifespan=lifespan)


class CreateBody(BaseModel):
    lobby_name: str = ""
    nickname: str


class JoinBody(BaseModel):
    nickname: str


def session_reply(lobby, player) -> dict:
    return {"lobby_id": lobby.id, "player_id": player.id, "token": player.token}


@app.get("/api/lobbies")
def list_lobbies():
    return manager.open_lobbies()


@app.post("/api/lobbies")
def create_lobby(body: CreateBody):
    try:
        return session_reply(*manager.create_lobby(body.lobby_name, body.nickname))
    except GameError as e:
        raise HTTPException(400, str(e))


@app.post("/api/lobbies/{lobby_id}/join")
async def join_lobby(lobby_id: str, body: JoinBody):
    try:
        return session_reply(*await manager.join_lobby(lobby_id, body.nickname))
    except GameError as e:
        raise HTTPException(400, str(e))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = ""):
    await manager.session(ws, token)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
