import { useState } from "react";

/**
 * Editing a target schema without writing any.
 *
 * The people who run migrations are implementation consultants, not engineers. Asking
 * them to hand-edit YAML puts a syntax error between them and their job, and a stray
 * indent reads as the tool being broken. So: a field list with plain-language controls,
 * and the raw document behind an "Advanced" toggle for anyone who wants it.
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

/** Plain words for things that are otherwise jargon. */
const TYPES: { value: string; label: string; hint: string }[] = [
  { value: "string", label: "Text", hint: "names, codes, free text" },
  { value: "email", label: "Email address", hint: "checked for a valid address" },
  { value: "date", label: "Date", hint: "normalised to one format" },
  { value: "datetime", label: "Date and time", hint: "" },
  { value: "integer", label: "Whole number", hint: "" },
  { value: "number", label: "Decimal number", hint: "" },
  { value: "boolean", label: "Yes / no", hint: "" },
  { value: "enum", label: "One of a fixed list", hint: "you set the allowed values" },
];

const labelFor = (t: string) => TYPES.find((x) => x.value === t)?.label ?? t;

export function blankField(n: number): Field {
  return { name: `field_${n}`, type: "string", required: false, unique: false };
}

export default function SchemaEditor({
  schema, onChange, lookups,
}: {
  schema: Schema;
  onChange: (s: Schema) => void;
  lookups: string[];
}) {
  const [open, setOpen] = useState<string | null>(null);

  const patch = (i: number, change: Partial<Field>) => {
    const fields = schema.fields.map((f, n) => (n === i ? { ...f, ...change } : f));
    onChange({ ...schema, fields });
  };
  const remove = (i: number) =>
    onChange({ ...schema, fields: schema.fields.filter((_, n) => n !== i) });
  const move = (i: number, by: number) => {
    const to = i + by;
    if (to < 0 || to >= schema.fields.length) return;
    const fields = [...schema.fields];
    [fields[i], fields[to]] = [fields[to], fields[i]];
    onChange({ ...schema, fields });
  };

  return (
    <div className="editor">
      <div className="field-row header-row">
        <label>
          <span className="lbl">Each row is one…</span>
          <input type="text" value={schema.entity}
                 onChange={(e) => onChange({ ...schema, entity: e.target.value })}
                 aria-label="Entity name" placeholder="employee" />
        </label>
        <span className="change">
          {schema.fields.length} field{schema.fields.length === 1 ? "" : "s"}
        </span>
      </div>

      {schema.fields.map((f, i) => {
        const id = `${i}-${f.name}`;
        const expanded = open === id;
        return (
          <div className={`field-row${expanded ? " expanded" : ""}`} key={id}>
            <div className="field-main">
              <label className="grow">
                <span className="lbl">Field name</span>
                <input type="text" value={f.name} className="mono"
                       aria-label={`Name of field ${i + 1}`}
                       onChange={(e) => patch(i, { name: e.target.value })} />
              </label>

              <label>
                <span className="lbl">Kind of value</span>
                <select value={f.type} aria-label={`Type of ${f.name}`}
                        onChange={(e) => patch(i, {
                          type: e.target.value,
                          allowed: e.target.value === "enum" ? (f.allowed ?? []) : null,
                        })}>
                  {TYPES.map((t) => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                  ))}
                </select>
              </label>

              <label className="tick">
                <input type="checkbox" checked={!!f.required}
                       onChange={(e) => patch(i, { required: e.target.checked })} />
                <span>Must be filled in</span>
              </label>

              <label className="tick">
                <input type="checkbox" checked={!!f.unique}
                       onChange={(e) => patch(i, { unique: e.target.checked })} />
                <span>Must be unique</span>
              </label>

              <div className="row-actions">
                <button type="button" className="icon" aria-label={`Move ${f.name} up`}
                        onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
                <button type="button" className="icon" aria-label={`Move ${f.name} down`}
                        onClick={() => move(i, 1)}
                        disabled={i === schema.fields.length - 1}>↓</button>
                <button type="button" className="icon"
                        aria-expanded={expanded}
                        aria-label={`More options for ${f.name}`}
                        onClick={() => setOpen(expanded ? null : id)}>
                  {expanded ? "Less" : "More"}
                </button>
                <button type="button" className="icon danger"
                        aria-label={`Remove ${f.name}`}
                        onClick={() => remove(i)}>Remove</button>
              </div>
            </div>

            {f.type === "enum" && (
              <label className="full">
                <span className="lbl">
                  Allowed values — the only things this field may contain
                </span>
                <input type="text" value={(f.allowed ?? []).join(", ")}
                       placeholder="ACTIVE, ON_LEAVE, EXITED"
                       aria-label={`Allowed values for ${f.name}`}
                       onChange={(e) => patch(i, {
                         allowed: e.target.value.split(",").map((v) => v.trim()).filter(Boolean),
                       })} />
              </label>
            )}

            {expanded && (
              <div className="more">
                <p className="change">
                  Optional. The more you fill in here, the fewer questions you get asked
                  later.
                </p>

                <label className="full">
                  <span className="lbl">Must look like (optional)</span>
                  <input type="text" value={f.pattern ?? ""} className="mono"
                         placeholder="^EMP-[0-9]{5}$"
                         aria-label={`Pattern for ${f.name}`}
                         onChange={(e) => patch(i, { pattern: e.target.value || null })} />
                  <span className="change">
                    A pattern, like <span className="mono">^EMP-[0-9]{5}$</span> for
                    codes such as EMP-00042. Anything that does not fit is queried, never
                    quietly changed.
                  </span>
                </label>

                {!!lookups.length && (
                  <label className="full">
                    <span className="lbl">Must exist in (optional)</span>
                    <select value={f.reference ?? ""}
                            aria-label={`Reference for ${f.name}`}
                            onChange={(e) => patch(i, { reference: e.target.value || null })}>
                      <option value="">— nothing —</option>
                      {lookups.map((l) => (
                        <option key={l} value={`${l}.code`}>{l} table</option>
                      ))}
                    </select>
                    <span className="change">
                      Check each value appears in one of your lookup files.
                    </span>
                  </label>
                )}

                <div className="pair">
                  <label>
                    <span className="lbl">Longest allowed (optional)</span>
                    <input type="number" min={1} value={f.max_length ?? ""}
                           aria-label={`Maximum length of ${f.name}`}
                           onChange={(e) => patch(i, {
                             max_length: e.target.value ? Number(e.target.value) : null,
                           })} />
                  </label>
                  <label className="tick">
                    <input type="checkbox" checked={!!f.case_sensitive}
                           onChange={(e) => patch(i, { case_sensitive: e.target.checked })} />
                    <span>Upper/lower case matters</span>
                  </label>
                </div>

                <label className="full">
                  <span className="lbl">Also known as (optional)</span>
                  <input type="text" value={(f.aliases ?? []).join(", ")}
                         placeholder="emp_id, staff_code, worker_number"
                         aria-label={`Aliases for ${f.name}`}
                         onChange={(e) => patch(i, {
                           aliases: e.target.value.split(",").map((v) => v.trim()).filter(Boolean),
                         })} />
                  <span className="change">
                    Other names the client's files use for this. Adding them here saves
                    you being asked about it.
                  </span>
                </label>
              </div>
            )}

            {!expanded && (
              <p className="summary change">
                {labelFor(f.type)}
                {f.required ? " · required" : ""}
                {f.unique ? " · unique" : ""}
                {f.pattern ? " · must match a pattern" : ""}
                {f.reference ? ` · must exist in ${f.reference.split(".")[0]}` : ""}
                {f.allowed?.length ? ` · one of ${f.allowed.length} values` : ""}
              </p>
            )}
          </div>
        );
      })}

      <button type="button" className="add-field"
              onClick={() => onChange({
                ...schema, fields: [...schema.fields, blankField(schema.fields.length + 1)],
              })}>
        + Add a field
      </button>
    </div>
  );
}
