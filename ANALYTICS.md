# Analytics

Every metric here is deterministic, testable, and decomposable. The LLM receives
these results; it never defines them.

Where a calculation rests on an assumption, this document says so and names the
configuration knob that controls it. There are no magic constants hiding in the
code — the ones that exist are here, with their reasoning.

---

## 1. League-adjusted fantasy points

**Module:** `analytics/scoring.py` · **Config:** `league.scoring`

A projected stat line becomes points by applying the league's own rates:

```
points = Σ (stat_units × rate_for(stat, position)) + bonuses + tiered_scoring
```

`fantasy-ai analyze player <name> --breakdown` prints every line.

A source's own `FPTS` column is **always discarded**. It was computed under
someone else's scoring settings, and using it would silently import a different
league's rules. The stat-mapping table maps those columns to "known, ignored".

### Three approximations

A season-total projection cannot exactly answer a per-game scoring question.
Three rules need per-game information:

**Per-game bonuses** ("+3 for a 100-yard rushing game"). We model per-game
values as normal with mean `season_total / games` and standard deviation
`cv × mean`, then:

```
E[games over threshold] = games × P(X > threshold)
```

The coefficient of variation is per-stat, because weekly volatility differs
enormously by category — passing yardage is stable, receiving touchdowns are
not. Defaults live in `DEFAULT_PER_GAME_CV` (`stats.py`) and range from 0.25
(pass attempts) to 1.20 (receiving TDs). Override per bonus with `per_game_cv`.
The normal assumption understates the extreme right tail, which is exactly why
the spiky stats carry large CVs.

*This only matters if your league configures bonuses. With none, the code never
runs.*

**Team-defense tiers** (points/yards allowed). Scored weekly, projected
seasonally. We integrate the normal per-game distribution across the tier table
and take the probability-weighted points, rather than mapping the season average
to one tier — a defense averaging 20.4 points allowed does not score the "14-20"
tier every week. Controlled by `defense.points_allowed_per_game_cv` (0.45).

**Field goals without a distance split.** When a league scores FGs by distance
but the projection reports only a total, the total is scored at a rate blended
over `scoring.kicking.distance_distribution` (defaults approximate league-wide
NFL kicking). If the projection *does* have buckets, they are scored directly
and the total is ignored so nothing is double-counted.

### Range straddling

If a league's ranges do not line up with our canonical FG buckets (`0-35`
against our `30-39`), points are averaged by **yard overlap** rather than
rounded to one side.

---

## 2. Replacement level

**Module:** `analytics/replacement.py` · **Config:** `analytics.replacement`

Replacement level is emphatically *not* "rank #X". In a 10-team league starting
2 RB / 2 WR / 1 FLEX, the flex slot is contested, so real RB demand sits
somewhere between 20 and 30 — and where exactly depends on how RB, WR and TE
value compare *under this league's scoring*.

### Method `starter_demand` (default)

Simulate the league's starting lineups:

1. Sort every projected player by league-adjusted points, descending.
2. Walk the list. A player claims a dedicated slot at his position if one is
   left league-wide (`count × teams`); otherwise he claims any flex slot he is
   eligible for.
3. Stop when every starting slot in the league is filled.

Whoever a position runs out of slots for defines its **starter demand**.
Replacement is the projection of the players just outside that pool.

Flex contention is settled by the same points comparison a real league settles
it by. So:

- a pass-heavy scoring system automatically deepens WR demand,
- a superflex automatically deepens QB demand,
- a TE-premium league automatically pulls tight ends into the flex,

with no position-specific code anywhere. `fantasy-ai analyze replacement` shows
the derivation.

### Noise damping

Replacement is the **average of a small window** (`window`, default 3) starting
at the replacement rank, not one player's number. A single optimistic projection
at exactly the replacement rank would otherwise shift every VOR at that position.

### Alternatives

| Method | Behaviour |
| --- | --- |
| `starter_demand` | As above. Default. |
| `fixed_rank` | Explicit per-position ranks (`QB12`, `RB30`, ...) for users who prefer a convention. |
| `blended` | Weighted average of the two, via `blend_weight`. |

### Positions with no starting slot

If a league starts no kicker, replacement for K is pinned to the best available
kicker, so every kicker's VOR is ≤ 0 and the board de-emphasises the position
rather than pretending it has starting value.

---

## 3. Value over replacement

**Module:** `analytics/vor.py`

```
VOR = projected_points − replacement_level(position)
```

