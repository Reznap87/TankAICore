"""Keep the container health allowlist strict when advertising money admission."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def smoke(monkeypatch):
    path = Path(__file__).parents[1] / '.github/scripts/production_web_container_smoke.py'
    spec = importlib.util.spec_from_file_location('budget_metadata_smoke', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'public_json_headers', lambda headers, path: None)
    return module


def payload():
    return {'ok': True, 'version': 'offline-fixture', 'auth_mode': 'session',
            'auth_required': True, 'registration_enabled': False, 'production_ready': False,
            'development_queue_enabled': False, 'live_model_requests_require_budget': True,
            'monetary_admission_configured': False}


def test_smoke_accepts_explicit_closed_monetary_metadata(smoke):
    smoke.health_contract({}, payload(), 'offline-fixture')


@pytest.mark.parametrize('field,value', [('live_model_requests_require_budget', False),
                                       ('monetary_admission_configured', True),
                                       ('live_model_requests_require_budget', 1),
                                       ('monetary_admission_configured', 0)])
def test_smoke_rejects_weakened_or_ambiguous_budget_metadata(smoke, field, value):
    body = payload()
    body[field] = value
    with pytest.raises(smoke.Failure):
        smoke.health_contract({}, body, 'offline-fixture')


def test_smoke_still_rejects_an_extra_public_field(smoke):
    body = payload()
    body['unapproved_field'] = 'synthetic'
    with pytest.raises(smoke.Failure):
        smoke.health_contract({}, body, 'offline-fixture')
