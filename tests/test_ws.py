"""End-to-end over real WebSockets using FastAPI's TestClient."""
from fastapi.testclient import TestClient

from server import main
from server.game import HIDER, SEEKER


def recv_until(ws, kind, limit=50):
    seen = []
    for _ in range(limit):
        m = ws.receive_json()
        seen.append(m)
        if m["t"] == kind:
            return m, seen
    raise AssertionError(f"never got {kind}; saw {[m['t'] for m in seen]}")


def test_full_game_over_websockets():
    main.manager.__init__()  # fresh state
    with TestClient(main.app) as client:
        host = client.post("/api/lobbies", json={"lobby_name": "Park", "nickname": "Alice"}).json()
        lid = host["lobby_id"]
        assert [l["name"] for l in client.get("/api/lobbies").json()] == ["Park"]
        bob = client.post(f"/api/lobbies/{lid}/join", json={"nickname": "Bob"}).json()
        cat = client.post(f"/api/lobbies/{lid}/join", json={"nickname": "Cat"}).json()
        assert client.post(f"/api/lobbies/{lid}/join", json={"nickname": "bob"}).status_code == 400

        with client.websocket_connect(f"/ws?token={host['token']}") as wa, \
             client.websocket_connect(f"/ws?token={bob['token']}") as wb, \
             client.websocket_connect(f"/ws?token={cat['token']}") as wc:
            for ws in (wa, wb, wc):
                recv_until(ws, "state")

            wa.send_json({"t": "set_settings", "settings": {"head_start_s": 0, "catch_radius_m": 100}})
            wa.send_json({"t": "set_team", "player_id": cat["player_id"], "team": SEEKER})
            # Non-host can't change teams
            wb.send_json({"t": "set_team", "player_id": bob["player_id"], "team": SEEKER})
            recv_until(wb, "error")

            wa.send_json({"t": "pos", "lat": 51.5100, "lon": -0.1000, "acc": 10})
            wb.send_json({"t": "pos", "lat": 51.5200, "lon": -0.1000, "acc": 10})
            wc.send_json({"t": "pos", "lat": 51.5000, "lon": -0.1000, "acc": 10})
            for ws in (wa, wb, wc):
                recv_until(ws, "pos_ack")

            wa.send_json({"t": "start"})
            state, _ = recv_until(wc, "compass")
            assert state["available"] and state["distance_m"] > 1000
            # The seeker never received any hider coordinates.
            _, seen_c = recv_until(wc, "seeker_map")
            for m in seen_c:
                assert "hider_map" not in m and m["t"] != "hider_map"

            # Cat walks up to Alice and catches her.
            wc.send_json({"t": "pos", "lat": 51.5101, "lon": -0.1000, "acc": 10})
            recv_until(wc, "pos_ack")
            wc.send_json({"t": "catch_list"})
            cands, _ = recv_until(wc, "catch_candidates")
            assert [h["name"] for h in cands["hiders"]] == ["Alice"]
            wc.send_json({"t": "catch_request", "hider_id": host["player_id"]})
            prompt, _ = recv_until(wa, "catch_prompt")
            wa.send_json({"t": "catch_reply", "catch_id": prompt["catch_id"], "accept": True})
            res, _ = recv_until(wc, "catch_result")
            assert res["ok"]
            st, _ = recv_until(wa, "state")
            assert st["you"]["team"] == SEEKER

            lobby = main.manager.lobbies[lid]
            assert [p.name for p in lobby.team(HIDER)] == ["Bob"]


def test_password_protected_lobby():
    main.manager.__init__()
    with TestClient(main.app) as client:
        host = client.post("/api/lobbies", json={"lobby_name": "Secret", "nickname": "Alice", "password": "hunter2"}).json()
        lid = host["lobby_id"]
        listed = client.get("/api/lobbies").json()
        assert listed[0]["locked"] is True and "password" not in str(listed).lower().replace("locked", "")

        join = lambda pw, nick="Bob": client.post(f"/api/lobbies/{lid}/join", json={"nickname": nick, "password": pw})
        r = join("")
        assert r.status_code == 403 and "needs a password" in r.json()["detail"]
        r = join("wrong")
        assert r.status_code == 403 and r.json()["detail"] == "Wrong password"
        assert join("hunter2").status_code == 200
        assert join(" hunter2 ", "Cat").status_code == 200   # stray spaces from phone keyboards are fine

        # Brute force is throttled: 5 wrong guesses per minute, then even the right one is refused.
        for _ in range(5):
            assert join("guess", "Dan").status_code == 403
        r = join("hunter2", "Dan")
        assert r.status_code == 429

        # Open lobbies still work without any password.
        open_l = client.post("/api/lobbies", json={"nickname": "Eve"}).json()
        assert client.post(f"/api/lobbies/{open_l['lobby_id']}/join", json={"nickname": "Fay"}).status_code == 200