**Methodology note on flex.** Replacement level *already* accounts for flex
contention, because the starter-pool simulation fills flex slots from the same
points-ordered pool. So a flex-eligible player's primary VOR is measured against
his own position's replacement. Adding a separate flex adjustment on top would
double-count the flex slot.

`flex_vor` is reported as a secondary, informational view: the player measured
against the best flex-eligible player who made *no* starting lineup at all —
"how much better is he than a streamable flex body?" It is not part of the draft
score.

**Our board order is VOR descending**, not raw points. Raw points would rank
every quarterback above every running back in a one-QB league, which is the
exact error replacement level exists to correct.

---

## 4. ADP value

**Module:** `analytics/market.py`

```
rank_difference    = ADP − our_rank              (positive = falling to you)
market_implied_vor = VOR our board assigns to slot round(ADP)
points_value       = our VOR − market_implied_vor
```

The rank difference is what people quote. The points conversion goes through
**our own board's VOR curve, read at both endpoints** — not through a local
slope. An earlier implementation multiplied the rank gap by the local
points-per-slot slope; for a kicker with ADP 220 whose VOR places him at board
slot 60, that produced a fictitious 150-point "discount" that took over the
entire board. Reading the curve at both ends bounds the number by the actual
span of board value.

**How this enters the draft score is a separate decision** — see §7. Briefly:
the surplus from a falling player is realised by *waiting for him*, and waiting
is what availability already prices.

---

## 5. Consensus disagreement

**Module:** `analytics/market.py`

Tracked from the source's expert panel: mean, median, standard deviation, best,
worst, expert count, model rank, model-vs-consensus.

The headline number is the **disagreement index**:

```
disagreement_index = expert_stdev / typical_stdev_at_this_rank
```

where the denominator is the **median spread among nearby-ranked players in this
very dataset**. Expert spread grows with rank — everyone agrees about the first
pick, nobody agrees about the 150th — so a raw standard deviation says almost
nothing on its own, and any fixed normalising formula (`sd / √rank` and friends)
mislabels one end of the board or the other. Calibrating against the data makes
the index self-adjusting: 1.0 is ordinary disagreement for that part of the
board, 2.0 is genuinely polarising.

Labels: `strong consensus` (≤0.7), `consensus`, `some disagreement` (≥1.15),
`polarising` (≥1.5).

A wide spread is **not** the same as a market discount. It says the player is
divisive, which is information about risk and upside, not about value.

---

## 6. Positional scarcity

**Module:** `analytics/scarcity.py`

Four separately observable quantities, all in fantasy points, rather than one
opaque index:

| Metric | Meaning |
| --- | --- |
| `next_player_delta` | Points lost by taking the next player at this position |
| `dropoff_3 / 5 / 10` | Points lost by waiting for N more to come off the board |
| `tier_cliff` | Points from this player to the top of the next tier |
| `starter_supply_ratio` | Players left above replacement ÷ unfilled starting slots league-wide |

A supply ratio below 1.0 means the position is already short of startable bodies.

`scarcity_index` (0–1) is a normalised convenience for sorting and display only.
Every decision that consumes scarcity uses the point-denominated values.

---

## 7. Draft score

**Module:** `analytics/draft_score.py` · **Config:** `analytics.weights`

The rule that shapes this design: **every component is already denominated in
fantasy points, and the total is a sum, not a normalised blend.** A draft score
of `+38.4` means "about 38 points of draft value over a replacement-level
starter, after accounting for what I would get if I waited, how he fits my
roster, what the market charges, and how uncertain he is".

```
draft_score = value + urgency + roster_fit + market + risk + tier_cliff
```

### value

VOR. The base.

### urgency — the cost of waiting

```
urgency = (1 − P_available_next_pick) × max(0, VOR − E[best same-position
          alternative still available at my next pick])
```

If a player is near-certain to come back, urgency is ~0 *no matter how good he
is* — you can take someone else now and still get him. If he will certainly be
gone, the full gap to your fallback is at stake.

`E[best alternative]` is the **exact expectation of the maximum** VOR over the
position's remaining players under independent availability:

```
E[max] = Σᵢ vorᵢ × P(i available) × Πⱼ<ᵢ (1 − P(j available))
```

ordered by VOR descending, so the `j < i` terms are the strictly better options
that would have been taken instead. A player certain to be there truncates the
sum, as he should. This is meaningfully better than "the next guy down".

### roster_fit

