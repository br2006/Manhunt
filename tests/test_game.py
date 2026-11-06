import pytest

from server.game import (
    ACTIVE, ENDED, ENDED_MAP_INTERVAL_S, HEAD_START, HIDER, LAST_HIDER, SEEKER, STALE_FIX_S,
    GameError, Lobby, nearest, view_for,
)

T0 = 1_000_000.0
# ~111 m per 0.001 deg latitude
BASE = (51.5, -0.1)


def make_lobby(n_hiders=2, n_seekers=1, head_start=60, **settings):
    lobby = Lobby(id="L", name="test", created_at=T0)
    lobby.settings.update({"head_start_s": head_start, "min_players": 2, **settings})
    players = {}
    for i in range(n_hiders):
        players[f"h{i}"] = lobby.add_player(f"hider{i}", T0)
    for i in range(n_seekers):
        players[f"s{i}"] = lobby.add_player(f"seeker{i}", T0)
    host = players["h0"]
    for key, p in players.items():
        lobby.set_team(host.id, p.id, SEEKER if key.startswith("s") else HIDER, T0)
    # Hiders spread north of the base, seekers at the base.
    for i in range(n_hiders):
        lobby.update_position(players[f"h{i}"].id, BASE[0] + 0.01 * (i + 1), BASE[1], 10, T0)
    for i in range(n_seekers):
        # Slightly different longitude so the leak check can't match by coincidence.
        lobby.update_position(players[f"s{i}"].id, BASE[0], BASE[1] - 0.0001 * (i + 1), 10, T0)
    return lobby, players, host


def move(lobby, p, lat, lon, t):
    lobby.update_position(p.id, lat, lon, 10, t)


def collect_numbers(obj, out=None):
    out = set() if out is None else out
    if isinstance(obj, dict):
        for v in obj.values():
            collect_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_numbers(v, out)
    elif isinstance(obj, float):
        out.add(obj)
    return out


def assert_no_leaks(lobby, outbox):
    """Seekers must never receive hider coordinates and vice versa."""
    for recips, msg in outbox:
        for pid in recips:
            me = lobby.players.get(pid)
            if me is None:
                continue
            nums = collect_numbers(msg)
            other = HIDER if me.team == SEEKER else SEEKER
            for p in lobby.team(other):
                assert p.lat not in nums and p.lon not in nums, (me.team, msg)


# ---------- lobby / start ----------
def test_host_only_actions_and_start_conditions():
    lobby, ps, host = make_lobby()
    with pytest.raises(GameError):
        lobby.set_team(ps["s0"].id, ps["h1"].id, SEEKER, T0)
    with pytest.raises(GameError):
        lobby.start(ps["s0"].id, T0)
    assert lobby.start_problem(T0) is None

    lobby.set_team(host.id, ps["s0"].id, HIDER, T0)
    assert "seeker" in lobby.start_problem(T0)
    lobby.set_team(host.id, ps["s0"].id, SEEKER, T0)

    ps["h1"].last_fix_at = None
    assert "GPS" in lobby.start_problem(T0)


def test_settings_clamped_and_host_only():
    lobby, ps, host = make_lobby()
    lobby.update_settings(host.id, {"catch_radius_m": 99999, "compass_interval_s": "7"}, T0)
    assert lobby.settings.catch_radius_m == 1000
    assert lobby.settings.compass_interval_s == 7
    with pytest.raises(GameError):
        lobby.update_settings(ps["s0"].id, {"catch_radius_m": 10}, T0)


def test_duplicate_nicknames_rejected():
    lobby, _, _ = make_lobby()
    with pytest.raises(GameError):
        lobby.add_player("HIDER0", T0)


def test_bad_accuracy_rejected():
    lobby, ps, _ = make_lobby()
    ok, _ = lobby.update_position(ps["h0"].id, 1, 1, 500, T0)
    assert not ok and ps["h0"].lat != 1


