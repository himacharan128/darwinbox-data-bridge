import { useCallback, useEffect, useRef, useState, Fragment } from "react";
import { api, ApiError, PHRASE, type RunState } from "./api";
import Review from "./Review";
import Wizard from "./Wizard";

const say = (k: string) => PHRASE[k] ?? k.replace(/_/g, " ").toLowerCase();
const titled = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** A field name rendered for someone who did not write it. */
const fieldLabel = (name: string) =>
  titled(name.replace(/_/g, " ").replace(/\bid\b/i, "ID"));

/* ------------------------------------------------------------------- hooks */

type View = "auto" | "desktop" | "mobile";

function useView(): [View, boolean, (v: View) => void] {
  const [choice, setChoice] = useState<View>(() => {
    try { return (localStorage.getItem("dbx.view") as View) || "auto"; }
    catch { return "auto"; }   // private windows must not break the page
  });
  const [narrow, setNarrow] = useState(
    () => typeof matchMedia === "function" && matchMedia("(max-width: 860px)").matches,
  );
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const q = matchMedia("(max-width: 860px)");
    const on = () => setNarrow(q.matches);
    q.addEventListener("change", on);
    return () => q.removeEventListener("change", on);
  }, []);
  const set = (v: View) => {
    setChoice(v);
    try { localStorage.setItem("dbx.view", v); } catch { /* not worth failing over */ }
  };
  return [choice, choice === "auto" ? narrow : choice === "mobile", set];
}

function useRun(runId: string | null) {
  const [state, setState] = useState<RunState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  // Which run the newest request was for. A reply about any other run is stale —
  // without this, switching runs left the previous one's records and review queue
  // on screen under the new run's name.
  const wanted = useRef<string | null>(runId);

  const refresh = useCallback(async () => {
    if (!runId) { setState(null); setError(null); setGone(false); return; }
    try {
      const data = await api.run(runId);
      if (wanted.current !== runId || data.run_id !== runId) return;
      setState(data); setError(null); setGone(false);
    } catch (e) {
      if (wanted.current !== runId) return;
      const api404 = e instanceof ApiError && e.status === 404;
      setGone(api404);
      setState(null);
      setError(e instanceof ApiError ? e.message : "Couldn't reach the server.");
    }
  }, [runId]);

  useEffect(() => {
    // Drop the old run's data before the new one arrives, so nothing is ever shown
    // under the wrong heading.
    wanted.current = runId;
    setState(null);
    setError(null);
    setGone(false);
    void refresh();
  }, [runId, refresh]);

  const busy = state?.status === "processing";
  useEffect(() => {
    if (!runId || !busy) return;              // stop polling once nothing is moving
    const t = setInterval(() => void refresh(), 1500);
    return () => clearInterval(t);
  }, [runId, busy, refresh]);

  return { state, error, gone, refresh };
}

/* ------------------------------------------------------------------ pieces */

function Stat({ n, label, tone }: { n: number; label: string; tone?: string }) {
  return (
    <div className={`stat${tone ? ` is-${tone}` : ""}`}>
      <b>{n}</b>
      <span>{label}</span>
    </div>
  );
}

const STAGES = [
  { key: "starting", label: "Reading your files" },
  { key: "mapping", label: "Working out what each column is" },
  { key: "validating", label: "Tidying values and checking them" },
  { key: "delivering", label: "Sending what's ready" },
];

/**
 * What it is doing, not just that it is busy.
 *
 * A lone progress bar on an empty page tells someone nothing except to wait. The
 * stages say what the work actually consists of, so the wait is legible and the
 * screen still has shape when it finishes.
 */
