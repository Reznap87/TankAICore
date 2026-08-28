#!/usr/bin/env python3
"""Read-only production live-provider readiness receipt.

This script inspects repository contracts and optional presence-only secret metadata.
It never requests or prints secret values, calls a model/search provider, or deploys.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

LIVE_PROVIDERS = {"openai", "anthropic"}
SEARCH_PROVIDERS = {"brave", "tavily"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _read(root: Path, relative: str) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read {relative}: {exc}") from exc


def _contains_all(text: str, needles: Iterable[str]) -> bool:
    return all(needle in text for needle in needles)


def _dynamic_container_value(source: str, name: str) -> bool:
    return any(
        marker in source
        for marker in (f"this.env.{name}", f"env.{name}")
    )


def _assignment_line_contains(source: str, name: str, *needles: str) -> bool:
    return any(
        name in line and all(needle in line for needle in needles)
        for line in source.splitlines()
    )


def _provider_secret(provider: str) -> str:
    if provider == "openai":
        return "OPENAI_API_KEY"
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    raise ValueError(f"unsupported live provider: {provider}")


def _search_secret(provider: str) -> str:
    if provider == "brave":
        return "BRAVE_SEARCH_API_KEY"
    if provider == "tavily":
        return "TAVILY_API_KEY"
    raise ValueError(f"unsupported search provider: {provider}")


def evaluate(
    root: Path,
    *,
    main_provider: str = "",
    main_model: str = "",
    critic_provider: str = "",
    critic_model: str = "",
    search_provider: str = "",
    present_secrets: set[str] | None = None,
) -> dict[str, object]:
    root = root.resolve()
    llm = _read(root, "tankai/core/llm.py")
    runtime = _read(root, "tankai/web/runtime.py")
    auth = _read(root, "tankai/web/auth.py")
    server = _read(root, "tankai/web/server.py")
    research = _read(root, "tankai/core/web_research.py")
    cloudflare = _read(root, "src/cloudflare.ts")
    deploy = _read(root, "DEPLOY.md")
    masterplan = _read(root, "TANKAI_MASTERPLAN.md")

    checks: list[Check] = []

    provider_adapter_ok = _contains_all(
        llm,
        (
            "class OpenAILLM",
            "class AnthropicLLM",
            "OPENAI_MODEL fehlt; Modell muss explizit konfiguriert werden",
            "ANTHROPIC_MODEL fehlt; Modell muss explizit konfiguriert werden",
        ),
    )
    checks.append(
        Check(
            "provider_adapters",
            "PASS" if provider_adapter_ok else "FAIL",
            "OpenAI and Anthropic adapters require explicit model IDs"
            if provider_adapter_ok
            else "required explicit live-provider adapters/model guards are missing",
        )
    )

    container_provider_dynamic = _dynamic_container_value(cloudflare, "TANKAI_LLM")
    checks.append(
        Check(
            "container_provider_selection",
            "PASS" if container_provider_dynamic else "FAIL",
            "production container provider is supplied through Worker environment"
            if container_provider_dynamic
            else "production container provider is not dynamically supplied; current source remains fail-closed/mock-only",
        )
    )

    live_mode_fail_closed = (
        "TANKAI_LIVE_PROVIDER_ENABLED" in cloudflare
        and _assignment_line_contains(
            cloudflare,
            "TANKAI_LLM",
            "liveProviderEnabled",
            '"mock"',
        )
    )
    checks.append(
        Check(
            "live_mode_fail_closed",
            "PASS" if live_mode_fail_closed else "FAIL",
            "live provider mode is opt-in and otherwise resolves to mock"
            if live_mode_fail_closed
            else "live-provider opt-in/mock fallback contract is missing",
        )
    )

    secret_forwarding_ok = all(
        _dynamic_container_value(cloudflare, name)
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    )
    checks.append(
        Check(
            "provider_secret_forwarding",
            "PASS" if secret_forwarding_ok else "FAIL",
            "provider secret bindings are forwarded to the container"
            if secret_forwarding_ok
            else "Worker-to-container forwarding for both supported live-provider secret names is incomplete",
        )
    )

    critic_runtime_ok = _contains_all(
        runtime,
        ("get_critic_llm", "TANKAI_REQUIRE_INDEPENDENT_CRITIC"),
    )
    critic_production_enforced = (
        _dynamic_container_value(cloudflare, "TANKAI_CRITIC_LLM")
        and _assignment_line_contains(
            cloudflare,
            "TANKAI_REQUIRE_INDEPENDENT_CRITIC",
            "liveProviderEnabled",
            '"1"',
            '"0"',
        )
    )
    checks.append(
        Check(
            "independent_critic_enforced",
            "PASS" if critic_runtime_ok and critic_production_enforced else "FAIL",
            "runtime supports a separate critic and production explicitly requires independence"
            if critic_runtime_ok and critic_production_enforced
            else "separate critic support exists but production does not yet force independent-critic mode",
        )
    )

    search_code_ok = _contains_all(
        research,
        (
            'provider == "brave"',
            'provider == "tavily"',
            "TANKAI_REQUIRE_RESEARCH_EVIDENCE",
        ),
    ) or _contains_all(
        research,
        ('provider == "brave"', 'provider == "tavily"'),
    )
    search_forwarding_ok = (
        _dynamic_container_value(cloudflare, "TANKAI_SEARCH_PROVIDER")
        and _dynamic_container_value(cloudflare, "BRAVE_SEARCH_API_KEY")
        and _dynamic_container_value(cloudflare, "TAVILY_API_KEY")
    )
    checks.append(
        Check(
            "research_provider_contract",
            "PASS" if search_code_ok and search_forwarding_ok else "FAIL",
            "Brave/Tavily selection and search secret forwarding are production-wired"
            if search_code_ok and search_forwarding_ok
            else "research adapters exist but production search-provider/secret forwarding is incomplete",
        )
    )

    budget_names = (
        "TANKAI_LLM_MAX_TOKENS",
        "TANKAI_LLM_TIMEOUT_SECONDS",
        "TANKAI_LLM_MAX_RETRIES",
        "TANKAI_LLM_MAX_CALLS_PER_RUN",
        "TANKAI_LIVE_SMOKE_MAX_TOKENS",
        "class LLMCallBudget",
    )
    budget_contract_ok = _contains_all(llm + runtime + cloudflare, budget_names)
    checks.append(
        Check(
            "runtime_budget_contract",
            "PASS" if budget_contract_ok else "FAIL",
            "explicit output-token, timeout, retry, per-run call ceiling and bounded-smoke controls exist"
            if budget_contract_ok
            else "explicit production token/timeout/retry/per-run-call/bounded-smoke controls are incomplete",
        )
    )

    provider_rate_contract_ok = (
        _contains_all(
            auth + server + llm + cloudflare,
            (
                "class ProviderCallRateLimiter",
                "provider_call_events",
                "TANKAI_PROVIDER_CALLS_PER_WINDOW",
                "TANKAI_PROVIDER_RATE_WINDOW_SECONDS",
                "set_call_guard",
                "LLMRateLimitExceeded",
            ),
        )
        and "provider_limiter.consume(context.user_id, identity)" in server
    )
    checks.append(
        Check(
            "provider_rate_contract",
            "PASS" if provider_rate_contract_ok else "FAIL",
            "persistent per-user/provider time-window rate ceiling is wired before model calls"
            if provider_rate_contract_ok
            else "persistent per-user/provider provider-call rate ceiling is incomplete",
        )
    )

    rollback_ok = (
        "TANKAI_LLM" in cloudflare
        and "mock" in cloudflare.lower()
        and "rollback" in (deploy + masterplan).lower()
    )
    checks.append(
        Check(
            "rollback_contract",
            "PASS" if rollback_ok else "FAIL",
            "verified mock baseline and rollback documentation are present"
            if rollback_ok
            else "mock baseline and explicit rollback documentation are not both present",
        )
    )

    target_values = (main_provider, main_model, critic_provider, critic_model, search_provider)
    if not any(target_values):
        checks.append(
            Check(
                "target_provider_policy",
                "UNKNOWN",
                "no target provider/model/search selection supplied to this read-only run",
            )
        )
    else:
        errors: list[str] = []
        if main_provider not in LIVE_PROVIDERS:
            errors.append("main provider must be openai or anthropic")
        if critic_provider not in LIVE_PROVIDERS:
            errors.append("critic provider must be openai or anthropic")
        if main_provider and critic_provider and main_provider == critic_provider:
            errors.append("main and critic provider families must differ")
        if not main_model.strip():
            errors.append("main model must be explicit")
        if not critic_model.strip():
            errors.append("critic model must be explicit")
        if search_provider not in SEARCH_PROVIDERS:
            errors.append("search provider must be brave or tavily")
        checks.append(
            Check(
                "target_provider_policy",
                "FAIL" if errors else "PASS",
                "; ".join(errors) if errors else "target main/critic/search providers are explicit and critic family is independent",
            )
        )

    if present_secrets is None:
        checks.append(
            Check(
                "required_secret_presence",
                "UNKNOWN",
                "presence-only production secret metadata was not supplied",
            )
        )
    elif all((main_provider, critic_provider, search_provider)):
        required = {
            _provider_secret(main_provider),
            _provider_secret(critic_provider),
            _search_secret(search_provider),
        }
        missing = sorted(required - present_secrets)
        checks.append(
            Check(
                "required_secret_presence",
                "FAIL" if missing else "PASS",
                "missing secret names: " + ", ".join(missing)
                if missing
                else "all required secret names are present; values were not requested",
            )
        )
    else:
        checks.append(
            Check(
                "required_secret_presence",
                "UNKNOWN",
                "target providers are incomplete, so required secret names cannot be derived",
            )
        )

    statuses = {check.status for check in checks}
    if "FAIL" in statuses:
        overall = "NOT_READY"
    elif "UNKNOWN" in statuses:
        overall = "UNKNOWN"
    else:
        overall = "READY"

    return {
        "gate": "ops.production.live_provider_readiness",
        "overall": overall,
        "read_only": True,
        "secret_values_requested": False,
        "paid_provider_call": False,
        "deployment_triggered": False,
        "checks": [asdict(check) for check in checks],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--main-provider", default="")
    parser.add_argument("--main-model", default="")
    parser.add_argument("--critic-provider", default="")
    parser.add_argument("--critic-model", default="")
    parser.add_argument("--search-provider", default="")
    parser.add_argument(
        "--present-secret",
        action="append",
        default=None,
        help="Secret name whose presence was verified externally; never pass a secret value.",
    )
    parser.add_argument("--output", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    present = None if args.present_secret is None else set(args.present_secret)
    result = evaluate(
        Path(args.root),
        main_provider=args.main_provider.strip().lower(),
        main_model=args.main_model.strip(),
        critic_provider=args.critic_provider.strip().lower(),
        critic_model=args.critic_model.strip(),
        search_provider=args.search_provider.strip().lower(),
        present_secrets=present,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["overall"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
