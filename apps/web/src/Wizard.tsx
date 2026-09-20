import { useCallback, useEffect, useState } from "react";
import { api, type RunFiles, type Sample, type SchemaState } from "./api";
import SchemaEditor, { blankField, type Schema } from "./SchemaEditor";

/**
 * Getting a run started: upload files, then agree a target schema.
 *
 * The schema step is not a formality. Nothing is mapped, cleaned or delivered until a
 * human approves one — a migration against a schema nobody agreed to makes every
 * decision after it unaccountable. So approval is what starts the work.
 */
export default function Wizard(
  { onReady, resume }: { onReady: (runId: string) => void; resume?: string | null },
) {
  const [runId, setRunId] = useState<string | null>(resume ?? null);

  if (!runId) return <PickFiles onUploaded={setRunId} />;
  return (
    <ChooseSchema runId={runId} onApproved={() => onReady(runId)}
                  onBack={() => setRunId(null)} />
  );
}

/* ------------------------------------------------------------------ step 1 */

function PickFiles({ onUploaded }: { onUploaded: (runId: string) => void }) {
  const [samples, setSamples] = useState<Sample[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { api.samples().then(setSamples).catch(() => {}); }, []);

  const send = async (fn: () => Promise<{ run_id: string }>) => {
    setBusy(true); setError(null);
    try { onUploaded((await fn()).run_id); }
    catch (e) { setError(String(e).replace("Error: ", "")); }
    finally { setBusy(false); }
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
          Add everything they sent you. Spreadsheets, exports, even scanned pages.
        </p>

        <label className={`dropzone${dragging ? " over" : ""}`}
               onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
               onDragLeave={() => setDragging(false)}
               onDrop={(e) => {
                 e.preventDefault(); setDragging(false);
                 if (e.dataTransfer.files?.length) {
                   void send(() => api.upload(e.dataTransfer.files));
                 }
               }}>
          <input type="file" multiple disabled={busy} style={{ display: "none" }}
                 onChange={(e) => e.target.files?.length &&
                   void send(() => api.upload(e.target.files!))} />
          <strong>Drop files here</strong>
          <span className="change">or click to choose</span>
        </label>

        {busy && <p className="change" role="status">Uploading…</p>}
        {error && <p style={{ color: "var(--bad)" }} role="alert">{error}</p>}
      </div>

      {!!samples.length && (
        <details className="panel collapse" style={{ marginTop: 14 }}>
          <summary>
            <span>Sample files</span>
            <span className="change">
              — don't have files to hand? Try one of these
            </span>
          </summary>
          <div className="samples" style={{ marginTop: 14 }}>
            {samples.map((s) => (
              <button key={s.name} disabled={busy} className="sample"
                      onClick={() => void send(() => api.startFromSample(s.name))}>
                <b>{s.name.replace(/^\d+-/, "").replace(/-/g, " ")}</b>
                <span className="change">{s.description}</span>
                <span className="pill">{s.files} files</span>
              </button>
            ))}
          </div>
        </details>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ step 2 */

type Mode = null | "supply" | "propose";

function ChooseSchema({
  runId, onApproved, onBack,
}: { runId: string; onApproved: () => void; onBack: () => void }) {
  const [files, setFiles] = useState<RunFiles | null>(null);
  const [schema, setSchema] = useState<SchemaState | null>(null);
  const [working, setWorking] = useState<Schema | null>(null);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<Mode>(null);
  const [raw, setRaw] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    api.runFiles(runId).then(setFiles).catch(() => {});
    const state = await api.schema(runId).catch(() => null);
    if (!state) return;
    setSchema(state);
    // Whatever is showing becomes what you are editing. Without this, asking the agent
    // updates a panel while the editor still holds whatever was there before — which
    // looks exactly like the agent having done nothing.
    if (state.active) {
      setWorking(state.active as Schema);
      setDraft(JSON.stringify(state.active, null, 2));
      setDirty(false);
    }
  }, [runId]);

  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true); setError(null); setNote(null);
    try { await fn(); if (done) setNote(done); await load(); }
    catch (e) { setError(String(e).replace("Error: ", "")); }
    finally { setBusy(false); }
  };

  const saveDraft = async () => {
    const body = raw ? draft : JSON.stringify(working);
    const saved = await api.putSchema(runId, body);
    setDirty(false);
    return saved;
  };

  const unreadable = files?.files.filter((f) => !f.supported) ?? [];
  // A small file whose first column is "code" is a lookup table — which is what a
  // reference constraint can point at.
  const lookupNames = (files?.files ?? [])
    .filter((f) => f.supported && f.columns[0] === "code" && f.columns.length <= 4)
    .map((f) => f.name.replace(/\.[^.]+$/, "").replace(/s$/, ""));

  const canApprove = !busy && (dirty || !!schema?.draft_version);

  return (
    <>
      <ol className="steps" aria-label="Progress">
        <li>
          <button type="button" className="steplink" onClick={onBack}>
            ✓ 1 · Your files
          </button>
        </li>
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
          <button type="button" className="icon" onClick={onBack} style={{ marginTop: 10 }}>
            ← Add or change files
          </button>
        </div>
      )}

      {!mode && !schema?.active && (
        <div className="panel">
          <h2>Step 2 — where should this data land?</h2>
          <p className="change" style={{ marginTop: 0 }}>
            The list of fields the data should end up as. Nothing is moved until you
            approve it.
          </p>
          <div className="choices">
            <button className="choice" disabled={busy} onClick={() => {
              setMode("supply");
              setWorking({ entity: "employee", fields: [blankField(1)] });
              setDirty(true);
            }}>
              <b>I know what the fields should be</b>
              <span className="change">
                Build the list yourself, one field at a time.
              </span>
            </button>
            <button className="choice primary-choice" disabled={busy} onClick={() => {
              setMode("propose");
              void act(() => api.recommendSchema(runId),
                       "The agent read your columns and proposed this. Edit anything.");
            }}>
              <b>Suggest it from my files</b>
              <span className="change">
                Reads the columns and drafts a list for you. You change anything you
                like before approving.
              </span>
            </button>
          </div>
        </div>
      )}

      {busy && mode === "propose" && !schema?.active && (
        <div className="panel" role="status">
          <h2>Reading your columns…</h2>
          <p className="change">Working out what the fields should be.</p>
        </div>
      )}

      {(mode || schema?.active) && !(busy && !schema?.active) && (
        <div className="panel">
          <div className="editor-head">
            <h2 style={{ margin: 0 }}>
              {schema?.origin === "recommended"
                ? "The agent's proposal — edit anything"
                : "Target schema"}
            </h2>
            <span className="spacer" />
            {schema?.showing_version && (
              <span className={`pill ${schema.showing_state === "approved" ? "ok" : "warn"}`}>
                <i className="dot" />v{schema.showing_version} · {schema.showing_state}
              </span>
            )}
            {dirty && <span className="pill warn"><i className="dot" />unsaved changes</span>}
            <button type="button" className="icon" aria-pressed={raw}
                    onClick={() => {
                      if (!raw && working) setDraft(JSON.stringify(working, null, 2));
                      setRaw(!raw);
                    }}>
              {raw ? "Back to the editor" : "Advanced: raw YAML"}
            </button>
          </div>

          <p className="change">Nothing is moved until you approve this.</p>

          {raw ? (
            <textarea value={draft}
              onChange={(e) => { setDraft(e.target.value); setDirty(true); }}
              aria-label="Target schema as YAML or JSON" rows={16}
              style={{ width: "100%", font: "12.5px ui-monospace, Menlo, monospace",
                       background: "var(--panel-2)", color: "var(--ink)", padding: 10,
                       border: "1px solid var(--line)", borderRadius: 8 }} />
          ) : working ? (
            <SchemaEditor schema={working} lookups={lookupNames}
                          onChange={(next) => { setWorking(next); setDirty(true); }} />
          ) : (
            <p className="empty">Nothing to edit yet.</p>
          )}

          {error && <p style={{ color: "var(--bad)" }} role="alert">{error}</p>}
          {note && <p className="change" role="status">{note}</p>}

          <div className="actions" style={{ marginTop: 14 }}>
            <button disabled={busy}
                    onClick={() => void act(() => api.recommendSchema(runId),
                                            "The agent proposed a schema.")}>
              {schema?.active ? "Ask the agent again" : "Ask the agent to propose one"}
            </button>
            <button disabled={busy || !dirty}
                    onClick={() => void act(() => saveDraft(),
                                            "Saved. Approve it when you are happy.")}>
              Save my changes
            </button>
            <span className="spacer" />
            <button className="primary" disabled={!canApprove}
                    onClick={() => void act(async () => {
                      const version = dirty
                        ? (await saveDraft()).version
                        : schema!.draft_version!;
                      await api.approveSchema(runId, version);
                      onApproved();
                    })}>
              Approve and start the migration
            </button>
          </div>
        </div>
      )}
    </>
  );
}
