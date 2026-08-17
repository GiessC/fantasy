# Architecture

## Data flow

```text
FantasyPros API ──┐
Sleeper API ──────┼──> sources ──> normalization ──> SQLite (append-only snapshots)
CSV exports ──────┤                                        │
demo generator ───┘                                        ▼
                                                   Analytics Engine
                                                           │
                                    ┌──────────────────────┼──────────────────┐
                                    ▼                      ▼                  ▼
                              Draft State            Simulator         Player Analysis
                                    │                      │                  │
                                    └──────────────────────┼──────────────────┘
                                                           ▼
                                                   LLM Context Builder
                                                           │
                                                           ▼
                                                LM Studio (OpenAI-compatible)
                                                           │
                                                           ▼
                                              validated Recommendation
```

## Layering

Imports flow one way only. Nothing below may import from a layer above it.

```text
cli/            presentation only: parse args, call a service, render
services/       orchestration across sources, storage, and analytics
llm/            context building, prompting, structured-output validation
draft/          draft order, persisted state, Monte Carlo simulation
analytics/      deterministic math -- pure over (config, stored data)
normalization/  source payloads -> canonical models
db/             SQLite schema, migrations, repositories
sources/        retrieval only; no persistence, no analytics
config/         typed YAML
stats/models/errors    shared vocabulary
```

Two rules carry most of the weight:

**`analytics/` never imports `sources/` or `llm/`.** That is what makes every
number a reproducible function of configuration plus stored data, and what lets
the whole analytics package be unit-tested with three-line fixtures instead of a
database or a network.

**`sources/` never writes to the database.** Adapters retrieve and shape their
own provider's data; `services/sync.py` is the only place that persists. So a
provider quirk cannot leak into storage, and identity resolution — which needs
the canonical player table — has exactly one home.

## Module responsibilities

### `config/`

Typed, validated YAML. `league.py` holds league rules; `app.py` holds tool
settings (sources, LLM, analytics tuning, paths); `scoring.py` compiles friendly
nested YAML into the flat rate table analytics consumes. Validation errors name
the file and the field.

### `sources/`

One adapter per provider, all sharing `http.py` for retries, rate limiting,
structured errors, and an on-disk cache. Also `csv_import.py` (a key-free
fallback that survives API drift) and `demo.py` (a synthetic season, so the
pipeline runs with no network at all).

### `normalization/`

`identity.py` resolves a source record to a canonical player — source id first,
cross-source id second, name only as a last resort, ambiguity reported rather
than guessed. `stat_mapping.py` maps every provider's stat dialect onto one
canonical vocabulary.

### `db/`

SQLite with no ORM. The schema is small, the queries are analytical, and
transparency matters more than abstraction when a stated goal is that every
number is explainable. Fact tables are **append-only**: rows are never updated,
so ADP movement and projection revisions stay queryable, and `latest_*` views
pick the newest snapshot per key. Writes are content-hash de-duplicated, which
makes `sync` idempotent while genuine changes still accumulate history.

### `analytics/`

One module per concept — scoring, replacement, VOR, lineup, tiers, scarcity,
market, risk, roster fit, availability, draft score — plus `engine.py`, which
only orchestrates. `engine.py` contains no new math.

### `draft/`

`order.py` is pure arithmetic over `(teams, rounds, draft_type)`, which is where
snake/reversal off-by-ones live and so is directly testable. `state.py`
persists a draft; `simulator.py` runs the Monte Carlo.

### `llm/`

An OpenAI-compatible client that degrades `json_schema → json_object → prompt`,
a context builder that sends a deliberate subset, and validation that rejects
any player the model was not shown.

### `services/`

The seam between everything and the CLI. `AnalysisService.board()` is one call
that resolves config, data, draft state, and simulation into an analysed board —
the same call a future FastAPI layer would make.

### `cli/`

Typer + Rich. No analytical logic. Every command has a `--json` form, and
expected errors become one clean line with a meaningful exit code.

## Data-flow requirements

Every externally-sourced record preserves:

- `source` and the source's own player id,
- `retrieved_at`,
- `season` (and `week` where applicable),
- the scoring/settings context it was fetched under,
- the raw payload, where useful.

Player identity is a canonical `player_id`. Display names are never identity —
see [DATA_SOURCES.md](DATA_SOURCES.md).

## Project layout

```text
fantasy/
├── README.md  ARCHITECTURE.md  CONFIGURATION.md  ANALYTICS.md
├── DATA_SOURCES.md  DRAFT_SIMULATION.md  LLM_INTEGRATION.md  DEVELOPMENT.md
├── pyproject.toml
├── config/
│   ├── league.example.yaml
│   └── sources.example.yaml
├── src/fantasy_ai/
│   ├── stats.py  models.py  errors.py  logging_setup.py
│   ├── config/       positions, ranges, scoring, league, app, loader
│   ├── db/           schema, database, repositories
│   ├── sources/      http, fantasypros, sleeper, csv_import, demo
│   ├── normalization/ identity, stat_mapping
│   ├── analytics/    scoring, replacement, vor, lineup, tiers, scarcity,
│   │                 market, risk, roster_fit, availability, draft_score,
│   │                 dataset, engine
│   ├── draft/        order, state, simulator
│   ├── llm/          client, context, prompts, parsing, schemas, recommender
│   ├── services/     sync, analysis, draft_import
│   └── cli/          main, context, render, commands/
├── tests/
└── data/             databases, HTTP cache, CSV imports (gitignored)
```

## Deliberate omissions

No Postgres, Redis, Docker, message queue, or cloud service. A draft assistant
that runs on one machine for one person needs none of them, and each would add a
failure mode on the day the tool has to work.

The web UI (FastAPI + React) is deferred until the engine is proven useful, and
would consume `services/` — the same entry points the CLI uses.
