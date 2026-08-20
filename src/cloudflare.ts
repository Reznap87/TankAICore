import { Container } from "@cloudflare/containers";

export class TankAIContainer extends Container<Env> {
  defaultPort = 8765;
  requiredPorts = [8765];
  sleepAfter = "5m";
  enableInternet = true;
  pingEndpoint = "/api/health";
  envVars = {
    TANKAI_HOST: "0.0.0.0",
    TANKAI_PORT: "8765",
    TANKAI_LLM: "mock",
    TANKAI_EMBEDDER: "hashing",
    TANKAI_RUN_STORE: "/tmp/tankai_runs.jsonl"
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
