from pathlib import Path

import pytest

import chessclock as cc

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_parse_colon_stages_with_fischer_increment():
    control = cc.parse_control("40/5400:1800+30")
    assert control.stages == (cc.Stage(40, 5_400_000), cc.Stage(None, 1_800_000))
    assert control.increment_ms == 30_000
    assert control.kind == "Fischer"


def test_parse_delay_and_sudden_death():
    assert cc.parse_control("180d3").delay_ms == 3_000
    assert cc.parse_control("180d3").kind == "Bronstein/delay"
    sudden = cc.parse_control("90")
    assert sudden.stages == (cc.Stage(None, 90_000),)
    assert sudden.kind == "sudden-death"


@pytest.mark.parametrize("text", [
    "40/5400,1800,900",   # open-ended stage must be last
    "0/300",              # move count must be at least 1
    "abc",                # not a time control
    "300.0005",           # sub-millisecond precision
    "300+5d3",            # increment and delay together
    "40/",                # missing seconds
])
def test_parse_rejects_bad_controls(text):
    with pytest.raises(cc.ControlError):
        cc.parse_control(text)


def test_additions_place_time_before_the_right_move():
    assert cc.additions(cc.parse_control("40/5400:1800")) == {41: 1_800_000}
    assert cc.additions(cc.parse_control("40/5400,20/1800,900")) == {41: 1_800_000, 61: 900_000}
    assert cc.additions(cc.parse_control("300+5")) == {}


def test_fischer_flags_on_move_43_at_12s_per_move():
    # Each move nets -7 s (12 s spent, 5 s back), so the clock is 6 s before move 43.
    flag, history = cc.forecast(cc.parse_control("300+5"), 12_000, 60)
    assert flag == 43
    assert history[-1] == (42, 6_000)


def test_delay_flags_on_move_26_at_10s_per_move():
    # Each move charges 7 s beyond the 3 s delay, so 180 s runs out on move 26.
    flag, _ = cc.forecast(cc.parse_control("180d3"), 10_000, 60)
    assert flag == 26


def test_delay_never_flags_when_thinks_fit_inside_the_delay():
    flag, history = cc.forecast(cc.parse_control("180d3"), 3_000, 60)
    assert flag is None
    assert history[-1] == (60, 180_000)


def test_sudden_death_flags_on_move_12_at_8s_per_move():
    flag, _ = cc.forecast(cc.parse_control("90"), 8_000, 60)
    assert flag == 12


def test_addition_is_applied_on_move_41():
    control = cc.parse_control("40/5400:1800+30")
    flag, history = cc.forecast(control, 45_000, 60)
    assert flag is None
    assert history[39] == (40, 4_800_000)   # 90 min less 40 moves at -15 s
    assert history[40] == (41, 6_585_000)   # +30 min added, then one net -15 s move


def test_infer_used_fischer_and_delay():
    fischer = cc.parse_control("300+5")
    assert cc.infer_used(fischer, 300_000, 293_000, 2, 1) == (12_000, True)
    assert cc.infer_used(fischer, 300_000, 300_000, 1, 1) == (0, False)  # clock not started yet

    delay = cc.parse_control("180d3")
    assert cc.infer_used(delay, 180_000, 173_000, 2, 1) == (10_000, True)
    assert cc.infer_used(delay, 180_000, 180_000, 2, 1) == (0, False)    # inside the delay

    with pytest.raises(cc.ControlError, match="line 7"):
        cc.infer_used(fischer, 300_000, 306_000, 2, 7)                   # rose beyond the increment


def test_read_pgn_skips_variations_and_counts_main_line_only():
    text = (EXAMPLES / "club-game.pgn").read_text(encoding="utf-8")
    headers, header_lines, body, first_line = cc.read_pgn(text)
    clocks, sans = cc.read_clocks(body, first_line)
    assert headers["TimeControl"] == "300+5"
    assert header_lines["TimeControl"] == 8
    assert first_line == 10
    assert len(clocks) == 10
    assert sans == 10
    assert all(ms != 1_000 for ms, _ in clocks)  # the 0:00:01 clock sits inside a variation


