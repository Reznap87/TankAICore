// Single-shot, offline rehearsal. Persistent intent always precedes native effects.
export class PilotDenied extends Error {
  constructor(readonly status: number, readonly code: string) { super(code); }
}
export function requirePilot(condition: unknown, code = "pilot_blocked"): asserts condition {
  if (!condition) throw new PilotDenied(409, code);
}
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const HASH = /^[0-9a-f]{64}$/;
export function pinnedImage(value: unknown): string {
  requirePilot(typeof value === "string" && /^registry\.cloudflare\.com\/[a-zA-Z0-9_./-]{1,400}@sha256:[0-9a-f]{64}$/.test(value), "immutable_pilot_image_required");
  return value;
}
function object(value: unknown): Record<string, unknown> {
  requirePilot(value !== null && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function exact(value: Record<string, unknown>, names: string[]) {
  requirePilot(Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name)));
}
function integer(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) >= 0; }
export type Receipt = {
  format: "tankai-pilot-supervisor-v1"; phase: "running" | "quiesced" | "inspect";
  snapshot_ready: boolean; child_running: boolean; root_id: string; root_sha256: string | null;
};
export function receipt(value: unknown, phase: Receipt["phase"]): Receipt {
  requirePilot(["running", "quiesced", "inspect"].includes(phase));
  const v = object(value);
  exact(v, ["format", "phase", "snapshot_ready", "child_running", "root_id", "root_sha256"]);
  requirePilot(v.format === "tankai-pilot-supervisor-v1" && v.phase === phase
    && v.snapshot_ready === (phase === "quiesced") && v.child_running === (phase === "running")
    && typeof v.root_id === "string" && UUID.test(v.root_id)
    && (phase === "running" ? v.root_sha256 === null : typeof v.root_sha256 === "string" && HASH.test(v.root_sha256)));
  return { format: "tankai-pilot-supervisor-v1", phase, snapshot_ready: v.snapshot_ready,
    child_running: v.child_running, root_id: v.root_id, root_sha256: v.root_sha256 as string | null };
}
export type Capsule = {
  schema: 1; generation: string; image: string; root_id: string; root_sha256: string;
  snapshot_id: string; snapshot_size: number; created_at: number; expires_at: number;
};
export function capsule(value: unknown): Capsule {
  const v = object(value);
  exact(v, ["schema", "generation", "image", "root_id", "root_sha256", "snapshot_id", "snapshot_size", "created_at", "expires_at"]);
  requirePilot(v.schema === 1 && typeof v.generation === "string" && UUID.test(v.generation)
    && typeof v.root_id === "string" && UUID.test(v.root_id)
    && typeof v.root_sha256 === "string" && HASH.test(v.root_sha256)
    && typeof v.snapshot_id === "string" && v.snapshot_id.length > 0 && v.snapshot_id.length <= 1024
    && !/[\x00-\x1f\x7f]/.test(v.snapshot_id)
    && integer(v.snapshot_size) && v.snapshot_size > 0 && v.snapshot_size <= 64 * 1024 ** 3
    && integer(v.created_at) && integer(v.expires_at) && v.expires_at - v.created_at === 86400000);
  return { schema: 1, generation: v.generation, image: pinnedImage(v.image), root_id: v.root_id,
    root_sha256: v.root_sha256, snapshot_id: v.snapshot_id, snapshot_size: v.snapshot_size,
    created_at: v.created_at, expires_at: v.expires_at };
}
export async function capsulePin(value: Capsule): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(capsule(value)));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return [...digest].map(byte => byte.toString(16).padStart(2, "0")).join("");
}
type Action = "start" | "capture" | "restore";
type Operation = { id: string; action: Action; pin: string };
type Phase = "empty" | "starting" | "running" | "capturing" | "captured" | "restoring" | "staged" | "blocked";
export type State = {
  schema: 1; revision: number; phase: Phase; last: Operation | null; image: string | null;
  capsule: { payload: Capsule; pin: string } | null; receipt: Receipt | null;
};
export const initialState = (): State => ({ schema: 1, revision: 0, phase: "empty", last: null, image: null, capsule: null, receipt: null });
export function state(value: unknown): State {
  const v = object(value);
  exact(v, ["schema", "revision", "phase", "last", "image", "capsule", "receipt"]);
  requirePilot(v.schema === 1 && integer(v.revision) && v.revision < Number.MAX_SAFE_INTEGER - 2
    && typeof v.phase === "string" && ["empty", "starting", "running", "capturing", "captured", "restoring", "staged", "blocked"].includes(v.phase));
  let last: Operation | null = null;
  if (v.last !== null) {
    const op = object(v.last);
    exact(op, ["id", "action", "pin"]);
    requirePilot(typeof op.id === "string" && UUID.test(op.id) && ["start", "capture", "restore"].includes(String(op.action))
      && typeof op.pin === "string" && (op.pin === "" || HASH.test(op.pin)));
    last = { id: op.id, action: op.action as Action, pin: op.pin };
  }
  let saved: State["capsule"] = null;
  if (v.capsule !== null) {
    const c = object(v.capsule);
    exact(c, ["payload", "pin"]);
    requirePilot(typeof c.pin === "string" && HASH.test(c.pin));
    saved = { payload: capsule(c.payload), pin: c.pin };
  }
  let proof: Receipt | null = null;
  if (v.receipt !== null) proof = receipt(v.receipt, object(v.receipt).phase as Receipt["phase"]);
  requirePilot(v.phase !== "empty" || (v.revision === 0 && last === null && v.image === null && saved === null && proof === null));
  requirePilot(v.phase === "empty" || last !== null);
  requirePilot(!["captured", "restoring", "staged"].includes(v.phase) || saved !== null);
  return { schema: 1, revision: v.revision, phase: v.phase as Phase, last,
    image: v.image === null ? null : pinnedImage(v.image), capsule: saved, receipt: proof };
}
export interface Journal { load(): State; commit(expectedRevision: number, next: State): void; }
export class SqlJournal implements Journal {
  constructor(private readonly storage: DurableObjectStorage) {
    storage.sql.exec("CREATE TABLE IF NOT EXISTS pilot_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)");
  }
  load(): State {
    const rows = this.storage.sql.exec<{ payload: string }>("SELECT payload FROM pilot_state WHERE id=1").toArray();
    return rows[0] ? state(JSON.parse(rows[0].payload)) : initialState();
  }
  commit(expectedRevision: number, next: State): void {
    this.storage.transactionSync(() => {
      requirePilot(this.load().revision === expectedRevision && next.revision === expectedRevision + 1, "stale_pilot_revision");
      this.storage.sql.exec("INSERT INTO pilot_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", JSON.stringify(state(next)));
    });
  }
}
export interface PilotRuntime {
  image(): string;
  startFresh(): Promise<unknown>;
  quiesce(): Promise<unknown>;
  sourceImage(): Promise<string>;
  snapshot(name: string): Promise<{ id: string; size: number }>;
  sourceStatus(): Promise<unknown>;
  stage(saved: Capsule, pin: string, operation: string): Promise<unknown>;
}
export function publicState(s: State) {
  return { phase: s.phase, revision: s.revision, operation_id: s.last?.id ?? null,
    automatic_resume: false, recovery_required: ["starting", "capturing", "restoring", "blocked"].includes(s.phase),
    capsule: s.capsule ? { pin: s.capsule.pin, generation: s.capsule.payload.generation,
      image: s.capsule.payload.image, root_id: s.capsule.payload.root_id, root_sha256: s.capsule.payload.root_sha256,
      created_at: s.capsule.payload.created_at, expires_at: s.capsule.payload.expires_at } : null };
}
function sameRoot(a: Receipt, b: Receipt) { requirePilot(a.root_id === b.root_id && a.root_sha256 === b.root_sha256, "pilot_root_mismatch"); }

