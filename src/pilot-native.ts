import { pinnedImage, requirePilot, type Capsule } from "./pilot-controller";

export const PILOT_IDLE_MS = 10 * 60 * 1000;
const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
export async function boundedBytes(stream: ReadableStream<Uint8Array> | null, limit: number): Promise<Uint8Array> {
  requirePilot(stream !== null, "pilot_stream_required");
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      requirePilot(size <= limit, "pilot_payload_too_large");
      chunks.push(item.value);
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

export class NativePilot {
  constructor(private readonly container: Container) {}
  image() { return pinnedImage(this.container.images.base); }
  async sourceImage() {
    const info = await this.container.inspect();
    requirePilot(info !== null, "pilot_container_not_running");
    return info.image;
  }
  async command(command: "status" | "quiesce"): Promise<unknown> {
    requirePilot(this.container.running, "pilot_container_not_running");
    const controller = new AbortController();
    let exited = false;
    const timer = setTimeout(() => { if (!exited) controller.abort(); }, command === "quiesce" ? 165000 : 10000);
    try {
      const child = await this.container.exec(["/usr/local/bin/python", "-m", "tankai.web.pilot_supervisor", command],
        { cwd: "/app", user: "10001:10001", env: {}, stdout: "pipe", stderr: "ignore", signal: controller.signal });
      const exit = child.exitCode.then(code => { exited = true; clearTimeout(timer); return code; },
        error => { exited = true; clearTimeout(timer); throw error; });
      const [code, bytes] = await Promise.all([exit, boundedBytes(child.stdout, 2048)]);
      requirePilot(code === 0, "pilot_control_failed");
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch (error) {
      if (!exited && !controller.signal.aborted) controller.abort();
      throw error;
    } finally { clearTimeout(timer); }
  }
  private async ready() {
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      requirePilot(this.container.running, "pilot_container_stopped");
      try { return await this.command("status"); } catch { /* No startup replay. */ }
      await sleep(200);
    }
    throw new Error("pilot_readiness_timeout");
  }
  async startFresh() {
    requirePilot(!this.container.running, "pilot_source_already_running");
    this.container.start({ image: this.image(), instance: "lite", enableInternet: false, env: {},
      entrypoint: ["python", "-m", "tankai.web.pilot_supervisor", "fresh"] });
    await this.container.setInactivityTimeout(PILOT_IDLE_MS);
    return this.ready();
  }
  quiesce() { return this.command("quiesce"); }
  sourceStatus() { return this.command("status"); }
  snapshot(name: string) {
    requirePilot(this.container.running, "pilot_container_stopped");
    return this.container.snapshotContainer({ name });
  }
  async startInspection(saved: Capsule, pin: string) {
    requirePilot(!this.container.running && this.image() === saved.image, "inspection_target_not_empty_or_foreign");
    this.container.start({ containerSnapshot: { id: saved.snapshot_id }, instance: "lite", enableInternet: false,
      env: {}, entrypoint: ["python", "-m", "tankai.web.pilot_supervisor", "inspect"], labels: { capsule: pin, role: "inspection" } });
    await this.container.setInactivityTimeout(PILOT_IDLE_MS);
    const proof = await this.ready();
    const info = await this.container.inspect();
    // Restored Containers deliberately report an empty image, per the native API.
    requirePilot(info !== null && info.image === "" && info.labels.capsule === pin && info.labels.role === "inspection", "inspection_metadata_mismatch");
    return proof;
  }
}
