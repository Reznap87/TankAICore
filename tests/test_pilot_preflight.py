"""Owner-only local preflight, with zero provider sends or budget mutations."""
import pytest

from tankai.core.monetary_budget import BudgetDenied, MonetaryBudget
from test_monetary_budget import Clock, OWNER, NOW, begin, policy, reserve
from test_request_dispatch import ENDPOINTS, sdks, tariff
from test_web_owner_pilot import (
    accounts, ledger, provider_factory, pilot, serving, login, calls, source_grant,
)
from test_web_server import Client


def test_admission_inspection_leaves_the_entire_ledger_unchanged(tmp_path):
    clock = Clock()
    with MonetaryBudget.create(tmp_path / 'pilot.sqlite', policy(), clock=clock) as budget:
        before = budget.export_snapshot()
        clock.now += 30
        result = budget.admission_status(owner_id=OWNER)
        assert result['checked_at'] == clock.now
        assert result['blockers'] == []
        assert result['dispatch_authorized'] is False
        assert budget.export_snapshot() == before
        assert budget.snapshot()['jobs'] == budget.snapshot()['attempts'] == 0
        # An inspection must not advance the persisted spending clock.
        clock.now -= 30
        assert budget.begin_job('real-admission', owner_id=OWNER).created


def test_inspection_after_restart_preserves_active_job_and_full_hold(tmp_path):
    path = tmp_path / 'pilot.sqlite'
    with MonetaryBudget.create(path, policy(), clock=Clock()) as budget:
        begin(budget)
        reserve(budget, amount=200_000)
    with MonetaryBudget(path, clock=Clock()) as reopened:
        before = reopened.export_snapshot()
        result = reopened.admission_status(owner_id=OWNER)
        assert 'concurrent_job_limit' in result['blockers']
        assert result['active_jobs'] == 1
        assert result['remaining_total_micro_eur'] == 9_800_000
        assert reopened.export_snapshot() == before


@pytest.mark.parametrize('elapsed,expected', [(-1, 'pilot_not_started'), (7 * 86_400, 'pilot_expired')])
def test_inspection_checks_time_window_without_changing_it(tmp_path, elapsed, expected):
    clock = Clock()
    with MonetaryBudget.create(tmp_path / 'pilot.sqlite', policy(), clock=clock) as budget:
        before = budget.export_snapshot()
        clock.now = NOW + elapsed
        assert expected in budget.admission_status(owner_id=OWNER)['blockers']
        assert budget.export_snapshot() == before


def test_inspection_checks_daily_exposure_and_job_quotas(tmp_path):
    configured = policy(per_job_micro_eur=200_000, daily_micro_eur=200_000,
                        max_jobs_daily=1, max_jobs_total=1)
    with MonetaryBudget.create(tmp_path / 'pilot.sqlite', configured, clock=Clock()) as budget:
        begin(budget)
        reserve(budget, amount=200_000)
        budget.close_job('job', owner_id=OWNER)
        result = budget.admission_status(owner_id=OWNER)
        assert set(result['blockers']) == {'daily_budget', 'daily_job_quota', 'total_job_quota'}
        assert result['remaining_daily_micro_eur'] == 0
        assert budget.snapshot()['held_micro_eur'] == 200_000


def test_inspection_respects_persisted_clock_and_owner(tmp_path):
    clock = Clock()
    with MonetaryBudget.create(tmp_path / 'pilot.sqlite', policy(), clock=clock) as budget:
        clock.now += 30
        begin(budget)
        clock.now -= 10
        assert 'clock_regression' in budget.admission_status(owner_id=OWNER)['blockers']
        with pytest.raises(BudgetDenied, match='owner_only'):
            budget.admission_status(owner_id='different-owner')


def test_owner_http_preflight_reports_scoped_checks_without_spending(accounts, ledger, provider_factory):
    before = ledger.export_snapshot()
    with serving(pilot(accounts, ledger)) as (app, base):
        client, _ = login(base)
        for _ in range(2):
            status, headers, body = client.get('/api/pilot/preflight')
            assert status == 200
            assert body['admission_checks_passed'] is True
            assert body['dispatch_authorized'] is False
            assert body['production_ready'] is False
            assert body['operational_verification'] == 'not_assessed'
            assert body['request_pricing'] == 'checked_at_dispatch'
            assert body['source_mode'] == 'supplied'
            assert body['blockers'] == []
            assert body['budget']['jobs'] == 0
            assert 'no-store' in headers['Cache-Control']
        assert calls(provider_factory) == 0
        assert ledger.export_snapshot() == before
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            assert not runtime.history.list_recent()