export class PilotCoordinator {
  constructor(private readonly journal: Journal, private readonly runtime: PilotRuntime, private readonly clock = Date.now) {}
  status() { return this.journal.load(); }
  async operate(action: Action, id: string, expected: number, pin = ""): Promise<State> {
    requirePilot(UUID.test(id) && integer(expected) && (action === "restore" ? HASH.test(pin) : pin === ""), "invalid_pilot_operation");
    const previous = this.journal.load();
    if (previous.last?.id === id) {
      requirePilot(previous.last.action === action && previous.last.pin === pin, "idempotency_conflict");
      return previous; // Never repeat an uncertain effect, including after eviction.
    }
    requirePilot(previous.revision === expected, "stale_pilot_revision");
    requirePilot(previous.phase === ({ start: "empty", capture: "running", restore: "captured" }[action]), "pilot_phase_conflict");
    const image = pinnedImage(this.runtime.image());
    if (previous.image !== null) requirePilot(previous.image === image, "pilot_image_changed");
    if (action === "restore") {
      const saved = previous.capsule;
      requirePilot(saved !== null && saved.pin === pin && await capsulePin(saved.payload) === pin, "pilot_capsule_pin_mismatch");
      requirePilot(saved.payload.image === image && this.clock() >= saved.payload.created_at && this.clock() < saved.payload.expires_at, "pilot_capsule_expired_or_foreign");
    }
    // No awaits between the final compare-and-set and native operation dispatch.
    const pending: State = { ...previous, revision: previous.revision + 1,
      phase: ({ start: "starting", capture: "capturing", restore: "restoring" } as const)[action],
      last: { id, action, pin }, image };
    this.journal.commit(previous.revision, pending);
    try {
      let next: State;
      if (action === "start") {
        const proof = receipt(await this.runtime.startFresh(), "running");
        requirePilot(await this.runtime.sourceImage() === image, "pilot_image_mismatch");
        next = { ...pending, phase: "running", receipt: proof };
      } else if (action === "capture") {
        const proof = receipt(await this.runtime.quiesce(), "quiesced");
        requirePilot(proof.root_id === previous.receipt?.root_id && await this.runtime.sourceImage() === image, "pilot_source_mismatch");
        const handle = await this.runtime.snapshot(id);
        sameRoot(proof, receipt(await this.runtime.sourceStatus(), "quiesced"));
        requirePilot(await this.runtime.sourceImage() === image, "pilot_image_mismatch");
        const created = this.clock();
        const payload = capsule({ schema: 1, generation: id, image, root_id: proof.root_id,
          root_sha256: proof.root_sha256, snapshot_id: handle.id, snapshot_size: handle.size,
          created_at: created, expires_at: created + 86400000 });
        next = { ...pending, phase: "captured", receipt: proof, capsule: { payload, pin: await capsulePin(payload) } };
      } else {
        const saved = pending.capsule;
        requirePilot(saved !== null);
        const proof = receipt(await this.runtime.stage(saved.payload, saved.pin, id), "inspect");
        sameRoot(receipt(previous.receipt, "quiesced"), proof);
        next = { ...pending, phase: "staged", receipt: proof };
      }
      next.revision += 1;
      this.journal.commit(pending.revision, next);
      return next;
    } catch {
      // If even the write fails, persisted pending intent still prevents retry.
      this.journal.commit(pending.revision, { ...pending, phase: "blocked", revision: pending.revision + 1 });
      throw new PilotDenied(502, "pilot_operation_uncertain");
    }
  }
}

