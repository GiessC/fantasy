# Configuration

Your league lives entirely in YAML. Changing rules never means changing Python.

Two files:

| File | Contents |
| --- | --- |
| `config/league.yaml` | `league:` (required), plus optional `analytics:` and `simulation:` tuning |
| `config/sources.yaml` | `sources:`, `llm:`, `paths:`, `log_level:` |

Either may be absent, in which case the bundled `*.example.yaml` is used — so a
fresh checkout runs with no setup. Validate at any time:

```bash
fantasy-ai validate-config
```

Errors name the file and the field. Warnings (draft slot unset, no API key,
rounds exceeding roster spots) are reported but never fatal.

---

## `league:`

### Basics

```yaml
league:
  name: My Fantasy League
  season: 2026            # required
  teams: 10               # required
  type: redraft           # redraft | keeper | dynasty
  notes:
    - "House rule: no trading draft picks."   # surfaced to the LLM
```

### Scoring

Only the values you set differ from the defaults. Everything is fractional.

```yaml
  scoring:
    passing:
      yards_per_point: 25       # or: points_per_yard: 0.04 (not both)
      touchdown: 4
      interception: -2
      two_point: 2
      completion: 0
      incompletion: 0
      attempt: 0
      first_down: 0
      sacked: 0

    rushing:
      yards_per_point: 10
      touchdown: 6
      two_point: 2
      attempt: 0
      first_down: 0

    receiving:
      yards_per_point: 10
      touchdown: 6
      reception: 0.5            # 0 standard | 0.5 half PPR | 1 full PPR
      target: 0
      two_point: 2
      first_down: 0

    fumbles:
      lost: -2
      total: 0
      return_touchdown: 6

    misc:
      return_yards_per_point: null
      return_touchdown: 6
      two_point: 2
```

#### Kicking

Flat, or bucketed by distance:

```yaml
    kicking:
      extra_point: 1
      extra_point_missed: 0
      field_goal:
        ranges:
          "0-39": 3
          "40-49": 4
          "50+": 5
      # or simply:  field_goal: 3
      field_goal_attempt: 0
      # Used only when a projection gives total FGs with no distance split:
      # distance_distribution:
      #   kick_fgm_0_19: 0.01
      #   kick_fgm_20_29: 0.21
      #   kick_fgm_30_39: 0.28
      #   kick_fgm_40_49: 0.29
      #   kick_fgm_50_plus: 0.21
```

Ranges accept `7`, `0-39`, `50+`, and `<=6`. They must not overlap. Ranges that
straddle an internal bucket boundary are resolved by yard-overlap weighting.

#### Team defense

```yaml
    defense:
      sack: 1
      interception: 2
      fumble_recovery: 2
      touchdown: 6
      safety: 2
      blocked_kick: 2
      points_allowed:
        "0": 10
        "1-6": 7
        "7-13": 4
        "14-20": 1
        "21-27": 0
        "28-34": -1
        "35+": -4
      # yards_allowed: { ... }
      points_allowed_per_game_cv: 0.45   # see ANALYTICS.md §1
```

#### IDP

```yaml
    idp:
      tackle_solo: 1.5
      tackle_assist: 0.75
      tackle_for_loss: 2
      sack: 4
      interception: 6
      pass_defended: 1.5
      forced_fumble: 4
      fumble_recovery: 4
      touchdown: 6
      safety: 4
```

#### Per-position overrides (TE premium, QB-specific rules)

Only the values you list are overridden; everything else inherits.

```yaml
    position_overrides:
      TE:
        receiving:
          reception: 1.5        # TE premium on top of a 0.5-PPR base
      QB:
        passing:
          touchdown: 6
```

#### Bonuses

```yaml
    bonuses:
      - name: 100-yard rushing game
        stat: rush_yd
        threshold: 100
        points: 3
        per_game: true          # false = once, on the season total
        repeat: false           # true = per each full threshold
        positions: [RB, WR, QB] # omit for all
        # per_game_cv: 0.55     # override the volatility assumption
      - name: 300-yard passing game
        stat: pass_yd
        threshold: 300
        points: 3
```

Per-game bonuses are estimated from a season projection — see
[ANALYTICS.md](ANALYTICS.md) §1 for the method and its assumption.

#### Anything else

```yaml
    custom:
      rec_first_down: 0.5       # any canonical stat key (src/fantasy_ai/stats.py)
    season_games: 17
```

A typo in a known section is an **error**, not a silent no-op:
`touchdwon: 4` fails validation rather than scoring zero all season.

---

### Roster and flex

Slot name → count. Reserve slots are `BENCH`, `IR`, `TAXI`.

```yaml
  roster:
    QB: 1
    RB: 2
    WR: 2
    TE: 1
    FLEX: 1
    K: 1
    DST: 1
    BENCH: 6
    IR: 1
    TAXI: 0

  flex:
    allowed_positions: [RB, WR, TE]     # for slots named FLEX
    slots:                              # any other flex slot
      SUPERFLEX: [QB, RB, WR, TE]
      WRT: [WR, TE]
```

Known flex names work without configuration: `FLEX`, `SUPERFLEX`/`SFLEX`/`OP`,
`WRT`, `RBWR`, `REC_FLEX`, `IDP_FLEX`/`DP`. A slot whose name contains `FLEX`
but has no eligibility is an error, not a guess.

