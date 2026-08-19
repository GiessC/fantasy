# Local Fantasy Football AI

A local fantasy-football draft assistant. Deterministic analytics computed from
**your** league's YAML, plus a local LLM (via LM Studio) that interprets those
numbers and explains the trade-offs.

The LLM is never the source of truth for a number. Every figure it sees was
computed here first, and its output is validated against what it was given
before you ever read it.

```
FantasyPros ──┐
              ├─ ingest ─ normalize ─ SQLite ─ analytics ─┬─ draft state
Sleeper ──────┘                                          ├─ simulator
CSV export ───┘                                          └─ LLM context ─ LM Studio
```

## Quick start

```bash
git clone <this repo> && cd fantasy
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp config/league.example.yaml config/league.yaml
cp config/sources.example.yaml config/sources.yaml
$EDITOR config/league.yaml            # your teams, scoring, roster, draft slot

fantasy-ai validate-config
fantasy-ai db init
fantasy-ai sync demo                  # synthetic data -- no API key, no network
fantasy-ai analyze board
```

`sync demo` generates a complete synthetic season so you can drive the whole
tool before wiring up real data. Everything it produces is labelled
`source: demo` and the names are invented — it is for exercising the pipeline,
never for real draft decisions.

### Real data

Put your key in `config/sources.yaml` (that file is git-ignored, which is why it
is the right place for it):

```yaml
sources:
  fantasypros:
    api_key: "your-key"               # https://www.fantasypros.com/api/
```

Prefer to keep the key out of config? Point at a file holding only the key, or
fall back to the environment — the first one set wins:

```yaml
    api_key_file: config/fantasypros.key    # chmod 600 it
    api_key_env: FANTASYPROS_API_KEY        # used if neither of the above is set
```

```bash
fantasy-ai validate-config            # confirms which one it read, never prints the key
fantasy-ai sync all
```

No API key? Every FantasyPros rankings and projections page has a **Download
CSV** button. Drop the files in `data/imports/` and run `fantasy-ai sync csv`.
That path needs no key and is more stable than the API.

### Local model

Load a model in LM Studio, start its server (Developer → Start Server), then:

```bash
fantasy-ai llm models                 # what the server reports
$EDITOR config/sources.yaml           # set llm.model to one of them
fantasy-ai llm status
fantasy-ai recommend
```

## Web UI

```bash
cd web && npm install && npm run build   # once
fantasy-ai serve --open
```

A live-draft board at <http://127.0.0.1:8765>: best available with the full
analytics, click any row for the complete decomposition, type a name to record
a pick, and your roster, open starting slots, and positional scarcity update as
the draft moves. It binds to localhost and has no authentication, because it is
not meant to leave your machine.

The UI consumes the same services the CLI does — the analysis cannot differ
between them. OpenAPI docs are at `/docs`.

## Drafting

```bash
fantasy-ai draft start --position 7
fantasy-ai draft pick "Player Name"   # every pick, by any team
fantasy-ai draft board                # best available, in context
fantasy-ai draft status
fantasy-ai draft undo

fantasy-ai simulate availability      # who survives to your next pick
fantasy-ai simulate draft             # take X now, simulate the rest, compare
fantasy-ai recommend --live           # terse model recommendation
fantasy-ai ask "Should I take a TE here or wait?"
```

Drafting on Sleeper? Point `sources.sleeper.draft_id` (or `league_id`) at it and
run `fantasy-ai draft import` — repeatedly, as the draft goes — to pull picks in
automatically.

## Understanding the numbers

Every metric is decomposable, and the CLI will show you the decomposition:

```bash
fantasy-ai analyze player "Player Name" --breakdown   # projection -> points, line by line
fantasy-ai analyze compare "Player A" "Player B"      # why A outranks B, per component
fantasy-ai analyze replacement                        # how replacement level was derived
fantasy-ai analyze tiers RB                           # tier breaks and the gaps behind them
fantasy-ai analyze scarcity
```

A draft score is a **sum of point-denominated components**, not an opaque
weighted blend:

