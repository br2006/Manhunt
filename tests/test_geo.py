import pytest

from server.geo import bearing_deg, haversine_m


def test_london_paris_distance_and_bearing():
    d = haversine_m(51.5074, -0.1278, 48.8566, 2.3522)
    assert d == pytest.approx(343_500, rel=0.01)
    assert bearing_deg(51.5074, -0.1278, 48.8566, 2.3522) == pytest.approx(148, abs=2)


def test_cardinal_bearings():
    assert bearing_deg(0, 0, 1, 0) == pytest.approx(0)
    assert bearing_deg(0, 0, 0, 1) == pytest.approx(90)
    assert bearing_deg(0, 0, -1, 0) == pytest.approx(180)
    assert bearing_deg(0, 0, 0, -1) == pytest.approx(270)


def test_zero_distance():
    assert haversine_m(51.5, -0.1, 51.5, -0.1) == 0
