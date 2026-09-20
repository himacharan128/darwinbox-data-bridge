import { useCallback, useEffect, useState } from "react";
import { api, type RunFiles, type Sample, type SchemaState } from "./api";

/**
 * Getting a run started: upload files, then agree a target schema.
 *
 * The schema step is not a formality. Nothing is mapped, cleaned or delivered until
 * a human has approved one — a migration against a schema nobody agreed to makes
 * every decision after it unaccountable. So approval is what starts the work.
 */
export default function Wizard(
  { onReady, resume }: { onReady: (runId: string) => void; resume?: string | null },
) {
  const [runId, setRunId] = useState<string | null>(resume ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!runId) {
    return <PickFiles busy={busy} setBusy={setBusy} error={error} setError={setError}
                      onUploaded={setRunId} />;
  }
  return <ChooseSchema runId={runId} onApproved={() => onReady(runId)} />;
}

/* ------------------------------------------------------------------ step 1 */

function PickFiles({
  busy, setBusy, error, setError, onUploaded,
}: {
  busy: boolean; setBusy: (b: boolean) => void;
  error: string | null; setError: (e: string | null) => void;
  onUploaded: (runId: string) => void;
}) {
  const [samples, setSamples] = useState<Sample[]>([]);
  const [dragging, setDragging] = useState(false);

  useEffect(() => { api.samples().then(setSamples).catch(() => {}); }, []);

  const send = async (fn: () => Promise<{ run_id: string }>) => {
    setBusy(true); setError(null);
    try { onUploaded((await fn()).run_id); }
    catch (e) { setError(String(e).replace("Error: ", "")); }
    finally { setBusy(false); }
  };

  const drop = (e: React.DragEvent) => {
    e.preventDefault(); setDragging(false);
    if (e.dataTransfer.files?.length) void send(() => api.upload(e.dataTransfer.files));
  };

  return (
    <>
      <ol className="steps" aria-label="Progress">
        <li aria-current="step">1 · Your files</li>
        <li>2 · Target schema</li>
        <li>3 · Review and send</li>
      </ol>

      <div className="panel">
        <h2>Step 1 — the client's files</h2>
        <p className="change" style={{ marginTop: 0 }}>
          CSV, Excel, JSON, YAML or PDF, including scans. Send lookup tables
          (departments, locations) along with them and they will be recognised.
        </p>

        <label
          className={`dropzone${dragging ? " over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={drop}
        >
          <input type="file" multiple disabled={busy} style={{ display: "none" }}
                 onChange={(e) => e.target.files?.length &&
                   void send(() => api.upload(e.target.files!))} />
          <strong>Drop files here</strong>
          <span className="change">or click to choose · nothing starts until you approve a schema</span>
        </label>

        {busy && <p className="change" role="status">Uploading…</p>}
        {error && <p style={{ color: "var(--bad)" }} role="alert">{error}</p>}
      </div>

      {!!samples.length && (
        <div className="panel" style={{ marginTop: 14 }}>
          <h2>…or use a bundled set</h2>
          <p className="change" style={{ marginTop: 0 }}>
            Each one is aimed at a different behaviour. Start with <b>02-messy</b>.
          </p>
          <div className="samples">
            {samples.map((s) => (
              <button key={s.name} disabled={busy} className="sample"
                      onClick={() => void send(() => api.startFromSample(s.name))}>
                <b>{s.name}</b>
                <span className="change">{s.description}</span>
                <span className="pill">{s.files} files</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ step 2 */

type Mode = null | "supply" | "propose";

function ChooseSchema({ runId, onApproved }: { runId: string; onApproved: () => void }) {
  const [files, setFiles] = useState<RunFiles | null>(null);
  const [schema, setSchema] = useState<SchemaState | null>(null);
  const [mode, setMode] = useState<Mode>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(() => {
    api.runFiles(runId).then(setFiles).catch(() => {});
    api.schema(runId).then(setSchema).catch(() => {});
  }, [runId]);
  useEffect(load, [load]);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true); setError(null); setNote(null);
    try { await fn(); if (done) setNote(done); load(); }
    catch (e) { setError(String(e).replace("Error: ", "")); }
    finally { setBusy(false); }
  };

  const draftVersion = schema?.versions.find((v) => v.state === "draft");
  const unreadable = files?.files.filter((f) => !f.supported) ?? [];

  return (
    <>
      <ol className="steps" aria-label="Progress">
        <li className="done">1 · Your files</li>
        <li aria-current="step">2 · Target schema</li>
        <li>3 · Review and send</li>
      </ol>

      {files && (
        <div className="panel" style={{ marginBottom: 14 }}>
          <h2>What was read</h2>
          <p style={{ marginTop: 0 }}>
            <b>{files.files.length}</b> files · <b>{files.total_rows}</b> rows ·
            {" "}<b>{files.total_columns}</b> columns
          </p>
          <div className="scroll">
            <table>
              <thead><tr><th>File</th><th>Type</th><th>Rows</th><th>Columns</th></tr></thead>
              <tbody>
                {files.files.map((f) => (
                  <tr key={f.name}>
                    <td data-label="File" className="mono">{f.name}</td>
                    <td data-label="Type">
                      <span className={`pill ${f.supported ? "ok" : "bad"}`}>
                        <i className="dot" />{f.supported ? f.kind : "not readable"}
                      </span>
                    </td>
                    <td data-label="Rows">{f.rows || "—"}</td>
                    <td data-label="Columns" className="change">
                      {f.columns.slice(0, 5).join(", ")}
                      {f.columns.length > 5 ? ` +${f.columns.length - 5}` : ""}
                      {f.note && <div style={{ color: "var(--warn)" }}>{f.note}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!!unreadable.length && (
            <p className="change" style={{ color: "var(--warn)" }}>
              {unreadable.length} file(s) could not be read and will be ignored.
            </p>
          )}
        </div>
      )}

      {!mode && (
        <div className="panel">
          <h2>Step 2 — where should this data land?</h2>
          <p className="change" style={{ marginTop: 0 }}>
            The target schema is the contract everything is measured against. Nothing is
            mapped or sent until you approve one.
          </p>
          <div className="choices">
            <button className="choice" onClick={() => {
              setMode("supply");
              api.schemaStarter(runId).then((r) => setDraft(r.body)).catch(() => {});
            }}>
              <b>I have a target schema</b>
              <span className="change">
                Paste or edit YAML or JSON. It is validated before you can approve it.
              </span>
            </button>
            <button className="choice primary-choice" disabled={busy} onClick={() => {
              setMode("propose");
              void act(() => api.recommendSchema(runId), "The agent proposed a schema — edit it below.");
            }}>
              <b>Propose one from my files</b>
              <span className="change">
                The agent reads the columns and suggests fields, types and rules. You edit
                and approve.
              </span>
            </button>
          </div>
        </div>
      )}

      {mode === "propose" && busy && (
        <div className="panel" role="status">
          <h2>Reading your columns…</h2>
          <p className="change">Working out what the destination should look like.</p>
        </div>
      )}

      {mode && !busy && (
        <>
          {schema?.active && (
            <div className="panel" style={{ marginBottom: 14 }}>
              <h2>
                Proposed schema — {schema.active.entity}
                {draftVersion && <span className="pill warn" style={{ marginLeft: 8 }}>
                  <i className="dot" />draft v{draftVersion.version}</span>}
              </h2>
              <div className="scroll">
                <table>
                  <thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Rules</th></tr></thead>
                  <tbody>
                    {schema.active.fields.map((f) => (
                      <tr key={f.name}>
                        <td data-label="Field" className="mono">{f.name}</td>
                        <td data-label="Type">{f.type}</td>
                        <td data-label="Required">{f.required ? "yes" : "—"}</td>
                        <td data-label="Rules" className="change">
                          {[f.unique && "unique",
                            f.pattern && `matches ${f.pattern}`,
                            f.allowed?.length && `one of ${f.allowed.join(", ")}`,
                            f.reference && `references ${f.reference}`,
                          ].filter(Boolean).join(" · ") || "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="panel">
            <h2>{mode === "supply" ? "Your schema" : "Edit before approving"}</h2>
            <p className="change" style={{ marginTop: 0 }}>
              Rename fields, change types, mark things required, add allowed values or
              patterns. Anything you constrain here is evidence the agent can use, which
              means fewer questions later.
            </p>
            <textarea value={draft} onChange={(e) => setDraft(e.target.value)}
              aria-label="Target schema as YAML or JSON" rows={12}
              placeholder={"entity: employee\nfields:\n  - name: employee_id\n    type: string\n    required: true\n    unique: true"}
              style={{ width: "100%", font: "12.5px ui-monospace, Menlo, monospace",
                       background: "var(--panel-2)", color: "var(--ink)", padding: 10,
                       border: "1px solid var(--line)", borderRadius: 8 }} />

            {error && <p style={{ color: "var(--bad)" }} role="alert">{error}</p>}
            {note && <p className="change" role="status">{note}</p>}

            <div className="actions" style={{ marginTop: 12 }}>
              <button disabled={busy || !draft.trim()}
                      onClick={() => void act(() => api.putSchema(runId, draft),
                                              "Saved as a new draft.")}>
                Save my edits as a draft
              </button>
              {mode === "supply" && (
                <button disabled={busy}
                        onClick={() => void act(() => api.recommendSchema(runId),
                                                "The agent proposed one.")}>
                  Ask the agent instead
                </button>
              )}
              <span className="spacer" />
              <button className="primary" disabled={busy || !draftVersion}
                      onClick={() => void act(async () => {
                        await api.approveSchema(runId, draftVersion!.version);
                        onApproved();
                      })}>
                Approve and start the migration
              </button>
            </div>
            {!draftVersion && (
              <p className="change">
                Save a draft first — approving is what starts the migration.
              </p>
            )}
          </div>
        </>
      )}
    </>
  );
}
