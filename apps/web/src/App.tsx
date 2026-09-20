import { useCallback, useEffect, useMemo, useState } from "react";
import { api, PHRASE, type Case, type RunState } from "./api";

const say = (k: string) => PHRASE[k] ?? k.replace(/_/g, " ").toLowerCase();

/** Poll while a run is active; stop once nothing is moving. */
function useRun(runId: string | null) {
  const [state, setState] = useState<RunState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!runId) return;
    try {
      setState(await api.run(runId));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [runId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while something is actually moving, then stop. A console that keeps
  // hammering a settled run is just noise on the network tab.
  const busy = state?.status === "processing" || state?.progress?.finished === false;
  useEffect(() => {
    if (!runId || !busy) return;
    const t = setInterval(() => void refresh(), 1200);
    return () => clearInterval(t);
  }, [runId, busy, refresh]);

  return { state, error, refresh };
}

function Working({ p }: { p: NonNullable<RunState["progress"]> }) {
  return (
    <div className="panel" role="status" aria-live="polite">
      <h2>The agent is working</h2>
      <p style={{ fontSize: 17, margin: "0 0 12px" }}>{p.message}</p>
      <div style={{ height: 8, borderRadius: 999, background: "var(--panel-2)",
                    overflow: "hidden" }}>
        <div style={{ width: `${Math.max(4, p.percent)}%`, height: "100%",
                      background: "var(--accent)", transition: "width .4s ease" }} />
      </div>
      {!!p.total && (
        <p className="change" style={{ marginTop: 8 }}>
          {p.done} of {p.total} columns examined
        </p>
      )}
      {p.failed && <p style={{ color: "var(--bad)" }}>{p.error}</p>}
    </div>
  );
}

function Stat({ n, label, tone }: { n: number; label: string; tone?: string }) {
  return (
    <div className="stat">
      <b style={tone ? { color: `var(--${tone})` } : undefined}>{n}</b>
      <span>{label}</span>
    </div>
  );
}

/**
 * One case at a time with Previous/Next.
 *
 * Navigating is only a change of view: it never submits an opinion. A consultant can
 * read all four cases, answer the easy ones and leave the hard one for a supervisor.
 */
function ReviewQueue({ state, onDone }: { state: RunState; onDone: () => void }) {
  const [i, setI] = useState(0);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const cases = state.cases;
  const c: Case | undefined = cases[Math.min(i, cases.length - 1)];

  useEffect(() => {
    setDraft("");
    setErr(null);
  }, [c?.key]);

  if (!cases.length) {
    return (
      <div className="panel empty">
        <p style={{ fontSize: 17, color: "var(--ok)" }}>Nothing needs your decision.</p>
        <p>The agent handled everything it had evidence for.</p>
      </div>
    );
  }
  if (!c) return null;

  const send = async (action: string, value: string | null) => {
    setBusy(true);
    setErr(null);
    try {
      await api.decide(state.run_id, c.key, action, value);
      setI((n) => Math.min(n, cases.length - 2 < 0 ? 0 : cases.length - 2));
      onDone();
    } catch (e) {
      setErr(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const evidence = c.evidence as Record<string, any>;
  return (
    <div className="panel case">
      <div className="nav">
        <button onClick={() => setI((n) => Math.max(0, n - 1))} disabled={i === 0}
                aria-label="Previous case">← Previous</button>
        <button onClick={() => setI((n) => Math.min(cases.length - 1, n + 1))}
                disabled={i >= cases.length - 1} aria-label="Next case">Next →</button>
        <span className="count">Case {i + 1} of {cases.length}</span>
        <span className="spacer" />
        <span className="pill warn"><i className="dot" />{say(c.class)}</span>
      </div>

      <h3>{c.headline}</h3>
      <p className="detail">{c.detail}</p>

      <dl className="evidence">
        {c.record && (<><dt>Record</dt><dd className="mono">{c.record}</dd></>)}
        {c.field && (<><dt>Target field</dt><dd className="mono">{c.field}</dd></>)}
        {!!c.values.length && (
          <><dt>Value in the file</dt>
            <dd className="mono">{c.values.map((v) => `"${v}"`).join("  ·  ")}</dd></>
        )}
        {!!c.sources.length && (
          <><dt>Where it came from</dt>
            <dd>{c.sources.slice(0, 3).map((s) => <div key={s}>{s}</div>)}</dd></>
        )}
        {c.rule && (<><dt>Rule it must satisfy</dt><dd className="mono">{c.rule}</dd></>)}
        {evidence?.crop && (
          <><dt>What the scan actually shows</dt>
            <dd>
              <img
                src={`/api/runs/${state.run_id}/crop?` + new URLSearchParams(
                  Object.entries(evidence.crop as Record<string, string>)
                    .map(([k, v]) => [k, String(v)]),
                ).toString()}
                alt={`Scanned region for ${c.field ?? "this value"}`}
                style={{ maxWidth: "100%", border: "1px solid var(--line)",
                         borderRadius: 6, background: "#fff", padding: 4 }}
              />
              {typeof evidence.read_confidence === "number" && (
                <div className="change">
                  read at {Math.round((evidence.read_confidence as number) * 100)}% confidence
                </div>
              )}
            </dd></>
        )}
        {!!evidence?.candidates?.length && (
          <><dt>What the agent measured</dt>
            <dd>{(evidence.candidates as string[]).map((line) => (
              <div key={line} className="mono">{line}</div>))}</dd></>
        )}
        {!!evidence?.closest?.length && (
          <><dt>Closest columns</dt>
            <dd>{(evidence.closest as string[]).map((l) => <div key={l} className="mono">{l}</div>)}</dd></>
        )}
        {evidence?.agree && (
          <><dt>Agrees on / conflicts on</dt>
            <dd className="mono">{(evidence.agree as string[]).join(", ") || "—"}
              {" / "}{(evidence.conflict as string[]).join(", ") || "—"}</dd></>
        )}
        {!!c.attempts.length && (
          <><dt>What was already tried</dt>
            <dd>{c.attempts.map((a) => <div key={a}>{a}</div>)}</dd></>
        )}
        {c.blocks > 1 && (
          <><dt>Effect</dt><dd>{c.blocks} records are waiting on this one answer.</dd></>
        )}
      </dl>

      {!!c.options.length && (
        <div className="options">
          {c.options.map((o) => (
            <button key={o.label} disabled={busy}
                    className={o.recommended ? "primary" : ""}
                    title={o.description ?? undefined}
                    onClick={() => void send("correct", o.value)}>
              {o.label}
            </button>
          ))}
        </div>
      )}

      <div style={{ margin: "10px 0" }}>
        <input type="text" value={draft} placeholder="…or type the correct value"
               aria-label="Corrected value"
               onChange={(e) => setDraft(e.target.value)} />
      </div>

      {err && <p style={{ color: "var(--bad)" }} role="alert">{err}</p>}

      <div className="actions">
        <button className="primary" disabled={busy || !draft.trim()}
                onClick={() => void send("correct", draft.trim())}>Save correction</button>
        {c.actions.includes("approve") && (
          <button disabled={busy} onClick={() => void send("approve", null)}>Approve</button>
        )}
        <button className="danger" disabled={busy}
                onClick={() => void send("reject", null)}>
          Exclude {c.blocks > 1 ? `${c.blocks} records` : "this record"}
        </button>
      </div>
      <p className="change" style={{ marginTop: 10 }}>
        Moving between cases does not answer them. Unanswered cases stay in the queue.
      </p>
    </div>
  );
}

function Records({ state }: { state: RunState }) {
  const [q, setQ] = useState("");
  const rows = state.records.filter(
    (r) => !q || JSON.stringify(r.values).toLowerCase().includes(q.toLowerCase()),
  );
  return (
    <div className="panel">
      <input type="text" value={q} placeholder="Search records"
             aria-label="Search records" onChange={(e) => setQ(e.target.value)} />
      <div className="scroll" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr><th>Record</th><th>Status</th><th>Name</th><th>What changed</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const changed = Object.entries(r.provenance).filter(([, p]) => p.changes.length);
              const tone = r.state === "delivered" ? "ok" : r.state === "blocked" ? "warn" : "";
              return (
                <tr key={r.key}>
                  <td className="mono">{r.key}</td>
                  <td><span className={`pill ${tone}`}><i className="dot" />{say(r.state)}</span></td>
                  <td>{[r.values.first_name, r.values.last_name].filter(Boolean).join(" ") || "—"}</td>
                  <td>
                    {changed.length ? (
                      <details>
                        <summary>{changed.length} field(s) cleaned</summary>
                        {changed.map(([name, p]) =>
                          p.changes.map((ch, k) => (
                            <div key={name + k} className="change">
                              <b>{name}</b> <s>{ch.before}</s> → {ch.after} — {ch.why}
                            </div>
                          )))}
                      </details>
                    ) : <span className="change">—</span>}
                    {!!r.issues.length && (
                      <div className="change" style={{ color: "var(--warn)" }}>{r.issues[0]}</div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Mappings({ state }: { state: RunState }) {
  return (
    <div className="panel scroll">
      <table>
        <thead><tr><th>File</th><th>Column</th><th>Decision</th><th>Mapped to</th><th>Why</th></tr></thead>
        <tbody>
          {state.mappings.map((m, i) => (
            <tr key={i}>
              <td className="mono">{m.file}</td>
              <td className="mono">{m.column}</td>
              <td>
                <span className={`pill ${m.decision === "auto_apply" ? "ok" : "warn"}`}>
                  <i className="dot" />{m.decision === "auto_apply" ? "Applied automatically" : say(m.decision)}
                </span>
              </td>
              <td className="mono">{m.field ?? "—"}</td>
              <td className="change">{m.evidence.slice(0, 3).join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Destination({ runId }: { runId: string }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.destination(runId).then(setData).catch((e) => setErr(String(e)));
  }, [runId]);
  if (err) return <div className="panel empty">Destination unreachable. {err}</div>;
  if (!data) return <div className="panel empty">Reading the destination…</div>;
  return (
    <>
      <div className="panel" style={{ marginBottom: 14 }}>
        <h2>What the destination actually holds</h2>
        <div className="scroll">
          <table>
            <thead><tr><th>Record</th><th>State</th><th>Received</th><th>Payload</th></tr></thead>
            <tbody>
              {data.records.map((r: any) => (
                <tr key={r.id}>
                  <td className="mono">{r.natural_key}</td>
                  <td><span className={`pill ${r.state === "accepted" ? "ok" : ""}`}>
                    <i className="dot" />{r.state === "accepted" ? "Stored" : "Rolled back"}</span></td>
                  <td className="change">{r.received_at?.slice(11, 19)}</td>
                  <td><details><summary>view</summary>
                    <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>
                      {JSON.stringify(r.payload, null, 1)}</pre></details></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data.records.length && <p className="empty">Nothing sent yet.</p>}
        </div>
      </div>
      <div className="panel">
        <h2>Every attempt, including the failures</h2>
        <div className="scroll">
          <table>
            <thead><tr><th>Record</th><th>Attempt</th><th>Outcome</th><th>HTTP</th></tr></thead>
            <tbody>
              {data.attempts.map((a: any) => (
                <tr key={a.id}>
                  <td className="mono">{a.natural_key}</td>
                  <td>#{a.attempt}</td>
                  <td><span className={`pill ${["accepted","duplicate"].includes(a.outcome) ? "ok" : "bad"}`}>
                    <i className="dot" />{say(a.outcome)}</span></td>
                  <td className="change">{a.status_code ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

export default function App() {
  const [runId, setRunId] = useState<string | null>(
    () => new URLSearchParams(location.search).get("run"),
  );
  const [runs, setRuns] = useState<any[]>([]);
  const [tab, setTab] = useState("review");
  const [busy, setBusy] = useState(false);
  const { state, error, refresh } = useRun(runId);

  const loadRuns = useCallback(() => { api.runs().then(setRuns).catch(() => {}); }, []);
  useEffect(loadRuns, [loadRuns]);

  const open = (id: string) => {
    setRunId(id);
    history.replaceState(null, "", `?run=${id}`);
  };

  const start = async () => {
    setBusy(true);
    try {
      const { run_id } = await api.startFromFixtures("run1");
      open(run_id);
      loadRuns();
    } finally { setBusy(false); }
  };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); await refresh(); loadRuns(); } finally { setBusy(false); }
  };

  const counts = state?.counts;
  const tone = useMemo(() => {
    if (!state) return "";
    return state.status.startsWith("completed") ? "ok"
      : state.status === "delivery_failed" ? "bad" : "warn";
  }, [state]);

  return (
    <div className="app">
      <header className="top">
        <h1>Data Bridge</h1>
        {state && <span className={`pill ${tone}`}><i className="dot" />{say(state.status)}</span>}
        <span className="spacer" />
        <select aria-label="Open a previous run" value={runId ?? ""}
                onChange={(e) => e.target.value && open(e.target.value)}
                style={{ padding: 9, borderRadius: 8, background: "var(--panel-2)",
                         color: "var(--ink)", border: "1px solid var(--line)" }}>
          <option value="">Run history…</option>
          {runs.map((r) => (
            <option key={r.id} value={r.id}>
              {r.id} · {r.label ?? ""} · {r.delivered} sent
            </option>
          ))}
        </select>
        <button className="primary" onClick={() => void start()} disabled={busy}>
          New migration
        </button>
      </header>

      {error && <div className="panel" style={{ color: "var(--bad)" }} role="alert">{error}</div>}

      {!runId && (
        <div className="panel empty">
          <p style={{ fontSize: 17 }}>Start a migration to see the agent work.</p>
          <p>It reads the client's exports, maps them to the target schema, cleans what it
             safely can, and asks you only about what it genuinely cannot settle.</p>
        </div>
      )}

      {state?.progress && !state.progress.finished && state.status === "processing" && (
        <Working p={state.progress} />
      )}

      {state && counts && state.status !== "processing" && (
        <>
          <div className="stats">
            <Stat n={counts.records} label="employees found" />
            <Stat n={counts.open_cases} label="need your decision" tone={counts.open_cases ? "warn" : undefined} />
            <Stat n={counts.ready} label="ready to send" />
            <Stat n={counts.delivered} label="sent successfully" tone="ok" />
            <Stat n={counts.blocked} label="waiting" />
            {!!counts.excluded && <Stat n={counts.excluded} label="excluded" />}
            <span className="spacer" />
            <button className="primary" disabled={busy || !counts.ready}
                    onClick={() => void act(() => api.deliver(state.run_id))}>
              Send {counts.ready} ready record{counts.ready === 1 ? "" : "s"}
            </button>
            <button className="danger" disabled={busy || !counts.delivered}
                    onClick={() => void act(() => api.rollback(state.run_id))}>
              Undo delivery
            </button>
          </div>

          <div className="tabs" role="tablist">
            {[["review", `Needs you (${counts.open_cases})`], ["records", "Records"],
              ["mappings", "How it mapped"], ["destination", "Destination"]].map(([id, label]) => (
              <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
                {label}
              </button>
            ))}
          </div>

          <div className="grid">
            <div>
              {tab === "review" && <ReviewQueue state={state} onDone={refresh} />}
              {tab === "records" && <Records state={state} />}
              {tab === "mappings" && <Mappings state={state} />}
              {tab === "destination" && <Destination runId={state.run_id} />}
            </div>
            <aside className="panel">
              <h2>What the agent did</h2>
              <ul className="feed">
                {[...state.activity].reverse().map((e, i) => (
                  <li key={i}>
                    <span className="who">{e.actor}</span>
                    <span>{e.summary}</span>
                  </li>
                ))}
              </ul>
            </aside>
          </div>
        </>
      )}
    </div>
  );
}
