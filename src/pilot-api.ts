import { timingSafeEqual } from "node:crypto";
import { PilotCoordinator, PilotDenied, UUID, publicState, requirePilot } from "./pilot-controller";
import { boundedBytes } from "./pilot-native";

const paths = new Map([["/_pilot/status", "GET"], ["/_pilot/inspection", "GET"], ["/_pilot/start", "POST"], ["/_pilot/capture", "POST"], ["/_pilot/restore", "POST"]]);
export function response(value: unknown, status = 200, revision?: number) {
  return Response.json(value, { status, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    ...(revision === undefined ? {} : { ETag: `"${revision}"` }) } });
}
export function pilotGuard(request: Request, token: unknown): Response | null {
  const supplied = request.headers.get("Authorization") ?? "";
  if (typeof token !== "string" || !/^[A-Za-z0-9_-]{43,128}$/.test(token)
      || supplied.length !== token.length + 7 || !/^[\x20-\x7e]+$/.test(supplied)
      || !timingSafeEqual(Buffer.from(supplied), Buffer.from("Bearer " + token))) {
    return response({ error: "pilot_unauthorized" }, 403);
  }
  return pilotEnvelope(request);
}
export function pilotEnvelope(request: Request): Response | null {
  const url = new URL(request.url);
  const method = paths.get(url.pathname);
  if (!method || url.search) return response({ error: "pilot_route_unknown" }, 404);
  if (request.method !== method) return response({ error: "pilot_method_denied" }, 405);
  if (method === "POST") {
    if (!/^"[0-9]{1,15}"$/.test(request.headers.get("If-Match") ?? "")) return response({ error: "pilot_revision_required" }, 428);
    if (!UUID.test(request.headers.get("Idempotency-Key") ?? "")) return response({ error: "pilot_operation_id_required" }, 400);
    if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(request.headers.get("Content-Type") ?? "")) return response({ error: "pilot_json_required" }, 415);
    const length = request.headers.get("Content-Length");
    if (length !== null && (!/^[0-9]{1,6}$/.test(length) || Number(length) > 512)) return response({ error: "pilot_payload_too_large" }, 413);
  }
  return null;
}
export async function pilotDispatch(request: Request, coordinator: PilotCoordinator): Promise<Response> {
  try {
    if (request.method === "GET") {
      const current = coordinator.status();
      return response(publicState(current), 200, current.revision);
    }
    const action = new URL(request.url).pathname.slice("/_pilot/".length);
    requirePilot(action === "start" || action === "capture" || action === "restore");
    const raw = await boundedBytes(request.body, 512);
    let value: unknown;
    try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)); }
    catch { return response({ error: "pilot_invalid_json" }, 400); }
    requirePilot(value !== null && typeof value === "object" && !Array.isArray(value), "pilot_invalid_body");
    const body = value as Record<string, unknown>;
    requirePilot(Object.keys(body).length === (action === "restore" ? 1 : 0) && (action !== "restore" || typeof body.pin === "string"), "pilot_invalid_body");
    const current = await coordinator.operate(action, request.headers.get("Idempotency-Key") ?? "",
      Number((request.headers.get("If-Match") ?? "").slice(1, -1)), action === "restore" ? String(body.pin) : "");
    const code = current.phase === "blocked" ? 409 : ["starting", "capturing", "restoring"].includes(current.phase) ? 202 : 200;
    return response(publicState(current), code, current.revision);
  } catch (error) {
    return error instanceof PilotDenied ? response({ error: error.code }, error.status) : response({ error: "pilot_unavailable" }, 503);
  }
}

export async function pilotProxy(request: Request, token: unknown, source: () => { fetch(request: Request): Promise<Response> }): Promise<Response> {
  const denied = pilotGuard(request, token);
  if (denied) return denied;
  try { return await source().fetch(request); }
  catch { return response({ error: "pilot_unavailable" }, 503); }
}
