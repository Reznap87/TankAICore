// TEST ONLY: native effects are fakes. This entrypoint is never a deployment target.
import { DurableObject } from "cloudflare:workers";
import { PilotCoordinator, PilotInspector, SqlJournal, publicState, type Capsule, type Journal, type State } from "../../src/pilot-controller";
import { pilotDispatch, pilotGuard, pilotProxy, response } from "../../src/pilot-api";
import { NativePilot } from "../../src/pilot-native";

const IMAGE = "registry.cloudflare.com/synthetic/pilot@sha256:" + "a".repeat(64);
const ROOT = "00000000-0000-4000-8000-000000000001";
const DIGEST = "b".repeat(64);
type TestEnv = { TEST_CASE: string; TOKEN: string; TEST_PILOT: DurableObjectNamespace<TestPilot> };
const proof = (phase: string) => ({ format: "tankai-pilot-supervisor-v1", phase, snapshot_ready: phase === "quiesced",
  child_running: phase === "running", root_id: ROOT, root_sha256: phase === "running" ? null : DIGEST });

export class TestPilot extends DurableObject<TestEnv> {
  private journal: Journal;
  private adapter: NativePilot | null = null;
  private confirmedRevision = 0;
  private testNative() { return this.adapter ??= this.nativeFake(); }
  constructor(ctx: DurableObjectState, env: TestEnv) {
    super(ctx, env);
    const backing = new SqlJournal(ctx.storage);
    this.journal = { load: () => backing.load(), commit: (revision: number, next: State) => {
      if (env.TEST_CASE === "finish-write-failure" && ["captured", "blocked"].includes(next.phase)) throw new Error("synthetic storage outage");
      const source = ctx.id.equals(env.TEST_PILOT.idFromName("source"));
      if (((env.TEST_CASE === "restore-finish-write-failure" && source)
          || (env.TEST_CASE === "inspection-write-failure" && !source))
          && ["staged", "blocked"].includes(next.phase)) throw new Error("synthetic restore receipt outage");
      backing.commit(revision, next);
    }, flush: async () => {
      if (env.TEST_CASE === "intent-sync-failure" || (env.TEST_CASE === "inspection-sync-failure"
          && !ctx.id.equals(env.TEST_PILOT.idFromName("source")))) throw new Error("synthetic sync failure");
      await backing.flush();
      this.confirmedRevision = backing.load().revision;
    } };
    ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS test_events (seq INTEGER PRIMARY KEY, event TEXT, details TEXT)");
  }
  private event(name: string, details: unknown = null) {
    const current = this.journal.load();
    if (!["starting", "capturing", "restoring"].includes(current.phase)) throw new Error("native effect before durable intent");
    if (current.revision !== this.confirmedRevision) throw new Error("native effect before intent sync completed");
    this.ctx.storage.sql.exec("INSERT INTO test_events(event,details) VALUES(?,?)", name, JSON.stringify(details));
  }
  private image() { return this.env.TEST_CASE === "unpinned-image" ? "registry.cloudflare.com/test:mutable" : this.env.TEST_CASE === "image-change" ? IMAGE.replace(/a{64}$/, "c".repeat(64)) : IMAGE; }
  async inspectionStatus() { return publicState(this.journal.load()); }
  private clock = () => Date.now() + (this.env.TEST_CASE === "expired" ? 2 * 86400000 : 0);
  async stage(saved: Capsule, pin: string, operation: string) {
    const native = this.env.TEST_CASE === "native-adapter" ? this.testNative() : null;
    const runtime = { image: () => this.image(), startInspection: async (input: Capsule, digest: string) => {
      this.event("inspection-start", { snapshot_id: input.snapshot_id });
      if (native) return native.startInspection(input, digest);
      if (this.env.TEST_CASE === "restore-error") throw new Error("synthetic lost restore reply");
      return { ...proof("inspect"), root_sha256: this.env.TEST_CASE === "inspect-mismatch" ? "f".repeat(64) : DIGEST };
    } };
    return new PilotInspector(this.journal, runtime, this.clock).stage(saved, pin, operation);
  }
  private nativeFake() {
    let running = false, restored = false, phase = "running";
    let labels: Record<string, string> = {};
    // A deliberately small fake of the native API: verifies actual adapter options.
    const container = {
      get running() { return running; }, images: { base: IMAGE },
      start: (options: ContainerStartupOptions) => {
        if (running || options.enableInternet !== false || options.instance !== "lite" || Object.keys(options.env ?? {}).length) throw new Error("unsafe start options");
        this.event("native-start", options);
        running = true; restored = !!options.containerSnapshot; phase = restored ? "inspect" : "running"; labels = options.labels ?? {};
        if (options.entrypoint?.at(-1) !== (restored ? "inspect" : "fresh")) throw new Error("unsafe entrypoint");
      },
      inspect: async () => ({ image: restored ? "" : IMAGE, labels }),
      setInactivityTimeout: async (value: number) => { if (value !== 600000) throw new Error("missing inactivity timeout"); },
      snapshotContainer: async (options: { name: string }) => { this.event("native-snapshot", options); return { id: "synthetic-native-handle", size: 2048 }; },
      exec: async (command: string[], options: ContainerExecOptions) => {
        this.event("native-exec", { command, user: options.user });
        if (command.slice(0, 3).join(" ") !== "/usr/local/bin/python -m tankai.web.pilot_supervisor" || options.user !== "10001:10001"
            || options.cwd !== "/app" || options.stderr !== "ignore" || Object.keys(options.env ?? {}).length) throw new Error("unsafe exec");
        if (command.at(-1) === "quiesce") phase = "quiesced";
        const bytes = new TextEncoder().encode(JSON.stringify(proof(phase)) + "\n");
        return { exitCode: Promise.resolve(0), stdout: new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close(); } }) };
      },
    };
    // This fixture is bundled by esbuild, not deployed; production checks use actual generated Container types.
    return new NativePilot(container as Container);
  }
  async fetch(request: Request) {
    const path = new URL(request.url).pathname;
    if (path === "/__test/audit") return Response.json({ state: this.journal.load(), events: this.ctx.storage.sql.exec("SELECT event,details FROM test_events ORDER BY seq").toArray() });
    if (path === "/__test/seed") {
      const body: { action: "capture" | "restore"; id: string; corrupt?: boolean } = await request.json();
      const current = this.journal.load();
      if (body.corrupt) {
        if (!current.capsule) throw new Error("missing test capsule");
        current.capsule.payload.snapshot_id = "tampered-handle";
        current.revision += 1;
        this.journal.commit(current.revision - 1, current);
      } else this.journal.commit(current.revision, { ...current, revision: current.revision + 1,
        phase: body.action === "capture" ? "capturing" : "restoring", last: { action: body.action, id: body.id, pin: current.capsule?.pin ?? "" } });
      return Response.json({ ok: true });
    }
    const denied = pilotGuard(request, this.env.TOKEN);
    if (denied) return denied;
    if (path === "/_pilot/inspection") return response(await this.env.TEST_PILOT.getByName("inspection").inspectionStatus());
    const native = this.env.TEST_CASE === "native-adapter" ? this.testNative() : null;
    const runtime = {
      image: () => this.image(),
      startFresh: async () => {
        this.event("start");
        if (native) return native.startFresh();
        await new Promise(resolve => setTimeout(resolve, 50));
        if (this.env.TEST_CASE === "start-error") throw new Error("synthetic lost start reply");
        return proof("running");
      },
      quiesce: async () => { this.event("quiesce"); return native ? native.quiesce() : proof(this.env.TEST_CASE === "unquiesced" ? "running" : "quiesced"); },
      sourceImage: async () => this.env.TEST_CASE === "wrong-source-image" ? "wrong" : native ? native.sourceImage() : IMAGE,
      snapshot: async (name: string) => {
        this.event("snapshot");
        if (this.env.TEST_CASE === "snapshot-error") throw new Error("synthetic lost snapshot reply");
        return native ? native.snapshot(name) : { id: "synthetic-native-handle", size: 2048 };
      },
      sourceStatus: async () => native ? native.sourceStatus() : ({ ...proof("quiesced"), root_sha256: this.env.TEST_CASE === "post-snapshot-change" ? "e".repeat(64) : DIGEST }),
      stage: (saved: Capsule, pin: string, operation: string) => this.env.TEST_PILOT.getByName("inspection").stage(saved, pin, operation),
    };
    return pilotDispatch(request, new PilotCoordinator(this.journal, runtime, this.clock));
  }
}
export default {
  async fetch(request: Request, env: TestEnv) {
    if (new URL(request.url).pathname.startsWith("/__test/")) {
      const role = request.headers.get("X-Test-Role") === "inspection" ? "inspection" : "source";
      return env.TEST_PILOT.getByName(role).fetch(request);
    }
    return pilotProxy(request, env.TOKEN, () => {
      if (env.TEST_CASE === "reject-resolution") throw new Error("binding must not be resolved");
      return env.TEST_PILOT.getByName("source");
    });
  },
};
