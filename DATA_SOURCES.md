# Data Sources

Four ingestion paths, all writing the same canonical models:

| Source | Provides | Auth |
| --- | --- | --- |
| **Sleeper** | Player metadata, identity, league/draft state, injury designations | none |
| **FantasyPros** | Expert consensus rankings, ADP, projections, expert dispersion | API key |
| **CSV import** | Rankings, ADP, projections from FantasyPros' export button | none |
| **Demo** | A complete synthetic season | none |

---

## Sleeper

`https://api.sleeper.app/v1` — no authentication.

| Endpoint | Used for |
| --- | --- |
| `/players/nfl` | Every player, keyed by Sleeper id (~5 MB) |
| `/state/nfl` | Current season and week |
| `/user/{username}` | Resolve a username to a user id |
| `/league/{id}` · `/league/{id}/rosters` · `/league/{id}/drafts` | League state |
| `/draft/{id}` · `/draft/{id}/picks` | Live draft |

By default only players at a position some league could start are stored
(`sources.sleeper.fantasy_positions_only`). Sleeper ships every player under
contract, so roughly half the payload is offensive linemen, punters, and long
snappers — none draftable in any format. IDP positions are kept.

Sleeper is the **identity backbone**: its ids are stable and widely
cross-referenced, and its player payload carries ESPN, Yahoo, Rotowire,
Sportradar and GSIS ids alongside its own. Those ids are whitespace-stripped on
the way in: Sleeper ships `gsis_id` as `" 00-0035057"`, and an unstripped id
silently fails to match the same id from another source. Syncing players first gives every
later source something to attach to without name matching.

The player payload is large and changes slowly, so it gets its own long TTL
(`player_cache_ttl_seconds`, 24h). **Draft picks are never cached** — during a
live draft, a stale pick list is worse than no pick list.

Team defenses arrive as position `DEF` keyed by team code; they are normalised
to `DST` with a readable name so they draft like anyone else.

**Team-level defensive plays.** `dst_tackle_loss`, `dst_forced_fum` and
`dst_fourth_down_stop` are scoreable, but no wired source publishes them today:
FantasyPros' DST projections carry sacks, interceptions, fumble recoveries,
touchdowns, safeties and points allowed, and nothing else. Configure them if
your league scores them, then import a CSV that has the columns — the reader
recognises `4th down stops`, `fourth down stops`, `TFL` and `FF` among others.
Without such a column the stat is simply absent and contributes nothing, rather
than being estimated.

`FF` and `TFL` are resolved by position: on a DST sheet they are the team stat,
anywhere else the individual one.

---

## FantasyPros

`https://api.fantasypros.com/public/v2/json/nfl` with an `x-api-key` header.

