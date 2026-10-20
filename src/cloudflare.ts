import { Container } from "@cloudflare/containers";
import { DurableObject } from "cloudflare:workers";
import { timingSafeEqual } from "node:crypto";

type TankAIRuntimeEnv = Env & {
  TANKAI_AUTH_STATE_TOKEN?: string;
  TANKAI_BOOTSTRAP_OWNER?: string;
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
    console.log(JSON.stringify({
      message: "request",
      method: request.method,
      path: url.pathname
    }));

    try {
      const container = env.TANKAI_CONTAINER.getByName("singleton");
      await container.startAndWaitForPorts();
      return await container.fetch(request);
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
