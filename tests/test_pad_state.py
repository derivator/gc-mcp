"""Tests for GameCube pad-state validation and API<->Pipes conversion."""

from __future__ import annotations

import pytest

from gc_mcp.controllers import pad_state as ps


class TestValidate:
    def test_button_canonicalises_case(self) -> None:
        assert ps.validate_button("a") == "A"
        assert ps.validate_button("start") == "START"
        assert ps.validate_button("D_UP") == "D_UP"

    def test_button_invalid(self) -> None:
        with pytest.raises(ValueError):
            ps.validate_button("bogus")

    def test_port_valid(self) -> None:
        assert ps.validate_port(0) == 0
        assert ps.validate_port(3) == 3

    def test_port_invalid(self) -> None:
        for bad in (-1, 4, 9):
            with pytest.raises(ValueError):
                ps.validate_port(bad)

    def test_stick_valid(self) -> None:
        assert ps.validate_stick("MAIN") == "main"
        assert ps.validate_stick("c") == "c"

    def test_stick_invalid(self) -> None:
        with pytest.raises(ValueError):
            ps.validate_stick("nope")

    def test_trigger_valid(self) -> None:
        assert ps.validate_trigger("l") == "L"
        assert ps.validate_trigger("R") == "R"

    def test_trigger_invalid(self) -> None:
        with pytest.raises(ValueError):
            ps.validate_trigger("x")


class TestConversion:
    def test_stick_center(self) -> None:
        assert ps.api_stick_to_pipe(0.0) == pytest.approx(0.5)

    def test_stick_extremes(self) -> None:
        assert ps.api_stick_to_pipe(-1.0) == pytest.approx(0.0)
        assert ps.api_stick_to_pipe(1.0) == pytest.approx(1.0)

    def test_stick_roundtrip(self) -> None:
        for api in (-1.0, -0.5, 0.0, 0.25, 1.0):
            pipe = ps.api_stick_to_pipe(api)
            assert ps.pipe_stick_to_api(pipe) == pytest.approx(api, abs=1e-6)

    def test_stick_clamped(self) -> None:
        assert ps.api_stick_to_pipe(2.0) == pytest.approx(1.0)
        assert ps.api_stick_to_pipe(-2.0) == pytest.approx(0.0)

    def test_trigger_identity(self) -> None:
        assert ps.api_trigger_to_pipe(0.0) == pytest.approx(0.0)
        assert ps.api_trigger_to_pipe(1.0) == pytest.approx(1.0)
        assert ps.api_trigger_to_pipe(0.7) == pytest.approx(0.7)
        assert ps.api_trigger_to_pipe(-0.1) == pytest.approx(0.0)
        assert ps.api_trigger_to_pipe(1.5) == pytest.approx(1.0)


class TestPadState:
    def test_to_dict(self) -> None:
        state = ps.PadState(port=1, buttons={"A", "START"}, main=(0.5, -1.0),
                            c=(-0.25, 0.0), l=1.0, r=0.0)
        d = state.to_dict()
        assert d["port"] == 1
        assert d["buttons"] == ["A", "START"]
        assert d["main_stick"] == {"x": 0.5, "y": -1.0}
        assert d["c_stick"] == {"x": -0.25, "y": 0.0}
        assert d["l_trigger"] == 1.0
        assert d["r_trigger"] == 0.0

    def test_default_state_neutral(self) -> None:
        d = ps.PadState(port=0).to_dict()
        assert d["main_stick"] == {"x": 0.0, "y": 0.0}
        assert d["c_stick"] == {"x": 0.0, "y": 0.0}
        assert d["l_trigger"] == 0.0
        assert d["buttons"] == []
