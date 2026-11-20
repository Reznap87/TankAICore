"""Environment-driven construction of the closed-pilot money admission.

Fail-closed by design: ohne vollstaendig gesetzte ``TANKAI_PILOT_*``-Werte wird
kein :class:`OwnerPilot` gebaut und die monetare Zulassung bleibt aus. Die
Tages-/Gesamtbudgets und die Laufzeit duerfen nicht automatisch vergeben werden
- sie muessen explizit gesetzt sein, sonst bleibt der Pilot deaktiviert.

OpenRouter-Free und Geldpilot sind strikt getrennte Betriebsmodi:
``AppContext._from_env_unlocked`` lehnt beide gemeinsam ab.

Ohne ``TANKAI_PILOT_APPROVALS_JSON`` sind Tarife und Endpunkte leer. Der Pilot
ist dann aktiv, lehnt aber jeden bezahlten Aufruf ab (fail-closed).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Mapping

from tankai.core.monetary_budget import MonetaryBudget, PilotPolicy
from tankai.core.request_dispatch import FrozenTextRequest
from tankai.core.request_pricing import TextTariff
from tankai.web.owner_pilot import OwnerPilot

#: Identitaet des Piloten. Alle drei muessen gesetzt sein.
IDENTITY_KEYS = (
    "TANKAI_PILOT_OWNER_ID",
    "TANKAI_PILOT_TENANT_ID",
    "TANKAI_PILOT_WORKSPACE_IDS",
)

#: Geldpolitics. Kein automatischer Default - alles muss explizit gesetzt sein.
POLICY_KEYS = (
    "TANKAI_PILOT_STARTS_AT",
    "TANKAI_PILOT_EXPIRES_AT",
    "TANKAI_PILOT_TOTAL_MICRO_EUR",
    "TANKAI_PILOT_DAILY_MICRO_EUR",
)

#: Persistenter Ledger. Liegt auf dem Datenvolumen, damit ein Neustart die
#: Reservierungen und Zaehler erhaelt.
LEDGER_KEY = "TANKAI_PILOT_LEDGER_PATH"

#: Explizit freigegebene Tarife/Endpunkte - Pflicht, siehe REQUIRED_KEYS.
APPROVALS_KEY = "TANKAI_PILOT_APPROVALS_JSON"

#: Pflichtwerte. ``APPROVALS_KEY`` gehoert dazu, weil ``OwnerPilot`` ohne
#: mindestens ein freigegebenes Tarif/Endpoint-Paar nicht existiert
#: (``MonetaryDispatch`` verlangt ``tariffs`` mit gleicher Schlusselmenge wie
#: ``endpoints``). Keine Freigabe also: kein Pilot und kein bezahlter Aufruf.
REQUIRED_KEYS = IDENTITY_KEYS + POLICY_KEYS + (LEDGER_KEY, APPROVALS_KEY)


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(f"[pilot] {message}")


def _text(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _integer(env: Mapping[str, str], key: str) -> int:
    raw = _text(env, key)
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{key} ist keine ganze Zahl: {raw!r}") from exc
    return value


def _workspaces(env: Mapping[str, str]) -> frozenset[str]:
    raw = _text(env, "TANKAI_PILOT_WORKSPACE_IDS")
    items = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        raise ValueError("TANKAI_PILOT_WORKSPACE_IDS ist leer")
    return items


def _policy(env: Mapping[str, str]) -> PilotPolicy:
    """Explizite Policy - keine Defaults, keine Ableitung."""
    return PilotPolicy(
        owner_id=_text(env, "TANKAI_PILOT_OWNER_ID"),
        starts_at=_integer(env, "TANKAI_PILOT_STARTS_AT"),
        expires_at=_integer(env, "TANKAI_PILOT_EXPIRES_AT"),
        total_micro_eur=_integer(env, "TANKAI_PILOT_TOTAL_MICRO_EUR"),
        daily_micro_eur=_integer(env, "TANKAI_PILOT_DAILY_MICRO_EUR"),
    )


def _ledger(path: Path, policy: PilotPolicy) -> MonetaryBudget:
    """Oeffnet einen vorhandenen Ledger oder initialisiert ihn exklusiv.

    Ein vorhandener Ledger mit abweichender Policy wird nicht umgeschrieben;
    er wird zuruckgewiesen, damit ein Neustart die einmalige Freigabe bewahrt.
    """
    path = path.absolute()
    if path.exists():
        ledger = MonetaryBudget(path)
        if ledger.policy != policy:
            raise ValueError("TANKAI_PILOT_LEDGER_PATH hat eine abweichende Policy")
        return ledger
    return MonetaryBudget.create(path, policy)


def _approvals(raw: str) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], TextTariff]]:
    """Wandelt das Freigabedokument in Endpunkte und Tarife um.

    Format: ``{"provider:model": {"endpoint": ..., "tariff": {...}}}``.
    Jede Angabe ist explizit; es gibt keine abgeleiteten Freigaben.
    """
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Freigabedokument muss ein Objekt sein")
    endpoints: dict[tuple[str, str], str] = {}
    tariffs: dict[tuple[str, str], TextTariff] = {}
    for key, entry in document.items():
        provider, _, model = str(key).partition(":")
        if not provider or not model or not isinstance(entry, dict):
            raise ValueError(f"ungueltiger Freigabeschluessel: {key!r}")
        endpoint = entry.get("endpoint")
        tariff = entry.get("tariff")
        if not isinstance(endpoint, str) or not isinstance(tariff, dict):
            raise ValueError(f"Freigabe {key!r} braucht endpoint und tariff")
        identity = (provider, model)
        if identity in endpoints:
            raise ValueError(f"doppelte Freigabe: {key!r}")
        endpoints[identity] = endpoint
        tariffs[identity] = TextTariff(
            tariff_id=str(tariff["tariff_id"]),
            provider=provider,
            model=model,
            valid_from=int(tariff["valid_from"]),
            valid_until=int(tariff["valid_until"]),
            source_url=str(tariff["source_url"]),
            billing_currency=str(tariff["billing_currency"]),
            input_per_million=str(tariff["input_per_million"]),
            output_per_million=str(tariff["output_per_million"]),
            request_fee=str(tariff["request_fee"]),
            eur_per_currency_unit=str(tariff["eur_per_currency_unit"]),
            tax_rate=str(tariff["tax_rate"]),
            surcharge_rate=str(tariff["surcharge_rate"]),
            fixed_surcharge_eur=str(tariff["fixed_surcharge_eur"]),
            fx_source_url=tariff.get("fx_source_url"),
            all_billable_usage_and_fees_verified=bool(
                tariff["all_billable_usage_and_fees_verified"]
            ),
        )
    return endpoints, tariffs


def _input_token_bound(request: FrozenTextRequest) -> int:
    """Konservativer Offline-Obergrenzenzaehler (Zeichen/4, Minimum 1)."""
    total = 0

    def walk(value: object) -> None:
        nonlocal total
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    try:
        walk(request.parameters())
    except Exception:
        return 1
    return max(1, total // 4 + 1)


def load_owner_pilot_from_env(
    *,
    environ: Mapping[str, str] | None = None,
    log: Callable[[str], None] | None = None,
) -> OwnerPilot | None:
    """Baut den Geldpiloten aus der Umgebung oder bleibt fail-closed.

    Ohne ``TANKAI_PILOT_OWNER_ID`` ist kein Pilot konfiguriert - das ist der
    Normalzustand und liefert ``None``. Sind einzelne ``TANKAI_PILOT_*``-Werte
    gesetzt, aber unvollstaendig, bleibt die Zulassung ebenfalls aus.
    """
    env = os.environ if environ is None else environ
    present = {key for key in REQUIRED_KEYS if _text(env, key)}
    if not present:
        return None
    missing = [key for key in REQUIRED_KEYS if key not in present]
    if missing:
        _warn(log, "unvollstaendig (" + ", ".join(missing) + ") - Zulassung bleibt aus")
        return None
    try:
        policy = _policy(env)
        ledger = _ledger(Path(_text(env, LEDGER_KEY)), policy)
        endpoints, tariffs = _approvals(_text(env, APPROVALS_KEY))
        return OwnerPilot(
            ledger,
            tenant_id=_text(env, "TANKAI_PILOT_TENANT_ID"),
            workspace_ids=_workspaces(env),
            tariffs=tariffs,
            endpoints=endpoints,
            input_token_bound=_input_token_bound,
        )
    except Exception as error:
        _warn(log, f"ungueltig ({type(error).__name__}: {error}) - Zulassung bleibt aus")
        return None
