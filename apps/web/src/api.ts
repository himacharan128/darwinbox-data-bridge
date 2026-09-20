export type Case = {
  key: string; id: string; class: string; headline: string; detail: string;
  record: string | null; field: string | null; sources: string[]; values: string[];
  evidence: Record<string, unknown>; rule: string | null; attempts: string[];
  actions: string[];
  options: { label: string; value: string | null; description: string | null; recommended: boolean }[];
  blocks: number; state: string;
};

export type Progress = {
  stage: string; message: string; done: number; total: number;
  percent: number; finished: boolean; failed: boolean; error: string | null;
};

export type RunState = {
  run_id: string;
  status: string;
  progress?: Progress;
  counts: { records: number; ready: number; delivered: number; blocked: number; excluded: number; open_cases: number };
  cases: Case[];
  records: {
    key: string; state: string; values: Record<string, string>; sources: string[];
    issues: string[]; cases: string[];
    provenance: Record<string, { raw: string | null; value: string; from: string;
      changes: { rule: string; before: string | null; after: string | null; why: string }[] }>;
  }[];
  mappings: { file: string; column: string; decision: string; field: string | null; score: number; gap: number; evidence: string[] }[];
  activity: { actor: string; action: string; summary: string; reason: string | null; before: unknown; after: unknown }[];
};

const json = async (r: Response) => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
  return r.json();
};

export const api = {
  runs: () => fetch("/api/runs").then(json),
  run: (id: string): Promise<RunState> => fetch(`/api/runs/${id}`).then(json),
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
};

/** Plain language, because the reader is an implementation consultant, not an engineer. */
export const PHRASE: Record<string, string> = {
  awaiting_review: "Needs your decision",
  partially_delivered: "Partly sent",
  completed: "All sent",
  completed_with_exclusions: "Sent, some excluded",
  processing: "Working…",
  delivery_failed: "Couldn't send",
  ready: "Ready to send",
  blocked: "Needs your decision",
  delivered: "Sent successfully",
  excluded: "Excluded by you",
  AMBIGUOUS_MAPPING: "Which field is this?",
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
