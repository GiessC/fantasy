# Draft Simulation

The simulator answers one question:

> If I draft Player X now, what is likely to be available later?

**Module:** `draft/simulator.py` · **Config:** `simulation:`

---

## The model

ADP is a **distribution**, not a schedule. Selecting players in exact ADP order
would make every availability probability 0 or 1 — both wrong and useless.

For each simulated pick by another team:

1. Take the shallowest `candidate_pool` (60) available players by ADP.
2. Give each a perturbed draft priority:

   ```
   priority = adp + need_shift + Normal(0, sigma)
   ```

3. The lowest priority is selected.

### sigma

The source's published ADP standard deviation when available. Otherwise
`clamp(adp_noise_fraction × adp, floor, ceiling)` — dispersion grows roughly
with ADP, with a floor (3 picks) so early picks are not treated as certain and a
ceiling (45) so deep sleepers do not become uniformly random.

### need_shift

Measured **in picks**: "this team will reach about three picks early for a
position it still has to start."

```
need_shift = -need_weight × teams        # position not yet started
           = -need_weight × teams × 0.5  # only a flex slot left
           = +need_weight × teams × 0.5  # position already filled
           = excluded                    # team is at its cap
```

It is deliberately **additive**. An earlier version scaled ADP multiplicatively,
which uniformly compressed the gaps between players and let the noise term swamp
the ADP signal — obvious first-round picks survived into the second round far
too often. The regression test `test_adp_signal_survives_the_need_adjustment`
guards against that returning.

`need_weight: 0` gives pure-ADP sampling.

### Positional caps

A simulated team will not draft a third quarterback in a one-QB league. Caps are
**derived from the league**, not hard-coded:

```
cap = dedicated_starting_slots + flex_capacity + bench_depth
```

where bench depth is 0 for K/DST, 3 for flex-eligible positions, 1 otherwise. A
superflex league raises the QB cap automatically, because QB gains flex
capacity.

Without caps, raw ADP sampling occasionally has a team take a fourth tight end,
which visibly distorts late-round availability.

---

## Availability

```bash
$ fantasy-ai simulate availability
                    Availability at pick 14
Player                Pos    ADP   Monte Carlo   Mean pick taken    Score
Ambrose Yardley       WR     6.4            0%             8.7    +179.7
Bodhi Vandermeer      TE    11.0           17%            11.1    +161.0
Harlan Bellweather    WR    13.9           78%            12.3    +158.9
Ulric Carrow          WR    28.8          100%        survives    +135.5

1000 simulations over 6 picks (pick 8 -> 14), seed 20260817
Expected best VOR still available by position: QB 112, RB 112, TE 108, WR 156
```

Availability probabilities feed back into a **second analytics pass**, so
`urgency` and the draft score reflect the simulated estimate rather than the
closed-form approximation. The CLI always labels which estimator produced a
number.

`expected_best_by_position` is what makes "if he is gone, what do I get?"
answerable — it is the fallback the urgency term prices against.

### Versus the closed form

`analytics/availability.py` provides a fast normal-CDF estimate used when no
simulation runs. It treats players as independent and cannot see positional
runs, both of which make it slightly optimistic about mid-round players
surviving. The simulator has neither limitation, which is why it is
authoritative.

---

## Strategy comparison

```bash
$ fantasy-ai simulate draft --candidates 4 --iterations 200

      Expected starting lineup if you take this player now
Player               Pos    Mean lineup pts   vs best   Std dev
Ambrose Yardley      WR              1516.0      +0.0      40.2
Bodhi Vandermeer     TE              1513.7      -2.3      56.8
Harlan Bellweather   WR              1504.7     -11.4      41.5
Hollis Galloway      WR              1487.0     -29.0      38.5
```

For each candidate: take them now, simulate the **rest of the draft**, and value
the resulting optimal starting lineup.

The user's later picks are made by a simple policy — best VOR that fills a
still-open starting slot, else best VOR at a quarter weight for bench depth — so
the comparison isolates the effect of *this* pick rather than of a sophisticated
future policy.

**Differences smaller than the standard deviation are noise**, and the CLI says
so. Raise `--iterations` to narrow them.

---

## Reproducibility

Every simulation is seeded (`simulation.seed`, default `20260817`). The same
seed and inputs give the same answer, which is what makes the results testable
and the analysis reproducible.

```bash
fantasy-ai --sim-seed 42 --sim-iterations 5000 simulate availability
```

Set `seed: null` for non-reproducible runs.

---

## Performance

Availability over ~7 picks with 1000 iterations: **~0.3 s**. Strategy comparison
over 4 candidates × 100 full drafts: **~1.5 s**.

Two things matter for keeping it that fast, and both were real regressions
caught during development:

- League-derived quantities (`dedicated_starters()`, `flex_capacity()`) are
  cached in the simulator's constructor. Recomputing them per candidate per pick
  rebuilt typed config models millions of times and cost a 60× slowdown.
- The user's autopilot solves the optimal lineup **once per pick**, not once per
  candidate. Per-candidate solves were exact but dominated the whole runtime,
  and produced the same ranking.

---

## What this does not claim

It does **not** predict any specific opponent. It is a decision-support model
over plausible draft flows, and it should be read as one: a 20% availability
means "in one simulated draft in five, he was still there", not "he has a 20%
chance in your actual draft".

Known simplifications:

- Every opponent uses the same behaviour model, unless profiles are supplied.
- Reactions to *specific* runs are only modelled through positional need.
- Auction drafts are not simulated (snake, linear and third-round reversal are).
- Keepers are not removed from the pool automatically; record them as picks.

---

## Opponent profiles

`DrafterProfile` supports per-opponent tendencies where you know them:

```python
DrafterProfile(
    slot=4,
    position_bias={"TE": -8.0},   # reaches ~8 picks early for tight ends
    reach_tendency=-3.0,          # drafts ~3 picks ahead of ADP generally
    name="Dave",
)
```

Both fields are in picks. The intended source is real league draft history —
positional tendencies, ADP deviations, whether someone starts runs — which
[DEVELOPMENT.md](DEVELOPMENT.md) lists as a planned feature. The mechanism is
built and tested; the automatic learning of profiles from history is not.

---

## Configuration

```yaml
simulation:
  iterations: 1000              # availability runs
  seed: 20260817                # null for non-reproducible
  adp_noise_fraction: 0.22      # sigma as a fraction of ADP
  adp_noise_floor: 3.0          # minimum sigma, in picks
  adp_noise_ceiling: 45.0       # maximum sigma
  need_weight: 0.35             # 0 = pure ADP
  undrafted_adp_padding: 40.0   # how far past the deepest ADP a no-ADP player sits
  candidate_pool: 60            # players considered per simulated pick
```
