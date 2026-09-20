import { useState } from "react";

/**
 * The target schema as a table you type into.
 *
 * The previous version repeated "Field name" and "Kind of value" above every row,
 * which is a form pretending to be a list. A table says those things once in the
 * header and gives the rest of the space back to the data. "More" opens the settings
 * that most fields never need.
 */

export type Field = {
  name: string;
  type: string;
  required?: boolean;
  unique?: boolean;
  allowed?: string[] | null;
  pattern?: string | null;
  reference?: string | null;
  max_length?: number | null;
  format?: string | null;
  case_sensitive?: boolean;
  description?: string | null;
  aliases?: string[] | null;
};

export type Schema = { entity: string; description?: string | null; fields: Field[] };

const TYPES = [
  { value: "string", label: "Text" },
  { value: "email", label: "Email address" },
  { value: "date", label: "Date" },
  { value: "datetime", label: "Date and time" },
  { value: "integer", label: "Whole number" },
  { value: "number", label: "Decimal number" },
  { value: "boolean", label: "Yes / no" },
  { value: "enum", label: "One of a fixed list" },
];

export function blankField(n: number): Field {
  return { name: `field_${n}`, type: "string", required: false, unique: false };
}

export default function SchemaEditor({
  schema, onChange, lookups,
}: { schema: Schema; onChange: (s: Schema) => void; lookups: string[] }) {
  const [open, setOpen] = useState<number | null>(null);

  const patch = (i: number, change: Partial<Field>) =>
    onChange({ ...schema, fields: schema.fields.map((f, n) => (n === i ? { ...f, ...change } : f)) });
  const remove = (i: number) => {
    onChange({ ...schema, fields: schema.fields.filter((_, n) => n !== i) });
    setOpen(null);
  };
  const move = (i: number, by: number) => {
    const to = i + by;
    if (to < 0 || to >= schema.fields.length) return;
    const fields = [...schema.fields];
    [fields[i], fields[to]] = [fields[to], fields[i]];
    onChange({ ...schema, fields });
    setOpen(null);
  };

  return (
    <>
      <div className="tickline" style={{ marginBottom: 14, gap: 12 }}>
        <label className="change" htmlFor="entity-name">Each row is one</label>
        <input id="entity-name" type="text" value={schema.entity} style={{ maxWidth: 220 }}
               onChange={(e) => onChange({ ...schema, entity: e.target.value })}
               placeholder="employee" />
        <span className="change">· {schema.fields.length} fields</span>
      </div>

      <div className="scroll">
        <table className="schema-table">
          <thead>
            <tr>
              <th style={{ minWidth: 190 }}>Field</th>
              <th style={{ minWidth: 150 }}>Holds</th>
              <th style={{ width: 90, textAlign: "center" }}>Required</th>
              <th style={{ width: 90, textAlign: "center" }}>Unique</th>
              <th style={{ width: 170 }} />
            </tr>
          </thead>
          <tbody>
            {schema.fields.map((f, i) => [
              <tr key={`r${i}`}>
                <td className="cell-input">
                  <input type="text" value={f.name} className="mono"
                         aria-label={`Name of field ${i + 1}`}
                         onChange={(e) => patch(i, { name: e.target.value })} />
                </td>
                <td className="cell-input">
                  <select value={f.type} aria-label={`What ${f.name} holds`}
                          onChange={(e) => patch(i, {
                            type: e.target.value,
                            allowed: e.target.value === "enum" ? (f.allowed ?? []) : null,
                          })}>
                    {TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                </td>
                <td className="tick">
                  <input type="checkbox" checked={!!f.required}
                         aria-label={`${f.name} must be filled in`}
                         onChange={(e) => patch(i, { required: e.target.checked })} />
                </td>
                <td className="tick">
                  <input type="checkbox" checked={!!f.unique}
                         aria-label={`${f.name} must be unique`}
                         onChange={(e) => patch(i, { unique: e.target.checked })} />
                </td>
                <td>
                  <div className="row-actions">
                    <button type="button" className="ghost" aria-label={`Move ${f.name} up`}
                            onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
                    <button type="button" className="ghost" aria-label={`Move ${f.name} down`}
                            onClick={() => move(i, 1)}
                            disabled={i === schema.fields.length - 1}>↓</button>
                    <button type="button" className="ghost" aria-expanded={open === i}
                            onClick={() => setOpen(open === i ? null : i)}>
                      {open === i ? "Less" : "More"}
                    </button>
                    <button type="button" className="ghost" aria-label={`Remove ${f.name}`}
                            style={{ color: "var(--bad)" }} onClick={() => remove(i)}>✕</button>
                  </div>
                </td>
              </tr>,
              open === i ? (
                <tr className="detail-row" key={`d${i}`}>
                  <td colSpan={5}>
                    <p className="change" style={{ margin: "0 0 12px" }}>
                      Optional. The more you fill in, the fewer questions you get asked later.
                    </p>
                    <div className="detail-grid">
                      {f.type === "enum" && (
                        <label style={{ gridColumn: "1 / -1" }}>
                          <span className="lbl">The only values allowed</span>
                          <input type="text" value={(f.allowed ?? []).join(", ")}
                                 placeholder="ACTIVE, ON_LEAVE, EXITED"
                                 aria-label={`Allowed values for ${f.name}`}
                                 onChange={(e) => patch(i, {
                                   allowed: e.target.value.split(",").map((v) => v.trim()).filter(Boolean),
                                 })} />
                        </label>
                      )}

                      <label>
                        <span className="lbl">Other names for it</span>
                        <input type="text" value={(f.aliases ?? []).join(", ")}
                               placeholder="emp_id, staff_code"
                               aria-label={`Other names for ${f.name}`}
                               onChange={(e) => patch(i, {
                                 aliases: e.target.value.split(",").map((v) => v.trim()).filter(Boolean),
                               })} />
                        <span className="hint">What the client's files call it.</span>
                      </label>

                      {!!lookups.length && (
                        <label>
                          <span className="lbl">Must appear in</span>
                          <select value={f.reference ?? ""}
                                  aria-label={`List ${f.name} must appear in`}
                                  onChange={(e) => patch(i, { reference: e.target.value || null })}>
                            <option value="">— no list —</option>
                            {lookups.map((l) => (
                              <option key={l} value={`${l}.code`}>the {l} list</option>
                            ))}
                          </select>
                          <span className="hint">One of your uploaded lookup files.</span>
                        </label>
                      )}

                      <label>
                        <span className="lbl">Must look like</span>
                        <input type="text" value={f.pattern ?? ""} className="mono"
                               placeholder="^EMP-[0-9]{5}$"
                               aria-label={`Pattern for ${f.name}`}
                               onChange={(e) => patch(i, { pattern: e.target.value || null })} />
                        <span className="hint">A shape, e.g. EMP- then five digits.</span>
                      </label>

                      <label>
                        <span className="lbl">Longest allowed</span>
                        <input type="number" min={1} value={f.max_length ?? ""}
                               aria-label={`Maximum length of ${f.name}`}
                               onChange={(e) => patch(i, {
                                 max_length: e.target.value ? Number(e.target.value) : null,
                               })} />
                      </label>

                      <label className="tickline" style={{ alignSelf: "end", paddingBottom: 8 }}>
                        <input type="checkbox" checked={!!f.case_sensitive}
                               onChange={(e) => patch(i, { case_sensitive: e.target.checked })} />
                        <span>Upper and lower case matter</span>
                      </label>
                    </div>
                  </td>
                </tr>
              ) : null,
            ])}
          </tbody>
        </table>
      </div>

      <button type="button" className="add-field"
              onClick={() => onChange({
                ...schema, fields: [...schema.fields, blankField(schema.fields.length + 1)],
              })}>
        + Add a field
      </button>
    </>
  );
}
