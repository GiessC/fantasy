# Local LLM Integration

The model interprets analytics. It never produces them.

Nothing in `llm/` computes a fantasy number, and everything it returns is
validated against the context it was given before you see it.

---

## Runtime

LM Studio exposes an OpenAI-compatible `/v1` API, so this speaks that protocol
rather than any model-specific SDK. The same code therefore works with LM
Studio, Ollama's OpenAI shim, llama.cpp's server, or vLLM — and swapping the
model behind it is a YAML edit.

```yaml
llm:
  enabled: true
  base_url: http://localhost:1234/v1
  model: muse-glimmer
  temperature: 0.2
  max_tokens: 1200
  timeout_seconds: 120
  structured_output: json_schema    # json_schema | json_object | prompt_only | off
  max_parse_retries: 2
  max_candidates: 8
```

```bash
fantasy-ai llm models     # what the server reports it has loaded
fantasy-ai llm status     # reachable? is the configured model actually loaded?
```

`llm status` distinguishes "server unreachable" from "server up, wrong model" —
and in the second case lists what *is* available, which is usually the fix.

---

## Division of responsibility

**The model does:** interpret analytics, compare candidates, explain trade-offs,
surface contextual considerations, produce a concise recommendation, answer
questions about the analysis.

**The model does not:** calculate fantasy points, calculate ADP, decide
replacement levels, invent statistics, silently correct database values, or
claim to have fetched live data.

That boundary is enforced structurally, not just requested politely. The model
only ever receives numbers that were already computed, and its output is checked
against those numbers before display.

---

## Structured output

Attempted in descending order of strictness, because local runtimes vary:

1. **`json_schema`** — the server constrains generation to the schema.
2. **`json_object`** — the server guarantees syntactically valid JSON.
3. **prompt-only** — we ask for JSON and parse defensively.

A server that rejects a mode with a 4xx is **automatically retried one step
down**, and the working mode is remembered for the rest of the session. So a
runtime without grammar support degrades quietly instead of failing.

The JSON Schema is written out by hand rather than generated from the Pydantic
model: local grammar compilers are picky, and want no `$ref`, no `anyOf`, every
property required, and `additionalProperties: false` throughout.

---

## Context builder

> Only send the relevant subset to the model. Do not dump the entire database
> into context.

The context object contains:

- league configuration summary (teams, scoring format, starting lineup, flex
  eligibility, superflex, house-rule notes),
- current draft position, pick number, and how long until your next turn,
- your roster, current optimal starting lineup, and open starting slots,
- **the top N candidates** (default 8) with their full analytics,
- positional scarcity and replacement levels,
- simulation output, including expected best available by position,
- data freshness, with an explicit warning when anything is stale.

Roughly **1,300–1,900 tokens** for 6–8 candidates. Internal player ids are
stripped — they cost tokens and invite the model to quote them back at you.

```bash
fantasy-ai llm context              # exactly what would be sent
fantasy-ai recommend --dry-run      # the full prompt, without calling the model
fantasy-ai recommend --show-context
```

Every candidate carries its decomposition, so the model can cite specific
numbers rather than gesture at them:

```json
{
  "name": "Nolan Pemberton",
  "position": "WR",
  "projected_points": 280.7,
  "vor": 209.98,
  "tier": 1,
  "players_left_in_tier": 1,
  "adp": 1.3,
  "availability_next_pick": 0.0,
  "roster_fit": "high",
  "risk": "low",
  "draft_score": 233.96,
  "draft_score_explained": {
    "value": 210.0, "urgency": 28.7, "roster_fit": 0.0,
    "market": 0.1, "risk": -4.9
  },
  "risk_factors": ["projection_uncertainty: expert sd 1.6, range 1-4 (-4.9 pts)"],
  "position_run_cost": 56.9
}
```

---

## Prompt philosophy

The system prompt states the ground rules directly:

