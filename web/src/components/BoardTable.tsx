import { useMemo, useState } from "react";
import type { Player } from "../types";
import { Availability, PositionBadge, Signed, num } from "./common";

type SortKey =
  | "draft_score"
  | "vor"
  | "projected_points"
  | "adp"
  | "adp_value_picks"
  | "availability_next_pick";

type SortDir = "asc" | "desc";

// The direction a column takes on its *first* click, chosen so that one click
// always puts the most interesting rows on top. ADP is the odd one out: a lower
// number means drafted earlier, so ascending is the useful default there.
const DEFAULT_DIR: Record<SortKey, SortDir> = {
  draft_score: "desc",
  vor: "desc",
  projected_points: "desc",
  adp: "asc",
  adp_value_picks: "desc",
  availability_next_pick: "desc",
};

const COLUMNS: { key: SortKey; label: string; title: string }[] = [
  { key: "projected_points", label: "Proj", title: "League-adjusted projected season points" },
  { key: "vor", label: "VOR", title: "Points above the replacement-level starter" },
  { key: "adp", label: "ADP", title: "Average draft position" },
  { key: "adp_value_picks", label: "Val", title: "ADP minus our rank, in picks" },
  {
    key: "availability_next_pick",
    label: "Avail",
    title: "Probability he is still there at your next pick",
  },
  { key: "draft_score", label: "Score", title: "Composite draft score, in points" },
];

export function BoardTable({
  players,
  selectedId,
  canPick,
  onSelect,
  onTake,
}: {
  players: Player[];
  selectedId: string | null;
  canPick: boolean;
  onSelect: (player: Player) => void;
  onTake: (player: Player) => void;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("draft_score");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Click a new column to sort by it; click the current one to reverse.
  function applySort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((current) => (current === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(DEFAULT_DIR[key]);
    }
  }

  const sorted = useMemo(() => {
    const copy = [...players];
    const factor = sortDir === "asc" ? 1 : -1;
    copy.sort((a, b) => {
      const left = a[sortKey];
      const right = b[sortKey];
      // Nulls sort last in BOTH directions. A missing value is not a small
      // one, and floating unprojected players to the top of an ascending sort
      // would bury the rows actually being asked for.
      if (left === null && right === null) return 0;
      if (left === null) return 1;
      if (right === null) return -1;
      if (left === right) return 0;
      return (left < right ? -1 : 1) * factor;
    });
    return copy;
  }, [players, sortKey, sortDir]);

  if (players.length === 0) {
    return (
      <div className="panel">
        <p className="muted" style={{ margin: 0 }}>
          No players match this filter.
        </p>
      </div>
    );
  }

  return (
    <div className="board-wrap">
      <table className="board">
        <thead>
          <tr>
            <th className="left">#</th>
            <th className="left">Player</th>
            <th>Bye</th>
            <th>Tier</th>
            {COLUMNS.map((column) => (
              <th
                key={column.key}
                className="sortable"
                title={`${column.title} — click to sort, click again to reverse`}
                aria-sort={
                  sortKey === column.key
                    ? sortDir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                tabIndex={0}
                role="columnheader"
                onClick={() => applySort(column.key)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    applySort(column.key);
                  }
                }}
              >
                {column.label}
                <span className="sort-arrow" aria-hidden="true">
                  {sortKey === column.key ? (sortDir === "asc" ? "▲" : "▼") : ""}
                </span>
              </th>
            ))}
            <th>Fit</th>
            <th>Risk</th>
            {canPick ? <th aria-label="Draft" /> : null}
          </tr>
        </thead>
        <tbody>
          {sorted.map((player, index) => (
            <tr
              key={player.player_id}
              className={player.player_id === selectedId ? "selected" : undefined}
              onClick={() => onSelect(player)}
            >
              <td className="left faint mono">{index + 1}</td>
              <td className="left">
                <div className="player-cell">
                  <PositionBadge position={player.position} />
                  <span>
                    <span className="player-name">{player.name}</span>{" "}
                    <span className="player-sub">
                      {player.team ?? "FA"} · {player.position}
                      {player.position_rank}
                    </span>
                  </span>
                </div>
              </td>
              <td className="faint">{player.bye_week ?? "–"}</td>
              <td className="faint">{player.tier ?? "–"}</td>
              <td>{num(player.projected_points)}</td>
              <td>
                <Signed value={player.vor} />
              </td>
              <td>{num(player.adp)}</td>
              <td>
                <Signed value={player.adp_value_picks} digits={0} />
              </td>
              <td>
                <Availability value={player.availability_next_pick} />
              </td>
              <td>
                <strong>{num(player.draft_score)}</strong>
              </td>
              <td>
                <Signed value={player.roster_fit_points} digits={0} />
              </td>
              <td className={`risk-${player.risk ?? "minimal"}`}>{player.risk ?? "–"}</td>
              {canPick ? (
                <td className="take-cell">
                  <button
                    className="tiny"
                    title={`Record ${player.name} as taken at the next pick`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onTake(player);
                    }}
                  >
                    Take
                  </button>
                </td>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