# ---------- phases & broadcasts ----------
def test_head_start_then_active_and_broadcast_intervals():
    lobby, ps, host = make_lobby(head_start=60, hider_map_interval_s=120, compass_interval_s=10)
    out = lobby.start(host.id, T0)
    assert lobby.phase == HEAD_START
    kinds = [m["t"] for _, m in out]
    assert "hider_map" in kinds and "seeker_map" in kinds and "seeker_distance" in kinds
    assert "compass" not in kinds  # no compass during head start
    assert_no_leaks(lobby, out)

    out = lobby.tick(T0 + 30)
    assert not any(m["t"] == "hider_map" for _, m in out)

    out = lobby.tick(T0 + 60)
    assert lobby.phase == ACTIVE
    compass = [m for r, m in out if m["t"] == "compass"]
    assert len(compass) == 1 and compass[0]["available"]
    assert compass[0]["bearing_deg"] == pytest.approx(0, abs=1)        # hiders are due north
    assert compass[0]["distance_m"] == pytest.approx(1112, rel=0.01)   # nearest is 0.01° away
    assert_no_leaks(lobby, out)

    out = lobby.tick(T0 + 120)
    assert any(m["t"] == "hider_map" for _, m in out)
    assert_no_leaks(lobby, out)


def test_view_for_never_leaks_other_team_positions():
    lobby, ps, host = make_lobby()
    lobby.start(host.id, T0)
    lobby.tick(T0 + 60)
    for p in lobby.players.values():
        v = view_for(lobby, p, T0 + 61)
        assert_no_leaks(lobby, [([p.id], v)])
    seeker_view = view_for(lobby, ps["s0"], T0 + 61)
    assert "hider_map" not in seeker_view and "seeker_distance" not in seeker_view
    hider_view = view_for(lobby, ps["h0"], T0 + 61)
    assert "seeker_map" not in hider_view and "compass" not in hider_view


def test_nearest_ignores_stale_fixes():
    lobby, ps, _ = make_lobby()
    ps["h0"].last_fix_at = T0 - STALE_FIX_S - 1
    n = nearest(ps["s0"], lobby.team(HIDER), T0)
    assert n[0] is ps["h1"]


# ---------- catching ----------
def test_catch_blocked_during_head_start():
    lobby, ps, host = make_lobby()
    lobby.start(host.id, T0)
    with pytest.raises(GameError):
        lobby.catch_candidates(ps["s0"].id, T0 + 1)


def test_catch_out_of_range_rejected():
    lobby, ps, host = make_lobby(head_start=0)
    lobby.start(host.id, T0)
    assert lobby.catch_candidates(ps["s0"].id, T0) == []
    with pytest.raises(GameError):
        lobby.request_catch(ps["s0"].id, ps["h0"].id, T0)


def test_catch_confirm_converts_and_seekers_win():
    lobby, ps, host = make_lobby(head_start=0, catch_radius_m=75)
    lobby.start(host.id, T0)
    s, h0, h1 = ps["s0"], ps["h0"], ps["h1"]

    move(lobby, s, h0.lat - 0.0003, h0.lon, T0 + 5)  # ~33 m away
    assert [h.id for h in lobby.catch_candidates(s.id, T0 + 5)] == [h0.id]
    out = lobby.request_catch(s.id, h0.id, T0 + 5)
    prompt = next(m for r, m in out if m["t"] == "catch_prompt")
    assert view_for(lobby, h0, T0 + 6)["catch_prompt"]["catch_id"] == prompt["catch_id"]

    with pytest.raises(GameError):  # only the target hider can reply
        lobby.reply_catch(h1.id, prompt["catch_id"], True, T0 + 6)
    lobby.reply_catch(h0.id, prompt["catch_id"], True, T0 + 6)
    assert h0.team == SEEKER and h0.caught_by == s.name
    assert lobby.phase == ACTIVE

    move(lobby, h0, h1.lat, h1.lon + 0.0002, T0 + 10)  # new seeker next to the last hider
    out = lobby.request_catch(h0.id, h1.id, T0 + 10)
    cid = next(m for r, m in out if m["t"] == "catch_prompt")["catch_id"]
    out = lobby.reply_catch(h1.id, cid, True, T0 + 11)
    assert lobby.phase == ENDED and lobby.winner == SEEKER
    assert any(m["t"] == "game_over" for _, m in out)
    assert [e["hider"] for e in lobby.events] == ["hider0", "hider1"]


def test_catch_deny_and_timeout():
    lobby, ps, host = make_lobby(head_start=0, catch_confirm_timeout_s=30)
    lobby.start(host.id, T0)
    s, h0 = ps["s0"], ps["h0"]
    move(lobby, s, h0.lat, h0.lon, T0 + 1)

    out = lobby.request_catch(s.id, h0.id, T0 + 1)
    cid = next(m for r, m in out if m["t"] == "catch_prompt")["catch_id"]
    out = lobby.reply_catch(h0.id, cid, False, T0 + 2)
    assert h0.team == HIDER
    assert next(m for r, m in out if m["t"] == "catch_result")["ok"] is False

    lobby.request_catch(s.id, h0.id, T0 + 3)
    with pytest.raises(GameError):
        lobby.request_catch(s.id, h0.id, T0 + 4)  # already pending
    out = lobby.tick(T0 + 40)
    assert not lobby.pending
    assert any(m["t"] == "catch_result" and not m["ok"] for _, m in out)
    assert h0.team == HIDER


