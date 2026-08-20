interface Env {
  TANKAI_CONTAINER: DurableObjectNamespace<
    import("./src/cloudflare").TankAIContainer
  >;
}
