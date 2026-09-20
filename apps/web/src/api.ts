export type Case = {
  key: string; id: string; class: string; headline: string; detail: string; who: string;
  record: string | null; field: string | null; sources: string[]; values: string[];
  evidence: Record<string, unknown>; rule: string | null; attempts: string[];
  actions: string[];
  options: { label: string; value: string | null; description: string | null; recommended: boolean }[];
  blocks: number; children: number; state: string;
};

export type Progress = {
  stage: string; message: string; done: number; total: number;
  percent: number; finished: boolean; failed: boolean; error: string | null;
};

export type RunState = {
  run_id: string;
  status: string;
  progress?: Progress;
  counts: {
    rows_read: number; records: number; delivered: number; needs_review: number;
    excluded: number; failed: number; open_cases: number;
    ready: number; blocked: number;
  };
  delivery_paused?: boolean;
  cases: Case[];
  failures?: { record: string; reason: string }[];
  records: {
    key: string; state: string; values: Record<string, string>; sources: string[];
    issues: string[]; cases: string[]; waiting_on_another?: boolean;
    provenance: Record<string, { raw: string | null; value: string; from: string;
      changes: { rule: string; before: string | null; after: string | null; why: string }[] }>;
  }[];
  mappings: { file: string; column: string; decision: string; field: string | null; score: number; gap: number; evidence: string[] }[];
  activity: { actor: string; action: string; summary: string; reason: string | null; before: unknown; after: unknown }[];
};

/** Every failure carries something a person can act on, and its status. */
export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

const json = async (r: Response) => {
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = body?.detail;
    throw new ApiError(
      r.status,
      typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d: any) => d?.msg ?? String(d)).join("; ")
        : r.status === 404 ? "That migration is no longer available."
        : r.status >= 500 ? "Something went wrong at our end. Nothing was changed."
        : r.statusText || "The request was refused.",
    );
  }
  return r.json();
};

export type SchemaState = {
  active: { entity: string; fields: { name: string; type: string; required?: boolean;
    unique?: boolean; allowed?: string[]; pattern?: string; reference?: string;
    description?: string }[] } | null;
  showing_version: number | null;
  showing_state: string | null;
  origin: string | null;
  draft_version: number | null;
  approved_version: number | null;
  versions: { version: number; state: string; origin: string; approved_by: string | null;
    created_at: string }[];
};

export type RunFiles = {
  files: { name: string; kind: string; supported: boolean; note: string | null;
    rows: number; columns: string[] }[];
  total_rows: number;
  total_columns: number;
};

export type Sample = { name: string; files: number; description: string };

export const api = {
  runs: () => fetch("/api/runs").then(json),
  run: (id: string): Promise<RunState> => fetch(`/api/runs/${id}`).then(json),
  samples: (): Promise<Sample[]> => fetch("/api/samples").then(json),
  startFromSample: (name: string) =>
    fetch("/api/runs/from-sample", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(json),
  runFiles: (run: string): Promise<RunFiles> => fetch(`/api/runs/${run}/files`).then(json),
  schemaStarter: (run: string): Promise<{ body: string }> =>
    fetch(`/api/runs/${run}/schema/starter`).then(json),
  startFromFixtures: (folder = "run1") =>
    fetch("/api/runs/from-fixtures", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ folder }),
    }).then(json),
  upload: (files: FileList) => {
    const body = new FormData();
    Array.from(files).forEach((f) => body.append("files", f));
    return fetch("/api/runs", { method: "POST", body }).then(json);
  },
  decide: (run: string, key: string, action: string, value: string | null, reason?: string) =>
    fetch(`/api/runs/${run}/cases/${encodeURIComponent(key)}/decide`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ action, value, reason }),
    }).then(json),
  deliver: (run: string) => fetch(`/api/runs/${run}/deliver`, { method: "POST" }).then(json),
  rollback: (run: string) => fetch(`/api/runs/${run}/rollback`, { method: "POST" }).then(json),
  destination: (run: string) => fetch(`/api/runs/${run}/destination`).then(json),
  audit: (run: string) => fetch(`/api/runs/${run}/audit`).then(json),
  schema: (run: string): Promise<SchemaState> => fetch(`/api/runs/${run}/schema`).then(json),
  recommendSchema: (run: string) =>
    fetch(`/api/runs/${run}/schema/recommend`, { method: "POST" }).then(json),
  putSchema: (run: string, body: string) =>
    fetch(`/api/runs/${run}/schema`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ body, origin: "edited" }),
    }).then(json) as Promise<{ version: number; state: string; fields: number }>,
  suggestAliases: (run: string, version: number) =>
    fetch(`/api/runs/${run}/schema/${version}/aliases`, { method: "POST" })
      .then(json) as Promise<{ version: number; suggestions: AliasOffer[] }>,
  approveSchema: (run: string, version: number) =>
    fetch(`/api/runs/${run}/schema/${version}/approve`, { method: "POST" }).then(json),
};

/** Plain language, because the reader is an implementation consultant, not an engineer. */
export type AliasOffer = {
  field: string;
  column: string;
  seen_in: string;
  samples: string[];
  why: string[];
};

export const PHRASE: Record<string, string> = {
  awaiting_schema: "Waiting for a target schema",
  awaiting_review: "Needs your decision",
  ready_to_send: "Sending…",
  partially_delivered: "Partly sent",
  completed: "All sent",
  completed_with_exclusions: "Sent, some excluded",
  processing: "Working…",
  delivery_failed: "Couldn't send",
  ready: "Ready to send",
  blocked: "Needs your decision",
  delivered: "Sent successfully",
  excluded: "Excluded by you",
  AMBIGUOUS_MAPPING: "Unclear column",
  UNMAPPED_REQUIRED: "Missing column",
  AMBIGUOUS_VALUE: "Unclear value",
  UNCERTAIN_IDENTITY: "Same person?",
  CONFLICTING_FACTS: "Sources disagree",
  MISSING_REQUIRED: "Missing information",
  VALIDATION_UNRESOLVED: "Failed a check",
  UNRESOLVED_REFERENCE: "Unknown reference",
  LOW_CONFIDENCE_EXTRACTION: "Hard to read",
  CROSS_RUN_COLLISION: "Already migrated",
  DELIVERY_PERMANENT_FAILURE: "Destination refused",
};
