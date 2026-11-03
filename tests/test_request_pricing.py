"""Offline tariffs only. No real provider prices, keys or inference requests."""
from dataclasses import replace
from decimal import localcontext
import json

import pytest

from tankai.core.monetary_budget import BudgetDenied, MonetaryBudget, PilotPolicy
from tankai.core.request_pricing import (
    PricingError, TextRequestBounds, TextTariff, quote_text_request,
)


NOW = 1_790_000_000
OWNER = "owner-test"


def tariff(**changes):
    return replace(TextTariff(
        tariff_id="offline-tariff-v1", provider="fixture", model="fixed-model-v1",
        valid_from=NOW, valid_until=NOW + 7 * 86_400,
        source_url="https://example.test/pricing", billing_currency="EUR",
        input_per_million="1", output_per_million="2", request_fee="0",
        eur_per_currency_unit="1", tax_rate="0", surcharge_rate="0",
        fixed_surcharge_eur="0", fx_source_url=None,
        all_billable_usage_and_fees_verified=True,
    ), **changes)


def bounds(**changes):
    return replace(TextRequestBounds("fixture", "fixed-model-v1", 12_000, 2_048), **changes)


def test_exact_eur_ceiling_and_recorded_bounds():
    quote = quote_text_request(tariff(), bounds(), now=NOW)
    assert quote.maximum_micro_eur == 16_096
    basis = json.loads(quote.billing_basis)
    assert basis["input_token_ceiling"] == 12_000
    assert basis["output_token_ceiling"] == 2_048
    assert basis["billing_currency"] == "EUR"
    assert basis["rounding"] == "ceiling-to-integer-micro-eur"
    assert quote.provider == "fixture"
    assert quote.model == "fixed-model-v1"


def test_usd_fees_tax_and_payment_surcharge_compound_before_upward_rounding():
    quoted = quote_text_request(tariff(
        billing_currency="USD", input_per_million="2", output_per_million="8",
        request_fee="0.001", eur_per_currency_unit="0.95", tax_rate="0.19",
        surcharge_rate="0.03", fixed_surcharge_eur="0.00005",
        fx_source_url="https://example.test/operator-approved-fx-ceiling",
    ), bounds(input_tokens=1_000, output_tokens=500), now=NOW)
    # (.002 + .004 + .001) * .95 * 1.19 * 1.03 + .00005
    # = .008200905 EUR; ceil to micro-EUR is 8201, never 8200.
    assert quoted.maximum_micro_eur == 8_201


def test_nonzero_sub_micro_euro_ceiling_is_never_rounded_down_to_free():
    quoted = quote_text_request(tariff(input_per_million="0.000000001", output_per_million="0"),
                                bounds(input_tokens=1, output_tokens=1), now=NOW)
    assert quoted.maximum_micro_eur == 1


def test_price_is_independent_of_process_decimal_precision():
    configured = tariff(
        billing_currency="USD", input_per_million="2.000000001",
        output_per_million="8.000000001", request_fee="0.000000001",
        eur_per_currency_unit="0.950000001", tax_rate="0.190000001",
        surcharge_rate="0.030000001", fixed_surcharge_eur="0.000000001",
        fx_source_url="https://example.test/fx",
    )
    expected = quote_text_request(configured, bounds(), now=NOW).maximum_micro_eur
    with localcontext() as context:
        context.prec = 2
        assert quote_text_request(configured, bounds(), now=NOW).maximum_micro_eur == expected


def test_zero_price_cannot_claim_an_available_free_quota():
    with pytest.raises(PricingError):
        quote_text_request(tariff(input_per_million="0", output_per_million="0"), bounds(), now=NOW)


def test_excessive_price_does_not_create_an_unbounded_quote():
    configured = tariff(request_fee="1000000", billing_currency="USD",
                        eur_per_currency_unit="10000", tax_rate="1", surcharge_rate="1",
                        fx_source_url="https://example.test/fx")
    with pytest.raises(PricingError):
        quote_text_request(configured, bounds(), now=NOW)