The key comes from the first of `sources.fantasypros.api_key` (inline in the
git-ignored `config/sources.yaml`), `api_key_file` (a file holding only the
key), or the environment variable named by `api_key_env`. See
[CONFIGURATION.md](CONFIGURATION.md#api-keys).

Provides the expert dispersion — best / worst / average / standard deviation
across the expert panel — that consensus disagreement and the risk model depend
on. No other free source publishes it.

### Robustness

This adapter is written defensively on purpose. FantasyPros has changed response
shapes before, and a draft is a bad time to find out. So:

- **Endpoint paths are configuration** (`sources.fantasypros.endpoints`), so a
  path change is a YAML edit rather than a code change.
- **Every field is extracted through an alias list.** `rank_ecr` / `ecr` /
  `rank`; `rank_std` / `std_dev` / `stdev`. `sync --verbose` prints which alias
  actually matched, so drift is visible immediately.
- **Statistics go through the shared mapping table**, handling both flat rows
  and nested `stats` objects.
- **An unrecognisable payload raises with the observed keys**, so the fix is
  obvious rather than a mystery.
- **CSV import is a first-class fallback** if the shape has drifted past what
  the aliases cover.

> **Verify on first run.** The API shapes here were written from public
> documentation and could not be tested against a live key. Run
> `fantasy-ai sync rankings --verbose` once: it prints the field mapping it
> found and any unmapped columns. If something is missing, add an alias in
> `sources/fantasypros.py` or fall back to CSV.

---

## CSV import

Every FantasyPros rankings and projections page has a **Download CSV** button,
and that format is considerably more stable than the API. This makes it a
first-class path, not a workaround:

```bash
fantasy-ai sync projections --from-csv data/imports/FantasyPros_Projections_RB.csv
fantasy-ai sync csv                       # everything in data/imports/
fantasy-ai sync csv file.csv --dataset rankings --position TE
```

The reader handles what real exports actually contain:

- a title line above the header,
- two-row grouped headers (`PASSING` above `YDS`),
- punctuation variants of the same column (`STD.DEV`, `Std Dev`, `AVG.`),
- the positional rank stuck to the position (`RB1` → `RB`),
- team codes appended to names (`Alvin Player NO`),
- thousands separators in numbers.

Two subtleties worth knowing:

**`ECR VS. ADP` is not an ADP.** It is a differential, and reading it as an ADP
would corrupt every market comparison. Columns whose names contain `vs`, `diff`
or `delta` are excluded from plain value lookups.

**Bare `ATT` / `YDS` / `TDS` are position-dependent.** On a QB sheet they are
passing, on an RB sheet rushing, on a WR sheet receiving. They are resolved
using the row's position, not a fixed guess. Use `--position` for a file with no
POS column.

`--verbose` prints the column mapping and everything it ignored.

---

## Demo source

```bash
fantasy-ai sync demo [--seed N]
```

Generates a complete, self-consistent synthetic season — players, projections,
expert rankings with dispersion, ADP, injury designations — with no network
access. It exists so that:

- the pipeline can be exercised end to end before any API key is in place,
- tests get realistic-shaped data without a fixture per table,
- a live-draft rehearsal is possible out of season.

Curves are shaped to resemble real positional distributions (a steep top end at
RB/WR, a long flat tail at QB, a cliff after the top few TEs) so replacement
level, tiers and scarcity produce plausible output. ADP is deliberately drifted
away from projection, so ADP value is exercised rather than trivially zero.

Everything is labelled `source: demo`, ids are prefixed `demo:`, and **every
name is invented — no real player's statistics are simulated or implied.**
Deterministic given a seed.

---

## Player identity

A display name is never the identity key. Every player gets a canonical
`player_id`, and each source's own id is recorded alongside it in
`player_source_ids`.

Resolution order for an incoming record from source *X*:

1. **This source's own id** — already linked.
2. **A cross-source id in the payload** — FantasyPros rows often carry Yahoo or
   Sleeper ids, and Sleeper publishes half a dozen.
3. **Name + position + team** — exact normalized-name match, disambiguated.
4. **Name + position** — accepted only when unambiguous.
5. **Mint a new canonical player.**

Name normalization strips accents, punctuation and suffixes, then aliases common
first-name variants:

```
"Ke'Shawn Vaughn"     -> keshawnvaughn
"Marvin Harrison Jr." -> marvinharrison
"Joshua Palmer"       -> joshpalmer      (matches "Josh Palmer")
"Amon-Ra St. Brown"   -> amonrastbrown
```

Team abbreviations are canonicalised too (`JAC`→`JAX`, `WSH`→`WAS`, `OAK`→`LV`).

Ambiguous matches are **reported, not guessed**. `sync --verbose` prints a
resolution summary: how many players matched by source id, by cross-source id,
by name, how many were newly created, and how many were ambiguous.

---

## Snapshots and history

Fact tables (`projections`, `rankings`, `adp`, `injuries`) are **append-only**.
Rows are never updated, so history stays queryable:

- ADP movement over time,
- projection revisions,
- ranking movement,
- eventually, source accuracy.

Writes are **content-hash de-duplicated**: a snapshot identical to the newest one
for the same key is skipped. So `sync` is idempotent — running it twice produces
the same database — while a genuine change appends a new snapshot and keeps the
old one. Reverting to a previous value is itself recorded.

`latest_*` views select the newest snapshot per key, and are what analytics
reads.

```bash
fantasy-ai db prune --keep 10     # trim history when it gets long
```

---

## Caching and outages

> Do not make the entire application dependent on live API availability after
> data has been successfully cached.

Responses are cached on disk keyed by method, URL and query, stored with their
retrieval time. When a source is unreachable and a cached response exists, **the
cached response is served and clearly flagged as stale** — in the sync report
and in the LLM's context — rather than failing.

```bash
fantasy-ai sync all --force       # ignore the cache
```

A draft is exactly when you do not want to discover that an API is down.

---

## Freshness

```bash
$ fantasy-ai data status
players        sleeper          11284 record(s)  2.1 hours ago
projections    fantasypros        612 record(s)  2.1 hours ago
rankings       fantasypros        612 record(s)  2.1 hours ago
adp            fantasypros        598 record(s)  26.4 hours ago  [STALE]
injuries       sleeper            143 record(s)  2.1 hours ago
```

Anything older than `staleness_warning_hours` (24) is flagged, and the same
warning is passed to the model so it can caveat its own answer.

`fantasy-ai data sync-log` shows recent sync runs with their status, record
counts and resolution summaries.

---

## Source priority

When several sources have data for the same player, priority is configurable:

```yaml
analytics:
  projection_source_priority: [fantasypros, csv, demo]
  ranking_source_priority:    [fantasypros, csv, demo]
  adp_source_priority:        [fantasypros, sleeper, csv, demo]
```

The first source with data for a player wins; ties break on recency. Every
analysed player records which source supplied its projection and ADP, so a
number can always be traced back to who published it and when.
