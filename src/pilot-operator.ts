// Local-only bridge. The deployed pilot authenticates the actual bearer token.
import { pilotEnvelope, response } from "./pilot-api";
import { boundedBytes } from "./pilot-native";

export async function operatorFetch(request: Request, target: () => Fetcher): Promise<Response> {
  const url = new URL(request.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.port !== "8766"
      || request.headers.has("Origin") || [...request.headers.keys()].some(name => name.startsWith("sec-fetch-"))) {
    return response({ error: "operator_loopback_cli_required" }, 403);
  }
  const authorization = request.headers.get("Authorization") ?? "";
  if (!/^Bearer [A-Za-z0-9_-]{43,128}$/.test(authorization)) return response({ error: "pilot_unauthorized" }, 403);
  const denied = pilotEnvelope(request);
  if (denied) return denied;
  try {
    const headers = new Headers({ Authorization: authorization, Accept: "application/json" });
    for (const name of ["Content-Type", "If-Match", "Idempotency-Key"]) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    const body = request.method === "POST" ? await boundedBytes(request.body, 512) : null;
    const forwarded = new Request("https://tankai-pilot.internal" + url.pathname,
      { method: request.method, headers, redirect: "manual", ...(body === null ? {} : { body: new Uint8Array(body).buffer }) });
    const upstream = await target().fetch(forwarded);
    if (!/^application\/json(?:;|$)/i.test(upstream.headers.get("Content-Type") ?? "")) return response({ error: "operator_invalid_upstream" }, 502);
    const bytes = await boundedBytes(upstream.body, 8192);
    const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    const result = response(value, upstream.status);
    const etag = upstream.headers.get("ETag");
    if (etag && /^"[0-9]{1,15}"$/.test(etag)) result.headers.set("ETag", etag);
    return result; // No cookies, redirects, CORS headers, or upstream diagnostic headers.
  } catch { return response({ error: "operator_upstream_uncertain" }, 502); }
}
export default {
  fetch(request: Request, env: PilotOperatorEnv) { return operatorFetch(request, () => env.PILOT); },
} satisfies ExportedHandler<PilotOperatorEnv>;
