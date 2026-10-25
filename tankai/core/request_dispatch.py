"""Opt-in monetary admission immediately around one bounded SDK invocation.

The default web runtime does not activate this guard. Trusted application code
must bind it to the current authenticated owner and a server-created job.
Trusted configuration must provide independently verified tariffs and an
OFFLINE complete-request token-bound function before any admission is possible.
No default price, token estimate, credential, network client or retry exists here.
SDK/transport configuration and actual upstream billing remain trust boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import math
import time
import threading
from typing import Any, Callable, Mapping, TypeVar
from urllib.parse import urlsplit

from .monetary_budget import MonetaryBudget
from .request_pricing import TextRequestBounds, TextTariff, quote_text_request


T = TypeVar("T")
MAX_TEXT_BYTES = 128_000


class DispatchDenied(RuntimeError):
    """No request may be sent after an admission failure."""


def _endpoint(value: str) -> str:
    if type(value) is not str or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise DispatchDenied("invalid approved endpoint")
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        parsed.port
        if parsed.scheme != "https":
            if parsed.scheme != "http" or not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError
    except ValueError as exc:
        raise DispatchDenied("invalid approved endpoint") from exc
    return value.rstrip("/")


@dataclass(frozen=True)
class FrozenTextRequest:
    """Exact final SDK parameters for the trusted offline counter; text is hidden."""
    provider: str
    model: str
    endpoint: str
    _canonical_json: str = field(repr=False)

    def parameters(self) -> dict:
        # A counter cannot change the data later passed to the SDK.
        return json.loads(self._canonical_json)


def _freeze(provider: str, model: str, endpoint: str, parameters: dict) -> FrozenTextRequest:
    if type(parameters) is not dict or provider not in {"openai", "anthropic"}:
        raise DispatchDenied("unsupported text request")
    expected = {"model", "messages", "max_tokens"}
    if provider == "openai" or "temperature" in parameters:
        expected.add("temperature")
    if provider == "anthropic":
        expected.add("system")
    if set(parameters) != expected or parameters.get("model") != model:
        raise DispatchDenied("unexpected or mismatched request fields")
    output = parameters["max_tokens"]
    if type(output) is not int or not 1 <= output <= 2048:
        raise DispatchDenied("invalid output token ceiling")
    if "temperature" in parameters:
        temperature = parameters["temperature"]
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise DispatchDenied("invalid temperature")
    messages = parameters["messages"]
    expected_roles = (["user"] if provider == "anthropic" else None)
    if type(messages) is not list or not 1 <= len(messages) <= 2:
        raise DispatchDenied("unbounded messages")
    roles = []
    size = 0
    for message in messages:
        if type(message) is not dict or set(message) != {"role", "content"}:
            raise DispatchDenied("unsupported message fields")
        if type(message["content"]) is not str or len(message["content"]) > MAX_TEXT_BYTES:
            raise DispatchDenied("non-text or oversized message")
        roles.append(message["role"])
        size += len(message["content"])
    if ((expected_roles is not None and roles != expected_roles)
            or (expected_roles is None and roles not in (["user"], ["system", "user"]))):
        raise DispatchDenied("unsupported message roles")
    if provider == "anthropic":
        system = parameters["system"]
        if type(system) is not str or len(system) > MAX_TEXT_BYTES:
            raise DispatchDenied("non-text or oversized system message")
        size += len(system)
    if size > MAX_TEXT_BYTES:
        raise DispatchDenied("oversized complete text request")
    try:
        encoded = json.dumps(parameters, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_TEXT_BYTES:
            raise DispatchDenied("oversized serialized request")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DispatchDenied("invalid serialized request") from exc
    return FrozenTextRequest(provider, model, endpoint, encoded)


class MonetaryDispatch:
    """One owner/job context shared by main and critic; no implicit job creation.

    Endpoints and tariffs come from trusted operator configuration. The counter
    must conservatively cover ALL billable input/framing for the selected model.
    Reported usage is never assumed to be a confirmed charge: every dispatched
    attempt retains its full reservation until separate trusted reconciliation.
    """
    def __init__(self, ledger: MonetaryBudget, *, owner_id: str, job_id: str,
                 tariffs: Mapping[tuple[str, str], TextTariff],
                 endpoints: Mapping[tuple[str, str], str],
                 input_token_bound: Callable[[FrozenTextRequest], int],
                 clock: Callable[[], int] | None = None,
                 authorize: Callable[[], None] | None = None) -> None:
        if type(ledger) is not MonetaryBudget or not callable(input_token_bound):
            raise DispatchDenied("initialized ledger and trusted input counter required")
        if authorize is not None and not callable(authorize):
            raise DispatchDenied("invalid trusted authorizer")
        if owner_id != ledger.policy.owner_id or type(job_id) is not str or not job_id:
            raise DispatchDenied("approved owner/job context required")
        if not tariffs or set(tariffs) != set(endpoints):
            raise DispatchDenied("matching explicit tariffs and endpoints required")
        for identity, tariff in tariffs.items():
            if (type(tariff) is not TextTariff
                    or identity != (tariff.provider, tariff.model)
                    or tariff.provider not in {"openai", "anthropic"}):
                raise DispatchDenied("invalid tariff identity")
        self.ledger, self.owner_id, self.job_id = ledger, owner_id, job_id
        self._tariffs = dict(tariffs)
        self._endpoints = {key: _endpoint(value) for key, value in endpoints.items()}
        self._input_token_bound = input_token_bound
        self._clock = clock or (lambda: int(time.time()))
        self._authorize = authorize
        self._uncertain_upstream = threading.Event()
        self._send_lock = threading.Lock()

    @property
    def uncertain_upstream(self) -> bool:
        """An SDK failure may have left upstream work running; never auto-clear."""
        return self._uncertain_upstream.is_set()

    def check_target(self, *, provider: str, model: str, endpoint: str) -> None:
        """Preflight both main and critic before admitting a web job; no spend."""
        identity = (provider, model)
        if identity not in self._tariffs or _endpoint(endpoint) != self._endpoints[identity]:
            raise DispatchDenied("unapproved provider/model/endpoint")
        tariff = self._tariffs[identity]
        now = self._clock()
        if type(now) is not int or not tariff.valid_from <= now < tariff.valid_until:
            raise DispatchDenied("approved target tariff is not current")

    def execute(self, *, attempt_id: str, provider: str, model: str, endpoint: str,
                parameters: dict, sdk_max_retries: int, timeout_seconds: float,
                send_once: Callable[[dict], T]) -> T:
        # Serializing this job's sends also prevents an already queued sibling
        # from proceeding after an uncertain failure. SQLite still coordinates
        # reservations made by independent connections/processes.
        with self._send_lock:
            if self.uncertain_upstream:
                raise DispatchDenied("uncertain upstream outcome; further job sends blocked")
            return self._execute_locked(
                attempt_id=attempt_id, provider=provider, model=model, endpoint=endpoint,
                parameters=parameters, sdk_max_retries=sdk_max_retries,
                timeout_seconds=timeout_seconds, send_once=send_once)

    def _execute_locked(self, *, attempt_id: str, provider: str, model: str, endpoint: str,
                        parameters: dict, sdk_max_retries: int, timeout_seconds: float,
                        send_once: Callable[[dict], T]) -> T:
        if type(sdk_max_retries) is not int or sdk_max_retries != 0:
            raise DispatchDenied("internal SDK retries must be disabled")
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 30 or not callable(send_once)):
            raise DispatchDenied("bounded SDK invocation required")
        identity = (provider, model)
        self.check_target(provider=provider, model=model, endpoint=endpoint)
        request = _freeze(provider, model, self._endpoints[identity], parameters)
        input_ceiling = self._input_token_bound(request)
        bounds = TextRequestBounds(provider, model, input_ceiling,
                                   request.parameters()["max_tokens"])
        quote = quote_text_request(self._tariffs[identity], bounds, now=self._clock())
        # A slow offline counter must not preserve a revoked session/role.
        if self._authorize is not None:
            self._authorize()
        reservation = self.ledger.reserve(self.job_id, attempt_id,
                                          owner_id=self.owner_id, quote=quote)
        if not reservation.dispatch_allowed:
            raise DispatchDenied("attempt already admitted; automatic replay is forbidden")
        try:
            # Recheck after the durable commit too, immediately before sending.
            # Denial here keeps the hold and blocks this uncertain job context.
            if self._authorize is not None:
                self._authorize()
            return send_once(request.parameters())
        except BaseException:
            self._uncertain_upstream.set()
            raise
        finally:
            # Includes timeouts, malformed responses, cancellation and a normal
            # response with usage. Tokens alone are not confirmed monetary cost.
            try:
                self.ledger.settle(attempt_id, owner_id=self.owner_id, actual_micro_eur=None)
            except BaseException:
                self._uncertain_upstream.set()
                raise


@dataclass(frozen=True)
class BoundDispatch:
    guard: MonetaryDispatch
    attempt_id: str

    def execute(self, **kwargs: Any) -> Any:
        return self.guard.execute(attempt_id=self.attempt_id, **kwargs)