def test_replay_of_club_game_matches_hand_computed_think_times():
    text = (EXAMPLES / "club-game.pgn").read_text(encoding="utf-8")
    headers, _, body, first_line = cc.read_pgn(text)
    clocks, _ = cc.read_clocks(body, first_line)
    ada, ben = cc.replay(headers, clocks, cc.parse_control("300+5"), 60_000)
    assert ada.total_ms == 76_000          # 0 + 12 + 20 + 4 + 40
    assert ada.longest == (40_000, 5, 13)  # 5. O-O on line 13
    assert ada.free_moves == 1             # 1. e4 left the clock at 5:00
    assert ben.total_ms == 85_000          # 9 + 6 + 30 + 15 + 25
    assert ben.longest == (30_000, 3, 12)  # 3... a6 on line 12
    assert ben.lowest == 240_000


def test_fmt_ms_and_to_ms():
    assert cc.fmt_ms(5_400_000) == "1:30:00"
    assert cc.fmt_ms(135_000) == "2:15"
    assert cc.fmt_ms(1_500) == "0:01.5"
    assert cc.to_ms("1.5") == 1_500
    assert cc.to_ms("0.001") == 1


def test_lint_reports_line_numbers():
    text = "a = 300+5\nb 300\nc = 40/5400,1800,900\na = 90\n"
    games, errors = cc.read_games(text)
    assert [g.name for g in games] == ["a"]
    assert errors[0][0] == 2 and "name = CONTROL" in errors[0][1]
    assert errors[1][0] == 3 and "only the last stage" in errors[1][1]
    assert errors[2] == (4, "duplicate game name 'a'")


def test_check_exit_codes(tmp_path, capsys):
    passing = tmp_path / "pass.txt"
    passing.write_text("good = 300+5 avg=5\n", encoding="utf-8")
    failing = tmp_path / "fail.txt"
    failing.write_text("bad = 90 avg=8\n", encoding="utf-8")
    broken = tmp_path / "broken.txt"
    broken.write_text("oops = nope\n", encoding="utf-8")

    assert cc.main(["check", str(passing)]) == cc.EXIT_OK
    assert cc.main(["check", str(failing)]) == cc.EXIT_GATE
    assert "Flag fall on move 12" in capsys.readouterr().out
    assert cc.main(["check", str(broken)]) == cc.EXIT_INPUT


def test_simulation_is_seeded_and_bounded(capsys):
    control = cc.parse_control("300+5")
    first = cc.simulate(control, 12_000, 200, 60, seed=9)
    second = cc.simulate(control, 12_000, 200, 60, seed=9)
    assert first == second
    assert cc.simulate(cc.parse_control("3600"), 1_000, 50, 60, seed=1) == []
    assert len(cc.simulate(cc.parse_control("1"), 60_000, 50, 60, seed=1)) == 50


def test_replay_missing_clock_is_an_input_error(tmp_path, capsys):
    pgn = tmp_path / "missing.pgn"
    pgn.write_text(
        '[White "A"]\n[Black "B"]\n[TimeControl "300+5"]\n\n'
        "1. e4 {[%clk 0:05:00]} 1... e5 2. Nf3 {[%clk 0:04:53]} *\n",
        encoding="utf-8",
    )
    assert cc.main(["replay", str(pgn)]) == cc.EXIT_INPUT
    assert "every move needs one" in capsys.readouterr().err


def test_replay_warns_when_header_differs_from_override(capsys):
    code = cc.main(["replay", str(EXAMPLES / "club-game.pgn"), "--control", "180+2"])
    out = capsys.readouterr().out
    assert code == cc.EXIT_OK
    assert "warning: TimeControl tag '300+5' differs from --control '180+2'" in out


def test_timeline_shows_per_move_budget(capsys):
    assert cc.main(["timeline", "40/5400:1800+30"]) == cc.EXIT_OK
    out = capsys.readouterr().out
    assert "Stage 1: moves 1-40, start clock 1:30:00, 135.000 s per move" in out
    assert "Stage 2: from move 41 to the end, time added 30:00" in out
