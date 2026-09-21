import { useState } from "react";
import type { AliasOffer } from "./api";

/**
 * Columns the agent thinks are other names for a field already in the schema.
 *
 * Nothing here is ticked to begin with, and that is deliberate. The agent is
 * confidently wrong about some of these — it is as certain that `strAuditUser`
 * means `designation` as it is that `dob` means `date_of_birth` — and no number
 * tells the two apart. The values do, which is why every row shows them.
 */
export default function AliasOffers({ offers, busy, onApply, onDismiss }: {
  offers: AliasOffer[];
  busy: boolean;
  onApply: (chosen: AliasOffer[]) => void;
  onDismiss: () => void;
}) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const id = (o: AliasOffer) => `${o.field}::${o.column}`;

  const toggle = (o: AliasOffer) => setPicked((was) => {
    const next = new Set(was);
    if (!next.delete(id(o))) next.add(id(o));
    return next;
  });

  if (!offers.length) {
    return (
      <div className="panel empty">
        <strong>Nothing to add</strong>
        Every column already matches a field by name.
        <div style={{ marginTop: 14 }}>
          <button onClick={onDismiss}>Close</button>
        </div>
      </div>
    );
  }

  const chosen = offers.filter((o) => picked.has(id(o)));

  return (
    <div className="panel">
      <h2>Suggested field names</h2>
      <p className="lead">
        Your files call some things by a different name than your schema does.
        The agent read the values and suggests these {offers.length} matches —
        <b> check the ones that are right</b>. Some will be wrong, so the values
        are shown beside each.
      </p>

      <ul className="offers">
        {offers.map((o) => {
          const on = picked.has(id(o));
          return (
            <li key={id(o)} className={on ? "on" : undefined}>
              <label>
                <input type="checkbox" checked={on} onChange={() => toggle(o)} />
                <span className="offer-body">
                  <span className="offer-head">
                    <b className="mono">{o.column}</b>
                    <span aria-hidden="true">→</span>
                    <b>{o.field}</b>
                  </span>
                  <span className="change">{o.seen_in}</span>
                  <span className="offer-values">
                    {o.samples.map((v, i) => <em key={i}>{v}</em>)}
                  </span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>

      <div className="actions" style={{ marginTop: 16 }}>
        <button className="primary" disabled={busy || !chosen.length}
                onClick={() => onApply(chosen)}>
          {chosen.length
            ? `Add ${chosen.length} other name${chosen.length === 1 ? "" : "s"}`
            : "Check the ones that are right"}
        </button>
        <button disabled={busy} onClick={onDismiss}>Skip this</button>
      </div>
    </div>
  );
}
