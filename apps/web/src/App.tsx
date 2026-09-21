import { useCallback, useEffect, useRef, useState, Fragment } from "react";
import { api, ApiError, PHRASE, type RunState } from "./api";
import Review from "./Review";
import Loading from "./Loading";
import PushDialog from "./PushDialog";
import Wizard from "./Wizard";

const say = (k: string) => PHRASE[k] ?? k.replace(/_/g, " ").toLowerCase();
const titled = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** A field name rendered for someone who did not write it. */
const fieldLabel = (name: string) =>
  titled(name.replace(/_/g, " ").replace(/\bid\b/i, "ID"));

/* ------------------------------------------------------------------- hooks */

type View = "auto" | "desktop" | "mobile";

function useView(): [View, boolean, (v: View) => void, boolean] {
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
  // Asking for Mobile on a wide screen means "show me the phone", not "stretch
  // one column across 1400px".
  return [choice, choice === "auto" ? narrow : choice === "mobile", set,
          choice === "mobile" && !narrow];
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
        <h2>Processing</h2>
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
          <thead><tr><th>Employee</th><th>Name</th><th>Status</th><th>Cleaned</th></tr></thead>
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
                  <td data-label="Cleaned">
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
      <h2>Column mappings</h2>
      <div className="scroll">
        <table className="responsive">
          <thead>
            <tr><th>Column</th><th>From</th><th>Target field</th><th>Evidence</th></tr>
          </thead>
          <tbody>
            {state.mappings.map((m, i) => (
              <tr key={i}>
                <td data-label="Column" className="mono">{m.column}</td>
                <td data-label="From" className="change">{m.file}</td>
                <td data-label="Target field">
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
                <td data-label="Evidence">
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
  if (r.status === "processing") return "processing\u2026";
  if (open) return `${open} to review`;
  if (r.delivered) return `${r.delivered} sent`;
  if (r.counts?.excluded) return `${r.counts.excluded} excluded`;
  if (r.counts?.records) return `${r.counts.records} ready to push`;
  return PHRASE[r.status] ?? "not started";
}

/**
 * What a run is called. A name somebody typed is shown exactly as they typed it —
 * only the names made up here (a bundled sample's folder, a bare id) are tidied.
 */
function runName(r: { id: string; label?: string | null }): string {
  const bundled = r.label?.match(/^(?:sample|fixtures)\/(.+)$/);
  if (bundled) {
    return bundled[1].replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }
  return r.label || `Run ${r.id.replace(/^run-/, "")}`;
}

/**
 * One migration in the sidebar, renamed the way a chat is: from the ⋯ menu, or by
 * double-clicking its name. Enter or clicking away saves, Escape puts it back, and
 * an empty or unchanged name is no change at all.
 */
function RunItem({ run, active, disabled, onOpen, onRenamed }: {
  run: any; active: boolean; disabled: boolean;
  onOpen: () => void; onRenamed: (label: string) => void;
}) {
  const [menu, setMenu] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const row = useRef<HTMLLIElement>(null);
  const input = useRef<HTMLInputElement>(null);
  // Leaving the field saves, and so does Enter — which also leaves it. Escape
  // unmounts the field, which can fire a late blur with the abandoned text.
  const settled = useRef(false);
  const name = runName(run);

  useEffect(() => {
    if (!editing) return;
    input.current?.focus();
    input.current?.select();   // typing replaces the old name, as it does in a chat list
  }, [editing]);

  useEffect(() => {
    if (!menu) return;
    const away = (e: Event) => {
      if (!row.current?.contains(e.target as Node)) setMenu(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(false); };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [menu]);

  const startEdit = () => {
    setMenu(false); setDraft(name); setProblem(null);
    settled.current = false; setEditing(true);
  };
  const cancel = () => { settled.current = true; setEditing(false); setProblem(null); };
  const save = async () => {
    if (settled.current) return;
    const next = draft.trim();
    if (!next || next === name) { cancel(); return; }
    settled.current = true;
    setSaving(true);
    try {
      const saved = await api.rename(run.id, next);
      setEditing(false);
      onRenamed(saved.label);
    } catch (e) {
      // Keep what they typed and say why, rather than silently reverting it.
      settled.current = false;
      setProblem(e instanceof ApiError ? e.message : "Couldn't reach the server.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <li ref={row} className={`runrow${active ? " on" : ""}${menu ? " menu-open" : ""}`}>
      {editing ? (
        <div className="runitem editing">
          <input ref={input} className="runitem-input" value={draft} maxLength={120}
                 aria-label="Migration name" readOnly={saving}
                 onChange={(e) => { setDraft(e.target.value); setProblem(null); }}
                 onKeyDown={(e) => {
                   if (e.key === "Enter") { e.preventDefault(); void save(); }
                   if (e.key === "Escape") { e.preventDefault(); cancel(); }
                 }}
                 onBlur={() => void save()} />
          {problem && <span className="runitem-problem" role="alert">{problem}</span>}
        </div>
      ) : (
        <button className={`runitem${active ? " on" : ""}`} title={name}
                onClick={onOpen} onDoubleClick={() => { if (!disabled) startEdit(); }}>
          <span className="runitem-name">{name}</span>
          <span className="runitem-meta">{runMeta(run)}</span>
        </button>
      )}
      {!editing && (
        <button className="runitem-more" aria-label={`Options for ${name}`}
                aria-haspopup="menu" aria-expanded={menu} disabled={disabled}
                onClick={() => setMenu(!menu)}>
          ⋯
        </button>
      )}
      {menu && (
        <div className="runmenu" role="menu" aria-label={`Options for ${name}`}>
          <button role="menuitem" autoFocus onClick={startEdit}>Rename</button>
        </div>
      )}
    </li>
  );
}

/* -------------------------------------------------------------- destination */

function Destination({ runId, stamp }: { runId: string; stamp: string }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [show, setShow] = useState<string | null>(null);

  // Read again whenever what was sent changes - a push or an undo while this tab is
  // open used to leave it showing what the destination held before.
  useEffect(() => {
    api.destination(runId).then(setData).catch((e) => setErr(String(e)));
  }, [runId, stamp]);

  if (err) return <div className="panel empty"><strong>Can't reach the destination</strong>{err}</div>;
  if (!data) return <Loading label="Checking what the destination holds…" rows={4} />;
  if (!data.records.length) {
    return <div className="panel empty">
      <strong>Nothing has been sent yet</strong>
      Press <b>Push to the target</b> above to send the employees that have
      nothing open against them.
    </div>;
  }

  // One line per employee: the copy the destination holds now. After an undo and a
  // second push it holds both - the first undone, the second stored - and listing
  // every copy oldest-first put "Undone" at the top of a run that had been resent.
  const copies = new Map<string, any[]>();
  for (const r of data.records) {
    copies.set(r.natural_key, [...(copies.get(r.natural_key) ?? []), r]);
  }
  const current = [...copies.entries()]
    .map(([, rows]) => rows.reduce((a, b) =>
      (b.generation > a.generation
        || (b.generation === a.generation && b.received_at > a.received_at)) ? b : a))
    .sort((a, b) => String(a.natural_key).localeCompare(String(b.natural_key)));

  return (
    <div className="panel">
      <h2>Destination records</h2>
      <div className="scroll">
        <table className="responsive">
          <thead><tr><th>Employee</th><th>Status</th><th>Received</th><th /></tr></thead>
          <tbody>
            {current.map((r: any) => {
              const sent = copies.get(r.natural_key)?.length ?? 1;
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
                      {sent > 1 && (
                        <span className="change" style={{ marginLeft: 8 }}>
                          sent {sent} times, earlier {sent === 2 ? "copy" : "copies"} undone
                        </span>
                      )}
                    </td>
                    <td data-label="Received" className="change">
                      {r.received_at?.slice(11, 19) ?? "—"}
                    </td>
                    <td data-label="">
                      <button className="ghost" aria-expanded={open}
                              onClick={() => setShow(open ? null : r.id)}>
                        {open ? "Hide" : "View values"}
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
      <h2>Rejected records</h2>
      <p className="lead">
        Not a question the agent is asking — the receiving system would not take them.
      </p>
      <div className="scroll">
        <table className="responsive">
          <thead><tr><th>Employee</th><th>Response</th></tr></thead>
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

  if (!data) return <Loading label="Loading the agreed target…" rows={3} />;
  if (!data.active) return <div className="panel empty">No schema agreed for this run yet.</div>;

  return (
    <div className="panel">
      <h2>Target fields</h2>
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
  const [pushing, setPushing] = useState(false);
  const [choice, isMobile, setView, phonePreview] = useView();
  const { state, error, gone, refresh } = useRun(runId);

  // A run that no longer exists should not leave a dead id in the address bar.
  useEffect(() => {
    if (gone) { setRunId(null); history.replaceState(null, "", location.pathname); }
  }, [gone]);

  useEffect(() => {
    document.body.classList.toggle("phone-preview", phonePreview);
    return () => document.body.classList.remove("phone-preview");
  }, [phonePreview]);

  const loadRuns = useCallback(() => { api.runs().then(setRuns).catch(() => {}); }, []);
  useEffect(loadRuns, [loadRuns]);
  // The sidebar says where each run got to, so it follows the open run: finishing
  // processing, a decision or a push all change what its line should say. It used
  // to be read once and after a few actions, and said "processing…" of runs that
  // had long finished.
  const where = state
    ? `${state.status}|${state.counts.open_cases}|${state.counts.delivered}` : "";
  useEffect(() => { if (where) loadRuns(); }, [where, loadRuns]);

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

  // The server has confirmed the name by the time this runs; the list catches up
  // on its next load rather than flashing the old one in between.
  const renamed = (id: string, name: string) => {
    setRuns((rs) => rs.map((r) => (r.id === id ? { ...r, label: name } : r)));
    loadRuns();
  };

  return (
    <>
    {pushing && state && counts && (
      <PushDialog runId={state.run_id} ready={counts.ready}
                  onClose={() => setPushing(false)}
                  onDone={() => void refresh()} />
    )}
    {phonePreview && (
      <div className="escape-hatch">
        <span>Previewing the mobile layout</span>
        <button onClick={() => setView("desktop")}>Exit</button>
      </div>
    )}
    <div className={`shell${isMobile ? " is-mobile" : ""}${navOpen ? " nav-open" : ""}`}
         data-layout={isMobile ? "mobile" : "desktop"}
         data-preview={phonePreview ? "phone" : undefined}>
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
                <RunItem key={r.id} run={r} active={r.id === runId && !wizard}
                         disabled={busy}
                         onOpen={() => { open(r.id); setNavOpen(false); }}
                         onRenamed={(name) => renamed(r.id, name)} />
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
              : state ? runName(runs.find((r) => r.id === runId) ?? { id: runId! })
              : "Data Bridge"}
          </h2>
          {state && !wizard && (
            <span className={`pill ${tone}`}><i className="dot" />{say(state.status)}</span>
          )}
          <span className="spacer" />
          {state && !wizard && counts && (
            <div className="send-controls">
              {/* Pushing to the target is the one action with a consequence outside
                  this tool, so it is a button somebody presses, not something that
                  happens while they are reading the queue. */}
              {counts.ready > 0 && (
                <button className="primary" disabled={busy}
                        onClick={() => setPushing(true)}>
                  Push {counts.ready} to the target
                </button>
              )}
              {state.delivery_paused && counts.ready === 0 && (
                <button className="primary" disabled={busy}
                        onClick={() => void act(() => api.deliver(state.run_id))}>
                  Resume sending
                </button>
              )}
              {!!counts.delivered && (
                <button className="danger" disabled={busy}
                        onClick={() => void act(() => api.rollback(state.run_id))}>
                  Undo sending
                </button>
              )}
            </div>
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

        {!wizard && runId && !state && !error && (
          <Loading label="Opening this migration…" rows={4} />
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
              <Stat n={counts.needs_review} label="needs review"
                    tone={counts.needs_review ? "warn" : undefined} />
              {!!counts.excluded && <Stat n={counts.excluded} label="excluded" />}
              {!!counts.failed && <Stat n={counts.failed} label="rejected" tone="bad" />}
            </div>

            {state.delivery_paused && (
              <div className="notice" role="status">
                <b>Sending is paused.</b> You undid a delivery, so nothing will go to
                the destination until you push again.
              </div>
            )}

            <div className="tabs" role="tablist">
              {[["review", `Review queue${counts.open_cases ? ` (${counts.open_cases})` : ""}`],
                ["records", "Employees"],
                ["mappings", "Columns"],
                ["schema", "Target fields"],
                ["destination", "Destination"],
                ...(counts.failed ? [["failures", `Rejected (${counts.failed})`]] : []),
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
                {tab === "destination" && (
                  <Destination runId={state.run_id}
                               stamp={`${counts.delivered}|${state.delivery_paused ?? ""}`} />
                )}
                {tab === "failures" && (
                  <Failures state={state} busy={busy}
                            onRetry={() => void act(() => api.deliver(state.run_id))} />
                )}
              </div>
              <aside className="panel">
                <h2>Agent history</h2>
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
    </>
  );
}