Superflex needs nothing beyond the slot — QB replacement level deepens
automatically, because it is derived from starter demand rather than a
convention.

Only `BENCH` counts toward draft-relevant roster capacity; `IR` and `TAXI` are
normally filled after the draft.

### Draft

```yaml
  draft:
    type: snake             # snake | linear | third_round_reversal | auction
    position: 7             # your 1-indexed slot
    rounds: 16
    seconds_per_pick: 90    # informational; cues the model to be brief
    reversal_round: 3       # third_round_reversal only (defaults to 3)
    budget: 200             # auction only (required)
```

### Keepers, waivers, playoffs

```yaml
  keepers:
    enabled: true           # requires type: keeper or dynasty
    max_keepers: 2
    cost_rule: round_penalty   # none | previous_round | round_penalty
                               # | adp_round | auction_value
    round_penalty: -1
    max_seasons: 3
    keepers:
      "Player Name": 5      # explicit declarations: name -> round cost

  waivers:
    type: rolling           # rolling | faab | reverse_standings | none
    faab_budget: 100

  playoff:
    teams: 6
    start_week: 15
```

`fantasy-ai draft start` records your declared keepers automatically, at your
pick in each named round and flagged as keepers — so they leave the board and
appear on your roster from the first pick, and the draft clock still starts at
pick 1.

Other teams' keepers cannot be known from your own config. Record them as you
learn them:

```bash
fantasy-ai draft pick "Their Keeper" --at 23 --keeper
```

Use `draft start --no-keepers` to skip the automatic step.

---

## `analytics:` and `simulation:`

Optional. Defaults are reasonable; each knob is documented in
[ANALYTICS.md](ANALYTICS.md) and [DRAFT_SIMULATION.md](DRAFT_SIMULATION.md).

```yaml
analytics:
  replacement:
    method: starter_demand
    window: 3
  tiers:
    sensitivity: 1.0
  risk:
    max_discount_points: 25.0
  weights:
    value: 1.0
    urgency: 1.0
    roster_fit: 1.0
    market_value: 0.25
    risk: 1.0
    tier_cliff: 1.0
    max_market_points: 8.0
  bench_start_share: 0.25

simulation:
  iterations: 1000
  seed: 20260817          # null for non-reproducible runs
  adp_noise_fraction: 0.22
  need_weight: 0.35
```

---

## `sources:` and `llm:`

```yaml
paths:
  data_dir: data
  database: data/fantasy.db
  http_cache: data/http-cache

sources:
  fantasypros:
    enabled: true
    base_url: https://api.fantasypros.com/public/v2/json/nfl
    api_key_env: FANTASYPROS_API_KEY     # name only, never the key
    scoring: auto                        # auto | STD | HALF | PPR
    positions: [QB, RB, WR, TE, K, DST]
    projection_week: 0                   # 0 = full season
    endpoints:                           # paths are config, not code
      consensus_rankings: "{season}/consensus-rankings"
      projections: "{season}/projections"

  sleeper:
    enabled: true
    base_url: https://api.sleeper.app/v1
    league_id: "1234567890"              # optional: import a live draft
    draft_id: "1234567890"
    username: your-sleeper-username      # resolves your draft slot
    player_cache_ttl_seconds: 86400

  csv_import:
    enabled: true
    directory: data/imports

  http:
    timeout_seconds: 20
    max_retries: 3
    backoff_seconds: 1.0
    rate_limit_interval: 0.0
    cache_ttl_seconds: 21600             # 0 disables the cache

  staleness_warning_hours: 24

llm:
  enabled: true
  base_url: http://localhost:1234/v1
  model: muse-glimmer                    # fantasy-ai llm models
  temperature: 0.2
  max_tokens: 1200
  timeout_seconds: 120
  structured_output: json_schema         # json_schema | json_object
                                         # | prompt_only | off
  max_parse_retries: 2
  max_candidates: 8
  # extra_body: { top_p: 0.9 }
  # system_prompt_override: "..."

log_level: info
```

---

## Environment overrides

| Variable | Overrides |
| --- | --- |
| `FANTASY_AI_LEAGUE_CONFIG` | League file path (also `--league`) |
| `FANTASY_AI_SOURCES_CONFIG` | Sources file path (also `--sources`) |
| `FANTASY_AI_DB` | `paths.database` |
| `FANTASY_AI_DATA_DIR` | `paths.data_dir` |
| `FANTASY_AI_LOG_LEVEL` | `log_level` |
| `FANTASY_AI_LLM_BASE_URL` | `llm.base_url` |
| `FANTASY_AI_LLM_MODEL` | `llm.model` |
| `FANTASY_AI_LLM_ENABLED` | `llm.enabled` |
| `FANTASY_AI_SIM_ITERATIONS` | `simulation.iterations` |
| `FANTASY_AI_SIM_SEED` | `simulation.seed` |
| `FANTASYPROS_API_KEY` | The key itself (name configurable) |

CLI flags win over environment variables, which win over YAML.

---

## What is deliberately not assumed

- Not every league is PPR.
- Not every league has one FLEX, or one QB.
- Not every league starts a kicker or a defense.
- Rosters are not a fixed shape.
- Scoring values are never hard-coded into analytics — they arrive through
  `CompiledScoring`, and the analytics package cannot see the YAML at all.
