import { useCallback, useEffect, useMemo, useState } from "react";
import { api, PHRASE, type Case, type RunState, type SchemaState } from "./api";
import Wizard from "./Wizard";

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

  // Left and right move between cases. Reviewing forty of these with a mouse is
  // slower than it needs to be, and navigating still never submits anything.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (e.key === "ArrowLeft") setI((n) => Math.max(0, n - 1));
      if (e.key === "ArrowRight") setI((n) => Math.min(cases.length - 1, n + 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cases.length]);

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
        <span className="count" aria-live="polite">Case {i + 1} of {cases.length}</span>
        <span className="sr-only">Use the left and right arrow keys to move between cases.</span>
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
          <><dt>Effect</dt>
            <dd>
              {c.blocks} records are waiting on this one answer
              {c.children > 0 && `, ${c.children} of them only because a record they `
                + `reference is blocked`}.
            </dd></>
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
              const tone = r.state === "delivered" ? "ok"
                : r.state === "blocked" ? "warn" : "";
              const label = r.state === "blocked" && r.waiting_on_another
                ? "Waiting on another record" : say(r.state);
              return (
                <tr key={r.key}>
                  <td data-label="Record" className="mono">{r.key}</td>
                  <td data-label="Status">
                    <span className={`pill ${tone}`}><i className="dot" />{label}</span>
                  </td>
                  <td data-label="Name">
                    {[r.values.first_name, r.values.last_name].filter(Boolean).join(" ") || "—"}
                  </td>
                  <td data-label="What changed">
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
              <td data-label="File" className="mono">{m.file}</td>
              <td data-label="Column" className="mono">{m.column}</td>
              <td data-label="Decision">
                <span className={`pill ${m.decision === "auto_apply" ? "ok" : "warn"}`}>
                  <i className="dot" />{m.decision === "auto_apply" ? "Applied automatically" : say(m.decision)}
                </span>
              </td>
              <td data-label="Mapped to" className="mono">{m.field ?? "—"}</td>
              <td data-label="Why" className="change">{m.evidence.slice(0, 3).join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Failures({
  state, onRetry, busy,
}: { state: RunState; onRetry: () => void; busy: boolean }) {
  const failures = state.failures ?? [];
  if (!failures.length) {
    return <div className="panel empty">The destination has refused nothing.</div>;
  }
  return (
    <div className="panel">
      <h2>The destination refused these</h2>
      <p className="change" style={{ marginTop: 0 }}>
        Not a question the agent is asking — the receiving system rejected them. Fix the
        data or the schema, then try again.
      </p>
      <div className="scroll">
        <table>
          <thead><tr><th>Record</th><th>What the destination said</th></tr></thead>
          <tbody>
            {failures.map((f) => (
              <tr key={f.record}>
                <td data-label="Record" className="mono">{f.record}</td>
                <td data-label="Reason" className="change">{f.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="actions" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy} onClick={onRetry}>
          Try sending these again
        </button>
      </div>
    </div>
  );
}

function Schema({ runId }: { runId: string }) {
  const [data, setData] = useState<SchemaState | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(() => {
    api.schema(runId).then(setData).catch((e) => setNote(String(e)));
  }, [runId]);
  useEffect(load, [load]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true); setNote(null);
    try { await fn(); setNote(message); load(); }
    catch (e) { setNote(String(e).replace("Error: ", "")); }
    finally { setBusy(false); }
  };

  if (!data) return <div className="panel empty">Loading the target schema…</div>;
  if (!data.active) {
    return <div className="panel empty">No schema has been agreed for this run yet.</div>;
  }
  const pending = data.versions.filter((v) => v.state === "draft");

  return (
    <>
      <div className="panel" style={{ marginBottom: 14 }}>
        <h2>Target schema — {data.active.entity}</h2>
        <p className="change" style={{ marginTop: 0 }}>
          {data.approved_version
            ? `Version ${data.approved_version} is approved. Records already sent keep the version they were sent under.`
            : "No version approved yet."}
        </p>
        <div className="scroll">
          <table>
            <thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Rules</th></tr></thead>
            <tbody>
              {data.active.fields.map((f) => (
                <tr key={f.name}>
                  <td className="mono">{f.name}</td>
                  <td>{f.type}</td>
                  <td>{f.required ? "yes" : "—"}</td>
                  <td className="change">
                    {[f.unique && "unique", f.pattern && `matches ${f.pattern}`,
                      f.allowed?.length && `one of ${f.allowed.join(", ")}`,
                      f.reference && `references ${f.reference}`]
                      .filter(Boolean).join(" · ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel" style={{ marginBottom: 14 }}>
        <h2>Versions</h2>
        <table>
          <thead><tr><th>Version</th><th>State</th><th>Where it came from</th><th></th></tr></thead>
          <tbody>
            {data.versions.map((v) => (
              <tr key={v.version}>
                <td className="mono">v{v.version}</td>
                <td><span className={`pill ${v.state === "approved" ? "ok" : ""}`}>
                  <i className="dot" />{v.state}</span></td>
                <td className="change">
                  {v.origin}{v.approved_by ? ` · approved by ${v.approved_by}` : ""}
                </td>
                <td>
                  {v.state === "draft" && (
                    <button disabled={busy}
                            onClick={() => void act(() => api.approveSchema(runId, v.version),
                                                    `Approved v${v.version}`)}>
                      Approve
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!data.versions.length && (
              <tr><td colSpan={4} className="change">No versions recorded yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Change the schema</h2>
        <p className="change" style={{ marginTop: 0 }}>
          Paste YAML or JSON, or let the agent propose one from the uploaded files.
          Either way it becomes a draft you approve.
        </p>
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)}
          aria-label="Schema as YAML or JSON" rows={8}
          placeholder={"entity: employee\nfields:\n  - name: employee_id\n    type: string\n    required: true"}
          style={{ width: "100%", font: "12.5px ui-monospace, Menlo, monospace",
                   background: "var(--panel-2)", color: "var(--ink)", padding: 10,
                   border: "1px solid var(--line)", borderRadius: 8 }} />
        <div className="actions" style={{ marginTop: 12 }}>
          <button className="primary" disabled={busy || !draft.trim()}
                  onClick={() => void act(() => api.putSchema(runId, draft), "Draft saved")}>
            Save as draft
          </button>
          <button disabled={busy}
                  onClick={() => void act(() => api.recommendSchema(runId),
                                          "The agent proposed a schema")}>
            Ask the agent to propose one
          </button>
          {!!pending.length && <span className="pill warn"><i className="dot" />
            {pending.length} draft awaiting approval</span>}
        </div>
        {note && <p className="change" style={{ marginTop: 10 }} role="status">{note}</p>}
      </div>
    </>
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

type Layout = "auto" | "desktop" | "mobile";

function useLayout(): [Layout, Layout, (l: Layout) => void] {
  const [choice, setChoice] = useState<Layout>(() => {
    try {
      return (localStorage.getItem("dbx.layout") as Layout) || "auto";
    } catch {
      return "auto";   // private windows and blocked storage must not break the page
    }
  });
  const [narrow, setNarrow] = useState(
    () => typeof matchMedia === "function" && matchMedia("(max-width: 820px)").matches,
  );

  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const q = matchMedia("(max-width: 820px)");
    const on = () => setNarrow(q.matches);
    q.addEventListener("change", on);
    return () => q.removeEventListener("change", on);
  }, []);

  const set = (l: Layout) => {
    setChoice(l);
    try { localStorage.setItem("dbx.layout", l); } catch { /* not worth failing over */ }
  };
  const effective: Layout = choice === "auto" ? (narrow ? "mobile" : "desktop") : choice;
  return [choice, effective, set];
}

export default function App() {
  const [choice, layout, setLayout] = useLayout();
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

  const [wizard, setWizard] = useState(false);

  const startWizard = () => {
    setWizard(true);
    setRunId(null);
    history.replaceState(null, "", location.pathname);
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
    <div className="app" data-layout={layout}>
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
        <div className="layout-toggle" role="group" aria-label="Layout">
          {(["auto", "desktop", "mobile"] as Layout[]).map((l) => (
            <button key={l} aria-pressed={choice === l} onClick={() => setLayout(l)}>
              {l[0].toUpperCase() + l.slice(1)}
            </button>
          ))}
        </div>
        <button className="primary" onClick={startWizard} disabled={busy}>
          New migration
        </button>
      </header>

      {error && <div className="panel" style={{ color: "var(--bad)" }} role="alert">{error}</div>}

      {wizard && (
        <Wizard resume={runId} onReady={(id) => { setWizard(false); open(id); loadRuns(); }} />
      )}

      {!wizard && !runId && (
        <div className="panel empty">
          <p style={{ fontSize: 17 }}>Start a migration to see the agent work.</p>
          <p>Upload the client's exports, agree a target schema, and the agent maps and
             cleans what it safely can — asking only about what it genuinely cannot settle.</p>
          <button className="primary" onClick={startWizard} style={{ marginTop: 12 }}>
            Start a migration
          </button>
        </div>
      )}

      {state?.status === "awaiting_schema" && !wizard && (
        <div className="panel empty">
          <p style={{ fontSize: 17 }}>This run is waiting for a target schema.</p>
          <p>Nothing is mapped or sent until one is approved.</p>
          <button className="primary" style={{ marginTop: 12 }}
                  onClick={() => setWizard(true)}>Choose a schema</button>
        </div>
      )}

      {state?.progress && !state.progress.finished && state.status === "processing" && (
        <Working p={state.progress} />
      )}

      {state && counts && !wizard
        && state.status !== "processing" && state.status !== "awaiting_schema" && (
        <>
          <div className="stats">
            <Stat n={counts.rows_read} label="rows read" />
            <Stat n={counts.records} label="employees found" />
            <Stat n={counts.delivered} label="sent to destination" tone="ok" />
            <Stat n={counts.needs_review} label="needs review"
                  tone={counts.needs_review ? "warn" : undefined} />
            {/* Only when they exist, so the usual four stay clean but nothing is hidden. */}
            {!!counts.excluded && <Stat n={counts.excluded} label="excluded by you" />}
            {!!counts.failed && <Stat n={counts.failed} label="destination refused" tone="bad" />}
            <span className="spacer" />
            {state.delivery_paused ? (
              <button className="primary" disabled={busy}
                      onClick={() => void act(() => api.deliver(state.run_id))}>
                Resume sending
              </button>
            ) : (
              <button className="danger" disabled={busy || !counts.delivered}
                      onClick={() => void act(() => api.rollback(state.run_id))}>
                Undo delivery
              </button>
            )}
          </div>

          {state.delivery_paused && (
            <div className="panel notice" role="status">
              <b>Sending is paused.</b> You undid a delivery, so nothing is being sent
              automatically. Press <b>Resume sending</b> when you are ready.
            </div>
          )}

          <div className="tabs" role="tablist">
            {[["review", `Needs you (${counts.open_cases})`], ["records", "Records"],
              ["mappings", "How it mapped"], ["schema", "Target schema"],
              ["destination", "Destination"],
              ...(counts.failed ? [["failures", `Failures (${counts.failed})`]] : []),
            ].map(([id, label]) => (
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
              {tab === "schema" && <Schema runId={state.run_id} />}
              {tab === "failures" && <Failures state={state} onRetry={() =>
                void act(() => api.deliver(state.run_id))} busy={busy} />}
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