@pytest.mark.parametrize(("field", "value"), [
    ("input_per_million", None), ("output_per_million", True),
    ("request_fee", 0.01), ("tax_rate", "unknown"),
    ("surcharge_rate", "NaN"), ("eur_per_currency_unit", "Infinity"),
    ("request_fee", "1e-9"), ("fixed_surcharge_eur", "-0.01"),
    ("input_per_million", "1000001"), ("tax_rate", "1.01"),
    ("surcharge_rate", "1.01"), ("request_fee", "0.0000000001"),
    ("eur_per_currency_unit", "0"), ("eur_per_currency_unit", "1.01"),
    ("billing_currency", "GBP"), ("billing_currency", "eur"), ("billing_currency", []),
    ("fx_source_url", "https://example.test/unexpected-fx"),
    ("all_billable_usage_and_fees_verified", False),
    ("all_billable_usage_and_fees_verified", 1),
    ("model", "auto:tankai"), ("model", "model-latest"),
    ("model", "default"), ("provider", "bad\nprovider"),
    ("valid_until", NOW), ("valid_from", True),
    ("source_url", "https://user:secret@example.test/pricing"),
    ("source_url", "https://example.test/pricing?key=secret"),
    ("source_url", "https://example.test:999999/pricing"),
])
def test_unknown_ambiguous_or_incomplete_tariff_is_rejected(field, value):
    with pytest.raises(PricingError):
        tariff(**{field: value})


@pytest.mark.parametrize("fx", [None, "", "http://example.test/fx", "https://example.test/fx#value"])
def test_usd_conversion_requires_an_explicit_safe_source(fx):
    with pytest.raises(PricingError):
        tariff(billing_currency="USD", fx_source_url=fx)


@pytest.mark.parametrize(("field", "value"), [
    ("input_tokens", None), ("input_tokens", True), ("input_tokens", 12_001),
    ("input_tokens", 0), ("input_tokens", 1.5), ("output_tokens", "2048"),
    ("output_tokens", 2_049), ("output_tokens", -1), ("model", "auto"),
])
def test_incomplete_or_excessive_request_bounds_are_rejected(field, value):
    with pytest.raises(PricingError):
        bounds(**{field: value})


@pytest.mark.parametrize("now", [NOW - 1, NOW + 7 * 86_400, True, 1.5])
def test_stale_future_or_ambiguous_time_does_not_produce_a_quote(now):
    with pytest.raises(PricingError):
        quote_text_request(tariff(), bounds(), now=now)


@pytest.mark.parametrize("changes", [{"provider": "other"}, {"model": "another-fixed-model"}])
def test_quote_is_bound_to_exact_provider_and_model(changes):
    with pytest.raises(PricingError):
        quote_text_request(tariff(), bounds(**changes), now=NOW)


def test_main_and_critic_quotes_reserve_against_same_job_ceiling(tmp_path):
    clock = lambda: NOW
    configured = PilotPolicy(OWNER, NOW, NOW + 86_400)
    main_tariff = tariff(input_per_million="10", output_per_million="0")
    critic_tariff = replace(main_tariff, provider="critic-fixture", model="critic-model-v1")
    main_quote = quote_text_request(main_tariff, bounds(), now=NOW)
    critic_quote = quote_text_request(critic_tariff, bounds(provider="critic-fixture", model="critic-model-v1"), now=NOW)
    with MonetaryBudget.create(tmp_path / "budget.sqlite", configured, clock=clock) as ledger:
        ledger.begin_job("job", owner_id=OWNER)
        admitted = ledger.reserve("job", "main", owner_id=OWNER, quote=main_quote)
        assert admitted.dispatch_allowed and admitted.reserved_micro_eur == 120_000
        with pytest.raises(BudgetDenied, match="job_budget"):
            ledger.reserve("job", "critic", owner_id=OWNER, quote=critic_quote)
        assert ledger.snapshot()["attempts"] == 1


def test_generated_quote_and_unknown_reservation_survive_restart(tmp_path):
    path = tmp_path / "budget.sqlite"
    configured = PilotPolicy(OWNER, NOW, NOW + 86_400)
    quoted = quote_text_request(tariff(), bounds(), now=NOW)
    with MonetaryBudget.create(path, configured, clock=lambda: NOW) as ledger:
        ledger.begin_job("job", owner_id=OWNER)
        assert ledger.reserve("job", "request", owner_id=OWNER, quote=quoted).dispatch_allowed
        ledger.settle("request", owner_id=OWNER, actual_micro_eur=None)
    with MonetaryBudget(path, clock=lambda: NOW) as restarted:
        assert restarted.snapshot()["held_micro_eur"] == 16_096
        assert not restarted.reserve("job", "request", owner_id=OWNER, quote=quoted).dispatch_allowed


def test_missing_tariff_or_bounds_are_never_inferred():
    for configured, limits in [(None, bounds()), (tariff(), None)]:
        with pytest.raises(PricingError):
            quote_text_request(configured, limits, now=NOW)


def test_quote_metadata_stays_within_the_existing_persistent_ledger_limit():
    configured = tariff(billing_currency="USD", fx_source_url="https://example.test/" + "a" * 1_980)
    with pytest.raises(PricingError, match="metadata"):
        quote_text_request(configured, bounds(), now=NOW)
