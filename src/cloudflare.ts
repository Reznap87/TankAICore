import { Container } from "@cloudflare/containers";
import { DurableObject } from "cloudflare:workers";
import { timingSafeEqual } from "node:crypto";

type TankAIRuntimeEnv = Env & {
  TANKAI_AUTH_STATE_TOKEN?: string;
  TANKAI_BOOTSTRAP_OWNER?: string;
  TANKAI_ALLOW_REGISTRATION?: string;
  TANKAI_LIVE_PROVIDER_ENABLED?: string;
  TANKAI_LLM?: string;
  OPENAI_API_KEY?: string;
  OPENAI_MODEL?: string;
  OPENAI_BASE_URL?: string;
  ANTHROPIC_API_KEY?: string;
  ANTHROPIC_MODEL?: string;
  TANKAI_CRITIC_LLM?: string;
  TANKAI_CRITIC_MODEL?: string;
  TANKAI_CRITIC_API_KEY?: string;
  TANKAI_CRITIC_BASE_URL?: string;
  TANKAI_SEARCH_PROVIDER?: string;
  TANKAI_SEARCH_API_KEY?: string;
  BRAVE_SEARCH_API_KEY?: string;
  TAVILY_API_KEY?: string;
  TANKAI_LLM_MAX_TOKENS?: string;
  TANKAI_LLM_TIMEOUT_SECONDS?: string;
  TANKAI_LLM_MAX_RETRIES?: string;
  TANKAI_LLM_MAX_CALLS_PER_RUN?: string;
  TANKAI_PROVIDER_CALLS_PER_WINDOW?: string;
  TANKAI_PROVIDER_RATE_WINDOW_SECONDS?: string;
  TANKAI_LIVE_SMOKE_MAX_TOKENS?: string;
};

function clean(value: string | undefined, fallback = ""): string {
  const normalized = value?.trim() ?? "";
  return normalized || fallback;
}

function enabled(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes(clean(value).toLowerCase());
}

export class TankAIContainer extends Container<TankAIRuntimeEnv> {
  defaultPort = 8765;
  requiredPorts = [8765];
  sleepAfter = "5m";
  enableInternet = true;
  pingEndpoint = "/api/health";

  private readonly liveProviderEnabled = enabled(this.env.TANKAI_LIVE_PROVIDER_ENABLED);

  envVars = {
    TANKAI_AUTH_STATE_URL: clean(this.env.TANKAI_AUTH_STATE_TOKEN) ? "https://tankai-core.reznaplecram.workers.dev/_tankai/auth-state" : "",
    TANKAI_AUTH_STATE_TOKEN: clean(this.env.TANKAI_AUTH_STATE_TOKEN),
    TANKAI_BOOTSTRAP_OWNER: clean(this.env.TANKAI_BOOTSTRAP_OWNER),
    TANKAI_ALLOW_REGISTRATION: enabled(this.env.TANKAI_ALLOW_REGISTRATION) ? "1" : "0",
    TANKAI_HOST: "0.0.0.0",
    TANKAI_PORT: "8765",
    TANKAI_LIVE_PROVIDER_ENABLED: this.liveProviderEnabled ? "1" : "0",
    TANKAI_LLM: this.liveProviderEnabled ? clean(this.env.TANKAI_LLM) : "mock",
    TANKAI_EMBEDDER: "hashing",
    TANKAI_RUN_STORE: "/tmp/tankai_runs.jsonl",

    OPENAI_API_KEY: this.liveProviderEnabled ? clean(this.env.OPENAI_API_KEY) : "",
    OPENAI_MODEL: this.liveProviderEnabled ? clean(this.env.OPENAI_MODEL) : "",
    OPENAI_BASE_URL: this.liveProviderEnabled ? clean(this.env.OPENAI_BASE_URL) : "",
    ANTHROPIC_API_KEY: this.liveProviderEnabled ? clean(this.env.ANTHROPIC_API_KEY) : "",
    ANTHROPIC_MODEL: this.liveProviderEnabled ? clean(this.env.ANTHROPIC_MODEL) : "",

    TANKAI_CRITIC_LLM: this.liveProviderEnabled ? clean(this.env.TANKAI_CRITIC_LLM) : "",
    TANKAI_CRITIC_MODEL: this.liveProviderEnabled ? clean(this.env.TANKAI_CRITIC_MODEL) : "",
    TANKAI_CRITIC_API_KEY: this.liveProviderEnabled ? clean(this.env.TANKAI_CRITIC_API_KEY) : "",
    TANKAI_CRITIC_BASE_URL: this.liveProviderEnabled ? clean(this.env.TANKAI_CRITIC_BASE_URL) : "",
    TANKAI_REQUIRE_INDEPENDENT_CRITIC: this.liveProviderEnabled ? "1" : "0",

    TANKAI_SEARCH_PROVIDER: this.liveProviderEnabled ? clean(this.env.TANKAI_SEARCH_PROVIDER) : "",
    TANKAI_SEARCH_API_KEY: this.liveProviderEnabled ? clean(this.env.TANKAI_SEARCH_API_KEY) : "",
    BRAVE_SEARCH_API_KEY: this.liveProviderEnabled ? clean(this.env.BRAVE_SEARCH_API_KEY) : "",
    TAVILY_API_KEY: this.liveProviderEnabled ? clean(this.env.TAVILY_API_KEY) : "",
    TANKAI_REQUIRE_RESEARCH_EVIDENCE: "1",
    TANKAI_STRICT_WEB_RESEARCH: this.liveProviderEnabled ? "1" : "0",

    TANKAI_LLM_MAX_TOKENS: clean(this.env.TANKAI_LLM_MAX_TOKENS, "2048"),
    TANKAI_LLM_TIMEOUT_SECONDS: clean(this.env.TANKAI_LLM_TIMEOUT_SECONDS, "30"),
    TANKAI_LLM_MAX_RETRIES: clean(this.env.TANKAI_LLM_MAX_RETRIES, "1"),
    TANKAI_LLM_MAX_CALLS_PER_RUN: clean(this.env.TANKAI_LLM_MAX_CALLS_PER_RUN, "40"),
    TANKAI_PROVIDER_CALLS_PER_WINDOW: clean(this.env.TANKAI_PROVIDER_CALLS_PER_WINDOW, "40"),
    TANKAI_PROVIDER_RATE_WINDOW_SECONDS: clean(this.env.TANKAI_PROVIDER_RATE_WINDOW_SECONDS, "60"),
    TANKAI_LIVE_SMOKE_MAX_TOKENS: clean(this.env.TANKAI_LIVE_SMOKE_MAX_TOKENS, "256")
  };
}