```
Draft score +251.3 = value +210.8  urgency +50.3  market -8.0  risk -1.8
  value:    280.9 projected - 70.1 replacement (RB)
  urgency:  80% chance he is gone; fallback at RB worth +147.8 VOR
  market:   our rank 1 vs ADP 5.3; shading 8.0 pts toward consensus
  risk:     expert sd 1.1 over a 5-9 range
```

See [ANALYTICS.md](ANALYTICS.md) for the methodology behind each one, including
the approximations and why they were chosen.

## Configuration

Two files, both YAML, both documented inline:

- `config/league.yaml` — your league. Scoring (including bonuses, kicker
  distance ranges, DST points-allowed tiers, TE premium, superflex, IDP),
  roster slots and flex eligibility, draft format, keepers. Plus optional
  `analytics:` and `simulation:` tuning.
- `config/sources.yaml` — data sources, the local model, and paths. No secrets:
  API keys are referenced by environment-variable name.

Nothing about a league is hard-coded. See [CONFIGURATION.md](CONFIGURATION.md).

## Command reference

| Command | What it does |
| --- | --- |
| `validate-config` | Load and validate YAML; report problems and warnings |
| `db init` / `db status` / `db prune` | Schema, row counts, snapshot pruning |
| `sync players\|rankings\|adp\|projections\|injuries\|csv\|demo\|all` | Fetch and store |
| `data status` / `data sync-log` | Freshness, staleness warnings, sync history |
| `analyze board\|players\|player\|compare\|tiers\|replacement\|scarcity` | Deterministic analysis |
| `draft start\|pick\|skip\|undo\|status\|board\|import\|list\|complete\|delete` | Live draft |
| `simulate availability\|draft\|player` | Monte Carlo |
| `recommend` / `ask` | Local model, over the analytics |
| `llm status\|models\|context` | Model diagnostics |
| `serve` | The web UI and its API |

Most commands take `--json` for piping. `--help` works everywhere.

## Design rules

- **Configuration over assumptions.** No hard-coded team count, scoring format,
  flex arrangement, or roster shape.
- **Deterministic math over LLM math.** The analytics package never imports the
  LLM or a source client; it is a pure function of config and stored data.
- **Everything is explainable.** Every number can be decomposed to its inputs.
- **Cache aggressively.** Once data is fetched, an outage cannot stop a draft.
- **Sources are isolated.** Provider quirks stay in `sources/`; analytics only
  ever sees the canonical vocabulary.
- **Identity is never a name.** Players are matched by source id first; names
  are a last resort, and ambiguity is reported rather than guessed.

## Project layout

```
src/fantasy_ai/
├── config/          typed YAML config (league rules, scoring, sources, tuning)
├── db/              SQLite schema, migrations, repositories
├── sources/         FantasyPros, Sleeper, CSV, synthetic demo
├── normalization/   identity resolution, stat-dialect mapping
├── analytics/       scoring, replacement, VOR, tiers, scarcity, risk, draft score
├── draft/           draft order, persisted state, Monte Carlo simulator
├── llm/             OpenAI-compatible client, context builder, validation
├── services/        orchestration shared by the CLI and the API
├── api/             FastAPI layer for the web UI
└── cli/             presentation only
```

```
web/                 React + TypeScript + Vite frontend
```

## Development

```bash
pytest                 # 418 tests, no network, no model server
ruff check src tests
mypy src/fantasy_ai

cd web && npm run typecheck && npm run build
npm run dev            # Vite dev server, proxying /api to fantasy-ai serve
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for what is built, what is stubbed, and
what remains.

## Documentation

| File | Contents |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layering and data flow |
| [CONFIGURATION.md](CONFIGURATION.md) | Every league setting |
| [ANALYTICS.md](ANALYTICS.md) | The math, and its assumptions |
| [DATA_SOURCES.md](DATA_SOURCES.md) | Sources, caching, freshness, identity |
| [DRAFT_SIMULATION.md](DRAFT_SIMULATION.md) | The simulation model and its limits |
| [LLM_INTEGRATION.md](LLM_INTEGRATION.md) | Prompting, structured output, validation |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Status, testing, roadmap |

## Stack

Python 3.11+, SQLite, Pydantic, httpx, Typer, Rich, pytest. The optional web UI
adds FastAPI, uvicorn, and React + TypeScript + Vite. No Docker, no Postgres, no
message queue — none of it is needed to draft.
