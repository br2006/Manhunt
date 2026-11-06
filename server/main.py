"""FastAPI entrypoint: REST for lobby list/create/join, one WebSocket for everything else."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .game import GameError, PasswordError
from .manager import Manager, TooManyAttempts

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
    password: str = ""  # blank = open lobby


class JoinBody(BaseModel):
    nickname: str
    password: str = ""


def session_reply(lobby, player) -> dict:
    return {"lobby_id": lobby.id, "player_id": player.id, "token": player.token}


def http_error(e: GameError) -> HTTPException:
    code = 429 if isinstance(e, TooManyAttempts) else 403 if isinstance(e, PasswordError) else 400
    return HTTPException(code, str(e))


@app.get("/api/lobbies")
def list_lobbies():
    return manager.open_lobbies()


@app.post("/api/lobbies")
async def create_lobby(body: CreateBody):
    try:
        return session_reply(*await manager.create_lobby(body.lobby_name, body.nickname, body.password))
    except GameError as e:
        raise http_error(e)


@app.post("/api/lobbies/{lobby_id}/join")
async def join_lobby(lobby_id: str, body: JoinBody, request: Request):
    client = request.client.host if request.client else "?"
    try:
        return session_reply(*await manager.join_lobby(lobby_id, body.nickname, body.password, client))
    except GameError as e:
        raise http_error(e)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = ""):
    await manager.session(ws, token)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
