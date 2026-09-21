import { useState } from "react";
import { api } from "./api";

type Outcome = Record<string, number>;

const RESULT_WORDS: Record<string, string> = {
  accepted: "stored",
  duplicate: "already there",
  rejected: "refused",
  failed: "could not be delivered",
  uncertain: "outcome unclear",
};

const REHEARSALS: [string, string, string][] = [
  ["none", "Behave normally", ""],
  ["transient", "Fail, then recover", "The destination drops the request. Pushing again should land it."],
  ["uncertain", "Answer unclearly", "It may or may not have stored the record; it gets reconciled by identity, not resent blindly."],
  ["timeout", "Stop answering", "The request hangs. Nothing is assumed either way."],
];

/**
 * Pushing is the one action with a consequence outside this tool, so it gets a
 * moment of its own: what is about to go, what happened to each record, and — for
 * anyone who wants to see the recovery behaviour — a rehearsed failure that lasts
 * exactly this push and no longer.
 */
export default function PushDialog({ runId, ready, onClose, onDone }: {
  runId: string;
  ready: number;
  onClose: () => void;
  onDone: () => void;
}) {
  const [simulate, setSimulate] = useState("none");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Outcome | null>(null);
  const [error, setError] = useState<string | null>(null);

  const push = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await api.deliver(runId, false, simulate);
      setResult(out.sent);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const lines = Object.entries(result ?? {}).filter(([, n]) => n > 0);

  return (
    <div className="scrim-modal" role="dialog" aria-modal="true" aria-label="Push to the target">
      <div className="modal">
        {result ? (
          <>
            <h2>What the destination said</h2>
            {lines.length ? (
              <ul className="outcomes">
                {lines.map(([k, n]) => (
                  <li key={k} className={k === "accepted" ? "ok" : k === "duplicate" ? "" : "bad"}>
                    <b>{n}</b> {RESULT_WORDS[k] ?? k}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="change">Nothing was outstanding, so nothing was sent.</p>
            )}
            {(result.failed || result.uncertain) > 0 && (
              <p className="change">
                Anything that did not land can be pushed again — records already stored
                are not sent twice.
              </p>
            )}
            <div className="actions">
              <span className="spacer" />
              <button className="primary" onClick={onClose}>Done</button>
            </div>
          </>
        ) : (
          <>
            <h2>Push to the target</h2>
            <p className="lead">
              <b>{ready}</b> {ready === 1 ? "employee has" : "employees have"} nothing
              open against {ready === 1 ? "them" : "them"} and will be written to the
              destination. Anything still under review stays here.
            </p>

            <label className="field">
              <span>If you want to see what happens when it goes wrong</span>
              <select value={simulate} disabled={busy}
                      onChange={(e) => setSimulate(e.target.value)}>
                {REHEARSALS.map(([v, label]) => (
                  <option key={v} value={v}>{label}</option>
                ))}
              </select>
            </label>
            {simulate !== "none" && (
              <p className="change">
                {REHEARSALS.find(([v]) => v === simulate)?.[2]} It applies to this push
                only.
              </p>
            )}

            {error && <p role="alert" style={{ color: "var(--bad)" }}>{error}</p>}

            <div className="actions">
              <button disabled={busy} onClick={onClose}>Cancel</button>
              <span className="spacer" />
              <button className="primary" disabled={busy} onClick={() => void push()}>
                {busy ? (
                  <><i className="spinner" aria-hidden="true" /> Sending {ready}…</>
                ) : (
                  <>Push {ready} to the target</>
                )}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