1. The supplied numbers are **authoritative**. Never recompute, adjust, or
   contradict them. A figure that is not present is unavailable, not estimable.
2. Never invent players, statistics, injuries, or news. Only players in the
   context exist.
3. **Distinguish computed fact from judgement.** "VOR is +47" is a fact from the
   context; "I would rather have the safer floor" is judgement.
4. Recommend for **this** league. Superflex is not 1-QB; non-PPR is not PPR.
5. Always explain **why the best alternative was not chosen** — that comparison
   is the most useful thing the model produces.
6. Be concise. This is used live, on the clock.

It also includes a short glossary of the analytics, so the model reads `vor`,
`availability_next_pick`, `roster_fit_points` and `position_run_cost` correctly
instead of inferring from the names.

`--live` adds an explicit brevity instruction: one line per bullet, three
bullets or fewer, no preamble.

Prompts live in `llm/prompts.py` so changes are reviewable in isolation.
`llm.system_prompt_override` replaces the system prompt entirely.

---

## Validation

Parsing is defensive, because local models are less reliable than hosted ones at
emitting bare JSON even under a response-format constraint:

1. Strip markdown fences.
2. Find the outermost balanced `{...}`, ignoring braces inside strings and
   escaped quotes.
3. Validate against the Pydantic schema.
4. **Verify every player the model named appears in the context.**

Step 4 is the one the schema cannot express, and it is the defence against the
failure that would matter most during a live draft. A near-miss (`"Joshua
Allen"` for `"Josh Allen"`, a dropped `Jr.`) is **repaired** using the same name
normalization the identity resolver uses. An outright invented player is
**rejected**.

On any failure the model is re-prompted with a specific correction, because
"your JSON was invalid" corrects far worse than:

```
you named 'Definitely Not Real', which is not in the candidates list.
Use only these names: Ambrose Yardley, Bodhi Vandermeer, Harlan Bellweather, ...
```

After `max_parse_retries`, the command falls back to showing the raw model
output **alongside the deterministic top candidate**, clearly labelled. A local
model that cannot produce clean JSON should not cost you your pick.

---

## Recommendation format

```
$ fantasy-ai recommend --live

╭─ Model recommendation ───────────────────────────────────────────╮
│ RECOMMENDATION: Ambrose Yardley  (confidence: high)               │
│                                                                   │
│ Why:                                                              │
│ - Highest VOR among the candidates                                │
│ - Only a 17% chance of making it back to pick 14                  │
│ - Fills an open starting slot                                     │
│                                                                   │
│ Alternatives:                                                     │
│ - Bodhi Vandermeer - safer floor, but less positional edge         │
│                                                                   │
│ Risks:                                                            │
│ - projection uncertainty                                          │
╰───────────────────────────────────────────────────────────────────╯
model=muse-glimmer mode=json_schema attempts=1 latency=2.3s
Deterministic top candidate: Ambrose Yardley (score +179.7). The model
interprets these numbers; it does not compute them.
```

The deterministic top candidate is always shown next to the model's answer, so
you can see at a glance whether they agree — and act on the numbers if the model
is unavailable, slow, or unconvincing.

---

## Asking questions

```bash
fantasy-ai ask "Should I take a tight end here or wait until round 5?"
fantasy-ai ask "How much does my roster need a quarterback?"
```

Answers are structured too (`answer`, `players_referenced`, `caveats`). An
unknown player in a free-form answer is a **warning** rather than a rejection —
you may have asked about someone outside the top candidates, and the answer can
still be useful with a caveat attached.

---

## Working without a model

Everything except `recommend` and `ask` runs with no model at all. Set
`llm.enabled: false` and the deterministic analysis is unaffected:

```bash
fantasy-ai analyze board          # the same board the model would see
fantasy-ai analyze compare A B    # the same comparison, without prose
fantasy-ai simulate availability
```

The model adds explanation and judgement. It is not load-bearing.
