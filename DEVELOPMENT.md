# Development

## Status

| Phase | State | Notes |
| --- | --- | --- |
| 1. Foundation | **Done** | Typed YAML config, SQLite + migrations, logging, CLI, env handling |
| 2. Data ingestion | **Done, API shapes unverified** | Sleeper + FantasyPros + CSV + demo, idempotent, cached |
| 3. Analytics | **Done** | Scoring, replacement, VOR, tiers, scarcity, market, risk, roster fit, draft score |
| 4. Draft state | **Done** | Persisted, add/undo/skip/import, snake / linear / third-round reversal |
| 5. Simulation | **Done** | Seeded Monte Carlo availability + strategy comparison |
| 6. LLM | **Done** | LM Studio via OpenAI-compatible API, structured output, validation |
| 7. Web UI | **Not started** | Deferred by design |

359 tests, 86% coverage, clean under `ruff` and `mypy`.

### What could not be verified here

Everything below was written from public documentation and exercised against
fixtures and mock transports, but **not against the live services**:

- **FantasyPros API response shapes.** Field aliases and endpoint paths are
  configuration, the parser is tolerant, and `--verbose` prints exactly what it
  matched — but the first run against a real key is the real test. CSV import is
  the fallback if shapes have drifted.
- **Sleeper draft import against a live draft.** The endpoints and payload
  shapes are well documented and covered by fixture tests; a real drafting
  session is still the proof.
- **A real local model.** The LLM loop was exercised against a stub server
  covering the healthy path, schema rejection, hallucinated players, and
  unparseable prose. A real model's output style will differ.

Everything that does not touch a network is tested for real.

---

## Getting set up

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                          # 359 tests, no network, no model server
pytest --cov=fantasy_ai         # with coverage
ruff check src tests
mypy src/fantasy_ai
```

Python 3.11+. (The original spec said 3.12; 3.11 is the floor so the code runs
on more machines, and nothing here needs a 3.12-only feature.)

---

## Testing philosophy

**No test requires a live API or a running model.** Source clients are driven by
`httpx.MockTransport` against fixture payloads; the local model is replaced by a
scripted fake. Simulations are seeded, so results are exact rather than
approximate.

| File | Covers |
| --- | --- |
| `test_config.py` | Range parsing, scoring compilation, league validation, loader |
| `test_scoring.py` | Points, bonuses, kicking, DST tiers, per-position overrides |
| `test_analytics.py` | Starter pools, replacement, VOR, lineups, tiers, scarcity, market, risk, roster fit, draft score |
| `test_db.py` | Migrations, snapshots, de-duplication, draft persistence |
| `test_draft.py` | Draft order, state transitions, simulator behaviour |
| `test_sources.py` | Identity, stat mapping, HTTP layer, each adapter |
| `test_services.py` | Sync orchestration, Sleeper draft import |
| `test_llm.py` | JSON extraction, schemas, name checking, client transport, retry loop |
| `test_integration.py` | Full pipeline, draft integration, CLI, cross-league behaviour |

Some tests exist specifically to pin down behaviour that is easy to break:

- **`test_adp_signal_survives_the_need_adjustment`** — the simulator's need
  shift must not swamp ADP (a real regression: multiplicative shifts once let
  first-round picks survive into round two).
- **`test_adp_value_is_bounded_by_board_span`** — ADP value must not blow up for
  players the market structurally ignores (another real regression: kickers took
  over the board).
- **`test_superflex_lifts_quarterbacks`**, **`test_ppr_lifts_receivers`**,
  **`test_no_kicker_slot_removes_kickers_from_contention`** — the same data must
  produce different boards under different rules. This is the property that
  makes "configuration over assumptions" real rather than aspirational.
- **`test_every_number_is_explainable`** — scoring lines must sum exactly to the
  projection, and draft-score components exactly to the total.

CLI tests run through the real `run()` entry point rather than Typer's
`CliRunner`, because `CliRunner` skips the error-to-exit-code mapping and would
leave that contract unverified.

---

## Conventions

- Type hints everywhere; `mypy` is clean.
- Errors the user can cause subclass `FantasyAIError` and carry an exit code, so
  the CLI prints one clean line instead of a traceback.
- No magic constants in analytics. Every tunable is in `config/app.py` with a
  comment explaining its value, and every approximation is documented at its
  implementation *and* in [ANALYTICS.md](ANALYTICS.md).
- Randomness is seeded and injectable.
- New scoring rules go in `config/scoring.py` + `stats.py`; analytics reads only
  `CompiledScoring`.
- New sources go in `sources/`, return canonical models, and never touch the
  database.

### Adding a migration

Append a new `Migration` to `MIGRATIONS` in `db/schema.py`. Never edit an
existing one — databases in the wild have already applied it.

### Adding a stat

1. Add the canonical key and label to `stats.py`.
2. Add source aliases to `normalization/stat_mapping.py`.
3. Add a scoring field to the relevant section in `config/scoring.py`.
4. Test it in `test_scoring.py`.

---

## Roadmap

### Near term

- **Verify FantasyPros against a live key.** Run `sync rankings --verbose` and
  adjust aliases if the field mapping looks wrong.
- **Verify Sleeper draft import** during a real draft.
- **Auction support.** Config and validation exist; the simulator models snake
  and linear orders only. Needs a values-over-replacement budget model.
- **Keeper handling in the draft.** Keepers are configurable and can be recorded
  as picks, but are not removed from the pool automatically.

### Medium term

- **Opponent profiles from history.** `DrafterProfile` is built and tested; what
  is missing is learning profiles from a league's past drafts (positional
  tendencies, ADP deviation, who starts runs).
- **Projection blending.** Currently one source wins per player by priority; a
  weighted blend across sources, with per-source accuracy tracked over time, is
  the natural next step — the snapshot history already supports it.
- **Weekly lineup mode.** The scoring, lineup solver, and projection storage all
  work per-week already; only the CLI surface is missing.
- **Bye-week awareness** in roster fit.

### Longer term

- **Web UI** (FastAPI + React), consuming `services/` — the same entry points
  the CLI uses. Deferred until the engine has proven useful in a real draft.
- **Model accuracy tracking.** Compare stored projections against actual results
  to measure which sources, and which of our own adjustments, actually helped.

---

## Known limitations

- **Auction drafts are not simulated.**
- **IDP is scoreable but untested against real data** — no IDP projection source
  is wired up.
- **Per-game bonuses and DST tiers are approximations.** Documented, configurable
  and only used when a league configures them, but approximations nonetheless.
- **The simulator does not model specific opponents** unless you supply profiles.
- **`analyze board` recomputes from scratch each run.** Fine at this scale
  (~300 players, well under a second), but it is not incremental.
- **CSV files with duplicate column names** (a QB export with passing and rushing
  both labelled `YDS`) will keep only the last. Use grouped-header exports, or
  the API, for those.

---

## Quality checklist

- [x] Type hints
- [x] Clear errors that name the file and field
- [x] No magic scoring constants
- [x] No hard-coded league assumptions
- [x] No hidden LLM calculations
- [x] Deterministic analytics
- [x] Seedable simulations
- [x] Reproducible analysis
- [x] Tests independent of live APIs and models
