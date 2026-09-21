import { useEffect, useState } from "react";
import { api, ApiError, PHRASE, type Case, type RunState } from "./api";

const fieldLabel = (n: string) =>
  n.replace(/_/g, " ").replace(/\bid\b/i, "ID").replace(/^./, (c) => c.toUpperCase());

/**
 * One question at a time, answered in one glance.
 *
 * The order is the order a person needs it: who this is about, what is wrong with
 * their data in a sentence, where it came from so they can check, then the choices.
 * The previous version led with the field name and the internal rule, which told the
 * reader what the machine cares about rather than what they have to decide.
 */
export default function Review({ state, onDone }: { state: RunState; onDone: () => void }) {
  const [i, setI] = useState(0);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  // An answer the data argues against, held for a second look before it is sent.
  const [cautioned, setCautioned] = useState<Case["options"][number] | null>(null);

  const cases = state.cases;
  const c: Case | undefined = cases[Math.min(i, Math.max(0, cases.length - 1))];

  useEffect(() => {
    setDraft(""); setErr(null); setConfirming(false); setCautioned(null);
  }, [c?.key]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(el.tagName)) return;
      if (e.key === "ArrowLeft") setI((n) => Math.max(0, n - 1));
      if (e.key === "ArrowRight") setI((n) => Math.min(cases.length - 1, n + 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cases.length]);

  if (!cases.length) {
    return (
      <div className="panel empty">
        <strong>Nothing needs you</strong>
        Everything the agent could decide on its own, it decided.
      </div>
    );
  }
  if (!c) return null;

  const send = async (action: string, value: string | null) => {
    setBusy(true); setErr(null);
    try {
      await api.decide(state.run_id, c.key, action, value);
      setI((n) => Math.max(0, Math.min(n, cases.length - 2)));
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "That didn't go through. Nothing was changed.");
    } finally { setBusy(false); setConfirming(false); setCautioned(null); }
  };

  const ev = (c.evidence ?? {}) as Record<string, any>;
  const reasons: string[] = ev.candidates ?? ev.closest ?? [];
  const value = c.values[0];
  const where = c.sources[0];

  return (
    <div className="panel case">
      <div className="nav">
        <button className="ghost" onClick={() => setI((n) => Math.max(0, n - 1))}
                disabled={i === 0}>← Previous</button>
        <button className="ghost" onClick={() => setI((n) => Math.min(cases.length - 1, n + 1))}
                disabled={i >= cases.length - 1}>Next →</button>
        <span className="count">{i + 1} of {cases.length}</span>
        <span className="spacer" />
        <span className="pill warn"><i className="dot" />{PHRASE[c.class] ?? c.class}</span>
      </div>

      {/* Who */}
      {c.record ? (
        <div className="subject">
          <span className="avatar" aria-hidden="true">
            {(c.who || c.record).slice(0, 1).toUpperCase()}
          </span>
          <div>
            <div className="subject-name">{c.who || c.record}</div>
            <div className="subject-id mono">{c.record}</div>
          </div>
        </div>
      ) : (
        <div className="subject">
          <span className="avatar file" aria-hidden="true">⬚</span>
          <div>
            <div className="subject-name">A whole column, not one employee</div>
            <div className="subject-id">{where ?? "across the uploaded files"}</div>
          </div>
        </div>
      )}

      {/* What is wrong */}
      <h3>{c.headline}</h3>
      <p className="detail">{c.detail}</p>

      {/* The offending value, large enough to read */}
      {value !== undefined && (
        <div className="callout">
          <span className="callout-label">Source value</span>
          <span className="callout-value mono">“{value}”</span>
          {where && <span className="callout-where">from {where}</span>}
        </div>
      )}

      {ev.crop && (
        <div className="callout">
          <span className="callout-label">Scanned image</span>
          <img alt={`Scan of ${c.field ? fieldLabel(c.field) : "this value"}`}
               src={`/api/runs/${state.run_id}/crop?` + new URLSearchParams(
                 Object.entries(ev.crop as Record<string, string>)
                   .map(([k, v]) => [k, String(v)])).toString()}
               style={{ maxWidth: "100%", borderRadius: 6, background: "#fff",
                        border: "1px solid var(--line)", padding: 4, marginTop: 6 }} />
          {typeof ev.read_confidence === "number" && (
            <span className="callout-where">
              The reader was only {Math.round(ev.read_confidence * 100)}% sure it read this right
            </span>
          )}
        </div>
      )}

      {ev.agree && (
        <div className="compare">
          <div><span className="callout-label">Matching fields</span>
            <p>{(ev.agree as string[]).map(fieldLabel).join(", ") || "nothing"}</p></div>
          <div><span className="callout-label">Conflicting fields</span>
            <p>{(ev.conflict as string[]).map(fieldLabel).join(", ") || "nothing"}</p></div>
        </div>
      )}

      {ev.cross_check && <CrossCheck x={ev.cross_check} />}

      {(c.found || !!(c.checked ?? []).length) && (
        <div className="looked">
          <span className="callout-label">Agent investigation</span>
          <ul>
            {(c.checked ?? []).map((k, i) => (
              <li key={i}>
                <b>{k.looked_at}</b>
                <span>{k.found}</span>
              </li>
            ))}
          </ul>
          {c.found && <p className="looked-said">{c.found}</p>}
        </div>
      )}

      {!!reasons.length && (
        <details className="why">
          <summary>Why the agent is unsure</summary>
          <ul className="reasons">{reasons.slice(0, 6).map((r) => <li key={r}>{r}</li>)}</ul>
        </details>
      )}

      {c.blocks > 1 && (
        <p className="impact">
          <b>{c.blocks} employees</b> are waiting on this answer
          {c.children > 0 && <>, {c.children} of them only because they report to one of them</>}.
        </p>
      )}

      {/* What to do */}
      <div className="resolve">
        <span className="resolve-label">
          {c.options.length ? "Choose the right one" : "Type the right value"}
        </span>

        {!!c.options.length && (
          <div className="options">
            {c.options.map((o) => (
              <button key={o.label} disabled={busy}
                      className={o.recommended ? "primary" : ""}
                      aria-pressed={cautioned?.label === o.label || undefined}
                      onClick={() => (o.caution ? setCautioned(o)
                                                : void send("correct", o.value))}>
                {o.label}
              </button>
            ))}
          </div>
        )}

        {cautioned && (
          <div className="notice caution" role="alert">
            <b>The data disagrees with this.</b> {cautioned.caution}
            <div className="caution-actions">
              <button className="danger" disabled={busy}
                      onClick={() => void send("correct", cautioned.value)}>
                Choose it anyway
              </button>
              <button className="ghost" disabled={busy} onClick={() => setCautioned(null)}>
                Go back
              </button>
            </div>
          </div>
        )}

        <div className="typeit">
          <input type="text" value={draft}
                 placeholder={c.options.length ? "…or something else" : "Type the correct value"}
                 aria-label="Correct value"
                 onChange={(e) => setDraft(e.target.value)}
                 onKeyDown={(e) => {
                   if (e.key === "Enter" && draft.trim()) void send("correct", draft.trim());
                 }} />
          <button className="primary" disabled={busy || !draft.trim()}
                  onClick={() => void send("correct", draft.trim())}>Save</button>
        </div>

        {c.actions.includes("approve") && (
          <button disabled={busy} onClick={() => void send("approve", null)}
                  style={{ marginTop: 10 }}>
            Yes, that's right — approve it
          </button>
        )}
      </div>

      {err && (
        <div className="notice bad" role="alert">
          <b>Couldn't save that.</b> {err}
        </div>
      )}

      <div className="actions">
        {confirming ? (
          <>
            <span className="change">
              {c.blocks > 1
                ? `Leave all ${c.blocks} out of this migration?`
                : "Leave this employee out of this migration?"} They stay in the file.
            </span>
            <span className="spacer" />
            <button className="ghost" onClick={() => setConfirming(false)}>Cancel</button>
            <button className="danger" disabled={busy}
                    onClick={() => void send("reject", null)}>Yes, leave out</button>
          </>
        ) : (
          <>
            <button className="ghost" onClick={() => setConfirming(true)} disabled={busy}>
              Can't fix this — leave {c.blocks > 1 ? `these ${c.blocks}` : "it"} out
            </button>
            <span className="spacer" />
            <span className="change">Moving between questions doesn't answer them.</span>
          </>
        )}
      </div>
    </div>
  );
}

type CrossCheckEvidence = {
  against: string[]; checked: number; agree: Record<string, number>;
  examples: { key: string; who?: string; here: string; there: string; file: string;
    matches: string[] }[];
};

/**
 * What the other files say about the same people. Measured, not the model's view,
 * so it sits apart from the agent's investigation: a count someone can check
 * against the rows listed under it.
 */
function CrossCheck({ x }: { x: CrossCheckEvidence }) {
  const tallies = Object.entries(x.agree).filter(([, n]) => n > 0);
  return (
    <div className="looked crosscheck">
      <span className="callout-label">Checked against {x.against.join(" and ")}</span>
      <p className="crosscheck-head">
        {tallies.length
          ? tallies.map(([lead, n]) => `${n} of ${x.checked} match ${lead.toLowerCase()}`)
              .join(" · ")
          : `None of the ${x.checked} match either reading`}
      </p>
      <ul>
        {x.examples.map((e) => (
          <li key={e.key}>
            <b>{e.who || e.key}</b>
            <span>
              <span className="mono">{e.here}</span> here,{" "}
              <span className="mono">{e.there}</span> in {e.file}
              {" — "}{e.matches.length ? e.matches.join(", ").toLowerCase() : "neither reading"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
