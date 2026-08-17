import { useEffect, useState } from "react";
import { api, RequestError } from "../api";
import type { PlayerDetail } from "../types";
import { Availability, Banner, PositionBadge, Signed, Spinner, num } from "./common";

export function PlayerDrawer({
  playerId,
  canPick,
  onClose,
  onTake,
}: {
  playerId: string;
  canPick: boolean;
  onClose: () => void;
  onTake: (playerId: string, name: string) => void;
}) {
  const [detail, setDetail] = useState<PlayerDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    api
      .player(playerId)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((cause: RequestError) => {
        if (!cancelled) setError(cause.message);
      });
    return () => {
      cancelled = true;
    };
  }, [playerId]);

  // Escape closes, which is what every drawer everywhere does.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const player = detail?.player;

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" aria-label="Player detail">
        <div className="drawer-head">
          <div>
            {player ? (
              <>
                <h2>{player.name}</h2>
                <div className="muted" style={{ marginTop: 2 }}>
                  <PositionBadge position={player.position} />{" "}
                  {player.team ?? "Free agent"} · {player.position}
                  {player.position_rank} · overall #{player.our_rank}
                  {player.bye_week ? ` · bye ${player.bye_week}` : ""}
                </div>
              </>
            ) : (
              <h2>Loading…</h2>
            )}
          </div>
          <div className="row">
            {canPick && player ? (
              <button
                className="primary"
                onClick={() => onTake(player.player_id, player.name)}
              >
                Take
              </button>
            ) : null}
            <button onClick={onClose} aria-label="Close">
              ✕
            </button>
          </div>
        </div>

        {error ? (
          <Banner kind="error" title="Could not load this player">
            {error}
          </Banner>
        ) : null}
        {!detail && !error ? (
          <p className="muted">
            <Spinner /> Loading analysis…
          </p>
        ) : null}

        {detail && player ? (
          <>
            <div className="stat-grid">
              <Stat label="Projected" value={num(player.projected_points)} />
              <Stat label="Per game" value={num(player.points_per_game)} />
              <Stat label="VOR" value={<Signed value={player.vor} />} />
              <Stat label="Draft score" value={num(player.draft_score)} />
              <Stat
                label="Available"
                value={<Availability value={player.availability_next_pick} />}
              />
              <Stat label="ADP" value={num(player.adp)} />
            </div>

            {player.components.length > 0 ? (
              <section style={{ marginTop: 18 }}>
                <h3 className="panel-title">
                  <span>Draft score, decomposed</span>
                  <span className="mono">{num(player.draft_score)}</span>
                </h3>
                {player.components.map((component) => (
                  <div className="component-row" key={component.name}>
                    <span className="component-name">{component.name}</span>
                    <span className="component-value">
                      <Signed value={component.contribution} />
                    </span>
                    <span className="faint">{component.description}</span>
                  </div>
                ))}
              </section>
            ) : null}

            <section style={{ marginTop: 18 }}>
              <h3 className="panel-title">
                <span>How the projection scores</span>
                <span className="mono">{num(player.projected_points)}</span>
              </h3>
              <table className="mini">
                <thead>
                  <tr>
                    <th className="left">Stat</th>
                    <th>Units</th>
                    <th>Rate</th>
                    <th>Points</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.scoring_lines.map((line) => (
                    <tr key={line.label}>
                      <td className="left">
                        {line.label}
                        {line.note ? (
                          <div className="faint">{line.note}</div>
                        ) : null}
                      </td>
                      <td>{line.units}</td>
                      <td>{line.rate}</td>
                      <td>
                        <Signed value={line.points} digits={2} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="faint" style={{ marginTop: 6 }}>
                Computed under your league's scoring, from{" "}
                {player.projection_source ?? "an unknown source"}.
              </p>
            </section>

            <section style={{ marginTop: 18 }}>
              <h3 className="panel-title">Full explanation</h3>
              <ul className="explain-list">
                {detail.explanation.map((line, index) => (
                  <li key={index}>{line}</li>
                ))}
              </ul>
            </section>

            {detail.tier_players.length > 1 ? (
              <section style={{ marginTop: 18 }}>
                <h3 className="panel-title">
                  {player.position} tier {player.tier}
                </h3>
                <p className="muted" style={{ margin: 0, fontSize: 13 }}>
                  {detail.tier_players.map((entry) => entry.name).join(" · ")}
                </p>
                {player.points_to_next_tier ? (
                  <p className="faint" style={{ marginTop: 4 }}>
                    {num(player.points_to_next_tier)} points down to the next tier.
                  </p>
                ) : null}
              </section>
            ) : null}
          </>
        ) : null}
      </aside>
    </>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}
