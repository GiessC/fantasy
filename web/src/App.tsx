import { useCallback, useEffect, useMemo, useState } from "react";
import { api, RequestError } from "./api";
import { BoardTable } from "./components/BoardTable";
import { Banner, Spinner } from "./components/common";
import { PickSearch, StartDraftForm } from "./components/DraftControls";
import { PlayerDrawer } from "./components/PlayerDrawer";
import {
  DataPanel,
  RecentPicksPanel,
  RecommendPanel,
  RosterPanel,
  ScarcityPanel,
} from "./components/SidePanels";
import type { ApiInfo, Board, DataStatus, DraftStatus, Player } from "./types";

const POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"];

export default function App() {
  const [info, setInfo] = useState<ApiInfo | null>(null);
  const [board, setBoard] = useState<Board | null>(null);
  const [status, setStatus] = useState<DraftStatus | null>(null);
  const [data, setData] = useState<DataStatus | null>(null);

  const [position, setPosition] = useState<string | null>(null);
  const [limit, setLimit] = useState(40);
  const [selected, setSelected] = useState<string | null>(null);

  const [error, setError] = useState<RequestError | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [nextBoard, nextStatus] = await Promise.all([
        api.board({ limit: 200 }),
        api.draftStatus(),
      ]);
      setBoard(nextBoard);
      setStatus(nextStatus);
      setError(null);
    } catch (cause) {
      setError(cause as RequestError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const details = await api.info();
        if (cancelled) return;
        setInfo(details);
        if (!details.has_data) {
          setLoading(false);
          return;
        }
        await refresh();
        api.dataStatus().then(setData).catch(() => undefined);
      } catch (cause) {
        if (!cancelled) {
          setError(cause as RequestError);
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  // Transient confirmations ("Recorded X") clear themselves.
  useEffect(() => {
    if (!flash) return;
    const timer = window.setTimeout(() => setFlash(null), 3200);
    return () => window.clearTimeout(timer);
  }, [flash]);

  const visible = useMemo(() => {
    if (!board) return [];
    const filtered = position
      ? board.players.filter((player) => player.position === position)
      : board.players;
    return filtered.slice(0, limit);
  }, [board, position, limit]);

  const canPick = Boolean(status?.active && !status.is_complete);

  const onTake = useCallback(
    async (player: Player) => {
      try {
        const outcome = await api.pick({ player_id: player.player_id });
        setStatus(outcome.status);
        setFlash(`Recorded ${player.name} at pick ${outcome.pick.overall_pick}.`);
        setSelected(null);
        await refresh();
      } catch (cause) {
        setError(cause as RequestError);
      }
    },
    [refresh],
  );

  async function runAction(action: () => Promise<unknown>, message: string) {
    try {
      await action();
      setFlash(message);
      await refresh();
    } catch (cause) {
      setError(cause as RequestError);
    }
  }

  if (loading) {
    return (
      <div className="empty-state">
        <Spinner /> <span style={{ marginLeft: 8 }}>Analysing the board…</span>
      </div>
    );
  }

  if (info && !info.has_data) {
    return (
      <div className="empty-state">
        <h2>No player data yet</h2>
        <p>
          Load a dataset, then reload this page.
        </p>
        <p>
          <code>fantasy-ai sync demo</code> generates a synthetic season offline, or{" "}
          <code>fantasy-ai sync all</code> pulls real data.
        </p>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          {info?.league.name ?? "Fantasy AI"}
          <span className="season">
            {info?.league.season} · {info?.league.teams}-team ·{" "}
            {info?.league.scoring_format}
          </span>
        </div>

        {status?.active ? (
          <div className={`clock${status.is_user_on_the_clock ? " you" : ""}`}>
            {status.is_complete ? (
              <span>Draft complete</span>
            ) : (
              <>
                <span className="pill-label">Pick</span>
                <span className="mono">
                  {status.current_round}.{String(status.on_the_clock_slot).padStart(2, "0")}
                </span>
                <span>
                  {status.is_user_on_the_clock
                    ? "You're on the clock"
                    : `Slot ${status.on_the_clock_slot}`}
                </span>
                {!status.is_user_on_the_clock && status.user_next_pick ? (
                  <span className="pill-label">
                    · you pick in {status.picks_until_user}
                  </span>
                ) : null}
              </>
            )}
          </div>
        ) : null}

        <div className="header-spacer" />

        <div className="header-actions">
          {status?.active ? (
            <>
              <PickSearch
                disabled={!canPick}
                onPicked={(next, name) => {
                  setStatus(next);
                  setFlash(`Recorded ${name}.`);
                  void refresh();
                }}
                onError={setError}
              />
              <button
                title="Record a pick whose player you don't know"
                disabled={!canPick}
                onClick={() => runAction(api.skip, "Advanced one pick.")}
              >
                Skip
              </button>
              <button
                className="danger"
                disabled={!status.drafted_count}
                onClick={() => runAction(api.undo, "Undid the last pick.")}
              >
                Undo
              </button>
            </>
          ) : info ? (
            <StartDraftForm
              defaultPosition={info.league.draft_position}
              teams={info.league.teams}
              onStarted={(next) => {
                setStatus(next);
                setFlash("Draft started.");
                void refresh();
              }}
              onError={setError}
            />
          ) : null}
        </div>
      </header>

      <div className="layout">
        <main className="main-column">
          {error ? (
            <Banner kind="error" title={error.kind}>
              {error.message}
              {error.hint ? <div className="faint">{error.hint}</div> : null}
            </Banner>
          ) : null}
          {flash ? <Banner kind="info">{flash}</Banner> : null}
          {board?.warnings.map((warning) => (
            <Banner kind="warn" key={warning}>
              {warning}
            </Banner>
          ))}

          <div className="toolbar">
            <div className="filters">
              <button
                aria-pressed={position === null}
                onClick={() => setPosition(null)}
              >
                All
              </button>
              {POSITIONS.map((code) => (
                <button
                  key={code}
                  aria-pressed={position === code}
                  onClick={() => setPosition(position === code ? null : code)}
                >
                  {code}
                </button>
              ))}
            </div>

            <div className="header-spacer" />

            <label className="faint" htmlFor="limit">
              Show
            </label>
            <select
              id="limit"
              style={{ width: 78 }}
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
            >
              {[15, 25, 40, 75, 150].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <button onClick={() => void refresh()} title="Recompute the board">
              Refresh
            </button>
          </div>

          {board ? (
            <>
              <BoardTable
                players={visible}
                selectedId={selected}
                canPick={canPick}
                onSelect={(player) => setSelected(player.player_id)}
                onTake={onTake}
              />
              <p className="faint" style={{ marginTop: 8 }}>
                {board.total_available} players available.{" "}
                {board.next_pick
                  ? `Availability measured at pick ${board.next_pick} (${board.availability_method}`
                  : "No draft position set, so availability is not estimated"}
                {board.simulation
                  ? `, ${board.simulation.iterations} simulations).`
                  : board.next_pick
                    ? ")."
                    : "."}{" "}
                Click a row for the full decomposition.
              </p>
            </>
          ) : null}
        </main>

        <aside className="side-column">
          {board ? <RosterPanel board={board} /> : null}
          <RecommendPanel
            llmEnabled={info?.llm_enabled ?? false}
            onSelectPlayer={(name) => {
              const match = board?.players.find((player) => player.name === name);
              if (match) setSelected(match.player_id);
            }}
          />
          {status?.active ? <RecentPicksPanel status={status} /> : null}
          {board ? <ScarcityPanel board={board} /> : null}
          <DataPanel data={data} />
        </aside>
      </div>

      {selected ? (
        <PlayerDrawer
          playerId={selected}
          canPick={canPick}
          onClose={() => setSelected(null)}
          onTake={(playerId, name) => {
            const match = board?.players.find((p) => p.player_id === playerId);
            if (match) void onTake(match);
            else setFlash(`Could not find ${name} on the board.`);
          }}
        />
      ) : null}
    </div>
  );
}
