"""Explicit owner/workspace binding for the existing shared monetary guard.

Only trusted startup code supplies this configuration; HTTP input and ordinary
environment flags never create/renew budgets, price facts or token counters.
No automatic recovery closes an active job or assumes upstream cancellation.
"""
from __future__ import annotations

from contextlib import contextmanager
import time
from types import MappingProxyType
from typing import Callable, Iterator, Mapping
from uuid import UUID, uuid4

from tankai.core.llm import AnthropicLLM, OpenAILLM
from tankai.core.monetary_budget import MonetaryBudget
from tankai.core.request_dispatch import DispatchDenied, FrozenTextRequest, MonetaryDispatch
from tankai.core.request_pricing import TextTariff
from tankai.core.supplied_sources import ApprovedSourceBundle
from tankai.web.auth import AuthContext, AuthStore
from tankai.web.source_manifest import SourceManifestDenied, WorkspaceSourceManifest


class OwnerPilotForbidden(PermissionError):
    """This current authenticated identity/workspace cannot spend this pilot."""


class OwnerPilot:
    def __init__(self, ledger: MonetaryBudget, *, tenant_id: str,
                 workspace_ids: frozenset[str],
                 tariffs: Mapping[tuple[str, str], TextTariff],
                 endpoints: Mapping[tuple[str, str], str],
                 input_token_bound: Callable[[FrozenTextRequest], int],
                 clock: Callable[[], int] | None = None,
                 source_manifests: tuple[WorkspaceSourceManifest, ...] = ()) -> None:
        if (type(ledger) is not MonetaryBudget or type(workspace_ids) is not frozenset
                or not 1 <= len(workspace_ids) <= 32):
            raise DispatchDenied("explicit shared ledger and approved workspaces required")
        for value in (tenant_id, ledger.policy.owner_id, *workspace_ids):
            try:
                if type(value) is not str or str(UUID(value)) != value:
                    raise ValueError
            except ValueError as exc:
                raise DispatchDenied("canonical server-side pilot identity required") from exc
        self.ledger = ledger
        self.tenant_id, self.workspace_ids = tenant_id, workspace_ids
        self._tariffs, self._endpoints = dict(tariffs), dict(endpoints)
        self._counter, self._clock = input_token_bound, clock
        if type(source_manifests) is not tuple or len(source_manifests) > 32:
            raise DispatchDenied("invalid source manifest collection")
        approved = {}
        for manifest in source_manifests:
            if (type(manifest) is not WorkspaceSourceManifest
                    or manifest.owner_id != ledger.policy.owner_id or manifest.tenant_id != tenant_id
                    or manifest.workspace_id not in workspace_ids or manifest.workspace_id in approved
                    or manifest.valid_from < ledger.policy.starts_at
                    or manifest.valid_until > ledger.policy.expires_at):
                raise DispatchDenied("source manifest does not match pilot scope")
            approved[manifest.workspace_id] = manifest
        self._sources = MappingProxyType(approved)
        # Validate configuration without creating an active job or permission.
        self._guard("configuration-check")

    def _guard(self, job_id: str, authorize: Callable[[], None] | None = None) -> MonetaryDispatch:
        return MonetaryDispatch(self.ledger, owner_id=self.ledger.policy.owner_id, job_id=job_id,
            tariffs=self._tariffs, endpoints=self._endpoints, input_token_bound=self._counter,
            clock=self._clock, authorize=authorize)

    def authorize(self, context: AuthContext, auth: AuthStore) -> None:
        if (type(context) is not AuthContext or context.user_id != self.ledger.policy.owner_id
                or context.tenant_id != self.tenant_id or context.workspace_id not in self.workspace_ids
                or context.role != "owner"):
            raise OwnerPilotForbidden("Besitzerpilot ist für diesen Zugang nicht freigegeben")
        current = auth.resolve_session_by_id(context.session_id)
        if current is None or (current.user_id, current.tenant_id, current.workspace_id, current.role) != (
                context.user_id, context.tenant_id, context.workspace_id, context.role):
            raise OwnerPilotForbidden("Anmeldung ist nicht mehr aktuell")
        access = auth.workspace_access(context.user_id, context.workspace_id)
        if access is None or access.tenant_id != self.tenant_id or access.role != "owner":
            raise OwnerPilotForbidden("Besitzerberechtigung ist nicht mehr gültig")
        self._current_sources(context)

    def _current_sources(self, context: AuthContext) -> ApprovedSourceBundle:
        manifest = self._sources.get(context.workspace_id)
        if manifest is None:
            raise OwnerPilotForbidden("Für diesen Arbeitsraum fehlen freigegebene Quellen")
        try:
            now = self._clock() if self._clock is not None else int(time.time())
        except Exception:
            raise DispatchDenied("source approval clock unavailable") from None
        if type(now) is not int or now < 0:
            raise DispatchDenied("source approval clock unavailable")
        try:
            return manifest.select(owner_id=context.user_id, tenant_id=context.tenant_id,
                                   workspace_id=context.workspace_id, now=now)
        except SourceManifestDenied:
            raise OwnerPilotForbidden("Quellenfreigabe ist derzeit nicht gültig") from None

    def sources_for(self, context: AuthContext, auth: AuthStore) -> ApprovedSourceBundle:
        self.authorize(context, auth)
        return self._current_sources(context)

    @contextmanager
    def scope(self, context: AuthContext, auth: AuthStore, tank) -> Iterator[str]:
        """Called while holding a workspace lease; SQLite admits one global job.

        A new server-generated ID is used every time. An interrupted active job
        therefore blocks the next admission, also after an application restart.
        No browser-supplied ID, elapsed timeout or normal clock expiry resumes it.
        """
        self.authorize(context, auth)
        if tank.llm_call_budget.monetary_dispatch is not None:
            raise DispatchDenied("a monetary context is already attached")
        for wrapper in (tank.llm, tank.critic_llm):
            if type(wrapper.delegate) not in (OpenAILLM, AnthropicLLM):
                raise DispatchDenied("pilot requires reviewed provider adapters")
            wrapper.delegate.requires_request_admission = True
        job_id = f"{context.tenant_id}:{context.workspace_id}:{uuid4()}"
        dispatch = self._guard(job_id, lambda: self.authorize(context, auth))
        for wrapper in (tank.llm, tank.critic_llm):
            adapter = wrapper.delegate
            dispatch.check_target(provider=adapter.provider_name, model=adapter.model,
                                  endpoint=str(adapter.client.base_url))
        self.authorize(context, auth)
        created = self.ledger.begin_job(job_id, owner_id=context.user_id)
        if not created.created:
            raise DispatchDenied("existing job IDs cannot be resumed by HTTP admission")
        tank.llm_call_budget.monetary_dispatch = dispatch
        try:
            yield job_id
        finally:
            tank.llm_call_budget.monetary_dispatch = None
            # Even when Core catches a provider exception, an uncertain outcome
            # keeps the durable active-job slot. Only separate operator review
            # can resolve it. Successful calls still retain their monetary holds.
            if not dispatch.uncertain_upstream:
                self.ledger.close_job(job_id, owner_id=context.user_id)

    def status(self, job_id: str) -> dict:
        summary = self.ledger.snapshot()
        return {"job_id": job_id, "scope": "shared-owner-pilot",
                "confirmed_micro_eur": summary["confirmed_micro_eur"],
                "held_micro_eur": summary["held_micro_eur"],
                "remaining_total_micro_eur": summary["remaining_total_micro_eur"],
                "active_jobs": summary["active_jobs"], "halt_reason": summary["halt_reason"]}
