# Chessclock

**Plan chess clocks offline: explain any time control, budget every stage, forecast flag falls, simulate games, and replay think times from PGN clock comments.**

Chessclock is a single-file, standard-library Python tool for club organisers, tournament directors and players who want to know what a time control really means before the game starts. Every clock value is exact integer milliseconds, so budgets and replayed think times add up to the second. Nothing touches the network, and there are no accounts or API keys.

## Features

- **Reads the time controls you actually see**: classical `40/5400`, multi-stage `40/5400:1800`, Fischer `300+5`, Bronstein or simple delay `180d3`, and sudden death `90`.
- **Plain-English explanation** of any control, including when time is added and what happens after the last counted move (`explain`).
- **Per-stage budgets**: start clock, time added, and seconds per move in each stage (`timeline`).
- **Constant-pace forecast**: how long a clock lasts at a given average think time, and the move it flags on (`forecast`).
- **Seeded Monte Carlo simulation** of flag falls under exponential thinking times, reproducible with `--seed` (`simulate`). Results are estimates.
- **PGN clock replay**: reads `%clk` comments from the main line, skips variations, and infers each move's think time under the control (`replay`).
- **Exact think times** from Fischer, delay and sudden-death clocks. Moves where the clock was not started or sat inside a delay are labelled as not measurable, not guessed.
- **Time-trouble counts** per player, with a configurable threshold, plus the lowest clock and the longest think with its PGN line number.
- **TimeControl header check**: warns when a PGN's `TimeControl` tag disagrees with the control you pass in, with the header's line number.
- **Games file linter** with `file:line:` errors for malformed lines, bad controls, and duplicate names (`lint`).
- **CI gate** that fails when any game with an `avg=` would run out of time within a set number of moves. Exit codes: `0` pass, `1` gate failed, `2` bad input (`check`).
- **Accepts both PGN and comma stage syntax**: `40/5400:1800` (PGN style) and `40/5400,1800` both work.

## Quick start

Requires Python 3. No install step.

The outputs below were worked out by hand from the code for these inputs. Run the commands to confirm them on your machine.

```sh
python3 chessclock.py explain 40/5400:1800+30
```

```text
Time control 40/5400:1800+30 (Fischer)
  Stage 1: moves 1-40, start clock 1:30:00, 135.000 s per move
  Stage 2: from move 41 to the end, time added 30:00
  Every move: +30 s Fischer increment
  A player loses on time when their clock reaches zero.
```

Replay a game with clock comments:

```sh
python3 chessclock.py replay examples/club-game.pgn
```

```text
examples/club-game.pgn: Ada vs Ben, control 300+5 (Fischer)
Ada: 5 moves, thinking 1:16 in total
  longest think 0:40 on move 5 (line 13)
  moves with clock under 1:00: 0; lowest clock 4:04
  moves with think time not measurable (clock not started, or inside the delay): 1
Ben: 5 moves, thinking 1:25 in total
  longest think 0:30 on move 3 (line 12)
  moves with clock under 1:00: 0; lowest clock 4:00
```

Gate a games file in CI:

```sh
python3 chessclock.py check examples/games.txt
```

```text
OK   blitz-5-3 (line 7): Survives 60 moves; clock after move 60: 5:00
OK   rapid-classical (line 8): Survives 60 moves; clock after move 60: 1:45:00
OK   bullet (line 9): Survives 60 moves; clock after move 60: 0:30
OK   bronstein-rapid (line 10): Survives 60 moves; clock after move 60: 5:00
FAIL sudden-death (line 11): Flag fall on move 12 (clock had 0:02 after move 11); needed 60 moves
1 game(s) would run out of time
```

The exit status is `1` here, because `sudden-death` is in the file on purpose.

## Time-control syntax

| Control | Meaning |
| --- | --- |
| `40/5400` | 40 moves in 5400 s; no time is added afterwards |
| `40/5400:1800` | as above, then 1800 s added before move 41 for the rest of the game |
| `40/5400,20/1800,900` | three stages; the last stage must leave out its move count |
| `300+5` | Fischer: 5 s added after every move |
| `180d3` | Bronstein (or simple) delay: the first 3 s of each move is free |
| `90` | sudden death, 90 s on the clock |

Times accept up to three decimal places. Anything else is rejected with a message.

The `d` delay suffix is Chessclock's own notation. PGN files use `TimeControl` tags such as `300+5` or `40/5400:1800`, which Chessclock reads as well. A delay written as `dN` in a PGN tag will be read as Chessclock's delay syntax.

## Games file format

```text
# name = CONTROL [avg=SECONDS]
blitz-5-3 = 300+5 avg=5
rapid-classical = 40/5400:1800+30 avg=45
```

Lines starting with `#` are comments. `avg=` is optional and is used by `check`.

## How to run

| Command | What it does |
| --- | --- |
| `python3 chessclock.py explain CONTROL` | explain a control in plain English |
| `python3 chessclock.py timeline CONTROL` | budget per stage |
| `python3 chessclock.py forecast CONTROL --avg SECONDS [--moves 60]` | run a clock at a constant pace |
| `python3 chessclock.py simulate CONTROL --mean SECONDS [--games 1000] [--moves 60] [--seed 1]` | seeded flag-fall estimate |
| `python3 chessclock.py replay GAME.pgn [--control CONTROL] [--trouble SECONDS]` | think times from `%clk` comments |
| `python3 chessclock.py lint GAMES.txt` | check a games file |
| `python3 chessclock.py check GAMES.txt [--moves 60]` | CI gate: exit `1` if a game runs out of time |

`replay` reads the first game in the file. Clock comments are required on every main-line move, and the count is checked against the move count.

## How to test

```sh
python3 -m pytest
```

Note: this build did not get to run the suite, so run it once before relying on it. The tests need `pytest` and cover parsing and rejection, flag-fall arithmetic for each timing system, delay and increment handling, PGN parsing (including variations and missing clocks), lint line numbers, `check` exit codes, and seeded simulation.

## Limits

- Think times from a PGN are inferred from clock differences. Clock comments can be rounded by the server, and a delay move inside the allowance cannot be measured exactly.
- `simulate` assumes exponential thinking times with the given mean. It is a model, not a prediction for a particular player.
- `replay` does not check that moves are legal. It counts SAN tokens only to match them to clock readings.
- Only one game per file is replayed.

## Project layout

```text
chessclock.py          the whole tool (standard library only)
examples/games.txt     a sample games file
examples/club-game.pgn a short game with %clk comments
tests/                 pytest suite
```
