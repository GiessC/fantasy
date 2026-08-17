// Wire types, mirroring src/fantasy_ai/api/schemas.py.
// Kept hand-written rather than generated so the surface stays small and
// reviewable; the API tests assert the shapes these describe.

export interface ScoreComponent {
  name: string;
  raw: number;
  weight: number;
  contribution: number;
  description: string;
}

export interface Player {
  player_id: string;
  name: string;
  position: string;
  team: string | null;
  bye_week: number | null;

  projected_points: number;
  points_per_game: number;
  our_rank: number;
  position_rank: number;
  replacement_points: number;
  vor: number;

  tier: number | null;
  players_left_in_tier: number | null;
  points_to_next_tier: number | null;

  adp: number | null;
  adp_value_picks: number | null;
  consensus_label: string | null;
  expert_stdev: number | null;

  next_player_delta: number | null;
  dropoff_5: number | null;

  availability_next_pick: number | null;
  availability_method: string | null;

  roster_fit: string | null;
  roster_fit_points: number | null;
  risk: string | null;
  risk_points: number | null;
  injury_status: string | null;

  draft_score: number | null;
  components: ScoreComponent[];

  projection_source: string | null;
  adp_source: string | null;
}

export interface ReplacementLevel {
  position: string;
  points: number;
  rank: number;
  demand: number;
  method: string;
  has_starting_demand: boolean;
  explanation: string;
}

export interface PositionScarcity {
  position: string;
  replacement_points: number;
  above_replacement_count: number;
  remaining_demand: number;
  starter_supply_ratio: number;
  average_decay: number;
  scarcity_index: number;
}

export interface LineupSlot {
  slot: string;
  player_id: string | null;
  name: string | null;
  position: string | null;
  points: number | null;
}

export interface PlayerRef {
  player_id: string;
  name: string;
  position: string | null;
  team: string | null;
}

export interface Roster {
  players: PlayerRef[];
  counts_by_position: Record<string, number>;
  lineup: LineupSlot[];
  lineup_points: number;
  open_slots: Record<string, number>;
  positions_needed: string[];
  picks_remaining: number;
}

export interface Simulation {
  iterations: number;
  seed: number | null;
  target_pick: number;
  picks_simulated: number;
  expected_best_by_position: Record<string, number>;
  expected_best_overall: number;
}

export interface Board {
  season: number;
  generated_at: string;
  availability_method: "monte-carlo" | "analytic" | "none";
  next_pick: number | null;
  current_pick: number | null;
  total_available: number;
  players: Player[];
  replacement: ReplacementLevel[];
  scarcity: PositionScarcity[];
  roster: Roster;
  warnings: string[];
  simulation: Simulation | null;
  state_version: string;
}

export interface ScoringLine {
  label: string;
  units: number;
  rate: number;
  points: number;
  note: string | null;
}

export interface PlayerDetail {
  player: Player;
  explanation: string[];
  scoring_lines: ScoringLine[];
  tier_players: PlayerRef[];
}

export interface Pick {
  overall_pick: number;
  round_number: number;
  slot: number;
  is_user: boolean;
  keeper: boolean;
  player_id: string | null;
  name: string | null;
  position: string | null;
  team: string | null;
}

export interface DraftStatus {
  active: boolean;
  draft_id: number | null;
  name: string | null;
  season: number | null;
  teams: number | null;
  rounds: number | null;
  draft_type: string | null;
  user_slot: number | null;
  current_pick: number | null;
  current_round: number | null;
  on_the_clock_slot: number | null;
  is_user_on_the_clock: boolean;
  user_next_pick: number | null;
  picks_until_user: number;
  picks_remaining_for_user: number;
  total_picks: number;
  drafted_count: number;
  is_complete: boolean;
  user_picks: number[];
  recent_picks: Pick[];
  roster: Roster;
  state_version: string;
}

export interface PickResult {
  pick: Pick;
  status: DraftStatus;
}

export interface League {
  name: string;
  season: number;
  type: string;
  teams: number;
  scoring_format: string;
  starting_lineup: Record<string, number>;
  flex_eligibility: Record<string, string[]>;
  bench: number;
  roster_size: number;
  rounds: number;
  draft_type: string;
  draft_position: number | null;
  superflex: boolean;
  notes: string[];
}

export interface ApiInfo {
  version: string;
  league: League;
  has_data: boolean;
  llm_enabled: boolean;
  config_warnings: string[];
  settings_files: Record<string, string>;
}

export interface Freshness {
  dataset: string;
  source: string;
  records: number;
  age_hours: number | null;
  described_age: string;
  stale: boolean;
}

export interface DataStatus {
  season: number;
  threshold_hours: number;
  datasets: Freshness[];
  has_data: boolean;
  stale_count: number;
  missing: string[];
}

export interface SearchResult {
  player_id: string;
  name: string;
  position: string | null;
  team: string | null;
  drafted: boolean;
}

export interface Recommendation {
  ok: boolean;
  recommendation: string | null;
  confidence: string | null;
  reasoning: string[];
  alternatives: { player: string; reason: string }[];
  risks: string[];
  deterministic_top: string | null;
  model: string | null;
  attempts: number;
  structured_mode: string | null;
  latency_seconds: number | null;
  raw_text: string | null;
  failures: string[];
}

export interface Answer {
  ok: boolean;
  answer: string | null;
  caveats: string[];
  unknown_players: string[];
  model: string | null;
  raw_text: string | null;
}

export interface LLMStatus {
  enabled: boolean;
  reachable: boolean;
  message: string;
  base_url: string;
  model: string;
  available_models: string[];
}

export interface ApiError {
  error: string;
  detail: string | null;
  hint: string | null;
}