const MAX_AUTH_STATE = 8 * 1024 * 1024;
const AUTH_CHUNK = 64 * 1024;

function authorizedState(request: Request, env: TankAIRuntimeEnv): boolean {
  const token = clean(env.TANKAI_AUTH_STATE_TOKEN);
  const supplied = request.headers.get("Authorization") ?? "";
  const expected = Buffer.from("Bearer " + token);
  const actual = Buffer.from(supplied);
  return /^[A-Za-z0-9_-]{43,128}$/.test(token) && actual.length === expected.length &&
    timingSafeEqual(actual, expected);
}

// Separate from the Container DO so a cold-starting Python process can restore
// its database without waiting on its own container startup RPC.
export class TankAIAuthState extends DurableObject<TankAIRuntimeEnv> {
  async fetch(request: Request): Promise<Response> {
    const headers = { "Cache-Control": "no-store" };
    if (!authorizedState(request, this.env)) return new Response(null, { status: 403, headers });
    const path = new URL(request.url).pathname;
    if ((path === "/registration-limit" || path === "/registration-resolve") && request.method === "POST") {
      const id = request.headers.get("X-Registration-Id") ?? "";
      const bucket = request.headers.get("X-Registration-Bucket") ?? "";
      if (!/^[0-9a-f]{32}$/.test(bucket) || !/^[0-9a-f-]{36}$/.test(id)) return new Response(null, { status: 400, headers });
      return this.ctx.storage.transaction(async storage => {
        const now = Date.now();
        const day = Math.floor(now / 86400000);
        const hour = Math.floor(now / 3600000);
        const globalKey = `registration-day-v2:${day % 2}`;
        const current = await storage.get<{ period: number; count: number; pending: Record<string, boolean> }>(globalKey);
        const global = current?.period === day ? current : { period: day, count: 0, pending: {} };
        if (path === "/registration-resolve") {
          if (global.pending[id]) {
            delete global.pending[id];
            if (request.headers.get("X-Registration-Outcome") === "rejected") global.count--;
            await storage.put(globalKey, global);
          }
          return new Response(null, { status: 204, headers });
        }
        // A bounded set of shards prevents unbounded durable storage even if an
        // attacker presents many IPs. Distinct clients never share a counter.
        const clientKey = `registration-shard-v2:${bucket.slice(0, 2)}`;
        const previous = await storage.get<{ period: number; counts: Record<string, number> }>(clientKey);
        const shard = previous?.period === hour ? previous : { period: hour, counts: {} };
        const clientCount = shard.counts[bucket] ?? 0;
        if (global.count >= 50 || clientCount >= 3 || (!clientCount && Object.keys(shard.counts).length >= 64)) return new Response(null, { status: 429, headers });
        shard.counts[bucket] = clientCount + 1;
        global.count++;
        global.pending[id] = true;
        await storage.put(clientKey, shard);
        await storage.put(globalKey, global);
        return new Response(null, { status: 204, headers });
      });
    }
    if (request.method === "GET") {
      return this.ctx.storage.transaction(async storage => {
        const meta = await storage.get<{ revision: number; count: number; size: number }>("meta");
        if (!meta) return new Response(null, { status: 404, headers });
        const data = new Uint8Array(meta.size);
        for (let i = 0; i < meta.count; i++) {
          const part = await storage.get<ArrayBuffer>(`part:${i}`);
          if (!part) throw new Error("Incomplete auth snapshot");
          data.set(new Uint8Array(part), i * AUTH_CHUNK);
        }
        return new Response(data, { headers: { ...headers, "Content-Type": "application/octet-stream", "X-Auth-Revision": String(meta.revision) } });
      });
    }
    if (request.method !== "PUT") return new Response(null, { status: 405, headers });
    const length = request.headers.get("Content-Length");
    if (!length || !/^\d+$/.test(length) || Number(length) > MAX_AUTH_STATE) return new Response(null, { status: 413, headers });
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > MAX_AUTH_STATE || new TextDecoder().decode(bytes.slice(0, 16)) !== "SQLite format 3\0") return new Response(null, { status: 400, headers });
    return this.ctx.storage.transaction(async storage => {
      const previous = await storage.get<{ revision: number; count: number }>("meta");
      if (request.headers.get("If-Match") !== String(previous?.revision ?? 0)) return new Response(null, { status: 409, headers });
      const count = Math.ceil(bytes.byteLength / AUTH_CHUNK);
      for (let i = 0; i < count; i++) await storage.put(`part:${i}`, bytes.slice(i * AUTH_CHUNK, (i + 1) * AUTH_CHUNK));
      for (let i = count; i < (previous?.count ?? 0); i++) await storage.delete(`part:${i}`);
      const revision = (previous?.revision ?? 0) + 1;
      await storage.put("meta", { count, revision, size: bytes.byteLength });
      return new Response(null, { status: 204, headers: { ...headers, "X-Auth-Revision": String(revision) } });
    });
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/_tankai/auth-state") {
      if (!authorizedState(request, env)) return new Response(null, { status: 403, headers: { "Cache-Control": "no-store" } });
      return env.TANKAI_AUTH_STATE.getByName("singleton").fetch(request);
    }
    let registration: { bucket: string; id: string; token: string } | undefined;
    if (url.pathname === "/api/auth/register" && request.method === "POST") {
      if (!enabled((env as TankAIRuntimeEnv).TANKAI_ALLOW_REGISTRATION)) return Response.json({ error: "Registrierung ist deaktiviert" }, { status: 403 });
      const ip = request.headers.get("CF-Connecting-IP");
      const token = clean((env as TankAIRuntimeEnv).TANKAI_AUTH_STATE_TOKEN);
      if (!ip || !/^[A-Za-z0-9_-]{43,128}$/.test(token)) return Response.json({ error: "Registrierung vorübergehend nicht verfügbar" }, { status: 503 });
      const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(token), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
      const hash = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(ip)));
      const bucket = Array.from(hash.slice(0, 16), byte => byte.toString(16).padStart(2, "0")).join("");
      const id = crypto.randomUUID();
      try {
        const limit = await env.TANKAI_AUTH_STATE.getByName("singleton").fetch(new Request("https://tankai.internal/registration-limit", {
          method: "POST", headers: { Authorization: `Bearer ${token}`, "X-Registration-Bucket": bucket, "X-Registration-Id": id }
        }));
        if (limit.status !== 204) return Response.json({ error: limit.status === 429 ? "Zu viele Registrierungsversuche. Bitte später erneut versuchen." : "Registrierung vorübergehend nicht verfügbar" }, { status: limit.status === 429 ? 429 : 503 });
      } catch {
        return Response.json({ error: "Registrierung vorübergehend nicht verfügbar" }, { status: 503 });
      }
      registration = { bucket, id, token };
    }
    console.log(JSON.stringify({
      message: "request",
      method: request.method,
      path: url.pathname
    }));

    try {
      const container = env.TANKAI_CONTAINER.getByName("singleton");
      await container.startAndWaitForPorts();
      const response = await container.fetch(request);
      if (registration) {
        // Only an explicit application rejection proves no account was created.
        // On timeouts, 5xx, or uncertain outcomes the reservation remains held.
        const outcome = response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429 ? "rejected" : "committed";
        try {
          if (outcome === "rejected" || response.status === 201) {
            await env.TANKAI_AUTH_STATE.getByName("singleton").fetch(new Request("https://tankai.internal/registration-resolve", {
              method: "POST", headers: { Authorization: `Bearer ${registration.token}`, "X-Registration-Bucket": registration.bucket, "X-Registration-Id": registration.id, "X-Registration-Outcome": outcome }
            }));
          }
        } catch { /* retain the durable reservation on uncertainty */ }
      }
      return response;
    } catch (error) {
      console.error(JSON.stringify({
        message: "container request failed",
        error: error instanceof Error ? error.message : String(error)
      }));
      return Response.json(
        { ok: false, error: "TankAI service unavailable" },
        { status: 503 }
      );
    }
  }
} satisfies ExportedHandler<Env>;
