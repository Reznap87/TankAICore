from pathlib import Path


MASTERPLAN = Path(__file__).resolve().parents[1] / "TANKAI_MASTERPLAN.md"


def test_current_reality_contract_tracks_repository_state_without_reopening_completed_work(
) -> None:
    text = MASTERPLAN.read_text(encoding="utf-8")
    current, separator, _history = text.partition("\n1. Ergebnis, das entstehen muss")

    assert separator
    assert "Version: 5.7.54" in current
    assert "Statusdatum: 24. Oktober 2026" in current
    assert "0. Verifizierter Projektstand und Ausführungsvertrag am 24. Oktober 2026" in current
    assert "b9eb349363889efe90e84460258ee19f0892b003" in current
    assert "docs/MONETARY_RESERVATIONS.md" in current
    assert "Opt-in-Anbindung an den" in current
    assert "Die öffentliche Standardkonfiguration aktiviert diesen Pfad nicht" in current
    assert "docs/OWNER_PILOT_ADMISSION.md" in current
    assert "docs/SOURCE_GRANTS.md" in current
    assert "Die explizite Chat-/Core-Anbindung" in current
    assert "dieser Freigaben ist implementiert und lokal ohne externe Aufrufe geprüft" in current
    assert "cea6f42965bc10f3a39090794c5552ff34161057" in current
    assert "32fc47a9656c3df39f1d3e16e2ec29d45ea4d3d0" in current
    assert "5e51377259ea5e0281607f7f1a7ce6592353543e" in current
    assert "1a75c2c10666cc8d3ef8f262d44e17111f69b48d" in current
    assert "Die Entwurfs-PRs #84 und #96" in current
    assert "7e0e908afc5d6c98e930fd9f2ad0317ffdca4a27" in current
    assert "e943e61a4ac8b1778f17798b68414656f093cf33" in current
    assert "b311abe08bed5cc644798052faf843d0eb136ab0" in current
    assert "317883fa39819c813e292700f6015b456c867084" in current
    assert "cb851b63e6afccd504eba1b2e64a11a728ccba7f" in current
    assert "f39f1a010db372e49ebb392173352828e885798d" in current
    assert "5ed5a4fadea14b6f7fdffcc71fe38b5386119422" in current
    assert "ein offener Entwurfs-Pull-Request #84" in current
    assert "TankAI Core CI Run #70" in current
    assert "TankAI Core CI Run #71" in current
    assert "TankAI Core CI Run #72" in current
    assert "TankAI Core CI Run #73" in current
    assert "TankAI Core CI Run #74" in current
    assert "TankAI Core CI Run #75" in current
    assert "TankAI Core CI Run #76" in current
    assert "TankAI Core CI Run #77" in current
    assert "TankAI Core CI Run #78" in current
    assert "TankAI Core CI Run #79" in current
    assert "TankAI Core CI Run #80" in current
    assert "TankAI Core CI Run #81" in current
    assert "TankAI Core CI Run #82" in current
    assert "TankAI Core CI Run #83" in current
    assert "TankAI Core CI Run #84" in current
    assert "TankAI Core CI Run #85" in current
    assert "TankAI Core CI Run #86" in current
    assert "TankAI Core CI Run #87" in current
    assert "TankAI Core CI Run #88" in current
    assert "TankAI Core CI Run #89" in current
    assert "TankAI Core CI Run #90" in current
    assert "TankAI Core CI Run #91" in current
    assert "TankAI Core CI Run #92" in current
    assert "TankAI Core CI Run #93" in current
    assert "TankAI Core CI Run #94" in current
    assert "TankAI Core CI Run #95" in current
    assert "TankAI Core CI Run #96" in current
    assert "TankAI Core CI Run #97" in current
    assert "TankAI Core CI Run #98" in current
    assert "TankAI Core CI Run #99" in current
    assert "TankAI Core CI Run #100" in current
    assert "TankAI Core CI Run #101" in current
    assert "TankAI Core CI Run #102" in current
    assert "TankAI Core CI Run #103" in current
    assert "TankAI Core CI Run #104" in current
    assert "TankAI Core CI Run #105" in current
    assert "TankAI Core CI Run #106" in current
    assert "TankAI Core CI Run #107" in current
    assert "TankAI Core CI Run #108" in current
    assert "TankAI Core CI Run #109" in current
    assert "TankAI Core CI Run #110" in current
    assert "TankAI Core CI Run #111" in current
    assert "TankAI Core CI Run #112" in current
    assert "TankAI Core CI Run #113" in current
    assert "TankAI Core CI Run #114" in current
    assert "TankAI Core CI Run #115" in current
    assert "TankAI Core CI Run #117" in current
    assert "TankAI Core CI Run #118" in current
    assert "TankAI Core CI Run #119" in current
    assert "TankAI Core CI Run #120" in current
    assert "TankAI Core CI Run #121" in current
    assert "TankAI Core CI Run #122" in current
    assert "TankAI Core CI Run #123" in current
    assert "TankAI Core CI Run #124" in current
    assert "TankAI Core CI Run #125" in current
    assert "TankAI Core CI Run #126" in current
    assert "TankAI Core CI Run #127" in current
    assert "TankAI Core CI Run #128" in current
    assert "TankAI Core CI Run #129" in current
    assert "TankAI Core CI Run #130" in current
    assert "TankAI Core CI Run #131" in current
    assert "TankAI Core CI Run #132" in current
    assert "TankAI Core CI Run #133" in current
    assert "TankAI Core CI Run #134" in current
    assert "TankAI Core CI Run #135" in current
    assert "TankAI Core CI Run #136" in current
    assert "TankAI Core CI Run #137" in current
    assert "TankAI Core CI Run #138" in current
    assert "TankAI Core CI Run #139" in current
    assert "TankAI Core CI Run #140" in current
    assert "TankAI Core CI Run #141" in current
    assert "TankAI Core CI Run #142" in current
    assert "TankAI Core CI Run #143" in current
    assert "TankAI Core CI Run #144" in current
    assert "TankAI Core CI Run #145" in current
    assert "TankAI Core CI Run #146" in current
    assert "TankAI Core CI Run #147" in current
    assert "TankAI Core CI Run #148" in current
    assert "TankAI Core CI Run #149" in current
    assert "TankAI Core CI Run #150" in current
    assert "TankAI Core CI Run #151" in current
    assert "TankAI Core CI Run #152" in current
    assert "TankAI Core CI Run #153" in current
    assert "TankAI Core CI Run #154" in current
    assert "TankAI Core CI Run #155" in current
    assert "TankAI Core CI Run #156" in current
    assert "TankAI Core CI Run #157" in current
    assert "PR #74" in current
    assert "PR #75" in current
    assert "PR #76" in current
    assert "PR #77" in current
    assert "PR #78" in current
    assert "PR #79" in current
    assert "PR #80" in current
    assert "PR #81" in current
    assert "PR #82" in current
    assert "TankAI Core CI Run #69" in current
    assert "PR #39" in current
    assert "PR #42" in current
    assert "PR #48" in current
    assert "PR #49" in current
    assert "PR #50" in current
    assert "PR #51" in current
    assert "PR #52" in current
    assert "PR #53" in current
    assert "PR #54" in current
    assert "PR #55" in current
    assert "PR #56" in current
    assert "PR #57" in current
    assert "PR #58" in current
    assert "PR #59" in current
    assert "PR #60" in current
    assert "PR #61" in current
    assert "PR #62" in current
    assert "PR #63" in current
    assert "PR #64" in current
    assert "PR #65" in current
    assert "PR #66" in current
    assert "PR #67" in current
    assert "PR #68" in current
    assert "PR #69" in current
    assert "PR #70" in current
    assert "PR #71" in current
    assert "PR #72" in current
    assert "PR #73" in current
    assert (
        "development.external_agent_gateway.v1 -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert (
        "development.external_agent_operator_cli -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert (
        "development.external_agent_job_schema.v1 -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert "development.external_agent_validation_errors.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_job_preflight.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_job_history.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_job_history_limit.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_job_pagination.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_conditional_polling.v1 -> IMPLEMENTED" in current
    assert (
        "development.external_agent_job_list_conditional_polling.v1 -> IMPLEMENTED"
        in current
    )
    assert (
        "development.external_agent_job_list_repository_filter.v1 -> IMPLEMENTED"
        in current
    )
    assert (
        "development.external_agent_job_list_repository_index.v1 -> IMPLEMENTED"
        in current
    )
    assert (
        "development.external_agent_discovery_conditional_get.v1 -> IMPLEMENTED"
        in current
    )
    assert (
        "development.external_agent_job_list_state_filter.v1 -> IMPLEMENTED"
        in current
    )
    assert (
        "development.external_agent_job_list_batch_lookup.v1 -> IMPLEMENTED"
        in current
    )
    assert (
        "development.external_agent_job_list_terminal_filter.v1 -> IMPLEMENTED"
        in current
    )
    assert "development.external_agent_job_state_contract.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_result_receipt.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_error_contract.v1 -> IMPLEMENTED" in current
    assert (
        "development.external_agent_method_error_contract.v1 -> IMPLEMENTED"
        in current
    )
    assert "development.external_agent_idempotency_outcome.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_admission_policy.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_cancel_idempotency.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_retry_contract.v1 -> IMPLEMENTED" in current
    assert "product.cooperative_ai.acceptance_baseline.v1 -> IMPLEMENTED" in current
    assert "product.commander.goal_and_alternatives.v1 -> IMPLEMENTED" in current
    assert "Candidate verbessert die Baseline nicht" in current
    assert "`pilot_eligible=false`" in current
    assert "`pilot_call_limit_met=true`" in current
    assert "security.web.per_response_csp_nonce.v1 -> IMPLEMENTED" in current
    assert "security.web.nonce_bound_inline_styles.v1 -> IMPLEMENTED" in current
    assert "security.web.cross_origin_response_isolation.v1 -> IMPLEMENTED" in current
    assert "security.web.cross_origin_embedder_policy.v1 -> IMPLEMENTED" in current
    assert "security.web.logout_origin_cleanup.v1 -> IMPLEMENTED" in current
    assert "security.web.hsts_policy.v1 -> IMPLEMENTED" in current
    assert "security.web.http_method_rejection.v1 -> IMPLEMENTED" in current
    assert "security.web.http_parser_error_contract.v1 -> IMPLEMENTED" in current
    assert "security.web.request_correlation_id.v1 -> IMPLEMENTED" in current
    assert "ops.ci.node24_action_runtime -> IMPLEMENTED" in current
    assert "ops.cloudflare_toolchain_dependency_security.v1 -> IMPLEMENTED" in current
    assert "ops.cloudflare_toolchain_dependency_security.v2 -> IMPLEMENTED" in current
    assert "development.local_qwen25_coder_runtime -> IMPLEMENTED" in current
    assert "development.local_qwen25_coder_model_integrity -> IMPLEMENTED" in current
    assert (
        "Die repositoryseitigen Vorarbeiten dafür sind durch PRs #26 bis #29 abgeschlossen"
        in current
    )
    assert "Der Rest dieses Gates ist deshalb EXTERN BLOCKIERT" in current
    assert "single_host_runner_bootstrap.readonly_doctor -> IMPLEMENTED" in current
    assert (
        "single_host_runner_bootstrap.configuration_readiness -> IMPLEMENTED"
        in current
    )
    assert "Der konkrete Host und" in current
    assert "bleiben OFFEN" in current
    assert "Für ops.production.live_provider_readiness einen read-only" not in current