export interface InspectionRuntime { image(): string; startInspection(saved: Capsule, pin: string): Promise<unknown>; }
export class PilotInspector {
  constructor(private readonly journal: Journal, private readonly runtime: InspectionRuntime, private readonly clock = Date.now) {}
  async stage(input: Capsule, pin: string, operation: string): Promise<Receipt> {
    const saved = capsule(input);
    requirePilot(HASH.test(pin) && await capsulePin(saved) === pin && UUID.test(operation), "pilot_capsule_pin_mismatch");
    requirePilot(pinnedImage(this.runtime.image()) === saved.image && this.clock() >= saved.created_at && this.clock() < saved.expires_at, "pilot_capsule_expired_or_foreign");
    const previous = this.journal.load();
    requirePilot(previous.phase === "empty", "inspection_target_already_used");
    const pending: State = { ...previous, revision: 1, phase: "restoring", image: saved.image,
      last: { id: operation, action: "restore", pin }, capsule: { payload: saved, pin } };
    this.journal.commit(0, pending);
    try {
      const proof = receipt(await this.runtime.startInspection(saved, pin), "inspect");
      requirePilot(proof.root_id === saved.root_id && proof.root_sha256 === saved.root_sha256, "pilot_root_mismatch");
      this.journal.commit(1, { ...pending, revision: 2, phase: "staged", receipt: proof });
      return proof;
    } catch {
      this.journal.commit(1, { ...pending, revision: 2, phase: "blocked" });
      throw new PilotDenied(502, "pilot_inspection_uncertain");
    }
  }
}
