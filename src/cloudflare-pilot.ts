import { DurableObject } from "cloudflare:workers";
import { PilotCoordinator, PilotInspector, SqlJournal, publicState, requirePilot, type Capsule } from "./pilot-controller";
import { NativePilot, PILOT_IDLE_MS } from "./pilot-native";
import { pilotDispatch, pilotGuard, pilotProxy, response } from "./pilot-api";

export class TankAIPilot extends DurableObject<PilotEnv> {
  readonly #journal: SqlJournal;
  constructor(ctx: DurableObjectState, env: PilotEnv) {
    super(ctx, env);
    this.#journal = new SqlJournal(ctx.storage);
    if (ctx.container?.running) {
      void ctx.blockConcurrencyWhile(() => ctx.container!.setInactivityTimeout(PILOT_IDLE_MS));
    }
  }
  #role(name: "source" | "inspection") { return this.ctx.id.equals(this.env.TANKAI_PILOT.idFromName(name)); }
  #native() {
    requirePilot(this.ctx.container !== undefined, "pilot_container_binding_required");
    return new NativePilot(this.ctx.container);
  }
  async stage(saved: Capsule, pin: string, operation: string) {
    requirePilot(this.#role("inspection"), "inspection_role_required");
    return new PilotInspector(this.#journal, this.#native()).stage(saved, pin, operation);
  }
  async inspectionStatus() {
    requirePilot(this.#role("inspection"), "inspection_role_required");
    return publicState(this.#journal.load());
  }
  async fetch(request: Request): Promise<Response> {
    const denied = pilotGuard(request, this.env.TANKAI_PILOT_CONTROL_TOKEN);
    if (denied) return denied;
    if (!this.#role("source")) return response({ error: "source_role_required" }, 403);
    // Status never constructs a native runtime or starts compute.
    if (request.method === "GET") {
      if (new URL(request.url).pathname === "/_pilot/inspection") {
        try { return response(await this.env.TANKAI_PILOT.getByName("inspection").inspectionStatus()); }
        catch { return response({ error: "pilot_state_unavailable" }, 503); }
      }
      try { const current = this.#journal.load(); return response(publicState(current), 200, current.revision); }
      catch { return response({ error: "pilot_state_unavailable" }, 503); }
    }
    try {
      const native = this.#native();
      const runtime = {
        image: () => native.image(), startFresh: () => native.startFresh(), quiesce: () => native.quiesce(),
        sourceImage: () => native.sourceImage(), snapshot: (name: string) => native.snapshot(name), sourceStatus: () => native.sourceStatus(),
        stage: (saved: Capsule, pin: string, operation: string) => this.env.TANKAI_PILOT.getByName("inspection").stage(saved, pin, operation),
      };
      return pilotDispatch(request, new PilotCoordinator(this.#journal, runtime));
    } catch { return response({ error: "pilot_unavailable" }, 503); }
  }
}

export default {
  async fetch(request: Request, env: PilotEnv): Promise<Response> {
    return pilotProxy(request, env.TANKAI_PILOT_CONTROL_TOKEN, () => env.TANKAI_PILOT.getByName("source"));
  },
} satisfies ExportedHandler<PilotEnv>;
