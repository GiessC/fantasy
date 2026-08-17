import { useEffect, useRef, useState } from "react";
import { api, RequestError } from "../api";
import type { DraftStatus, SearchResult } from "../types";
import { PositionBadge } from "./common";

/**
 * Search-to-pick.
 *
 * The fastest way to keep a live draft in sync is to type the name that was
 * just called and press enter, so this owns focus (`/` from anywhere), supports
 * arrow-key selection, and clears itself after recording.
 */
export function PickSearch({
  disabled,
  onPicked,
  onError,
}: {
  disabled: boolean;
  onPicked: (status: DraftStatus, name: string) => void;
  onError: (error: RequestError) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [active, setActive] = useState(0);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "/" && document.activeElement !== inputRef.current) {
        event.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  useEffect(() => {
    if (query.trim().length < 2) {
      setResults([]);
      return;
    }
    let cancelled = false;
    // Debounced: a draft room types fast and the search hits SQLite each time.
    const timer = window.setTimeout(() => {
      api
        .search(query.trim())
        .then((found) => {
          if (!cancelled) {
            setResults(found);
            setActive(0);
          }
        })
        .catch(() => {
          if (!cancelled) setResults([]);
        });
    }, 140);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  async function take(result: SearchResult) {
    if (result.drafted || busy) return;
    setBusy(true);
    try {
      const outcome = await api.pick({ player_id: result.player_id });
      setQuery("");
      setResults([]);
      onPicked(outcome.status, result.name);
    } catch (cause) {
      onError(cause as RequestError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="search-wrap">
      <input
        ref={inputRef}
        value={query}
        disabled={disabled || busy}
        placeholder={disabled ? "Start a draft to record picks" : "Record a pick — type a name, press / to focus"}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setActive((index) => Math.min(index + 1, results.length - 1));
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setActive((index) => Math.max(index - 1, 0));
          } else if (event.key === "Enter" && results[active]) {
            event.preventDefault();
            void take(results[active]);
          } else if (event.key === "Escape") {
            setQuery("");
            setResults([]);
          }
        }}
        aria-label="Record a pick by player name"
      />
      {results.length > 0 ? (
        <div className="search-results" role="listbox">
          {results.map((result, index) => (
            <div
              key={result.player_id}
              role="option"
              aria-selected={index === active}
              className={`search-item${index === active ? " active" : ""}${
                result.drafted ? " drafted" : ""
              }`}
              onMouseEnter={() => setActive(index)}
              onClick={() => void take(result)}
            >
              <span>
                {result.position ? (
                  <PositionBadge position={result.position} />
                ) : null}{" "}
                {result.name}{" "}
                <span className="faint">{result.team ?? ""}</span>
              </span>
              {result.drafted ? <span className="faint">drafted</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function StartDraftForm({
  defaultPosition,
  teams,
  onStarted,
  onError,
}: {
  defaultPosition: number | null;
  teams: number;
  onStarted: (status: DraftStatus) => void;
  onError: (error: RequestError) => void;
}) {
  const [position, setPosition] = useState(defaultPosition ?? 1);
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    try {
      onStarted(await api.startDraft({ position }));
    } catch (cause) {
      onError(cause as RequestError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="row wrap">
      <label className="faint" htmlFor="slot">
        Your slot
      </label>
      <select
        id="slot"
        style={{ width: 78 }}
        value={position}
        onChange={(event) => setPosition(Number(event.target.value))}
      >
        {Array.from({ length: teams }, (_, index) => index + 1).map((slot) => (
          <option key={slot} value={slot}>
            {slot}
          </option>
        ))}
      </select>
      <button className="primary" onClick={start} disabled={busy}>
        Start draft
      </button>
    </div>
  );
}