A non-positive adjustment, so it can never inflate a player above his own
projection. See §9.

### market — shrinkage, not a bargain bonus

```
market = −weight × (our VOR − market_implied VOR),  clamped to ±max_market_points
```

The direction is deliberate and it is the opposite of what "ADP value"
intuitively suggests, so it is worth being explicit:

The surplus from a player falling past his worth is realised by **waiting for
him** — and waiting is exactly what `urgency` already prices, since a player
certain to be available earns no urgency credit. Nothing is lost by not paying
for it twice.

What remains, once that is stripped out, is **information**. When our projection
and the entire market disagree, the market has aggregated depth charts, camp
reports, and beat coverage that a projection source may not have. So we shade
toward it.

The cap (`max_market_points`, 8 by default) keeps this a tiebreaker. Without it,
positions the market structurally ignores — kickers and defenses, whose ADPs sit
150+ picks below where raw VOR puts them — swamp every other term. Set
`weights.market_value: 0` to ignore the market entirely.

### risk

The explicit, capped risk discount. See §10.

### tier_cliff

A top-up for the part of a tier drop that `urgency` did not already capture,
capped so the two never double-count:

```
tier_cliff = (1 − P_available) × max(0, tier_drop − urgency_gap)
```

Usually zero. It becomes non-zero when a player is the last of his tier and the
fall-off is steeper than the same-position alternatives suggest.

### Weights

All default to `1.0` (0.25 for market) so the composite reads in real points.
Changing a weight expresses a preference — chase upside, ignore the market — not
a unit conversion.

### Explainability

```bash
fantasy-ai analyze player "Name"                   # every component, with its description
fantasy-ai analyze compare "Player A" "Player B"   # component-by-component difference
```

---

## 8. Availability probability

**Modules:** `analytics/availability.py` (closed form), `draft/simulator.py`
(Monte Carlo) · **Config:** `simulation`

Two estimators. The CLI always labels which produced a number.

### Closed form — fast fallback

A player's realised draft slot is normal around his ADP; availability at pick
`N` is `P(D ≥ N)`, with a half-pick continuity correction.

Sigma is the source's published ADP standard deviation when available.
Otherwise `clamp(fraction × adp, floor, ceiling)` — dispersion grows roughly
with ADP (nobody is unsure about pick 1, everyone is unsure about pick 140),
with a floor so early picks are not treated as certain and a ceiling so deep
sleepers do not become uniformly random.

Two known limitations, both pushing the same direction (slightly optimistic
about mid-round players surviving):

- players are treated as independent, when in reality exactly one goes per pick;
- roster construction is invisible, so a run on tight ends cannot be seen.

### Monte Carlo — authoritative

The simulator (see [DRAFT_SIMULATION.md](DRAFT_SIMULATION.md)) has neither
limitation. When it runs, its probabilities are fed back into a second analytics
pass, so urgency and the draft score reflect the simulated estimate.

---

## 9. Roster fit

**Module:** `analytics/roster_fit.py` · **Config:** `analytics.bench_start_share`

Roster fit should influence recommendations without overwhelming raw player
value, so it is expressed as a **points adjustment relative to a
perfectly-fitting player** — zero for a player who slots straight into an open
starting spot, negative for one who would mostly sit:

```
lineup_gain = best_lineup(roster + player) − best_lineup(roster)
depth_gain  = bench_start_share × (points − lineup_gain)
roster_fit  = (lineup_gain + depth_gain) − points        # ≤ 0
```

`best_lineup` is an **exact** maximum-point starting lineup, not a heuristic.
The sets of players that can be simultaneously matched into slots form a
transversal matroid, so weight-ordered greedy with augmenting paths (Kuhn's
algorithm) is provably optimal — for nested eligibility (`QB` inside
`SUPERFLEX`) and crossing eligibility (`WRT` alongside `FLEX`) alike.

**`bench_start_share`** (default 0.25) is the fraction of the fantasy season a
rostered non-starter actually starts. Across a ~14-week regular season, the
player ahead of him misses roughly one bye plus two to four games to injury or
matchup, and there are usually two plausible fill-ins. Set it to 0 to treat
bench players as worthless.

### End of draft

When picks remaining no longer cover the starting slots still open, taking a
player who fills none of them means a starting slot is empty on opening day.
`forced_fill_penalty` charges the value of the best player who could have filled
the most valuable open slot — so the board insists on a kicker at exactly the
right moment, with no hard-coded "draft K in round 15" rule.