def test_preflight_denies_other_owner_before_creating_runtime(accounts, ledger, provider_factory):
    with serving(pilot(accounts, ledger)) as (app, base):
        assert Client(base).get('/api/pilot/preflight')[0] == 401
        client, _ = login(base, 'other-owner@example.test')
        status, _, body = client.get('/api/pilot/preflight')
        assert status == 403
        assert 'budget' not in body
        assert not app.runtimes._runtimes
        assert calls(provider_factory) == 0


def test_http_preflight_explains_unconfigured_pilot_without_runtime_or_spend(accounts, provider_factory):
    with serving(None) as (app, base):
        client, _ = login(base)
        status, _, body = client.get('/api/pilot/preflight')
        assert status == 200 and body['configured'] is False
        assert body['blockers'] == ['owner_pilot_not_configured']
        assert body['admission_checks_passed'] is False
        assert calls(provider_factory) == 0 and not app.runtimes._runtimes


def test_expired_sources_are_explained_only_to_current_owner(accounts, ledger, provider_factory):
    configured = pilot(accounts, ledger, source_manifests=(
        source_grant(accounts, valid_until=NOW + 1),
        source_grant(accounts, accounts.second, valid_until=NOW + 1),
    ), clock=lambda: NOW + 2)
    with serving(configured) as (_, base):
        client, _ = login(base)
        status, _, body = client.get('/api/pilot/preflight')
        assert status == 200
        assert body['blockers'] == ['source_approval_unavailable']
        assert body['admission_checks_passed'] is False
        assert calls(provider_factory) == 0


def test_expired_tariffs_block_without_counting_or_sending(accounts, ledger, provider_factory):
    configured = pilot(accounts, ledger, tariffs={
        key: tariff(*key, valid_until=NOW + 1) for key in ENDPOINTS
    }, clock=lambda: NOW + 2)
    with serving(configured) as (_, base):
        client, _ = login(base)
        status, _, body = client.get('/api/pilot/preflight')
        assert status == 200
        assert set(body['blockers']) == {'main_target_not_current', 'critic_target_not_current'}
        assert calls(provider_factory) == ledger.snapshot()['jobs'] == 0


def test_uncertain_active_job_is_reported_and_not_released(accounts, ledger, provider_factory):
    ledger.begin_job('unfinished', owner_id=accounts.owner)
    before = ledger.export_snapshot()
    with serving(pilot(accounts, ledger)) as (_, base):
        client, _ = login(base)
        status, _, body = client.get('/api/pilot/preflight')
        assert status == 200 and 'concurrent_job_limit' in body['blockers']
        assert body['budget']['active_jobs'] == 1
        assert ledger.export_snapshot() == before and calls(provider_factory) == 0


def test_demoted_session_cannot_receive_preflight_data(accounts, ledger, provider_factory, monkeypatch):
    configured = pilot(accounts, ledger)
    original = ledger.admission_status
    def revoke_during_check(**kwargs):
        result = original(**kwargs)
        accounts.store.add_user_to_workspace(user_id=accounts.owner, workspace_id=accounts.workspace, role='member')
        return result
    monkeypatch.setattr(ledger, 'admission_status', revoke_during_check)
    with serving(configured) as (_, base):
        client, _ = login(base)
        status, _, body = client.get('/api/pilot/preflight')
        assert status == 403 and 'budget' not in body and calls(provider_factory) == 0


def test_unavailable_ledger_gives_generic_failure_without_details(accounts, ledger, provider_factory):
    configured = pilot(accounts, ledger)
    ledger.close()
    with serving(configured) as (_, base):
        client, _ = login(base)
        status, _, body = client.get('/api/pilot/preflight')
        assert status == 503 and body['blockers'] == ['pilot_state_unavailable']
        assert 'budget' not in body and calls(provider_factory) == 0