def test_time_limit_hiders_win():
    lobby, ps, host = make_lobby(head_start=60, time_limit_s=600)
    lobby.start(host.id, T0)
    lobby.tick(T0 + 659)
    assert lobby.phase == ACTIVE
    lobby.tick(T0 + 660)
    assert lobby.phase == ENDED and lobby.winner == HIDER


def test_host_handover_on_leave():
    lobby, ps, host = make_lobby()
    lobby.remove_player(host.id, T0)
    assert lobby.host_id == ps["h1"].id


# ---------- team chat ----------
def test_chat_only_between_teammates_during_game():
    lobby, ps, host = make_lobby(head_start=60)
    h0, h1, s = ps["h0"], ps["h1"], ps["s0"]
    with pytest.raises(GameError):  # not in the lobby phase
        lobby.send_chat(h0.id, h1.id, "hi", T0)
    lobby.start(host.id, T0)

    out = lobby.send_chat(h0.id, h1.id, "  meet   at the tree ", T0 + 1)
    msg = next(m for r, m in out if m["t"] == "chat")
    assert next(r for r, m in out if m["t"] == "chat") == [h1.id]
    assert msg["text"] == "meet at the tree" and msg["from_name"] == h0.name
    assert_no_leaks(lobby, out)

    with pytest.raises(GameError):
        lobby.send_chat(h0.id, s.id, "👀", T0 + 2)   # other team
    with pytest.raises(GameError):
        lobby.send_chat(h0.id, h0.id, "me", T0 + 2)  # self
    with pytest.raises(GameError):
        lobby.send_chat(h0.id, h1.id, "   ", T0 + 2)  # empty
    long = lobby.send_chat(h0.id, h1.id, "x" * 500, T0 + 2)
    assert len(long[0][1]["text"]) == 200


def test_chat_rate_limited():
    lobby, ps, host = make_lobby()
    lobby.start(host.id, T0)
    h0, h1 = ps["h0"], ps["h1"]
    for i in range(5):
        lobby.send_chat(h0.id, h1.id, "👍", T0 + i * 0.1)
    with pytest.raises(GameError):
        lobby.send_chat(h0.id, h1.id, "👍", T0 + 1)
    lobby.send_chat(h0.id, h1.id, "👍", T0 + 11)  # window has passed


def test_chat_follows_team_change_after_catch():
    lobby, ps, host = make_lobby(head_start=0)
    lobby.start(host.id, T0)
    s, h0, h1 = ps["s0"], ps["h0"], ps["h1"]
    move(lobby, s, h0.lat, h0.lon, T0 + 1)
    cid = next(m for r, m in lobby.request_catch(s.id, h0.id, T0 + 1) if m["t"] == "catch_prompt")["catch_id"]
    lobby.reply_catch(h0.id, cid, True, T0 + 2)
    lobby.send_chat(h0.id, s.id, "I'm with you now", T0 + 3)
    with pytest.raises(GameError):
        lobby.send_chat(h0.id, h1.id, "sorry", T0 + 3)


# ---------- survival-time win rules ----------
def catch(lobby, seeker, hider, t):
    move(lobby, hider, hider.lat, hider.lon, t)   # phones keep reporting, so fixes stay fresh
    move(lobby, seeker, hider.lat, hider.lon, t)
    out = lobby.request_catch(seeker.id, hider.id, t)
    cid = next(m for r, m in out if m["t"] == "catch_prompt")["catch_id"]
    return lobby.reply_catch(hider.id, cid, True, t)


def test_catch_after_survival_time_leaves_last_hider_winner():
    lobby, ps, host = make_lobby(n_hiders=3, head_start=0, survival_time_s=600, time_limit_s=3600)
    lobby.start(host.id, T0)
    catch(lobby, ps["s0"], ps["h0"], T0 + 100)      # before survival: game continues with 2
    assert lobby.phase == ACTIVE
    out = lobby.tick(T0 + 600)                       # survival reached, 2 left -> keep playing
    assert lobby.phase == ACTIVE
    assert [m["hiders_left"] for _, m in out if m["t"] == "survival_reached"] == [2]
    assert not any(m["t"] == "survival_reached" for _, m in lobby.tick(T0 + 601))  # announced once

    out = catch(lobby, ps["s0"], ps["h1"], T0 + 700)  # drops to 1 after survival -> h2 wins alone
    assert lobby.phase == ENDED
    assert (lobby.winner, lobby.winner_id) == (LAST_HIDER, ps["h2"].id)
    over = next(m for _, m in out if m["t"] == "game_over")
    assert over["winner_name"] == ps["h2"].name


