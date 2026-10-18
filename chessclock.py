#!/usr/bin/env python3
"""Chessclock: offline chess time-control planner, clock simulator and PGN clock replayer.

Standard library only. Every clock value is an integer number of milliseconds,
so budgets and replayed think times are exact. Simulated think times are
estimates and are labelled as such in the output.

Time-control notation (a superset of the PGN TimeControl tag):

    40/5400            40 moves in 90 minutes, then no more time is added
    40/5400:1800       as above, then 30 minutes added for the rest of the game
    40/5400,1800       same as the colon form; commas are accepted too
    300+5              sudden death with a 5 s Fischer increment after each move
    180d3              Bronstein (or simple) delay of 3 s per move
    90                 sudden death, 90 s on the clock
"""

from __future__ import annotations

import argparse
import random
import re
import statistics
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

EXIT_OK = 0
EXIT_GATE = 1
EXIT_INPUT = 2

DEFAULT_MOVES = 60
DEFAULT_TROUBLE_S = "60"
DEFAULT_GAMES = 1000

CONTROL_RE = re.compile(r"([0-9./,:]+)(?:\+([0-9.]+)|[dD]([0-9.]+))?")
TAG_RE = re.compile(r'\[(\w+)\s+"([^"]*)"\]')
TAG_START_RE = re.compile(r'^\[\w+\s+"', re.MULTILINE)
CLK_RE = re.compile(r"\[%clk\s+((?:\d+:)?\d+:\d+(?:\.\d+)?)\]")
TOKEN_RE = re.compile(r"\{([^}]*)\}|([()])")
RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}
GAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$")
AVG_RE = re.compile(r"\s+avg=([0-9.]+)$")


class ControlError(ValueError):
    """Raised for any input that cannot be read or is internally inconsistent."""


@dataclass(frozen=True)
class Stage:
    moves: int | None  # None marks the open-ended final stage ("rest of game")
    ms: int


@dataclass(frozen=True)
class Control:
    text: str
    stages: tuple[Stage, ...]
    increment_ms: int = 0
    delay_ms: int = 0

    @property
    def kind(self) -> str:
        if self.increment_ms:
            return "Fischer"
        if self.delay_ms:
            return "Bronstein/delay"
        if len(self.stages) == 1 and self.stages[0].moves is None:
            return "sudden-death"
        return "classical"

    def key(self) -> tuple:
        """Identity of the timing rules, ignoring how the control was written."""
        return (self.stages, self.increment_ms, self.delay_ms)


@dataclass
class Game:
    line: int
    name: str
    control: Control
    avg_ms: int | None


@dataclass
class SideStats:
    name: str
    moves: int = 0
    total_ms: int = 0
    free_moves: int = 0  # moves inside the delay allowance; exact think time is not recoverable
    longest: tuple[int, int, int] = (0, 0, 0)  # (ms, move number, line)
    trouble: int = 0
    lowest: int | None = None
    flagged_on: int | None = None


# ---------------------------------------------------------------- time helpers

