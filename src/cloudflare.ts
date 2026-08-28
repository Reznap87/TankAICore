import { Container } from "@cloudflare/containers";

type TankAIRuntimeEnv = Env & {
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
    TANKAI_LIVE_SMOKE_MAX_TOKENS: clean(this.env.TANKAI_LIVE_SMOKE_MAX_TOKENS, "256")
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
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