def test_single_hider_left_before_survival_wins_when_survival_time_hits():
    lobby, ps, host = make_lobby(n_hiders=2, head_start=60, survival_time_s=600)
    lobby.start(host.id, T0)
    lobby.tick(T0 + 60)
    catch(lobby, ps["s0"], ps["h0"], T0 + 100)
    lobby.tick(T0 + 659)
    assert lobby.phase == ACTIVE                     # survival measured from end of head start
    lobby.tick(T0 + 660)
    assert (lobby.phase, lobby.winner, lobby.winner_id) == (ENDED, LAST_HIDER, ps["h1"].id)


def test_all_caught_before_survival_seekers_win():
    lobby, ps, host = make_lobby(n_hiders=2, head_start=0, survival_time_s=600)
    lobby.start(host.id, T0)
    catch(lobby, ps["s0"], ps["h0"], T0 + 100)
    catch(lobby, ps["s0"], ps["h1"], T0 + 200)
    assert (lobby.winner, lobby.winner_id) == (SEEKER, None)


def test_hard_limit_with_several_hiders_is_team_win():
    lobby, ps, host = make_lobby(n_hiders=3, head_start=0, survival_time_s=600, time_limit_s=1200)
    lobby.start(host.id, T0)
    lobby.tick(T0 + 1200)
    assert (lobby.winner, lobby.winner_id) == (HIDER, None)


# ---------- after the game ----------
def test_everyone_sees_everyone_after_game_over():
    lobby, ps, host = make_lobby(n_hiders=2, head_start=0, time_limit_s=600, survival_time_s=600)
    lobby.start(host.id, T0)
    for p in lobby.players.values():  # nothing all-revealing while playing
        assert "all_map" not in view_for(lobby, p, T0 + 1)

    out = lobby.tick(T0 + 600)  # hard limit, 2 hiders -> hiders win
    assert lobby.phase == ENDED
    everyone = {p.id for p in lobby.players.values()}
    for recips, msg in out:
        if msg["t"] == "state":
            assert {e["id"] for e in msg["all_map"]} == everyone
            assert all(e["lat"] is not None for e in msg["all_map"])

    # Live positions keep being shared every ENDED_MAP_INTERVAL_S.
    move(lobby, ps["s0"], 51.6, -0.2, T0 + 605)
    assert lobby.tick(T0 + 605) == []
    out = lobby.tick(T0 + 600 + ENDED_MAP_INTERVAL_S)
    (recips, msg), = out
    assert msg["t"] == "all_map" and set(recips) == everyone
    s_entry = next(e for e in msg["players"] if e["id"] == ps["s0"].id)
    assert (s_entry["lat"], s_entry["lon"], s_entry["team"]) == (51.6, -0.2, SEEKER)

    # Leavers disappear from the map in state views.
    lobby.remove_player(ps["h1"].id, T0 + 620)
    assert ps["h1"].id not in {e["id"] for e in view_for(lobby, ps["h0"], T0 + 620)["all_map"]}


def test_anyone_can_chat_after_game_over():
    lobby, ps, host = make_lobby(head_start=0, time_limit_s=600, survival_time_s=600)
    lobby.start(host.id, T0)
    lobby.tick(T0 + 600)
    out = lobby.send_chat(ps["h0"].id, ps["s0"].id, "meet at the gate", T0 + 601)
    assert next(r for r, m in out if m["t"] == "chat") == [ps["s0"].id]


# ---------- lobby password ----------
def test_lobby_password_hashing():
    lobby = Lobby(id="P", name="pw", created_at=T0)
    assert not lobby.locked and lobby.check_password("anything")
    lobby.set_password("  secret  ")          # surrounding whitespace ignored
    assert lobby.locked and lobby.password_hash != b"secret"
    assert lobby.check_password("secret") and lobby.check_password(" secret")
    assert not lobby.check_password("Secret") and not lobby.check_password("")
    assert "secret" not in repr(lobby)
    with pytest.raises(GameError):
        lobby.set_password("x" * 65)
    lobby.set_password("")
    assert not lobby.locked
