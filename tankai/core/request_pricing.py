"""Exact, offline price ceilings for the existing owner-pilot reservation ledger.

Tariffs are trusted operator inputs, never values supplied by a model or client.
This module neither discovers prices nor proves their completeness. The operator
must verify the source, billing currency, taxes, FX ceiling and every surcharge.
The adapter must establish token ceilings for its COMPLETE outbound text request,
including system messages/history/tools/framing, and enforce its output ceiling.
Reasoning or other billed tokens must be covered by those ceilings and rates.
Unbounded tools, non-text modalities, routing aliases and unknown charges require
a different reviewed contract; they must not be priced as ordinary text here.

No SDK, credential, network request, provider activation or automatic settlement
is involved. A valid quote alone never authorizes dispatch or a paid pilot.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import json
import re
from urllib.parse import urlsplit

from .monetary_budget import MAX_AMOUNT, MAX_TIMESTAMP, PriceQuote


_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,9})?\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,159}\Z")


class PricingError(ValueError):
    """A complete conservative price ceiling cannot be established; do not send."""


def _number(value: str, name: str, *, maximum: int, positive: bool = False) -> Fraction:
    # Exact finite decimal strings only: no floats, NaN, infinities or exponents.
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise PricingError(f"explicit finite decimal string required for {name}")
    parsed = Fraction(Decimal(value))
    if parsed > maximum or (positive and parsed <= 0):
        raise PricingError(f"invalid {name}")
    return parsed


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PricingError(f"invalid {name}")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise PricingError(f"invalid {name}")
    return value


def _source(url: str, name: str) -> None:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise PricingError(f"invalid {name}")
    try:
        source = urlsplit(url)
        if (source.scheme != "https" or not source.hostname or source.username
                or source.password or source.query or source.fragment):
            raise PricingError(f"invalid {name}")
        source.port  # Reject malformed or out-of-range URL ports as well.
    except ValueError as exc:
        raise PricingError(f"invalid {name}") from exc


def _model(value: str) -> None:
    _identifier(value, "model")
    if value.lower().split(":", 1)[0] in {"auto", "latest", "default"} or value.lower().endswith("-latest"):
        raise PricingError("routing aliases do not establish a priced model")


@dataclass(frozen=True)
class TextRequestBounds:
    """Trusted adapter-proven ceilings, not estimates supplied by a model.

    The adapter integration must count all billable input and constrain
    all billable output. Instantiating this value is not a token-count proof.
    """
    provider: str
    model: str
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        _identifier(self.provider, "provider")
        _model(self.model)
        _integer(self.input_tokens, "complete input token ceiling", 1, 12_000)
        _integer(self.output_tokens, "complete output token ceiling", 1, 2_048)


@dataclass(frozen=True)
class TextTariff:
    """Explicit operator-verified text tariff; every zero must be deliberate.

    Rates and request_fee are in billing_currency. Taxes and percentage payment
    surcharges compound conservatively; fixed_surcharge_eur is an additional
    all-in per-request ceiling. For a tariff that already includes taxes, the
    operator explicitly sets tax_rate='0' after verifying that treatment.

    EUR ceiling = ((input/1e6 * input_per_million + output/1e6 *
    output_per_million + request_fee) * eur_per_currency_unit *
    (1 + tax_rate) * (1 + surcharge_rate)) + fixed_surcharge_eur.
    """
    tariff_id: str
    provider: str
    model: str
    valid_from: int
    valid_until: int
    source_url: str
    billing_currency: str
    input_per_million: str
    output_per_million: str
    request_fee: str
    eur_per_currency_unit: str
    tax_rate: str
    surcharge_rate: str
    fixed_surcharge_eur: str
    fx_source_url: str | None
    all_billable_usage_and_fees_verified: bool

    def __post_init__(self) -> None:
        _identifier(self.tariff_id, "tariff_id")
        _identifier(self.provider, "provider")
        _model(self.model)
        _integer(self.valid_from, "valid_from", 0, MAX_TIMESTAMP)
        _integer(self.valid_until, "valid_until", 0, MAX_TIMESTAMP)
        if self.valid_until <= self.valid_from:
            raise PricingError("tariff validity must be positive")
        _source(self.source_url, "tariff source")
        if type(self.billing_currency) is not str or self.billing_currency not in {"EUR", "USD"}:
            raise PricingError("billing currency is not supported by this contract")
        for name in ("input_per_million", "output_per_million", "request_fee", "fixed_surcharge_eur"):
            _number(getattr(self, name), name, maximum=1_000_000)
        for name in ("tax_rate", "surcharge_rate"):
            _number(getattr(self, name), name, maximum=1)
        fx = _number(self.eur_per_currency_unit, "EUR conversion ceiling", maximum=10_000, positive=True)
        if self.billing_currency == "EUR":
            if fx != 1 or self.fx_source_url is not None:
                raise PricingError("EUR billing requires conversion 1 and no FX source")
        else:
            _source(self.fx_source_url, "FX ceiling source")
        if self.all_billable_usage_and_fees_verified is not True:
            raise PricingError("operator verification of complete billing coverage is required")


def quote_text_request(tariff: TextTariff, bounds: TextRequestBounds, *, now: int) -> PriceQuote:
    """Return an exact upward-rounded ceiling, usable by MonetaryBudget.reserve.

    No tariff or quota is fetched or inferred. Zero-price quotes are refused:
    an apparent free rate does not prove an available, non-billing free quota.
    The returned PriceQuote retains limits and components without request text.
    """
    if type(tariff) is not TextTariff or type(bounds) is not TextRequestBounds:
        raise PricingError("explicit reviewed tariff and request bounds required")
    _integer(now, "current UTC timestamp", 0, MAX_TIMESTAMP)
    if not tariff.valid_from <= now < tariff.valid_until:
        raise PricingError("tariff is not current")
    if (tariff.provider, tariff.model) != (bounds.provider, bounds.model):
        raise PricingError("priced provider/model does not match the bounded request")
    rate = lambda name: Fraction(Decimal(getattr(tariff, name)))
    billed = (bounds.input_tokens * rate("input_per_million") / 1_000_000
              + bounds.output_tokens * rate("output_per_million") / 1_000_000
              + rate("request_fee"))
    euros = (billed * rate("eur_per_currency_unit") * (1 + rate("tax_rate"))
             * (1 + rate("surcharge_rate")) + rate("fixed_surcharge_eur"))
    micros = euros * 1_000_000
    ceiling = (micros.numerator + micros.denominator - 1) // micros.denominator
    if not 1 <= ceiling <= MAX_AMOUNT:
        raise PricingError("zero or excessive price ceiling; no dispatch authorization")
    basis = {
        "method": "all-in-text-upper-bound-v1",
        "input_token_ceiling": bounds.input_tokens,
        "output_token_ceiling": bounds.output_tokens,
        "billing_currency": tariff.billing_currency,
        **{name: getattr(tariff, name) for name in (
            "input_per_million", "output_per_million", "request_fee", "eur_per_currency_unit",
            "tax_rate", "surcharge_rate", "fixed_surcharge_eur")},
        "fx_source_url": tariff.fx_source_url,
        "rounding": "ceiling-to-integer-micro-eur",
    }
    try:
        return PriceQuote(tariff.tariff_id, tariff.provider, tariff.model,
                          tariff.valid_from, tariff.valid_until, ceiling,
                          tariff.source_url, json.dumps(basis, sort_keys=True, separators=(",", ":")))
    except ValueError as exc:
        raise PricingError("quote metadata exceeds the ledger contract") from exc