def to_ms(text: str) -> int:
    """Convert a decimal number of seconds to exact integer milliseconds."""
    try:
        value = Decimal(text) * 1000
    except InvalidOperation as exc:
        raise ControlError(f"not a number: {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise ControlError(f"not a valid time: {text!r}")
    if value != value.to_integral_value():
        raise ControlError(f"{text!r} has more than three decimal places")
    return int(value)


def clock_ms(text: str) -> int:
    """Read a PGN clock such as 0:04:59.8, 4:59 or 1:02:03 into milliseconds."""
    seconds = Decimal(0)
    for part in text.split(":"):
        seconds = seconds * 60 + Decimal(part)
    return int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def fmt_ms(ms: int) -> str:
    """Format milliseconds as h:mm:ss, m:ss, or with a fraction when one exists."""
    sign = "-" if ms < 0 else ""
    hours, rest = divmod(abs(ms), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, frac = divmod(rest, 1000)
    text = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    if frac:
        text += "." + f"{frac:03d}".rstrip("0")
    return sign + text


# ---------------------------------------------------------------- control model

def parse_control(text: str) -> Control:
    """Read a time control such as 40/5400:1800+30, 300+5, 180d3 or 90."""
    raw = text.strip().replace(" ", "")
    match = CONTROL_RE.fullmatch(raw)
    if not match:
        raise ControlError(f"cannot read time control {text.strip()!r}")
    body, inc, delay = match.groups()
    parts = body.replace(":", ",").split(",")
    stages = []
    for number, part in enumerate(parts, 1):
        last = number == len(parts)
        moves = None
        seconds = part
        if "/" in part:
            moves_text, seconds = part.split("/", 1)
            if not moves_text.isdigit() or int(moves_text) < 1:
                raise ControlError(f"stage {number}: move count must be a whole number of at least 1")
            moves = int(moves_text)
        if moves is None and not last:
            raise ControlError(f"stage {number}: only the last stage may leave out the move count")
        ms = to_ms(seconds) if seconds else 0
        if ms <= 0:
            raise ControlError(f"stage {number}: time must be more than zero")
        stages.append(Stage(moves, ms))
    return Control(
        text.strip(),
        tuple(stages),
        to_ms(inc) if inc else 0,
        to_ms(delay) if delay else 0,
    )


def additions(control: Control) -> dict[int, int]:
    """Map a player's move number to the time added to their clock before that move."""
    adds: dict[int, int] = {}
    first = 1
    for index, stage in enumerate(control.stages):
        if index:
            adds[first] = stage.ms
        if stage.moves is None:
            break
        first += stage.moves
    return adds


def play(control: Control, thinks: list[int]) -> tuple[int | None, list[tuple[int, int]]]:
    """Run one clock through think times in ms.

    Returns (move the clock ran out on or None, [(move, clock after move)]).
    """
    adds = additions(control)
    clock = control.stages[0].ms
    history: list[tuple[int, int]] = []
    for move, think in enumerate(thinks, 1):
        clock += adds.get(move, 0)
        charge = max(0, think - control.delay_ms) if control.delay_ms else think
        if charge >= clock:
            return move, history
        clock = clock - charge + control.increment_ms
        history.append((move, clock))
    return None, history


def timeline_lines(control: Control) -> list[str]:
    lines = []
    first = 1
    for number, stage in enumerate(control.stages, 1):
        label = "start clock" if number == 1 else "time added"
        if stage.moves is None:
            span = f"from move {first} to the end"
            per_move = ""
        else:
            span = f"moves {first}-{first + stage.moves - 1}"
            per_move = f", {stage.ms / stage.moves / 1000:.3f} s per move"
            first += stage.moves
        lines.append(f"Stage {number}: {span}, {label} {fmt_ms(stage.ms)}{per_move}")
    if control.increment_ms:
        lines.append(f"Every move: +{control.increment_ms / 1000:g} s Fischer increment")
    if control.delay_ms:
        lines.append(
            f"Every move: the first {control.delay_ms / 1000:g} s of thinking is free; "
            "only time beyond it is charged"
        )
    return lines


def explain_lines(control: Control) -> list[str]:
    lines = [f"Time control {control.text} ({control.kind})"]
    lines += ["  " + line for line in timeline_lines(control)]
    if control.stages[-1].moves is not None:
        counted = sum(stage.moves for stage in control.stages if stage.moves is not None)
        lines.append(f"  After move {counted}: no more time is added; play continues on the clock left")
    lines.append("  A player loses on time when their clock reaches zero.")
    return lines


# ---------------------------------------------------------------- projections

def forecast(control: Control, avg_ms: int, moves: int) -> tuple[int | None, list[tuple[int, int]]]:
    return play(control, [avg_ms] * moves)


def simulate(control: Control, mean_ms: int, games: int, moves: int, seed: int) -> list[int]:
    """Return the move number each simulated flag fall happened on (only flagged games)."""
    rng = random.Random(seed)
    flags = []
    for _ in range(games):
        thinks = [max(1, round(rng.expovariate(1 / mean_ms))) for _ in range(moves)]
        flag, _ = play(control, thinks)
        if flag is not None:
            flags.append(flag)
    return flags


# ---------------------------------------------------------------- PGN replay

def read_pgn(text: str) -> tuple[dict[str, str], dict[str, int], str, int]:
    """Split the first game of a PGN file into headers, header line numbers, movetext and its first line."""
    headers: dict[str, str] = {}
    header_lines: dict[str, int] = {}
    pos = 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        stripped = line.strip()
        if not stripped:
            pos += len(line)
            continue
        if not stripped.startswith("["):
            break
        match = TAG_RE.fullmatch(stripped)
        if not match:
            raise ControlError(f"line {number}: malformed PGN tag")
        headers[match[1]] = match[2]
        header_lines[match[1]] = number
        pos += len(line)
    body = text[pos:]
    later_game = TAG_START_RE.search(body)
    if later_game:
        body = body[: later_game.start()]
    return headers, header_lines, body, text.count("\n", 0, pos) + 1


def san_count(segment: str) -> int:
    count = 0
    for token in segment.split():
        token = re.sub(r"^\d+\.+", "", token)
        if not token or token in RESULTS or token.startswith("$"):
            continue
        count += 1
    return count


def read_clocks(body: str, first_line: int) -> tuple[list[tuple[int, int]], int]:
    """Return ([(clock after move in ms, line)], SAN move count) for the main line only."""
    clocks: list[tuple[int, int]] = []
    sans = 0
    depth = 0
    pos = 0
    for match in TOKEN_RE.finditer(body):
        if depth == 0:
            sans += san_count(body[pos:match.start()])
        pos = match.end()
        if match.group(2) == "(":
            depth += 1
        elif match.group(2) == ")":
            depth -= 1
            if depth < 0:
                raise ControlError(f"line {first_line + body.count(chr(10), 0, match.start())}: unbalanced ')'")
        elif depth == 0:
            clk = CLK_RE.search(match.group(1))
            if clk:
                line = first_line + body.count("\n", 0, match.start())
                clocks.append((clock_ms(clk.group(1)), line))
    if depth != 0:
        raise ControlError("unbalanced '(' in movetext")
    sans += san_count(body[pos:])
    return clocks, sans


def infer_used(control: Control, before: int, after: int, move: int, line: int) -> tuple[int, bool]:
    """Work out think time for one move from the clock before and after it.

    Returns (think ms, exact). Under delay timing a move inside the delay leaves
    the clock unchanged, so its think time is only known to be within the delay.
    """
    if move == 1 and after >= before:
        return 0, False  # clock had not started yet
    if control.delay_ms:
        spent = before - after
        if spent < 0:
            raise ControlError(f"line {line}: clock rose from {fmt_ms(before)} to {fmt_ms(after)} during a move")
        if spent == 0:
            return 0, False
        return spent + control.delay_ms, True
    if control.increment_ms:
        used = before + control.increment_ms - after
    else:
        used = before - after
    if used < 0:
        raise ControlError(
            f"line {line}: clock rose from {fmt_ms(before)} to {fmt_ms(after)}, "
            f"more than the {control.kind} timing allows"
        )
    return used, True


def replay(headers: dict[str, str], clocks: list[tuple[int, int]], control: Control,
           trouble_ms: int) -> list[SideStats]:
    adds = additions(control)
    clock = [control.stages[0].ms, control.stages[0].ms]
    sides = [SideStats(headers.get("White", "White")), SideStats(headers.get("Black", "Black"))]
    for ply, (after, line) in enumerate(clocks):
        side = ply % 2
        move = ply // 2 + 1
        stats = sides[side]
        before = clock[side] + adds.get(move, 0)
        used, exact = infer_used(control, before, after, move, line)
        stats.moves += 1
        stats.total_ms += used
        if not exact:
            stats.free_moves += 1
        if used > stats.longest[0]:
            stats.longest = (used, move, line)
        if after < trouble_ms:
            stats.trouble += 1
        stats.lowest = after if stats.lowest is None else min(stats.lowest, after)
        if after <= 0 and stats.flagged_on is None:
            stats.flagged_on = move
        clock[side] = after
    return sides


# ---------------------------------------------------------------- game files

def read_games(text: str) -> tuple[list[Game], list[tuple[int, str]]]:
    """Read 'name = CONTROL [avg=SECONDS]' lines. Returns (games, [(line, message)])."""
    games: list[Game] = []
    errors: list[tuple[int, str]] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        match = GAME_RE.match(line)
        if not match:
            errors.append((number, "expected 'name = CONTROL [avg=SECONDS]'"))
            continue
        name, rest = match[1], match[2]
        avg_text = None
        avg_match = AVG_RE.search(rest)
        if avg_match:
            avg_text = avg_match[1]
            rest = rest[: avg_match.start()]
        if name in seen:
            errors.append((number, f"duplicate game name {name!r}"))
            continue
        seen.add(name)
        try:
            control = parse_control(rest)
            avg_ms = None
            if avg_text is not None:
                avg_ms = to_ms(avg_text)
                if avg_ms <= 0:
                    raise ControlError("avg must be more than zero")
        except ControlError as exc:
            errors.append((number, str(exc)))
            continue
        games.append(Game(number, name, control, avg_ms))
    return games, errors


# ---------------------------------------------------------------- commands

def cmd_explain(args) -> int:
    print("\n".join(explain_lines(parse_control(args.control))))
    return EXIT_OK


def cmd_timeline(args) -> int:
    print("\n".join(timeline_lines(parse_control(args.control))))
    return EXIT_OK


def cmd_forecast(args) -> int:
    control = parse_control(args.control)
    avg = to_ms(args.avg)
    if avg <= 0:
        raise ControlError("--avg must be more than zero")
    if args.moves < 1:
        raise ControlError("--moves must be at least 1")
    flag, history = forecast(control, avg, args.moves)
    print(f"Control {control.text} ({control.kind}), each move takes {fmt_ms(avg)}")
    print(forecast_summary(flag, history, args.moves))
    return EXIT_OK


def forecast_summary(flag: int | None, history: list[tuple[int, int]], moves: int) -> str:
    if flag is not None:
        left = history[-1][1] if history else None
        note = f" (clock had {fmt_ms(left)} after move {flag - 1})" if left is not None else ""
        return f"Flag fall on move {flag}{note}"
    return f"Survives {moves} moves; clock after move {moves}: {fmt_ms(history[-1][1])}"


def cmd_simulate(args) -> int:
    control = parse_control(args.control)
    mean = to_ms(args.mean)
    if mean <= 0:
        raise ControlError("--mean must be more than zero")
    if args.games < 1 or args.moves < 1:
        raise ControlError("--games and --moves must be at least 1")
    flags = simulate(control, mean, args.games, args.moves, args.seed)
    print(f"Control {control.text} ({control.kind}), estimate with exponential thinking, "
          f"mean {fmt_ms(mean)}, seed {args.seed}")
    print(f"Games: {args.games}, moves per game: {args.moves}")
    print(f"Flag-fall rate: {len(flags) / args.games:.1%} ({len(flags)} games)")
    if flags:
        print(f"Median flag move: {statistics.median(flags):g}, earliest: {min(flags)}")
    else:
        print("No simulated game flagged within the moves given.")
    return EXIT_OK


def cmd_replay(args) -> int:
    path = Path(args.pgn)
    headers, header_lines, body, first_line = read_pgn(path.read_text(encoding="utf-8"))
    declared = headers.get("TimeControl", "-")
    trouble = to_ms(args.trouble)
    if args.control:
        control = parse_control(args.control)
    elif declared != "-":
        control = parse_control(declared)
    else:
        raise ControlError("no TimeControl tag in the PGN; pass --control")

    fen = headers.get("FEN", "")
    if fen and len(fen.split()) > 1 and fen.split()[1] == "b":
        raise ControlError("replay supports games that start with White to move")

    clocks, sans = read_clocks(body, first_line)
    if not clocks:
        raise ControlError("no %clk comments in the main line")
    if sans != len(clocks):
        raise ControlError(
            f"{path}:{first_line}: {sans} moves but {len(clocks)} %clk readings; every move needs one"
        )

    print(f"{path}: {headers.get('White', 'White')} vs {headers.get('Black', 'Black')}, "
          f"control {control.text} ({control.kind})")
    if args.control and declared != "-" and parse_control(declared).key() != control.key():
        line = header_lines.get("TimeControl", 0)
        print(f"{path}:{line}: warning: TimeControl tag {declared!r} differs from --control {control.text!r}")

    sides = replay(headers, clocks, control, trouble)
    for stats in sides:
        if not stats.moves:
            print(f"{stats.name}: no moves in the main line")
            continue
        print(f"{stats.name}: {stats.moves} moves, thinking {fmt_ms(stats.total_ms)} in total")
        used, move, line = stats.longest
        print(f"  longest think {fmt_ms(used)} on move {move} (line {line})")
        print(f"  moves with clock under {fmt_ms(trouble)}: {stats.trouble}; "
              f"lowest clock {fmt_ms(stats.lowest)}")
        if stats.free_moves:
            print(f"  moves with think time not measurable (clock not started, or inside the delay): "
                  f"{stats.free_moves}")
        if stats.flagged_on is not None:
            print(f"  clock ran out on move {stats.flagged_on}")
    return EXIT_OK


def cmd_lint(args) -> int:
    games, errors = read_games(Path(args.file).read_text(encoding="utf-8"))
    for line, message in errors:
        print(f"{args.file}:{line}: {message}")
    if errors:
        print(f"{len(errors)} error(s)")
        return EXIT_INPUT
    print(f"OK: {len(games)} game(s)")
    return EXIT_OK


def cmd_check(args) -> int:
    if args.moves < 1:
        raise ControlError("--moves must be at least 1")
    games, errors = read_games(Path(args.file).read_text(encoding="utf-8"))
    for line, message in errors:
        print(f"{args.file}:{line}: {message}")
    if errors:
        print(f"{len(errors)} error(s); no games checked")
        return EXIT_INPUT
    failures = 0
    for game in games:
        if game.avg_ms is None:
            print(f"SKIP {game.name}: no avg= given, syntax only")
            continue
        flag, history = forecast(game.control, game.avg_ms, args.moves)
        if flag is not None and flag <= args.moves:
            failures += 1
            print(f"FAIL {game.name} (line {game.line}): {forecast_summary(flag, history, args.moves)}; "
                  f"needed {args.moves} moves")
        else:
            print(f"OK   {game.name} (line {game.line}): {forecast_summary(flag, history, args.moves)}")
    if failures:
        print(f"{failures} game(s) would run out of time")
        return EXIT_GATE
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chessclock",
        description="Offline chess time-control planner, clock simulator and PGN clock replayer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("explain", help="explain a time control in plain English")
    p.add_argument("control")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("timeline", help="budget per stage for a time control")
    p.add_argument("control")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("forecast", help="run a clock at a constant average think time")
    p.add_argument("control")
    p.add_argument("--avg", required=True, metavar="SECONDS", help="average think time per move")
    p.add_argument("--moves", type=int, default=DEFAULT_MOVES)
    p.set_defaults(func=cmd_forecast)

    p = sub.add_parser("simulate", help="seeded Monte Carlo of flag falls")
    p.add_argument("control")
    p.add_argument("--mean", required=True, metavar="SECONDS", help="mean think time per move")
    p.add_argument("--games", type=int, default=DEFAULT_GAMES)
    p.add_argument("--moves", type=int, default=DEFAULT_MOVES)
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("replay", help="think times and time trouble from a PGN with %%clk comments")
    p.add_argument("pgn")
    p.add_argument("--control", help="override the TimeControl tag")
    p.add_argument("--trouble", default=DEFAULT_TROUBLE_S, metavar="SECONDS",
                   help="clock threshold for time trouble")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("lint", help="check a games file with line-numbered errors")
    p.add_argument("file")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("check", help="CI gate: every game with avg= must last --moves moves")
    p.add_argument("file")
    p.add_argument("--moves", type=int, default=DEFAULT_MOVES)
    p.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ControlError, OSError, UnicodeDecodeError) as exc:
        print(f"chessclock: {exc}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
