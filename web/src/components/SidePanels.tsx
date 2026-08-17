import { useState } from "react";
import { api, RequestError } from "../api";
import type { Board, DataStatus, DraftStatus, Recommendation } from "../types";
import { Banner, Panel, PositionBadge, Spinner, num } from "./common";

export function RosterPanel({ board }: { board: Board }) {
  const roster = board.roster;
  const open = Object.entries(roster.open_slots);

  return (
    <Panel
      title="Your roster"
      action={
        roster.lineup_points > 0 ? (
          <span className="mono muted">{num(roster.lineup_points)} pts</span>
        ) : undefined
      }
    >
      {roster.lineup.length === 0 ? (
        <p className="muted" style={{ margin: 0 }}>
          Nothing drafted yet.
        </p>
      ) : (
        <div>
          {roster.lineup.map((slot, index) => (
            <div className="lineup-row" key={`${slot.slot}-${index}`}>
              <span className="lineup-slot">{slot.slot}</span>
              {slot.name ? (
                <>
                  <span>{slot.name}</span>
                  <span className="mono faint">{num(slot.points)}</span>
                </>
              ) : (
                <>
                  <span className="lineup-empty">empty</span>
                  <span />
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {open.length > 0 ? (
        <div style={{ marginTop: 10 }}>
          <div className="faint" style={{ marginBottom: 3 }}>
            Still to fill · {roster.picks_remaining} picks left
          </div>
          {open.map(([slot, count]) => (
            <span className="need-chip" key={slot}>
              {slot}
              {count > 1 ? ` ×${count}` : ""}
            </span>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

export function RecommendPanel({
  llmEnabled,
  onSelectPlayer,
}: {
  llmEnabled: boolean;
  onSelectPlayer: (name: string) => void;
}) {
  const [result, setResult] = useState<Recommendation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<RequestError | null>(null);

  async function run() {
    setLoading(true);
    setError(null);
    try {
      setResult(await api.recommend({ live: true }));
    } catch (cause) {
      setError(cause as RequestError);
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Panel
      title="Recommendation"
      action={
        <button className="tiny" onClick={run} disabled={loading}>
          {loading ? <Spinner /> : result ? "Again" : "Ask model"}
        </button>
      }
    >
      {!llmEnabled ? (
        <p className="faint" style={{ margin: 0 }}>
          The local model is disabled in <code>config/sources.yaml</code>. The board
          above is unaffected — it is computed without any model.
        </p>
      ) : null}

      {error ? (
        <Banner kind="error" title="Model unavailable">
          {error.message}
          {error.hint ? <div className="faint">{error.hint}</div> : null}
        </Banner>
      ) : null}

      {result?.ok && result.recommendation ? (
        <div>
          <div className="rec-headline">
            <button
              className="tiny"
              style={{ padding: 0, border: "none", background: "none" }}
              onClick={() => onSelectPlayer(result.recommendation!)}
              title="Show this player's analysis"
            >
              {result.recommendation}
            </button>
            {result.confidence ? (
              <span className="confidence">{result.confidence}</span>
            ) : null}
          </div>
          <ul className="rec-list">
            {result.reasoning.map((reason, index) => (
              <li key={index}>{reason}</li>
            ))}
          </ul>
          {result.alternatives.length > 0 ? (
            <div style={{ marginTop: 8 }}>
              <div className="faint">Alternatives</div>
              <ul className="rec-list">
                {result.alternatives.map((alt) => (
                  <li key={alt.player}>
                    <strong>{alt.player}</strong> — {alt.reason}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {result.risks.length > 0 ? (
            <div style={{ marginTop: 8 }}>
              <div className="faint">Risks</div>
              <ul className="rec-list">
                {result.risks.map((risk, index) => (
                  <li key={index}>{risk}</li>
                ))}
              </ul>
            </div>
          ) : null}
          <p className="faint" style={{ marginTop: 8 }}>
            {result.model} · {result.structured_mode} · {result.attempts} attempt
            {result.attempts === 1 ? "" : "s"}
            {result.latency_seconds ? ` · ${result.latency_seconds}s` : ""}
          </p>
        </div>
      ) : null}

      {result && !result.ok ? (
        <Banner kind="warn" title="Model output was unusable">
          <div>{result.failures[0] ?? "The model did not return valid JSON."}</div>
          {result.deterministic_top ? (
            <div style={{ marginTop: 6 }}>
              Deterministic top candidate: <strong>{result.deterministic_top}</strong>
            </div>
          ) : null}
        </Banner>
      ) : null}

      {!result && !error && !loading ? (
        <p className="faint" style={{ margin: 0 }}>
          The model reads the analytics above and explains the trade-off. It never
          computes a number.
        </p>
      ) : null}
    </Panel>
  );
}

export function RecentPicksPanel({ status }: { status: DraftStatus }) {
  if (!status.active || status.recent_picks.length === 0) return null;
  return (
    <Panel title="Recent picks">
      {status.recent_picks.map((pick) => (
        <div
          className={`pick-row${pick.is_user ? " you" : ""}`}
          key={pick.overall_pick}
        >
          <span className="pick-no">
            {pick.round_number}.{String(pick.slot).padStart(2, "0")}
          </span>
          <span>
            {pick.name ?? <em className="faint">unknown</em>}
            {pick.position ? (
              <span className="faint"> · {pick.position}</span>
            ) : null}
            {pick.keeper ? <span className="faint"> · keeper</span> : null}
          </span>
        </div>
      ))}
    </Panel>
  );
}

export function ScarcityPanel({ board }: { board: Board }) {
  return (
    <Panel title="Positional scarcity">
      <table className="mini">
        <thead>
          <tr>
            <th className="left">Pos</th>
            <th>Left</th>
            <th>Need</th>
            <th>Decay</th>
          </tr>
        </thead>
        <tbody>
          {board.scarcity.map((entry) => (
            <tr key={entry.position}>
              <td className="left">
                <PositionBadge position={entry.position} />
              </td>
              <td>{entry.above_replacement_count}</td>
              <td>{entry.remaining_demand}</td>
              <td>{num(entry.average_decay)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="faint" style={{ marginTop: 6 }}>
        Left = players above replacement. Need = unfilled starting slots league-wide.
        Decay = points lost per player down the board.
      </p>
    </Panel>
  );
}

export function DataPanel({ data }: { data: DataStatus | null }) {
  if (!data) return null;
  return (
    <Panel title="Data">
      <table className="mini">
        <tbody>
          {data.datasets
            .filter((entry) => entry.records > 0)
            .map((entry) => (
              <tr key={`${entry.dataset}-${entry.source}`}>
                <td className="left">
                  {entry.dataset}
                  <span className="faint"> · {entry.source}</span>
                </td>
                <td className={entry.stale ? "negative" : "faint"}>
                  {entry.described_age}
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      {data.stale_count > 0 ? (
        <p className="faint" style={{ marginTop: 6 }}>
          {data.stale_count} dataset(s) older than {data.threshold_hours}h. Run{" "}
          <code>fantasy-ai sync all</code>.
        </p>
      ) : null}
    </Panel>
  );
}