function Working({ p }: { p: NonNullable<RunState["progress"]> }) {
  const at = Math.max(0, STAGES.findIndex((s) => s.key === p.stage));
  return (
    <div className="panel" role="status" aria-live="polite">
      <div className="progress-head">
        <h2>Working through your files</h2>
        <span className="change">this usually takes under a minute</span>
      </div>
      <p className="progress-now">{p.message}</p>
      {!!p.total && (
        <>
          <div className="bar"><i style={{ width: `${Math.max(4, p.percent)}%` }} /></div>
          <p className="change" style={{ margin: 0 }}>
            {p.done} of {p.total} columns looked at
          </p>
        </>
      )}

      <ul className="stages">
        {STAGES.map((s, i) => (
          <li key={s.key} className={i < at ? "done" : i === at ? "now" : ""}>
            <span className="tickmark">✓</span>
            <span>{s.label}</span>
          </li>
        ))}
      </ul>

      {p.failed && (
        <div className="notice bad" role="alert" style={{ marginTop: 16 }}>
          <b>That didn't finish.</b> {p.error}
        </div>
      )}

      <div className="skeleton" aria-hidden="true">
        <i style={{ width: "72%" }} /><i style={{ width: "54%" }} /><i style={{ width: "63%" }} />
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- records */

function Records({ state }: { state: RunState }) {
  const [q, setQ] = useState("");
  const rows = state.records.filter(
    (r) => !q || JSON.stringify(r.values).toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="panel">
      <input type="search" value={q} placeholder="Search employees"
             aria-label="Search employees" onChange={(e) => setQ(e.target.value)} />
      <div className="scroll" style={{ marginTop: 14 }}>
        <table className="responsive">
          <thead><tr><th>Employee</th><th>Name</th><th>Status</th><th>Tidied up</th></tr></thead>
          <tbody>
            {rows.map((r) => {
              const changed = Object.entries(r.provenance).filter(([, p]) => p.changes.length);
              const tone = r.state === "delivered" ? "ok" : r.state === "blocked" ? "warn" : "";
              const label = r.state === "blocked" && r.waiting_on_another
                ? "Waiting on their manager" : say(r.state);
              return (
                <tr key={r.key}>
                  <td data-label="Employee" className="mono">{r.key}</td>
                  <td data-label="Name">
                    {[r.values.first_name, r.values.last_name].filter(Boolean).join(" ") || "—"}
                  </td>
                  <td data-label="Status">
                    <span className={`pill ${tone}`}><i className="dot" />{label}</span>
                  </td>
                  <td data-label="Tidied up">
                    {changed.length ? changed.slice(0, 3).map(([name, p]) =>
                      p.changes.map((ch, k) => (
                        <div key={name + k} className="change">
                          <b>{fieldLabel(name)}</b>: {ch.why}
                        </div>
                      ))) : <span className="change">nothing needed</span>}
                    {!!r.issues.length && (
                      <div className="change" style={{ color: "var(--warn)" }}>{r.issues[0]}</div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && <p className="empty">Nothing matches “{q}”.</p>}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- mappings */

function Mappings({ state }: { state: RunState }) {
  return (
    <div className="panel">
      <h2>Where each column went</h2>
      <div className="scroll">
        <table className="responsive">
          <thead>
            <tr><th>Column</th><th>From</th><th>Goes to</th><th>Why</th></tr>
          </thead>
          <tbody>
            {state.mappings.map((m, i) => (
              <tr key={i}>
                <td data-label="Column" className="mono">{m.column}</td>
                <td data-label="From" className="change">{m.file}</td>
                <td data-label="Goes to">
                  {m.field ? (
                    <>
                      <b>{fieldLabel(m.field)}</b>{" "}
                      <span className={`pill ${m.decision === "auto_apply" ? "ok" : "warn"}`}>
                        <i className="dot" />
                        {m.decision === "auto_apply" ? "matched" : "needs you"}
                      </span>
                    </>
                  ) : <span className="change">left alone</span>}
                </td>
                <td data-label="Why">
                  {m.evidence.length ? (
                    <ul className="reasons">
                      {m.evidence.slice(0, 2).map((e) => <li key={e}>{e}</li>)}
                    </ul>
                  ) : <span className="change">no clear match</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** One short line saying where a run actually got to. */
function runMeta(r: any): string {
  const open = r.counts?.open_cases ?? 0;
  if (r.status === "awaiting_schema") return "needs a schema";
  if (r.status === "processing") return "working\u2026";
  if (open) return `${open} need${open === 1 ? "s" : ""} you`;
  if (r.delivered) return `${r.delivered} sent`;
  if (r.counts?.excluded) return `${r.counts.excluded} excluded`;
  if (r.counts?.records) return `${r.counts.records} ready to send`;
  return PHRASE[r.status] ?? "not started";
}

/* -------------------------------------------------------------- destination */

function Destination({ runId }: { runId: string }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [show, setShow] = useState<string | null>(null);

  useEffect(() => { api.destination(runId).then(setData).catch((e) => setErr(String(e))); }, [runId]);

  if (err) return <div className="panel empty"><strong>Can't reach the destination</strong>{err}</div>;
  if (!data) return <div className="panel empty">Checking the destination…</div>;
  if (!data.records.length) {
    return <div className="panel empty">
      <strong>Nothing has been sent yet</strong>
      Employees are sent as soon as nothing is holding them up.
    </div>;
  }

  return (
    <div className="panel">
      <h2>What the destination holds</h2>
      <div className="scroll">
        <table className="responsive">
          <thead><tr><th>Employee</th><th>Status</th><th>Received</th><th /></tr></thead>
          <tbody>
            {data.records.map((r: any) => {
              const open = show === r.id;
              const payload = (r.payload ?? {}) as Record<string, unknown>;
              return (
                <Fragment key={r.id}>
                  <tr className={open ? "open" : undefined}>
                    <td data-label="Employee" className="mono">{r.natural_key}</td>
                    <td data-label="Status">
                      <span className={`pill ${r.state === "accepted" ? "ok" : ""}`}>
                        <i className="dot" />{r.state === "accepted" ? "Stored" : "Undone"}
                      </span>
                    </td>
                    <td data-label="Received" className="change">
                      {r.received_at?.slice(11, 19) ?? "—"}
                    </td>
                    <td data-label="">
                      <button className="ghost" aria-expanded={open}
                              onClick={() => setShow(open ? null : r.id)}>
                        {open ? "Hide" : "What was sent"}
                      </button>
                    </td>
                  </tr>
                  {open && (
                    <tr className="detail-row">
                      <td colSpan={4}>
                        <div className="detail-body">
                          <h3>The {Object.keys(payload).length} values stored for {r.natural_key}</h3>
                          {Object.keys(payload).length === 0 ? (
                            <p className="change">Nothing was recorded for this employee.</p>
                          ) : (
                            <dl className="kv">
                              {Object.entries(payload).map(([k, v]) => (
                                <Fragment key={k}>
                                  <dt>{fieldLabel(k)}</dt>
                                  <dd>{v === null || v === "" ? (
                                    <span className="change">left blank</span>
                                  ) : String(v)}</dd>
                                </Fragment>
                              ))}
                            </dl>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Failures({ state, onRetry, busy }: {
  state: RunState; onRetry: () => void; busy: boolean;
}) {
  const failures = state.failures ?? [];
  if (!failures.length) {
    return <div className="panel empty">The destination has refused nothing.</div>;
  }
  return (
    <div className="panel">
      <h2>The destination refused these</h2>
      <p className="lead">
        Not a question the agent is asking — the receiving system would not take them.
      </p>
      <div className="scroll">
        <table className="responsive">
          <thead><tr><th>Employee</th><th>What it said</th></tr></thead>
          <tbody>
            {failures.map((f) => (
              <tr key={f.record}>
                <td data-label="Employee" className="mono">{f.record}</td>
                <td data-label="Reason">{f.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="actions">
        <button className="primary" disabled={busy} onClick={onRetry}>Try again</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ schema */

function Schema({ runId }: { runId: string }) {
  const [data, setData] = useState<any>(null);
  useEffect(() => { api.schema(runId).then(setData).catch(() => {}); }, [runId]);

  if (!data) return <div className="panel empty">Loading…</div>;
  if (!data.active) return <div className="panel empty">No schema agreed for this run yet.</div>;

  return (
    <div className="panel">
      <h2>What the data is being turned into</h2>
      <p className="lead">
        Agreed at the start of this run{data.approved_version
          ? ` (version ${data.approved_version})` : ""}. Employees already sent keep the
        version they were sent under.
      </p>
      <div className="scroll">
        <table className="responsive">
          <thead><tr><th>Field</th><th>Holds</th><th>Required</th><th>Rules</th></tr></thead>
          <tbody>
            {data.active.fields.map((f: any) => (
              <tr key={f.name}>
                <td data-label="Field"><b>{fieldLabel(f.name)}</b><br />
                  <span className="change mono">{f.name}</span></td>
                <td data-label="Holds">{f.type === "string" ? "Text" : titled(f.type)}</td>
                <td data-label="Required">{f.required ? "Yes" : "—"}</td>
                <td data-label="Rules" className="change">
                  {[f.unique && "every value different",
                    f.allowed?.length && `one of: ${f.allowed.join(", ")}`,
                    f.reference && `must be in the ${f.reference.split(".")[0]} list`,
                    f.pattern && "a set format",
                  ].filter(Boolean).join(" · ") || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------- app */

export default function App() {
  const [runId, setRunId] = useState<string | null>(
    () => new URLSearchParams(location.search).get("run"));
  const [runs, setRuns] = useState<any[]>([]);
  const [tab, setTab] = useState("review");
  const [busy, setBusy] = useState(false);
  const [wizard, setWizard] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [choice, isMobile, setView] = useView();
  const { state, error, gone, refresh } = useRun(runId);

  // A run that no longer exists should not leave a dead id in the address bar.
  useEffect(() => {
    if (gone) { setRunId(null); history.replaceState(null, "", location.pathname); }
  }, [gone]);

  const loadRuns = useCallback(() => { api.runs().then(setRuns).catch(() => {}); }, []);
  useEffect(loadRuns, [loadRuns]);

  const open = (id: string) => {
    setRunId(id); setWizard(false);
    history.replaceState(null, "", `?run=${id}`);
  };
  const startWizard = () => {
    setWizard(true); setRunId(null);
    history.replaceState(null, "", location.pathname);
  };
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); await refresh(); loadRuns(); } finally { setBusy(false); }
  };

  const counts = state?.counts;
  const tone = !state ? "" :
    state.status.startsWith("completed") ? "ok" :
    state.status === "delivery_failed" ? "bad" : "warn";

  const label = (r: any) =>
    (r.label ?? r.id).replace(/^(sample|fixtures)\//, "").replace(/[-_]/g, " ");

  return (
    <div className={`shell${isMobile ? " is-mobile" : ""}${navOpen ? " nav-open" : ""}`}
         data-layout={isMobile ? "mobile" : "desktop"}>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">d</span>
          <h1>Data Bridge</h1>
        </div>

        <button className="primary new-run" onClick={startWizard} disabled={busy}>
          + New migration
        </button>

        <nav aria-label="Your migrations">
          <p className="nav-head">Migrations</p>
          {runs.length ? (
            <ul className="runlist">
              {runs.map((r) => (
                <li key={r.id}>
                  <button className={`runitem${r.id === runId && !wizard ? " on" : ""}`}
                          onClick={() => { open(r.id); setNavOpen(false); }}>
                    <span className="runitem-name">{label(r)}</span>
                    <span className="runitem-meta">{runMeta(r)}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="nav-empty">Nothing yet.</p>
          )}
        </nav>

        <div className="sidebar-foot">
          <div className="viewtoggle" role="group" aria-label="Layout">
            {(["auto", "desktop", "mobile"] as View[]).map((v) => (
              <button key={v} aria-pressed={choice === v} onClick={() => setView(v)}>
                {titled(v)}
              </button>
            ))}
          </div>
        </div>
      </aside>

      {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />}

      <main className="work">
        <header className="topbar">
          <button className="ghost menu" aria-label="Menu" onClick={() => setNavOpen(true)}>☰</button>
          <h2 className="work-title">
            {wizard ? "New migration"
              : state ? label(runs.find((r) => r.id === runId) ?? { id: runId })
              : "Data Bridge"}
          </h2>
          {state && !wizard && (
            <span className={`pill ${tone}`}><i className="dot" />{say(state.status)}</span>
          )}
          <span className="spacer" />
          {state && !wizard && counts && (
            state.delivery_paused ? (
              <button className="primary" disabled={busy}
                      onClick={() => void act(() => api.deliver(state.run_id))}>
                Resume sending
              </button>
            ) : (
              <button className="danger" disabled={busy || !counts.delivered}
                      onClick={() => void act(() => api.rollback(state.run_id))}>
                Undo sending
              </button>
            )
          )}
        </header>

        {error && (
          <div className="notice bad" role="alert">
            <b>{error}</b>
            {gone && <> The migration you had open is gone — start a new one from the left.</>}
          </div>
        )}

        {wizard && <Wizard resume={runId} onReady={(id) => { open(id); loadRuns(); }} />}

        {!wizard && !runId && !error && (
          <div className="panel empty">
            <strong>Nothing open</strong>
            Start a migration from the left, or pick one you've already run.
          </div>
        )}

        {!wizard && state?.status === "awaiting_schema" && (
          <div className="panel empty">
            <strong>Waiting on you</strong>
            This migration has your files but no agreed target yet. Nothing moves until
            there is one.
            <div style={{ marginTop: 16 }}>
              <button className="primary" onClick={() => setWizard(true)}>Carry on</button>
            </div>
          </div>
        )}

        {!wizard && state?.status === "processing" && state.progress && (
          <Working p={state.progress} />
        )}

        {!wizard && state && counts
          && state.status !== "processing" && state.status !== "awaiting_schema" && (
          <>
            <div className="stats">
              <Stat n={counts.rows_read} label="rows read" />
              <Stat n={counts.records} label="employees found" />
              <Stat n={counts.delivered} label="sent" tone="ok" />
              <Stat n={counts.needs_review} label="need you"
                    tone={counts.needs_review ? "warn" : undefined} />
              {!!counts.excluded && <Stat n={counts.excluded} label="left out" />}
              {!!counts.failed && <Stat n={counts.failed} label="refused" tone="bad" />}
            </div>

            {state.delivery_paused && (
              <div className="notice" role="status">
                <b>Sending is paused.</b> You undid a delivery, so nothing is going out
                automatically. Press <b>Resume sending</b> above when you're ready.
              </div>
            )}

            <div className="tabs" role="tablist">
              {[["review", `Needs you${counts.open_cases ? ` (${counts.open_cases})` : ""}`],
                ["records", "Employees"],
                ["mappings", "Columns"],
                ["schema", "Target"],
                ["destination", "Sent"],
                ...(counts.failed ? [["failures", `Refused (${counts.failed})`]] : []),
              ].map(([id, l]) => (
                <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
                  {l}
                </button>
              ))}
            </div>

            <div className="grid">
              <div>
                {tab === "review" && <Review state={state} onDone={refresh} />}
                {tab === "records" && <Records state={state} />}
                {tab === "mappings" && <Mappings state={state} />}
                {tab === "schema" && <Schema runId={state.run_id} />}
                {tab === "destination" && <Destination runId={state.run_id} />}
                {tab === "failures" && (
                  <Failures state={state} busy={busy}
                            onRetry={() => void act(() => api.deliver(state.run_id))} />
                )}
              </div>
              <aside className="panel">
                <h2>What the agent did</h2>
                <ul className="feed">
                  {[...state.activity].reverse().map((e, i) => (
                    <li key={i}>
                      <span className={`who${e.actor === "human" ? " human"
                        : e.action?.startsWith("delivery") ? " sent" : ""}`}>
                        {e.actor === "mock_api" ? "sent" : e.actor}
                      </span>
                      <span>{e.summary}</span>
                    </li>
                  ))}
                </ul>
              </aside>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