---

## 10. Risk

**Module:** `analytics/risk.py` · **Config:** `analytics.risk`

Risk is **represented explicitly, never folded into a projection**. Nothing here
modifies projected points; the profile is computed separately and enters the
draft score as a named, capped discount the user can see and zero out.

| Component | Source | Weight |
| --- | --- | --- |
| `projection_uncertainty` | Expert-ranking dispersion — the best available proxy for "nobody knows his role" | `consensus_weight` |
| `injury` | Current designation on a 0–1 severity scale | `injury_weight` |
| `age` | Years past the position's decline age (RBs early, QBs late) | `age_weight` |
| `role_uncertainty` | Rookies and players with no track record | `rookie_uncertainty` |

Age curves are per-position and configurable (`age_curve`, default QB 34 /
RB 27 / WR 30 / TE 31).

Role uncertainty widens outcomes in *both* directions, which is why it is
reported separately and weighted lightly — it is not a synonym for "bad".

The total is capped at `max_discount_points` (25) so risk can never dominate
value. `fantasy-ai analyze player <name>` shows each charge and its reason.

Players whose status means "will not play this season" (IR, PUP, retired) are
excluded from the board entirely rather than merely discounted.

---

## 11. Tiers

**Module:** `analytics/tiers.py` · **Config:** `analytics.tiers`

A tier break must be explainable, so the algorithm is local and simple rather
than a black-box clustering:

1. Sort a position's players by projected points, descending.
2. Compute consecutive gaps.
3. Break where a gap exceeds `mean(gap) + sensitivity × stdev(gap)`, subject to
   `min_gap_points` and min/max tier sizes.

Gap statistics come from the *top* of the position only
(`max_players_per_position`); a long tail of interchangeable bench players would
otherwise flatten the mean and make every early gap look enormous.

```
$ fantasy-ai analyze tiers TE
TE Tier 1: 1 player(s), 199.9-199.9 pts
    Bodhi Vandermeer               199.9

TE Tier 2: 1 player(s), 166.1-166.1 pts (opened by a 33.8-pt drop vs a 5.0-pt typical gap)
    Emory Hollingsworth            166.1
```

Each player also carries `players_left_in_tier` and `points_to_next_tier`, which
is what makes "last man in the tier" actionable.

---

## 12. Explainability

Every recommendation decomposes all the way down:

```
$ fantasy-ai analyze player "Bodhi Vandermeer" --breakdown

Bodhi Vandermeer (TE - ATL)
Projected points: 199.9 (11.8/game, source: demo)
Our rank: 16 overall, TE1
VOR = 199.9 projected - 63.1 replacement (TE) = +136.8
Tier 1 of TE (1 left in tier, 33.8 pts to next tier)
Our rank 16 vs ADP 11.0 (-5.0 picks) -- slight reach
Experts: mean 10.8, sd 2.3, range 7-13, typical sd at this rank 2.4 -- consensus
Next TE costs 33.8 pts; waiting for 5 more TEs off the board costs 93.2
P(available at pick 14) = 17% (ADP 11.0, sd 3.0, monte-carlo)
Roster fit high: starting-lineup gain 199.9 pts
Risk medium: -7.3 pts -- projection_uncertainty 2.8; injury 4.5 (Out)
Draft score +161.0 = value +136.8 urgency +29.0 market +2.6 risk -7.3

  How Bodhi Vandermeer's 199.9 points are computed
  Receiving yards   1146   0.1   +114.60
  Receptions       106.2   0.5    +53.10
  Receiving TD       5.6     6    +33.60
  Fumbles lost       0.7    -2     -1.40
                                 +199.90  total
```

Two invariants the test suite enforces:

- the scoring lines sum **exactly** to the projection;
- the draft-score components sum **exactly** to the total.

---

## Configuration summary

```yaml
analytics:
  replacement:
    method: starter_demand      # starter_demand | fixed_rank | blended
    window: 3                   # players averaged at the replacement rank
    offset: 0
    fixed_ranks: {}             # e.g. {QB: 12, RB: 30}
    blend_weight: 0.5
  tiers:
    sensitivity: 1.0            # break at mean + N standard deviations
    min_gap_points: 1.0
    max_tier_size: 8
  risk:
    consensus_weight: 0.15
    injury_weight: 6.0
    age_weight: 1.5
    age_curve: {QB: 34, RB: 27, WR: 30, TE: 31}
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
```
