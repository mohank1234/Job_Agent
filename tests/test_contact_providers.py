"""Account checks must never reveal secrets or call email lookup endpoints."""
import httpx
import pytest

from tools import check_contact_providers as checks


PROVIDERS = ('PROSPEO_API_KEY', 'HUNTER_API_KEY', 'TOMBA_API_KEY', 'TOMBA_SECRET', 'APOLLO_API_KEY')


def test_missing_keys_skip_all_http_and_optional_enrichment(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('No HTTP or enrichment is allowed without keys')
    monkeypatch.setattr(checks.httpx, 'get', forbidden)
    monkeypatch.setattr(checks.httpx, 'post', forbidden)
    monkeypatch.setattr(checks, 'check_apollo_enrichment', forbidden)
    checks.main([])
    output = capsys.readouterr().out
    assert all(name in output for name in ('Prospeo:', 'Hunter:', 'Tomba:', 'Apollo:'))
    assert 'OK' not in output


@pytest.mark.parametrize('present', ['TOMBA_API_KEY', 'TOMBA_SECRET'])
def test_partial_tomba_credentials_do_not_call_api(monkeypatch, present):
    monkeypatch.setenv(present, 'test-only')
    monkeypatch.setattr(checks.httpx, 'get', lambda *a, **k: pytest.fail('Incomplete keys'))
    assert 'not both set' in checks.check_tomba()


def test_account_checks_use_only_documented_free_endpoints(monkeypatch, capsys):
    for name in PROVIDERS[:4]:
        monkeypatch.setenv(name, 'fake-' + name)
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if 'prospeo' in url:
            assert kwargs['headers']['X-KEY'] == 'fake-PROSPEO_API_KEY'
            return httpx.Response(200, json={'error': False, 'response': {'current_plan': 'FREE', 'remaining_credits': 100}})
        if 'hunter' in url:
            assert kwargs['params']['api_key'] == 'fake-HUNTER_API_KEY'
            return httpx.Response(200, json={'data': {'plan_name': 'Free', 'requests': {'credits': {'used': 2, 'available': 50}}}})
        assert kwargs['headers']['X-Tomba-Secret'] == 'fake-TOMBA_SECRET'
        return httpx.Response(200, json={'data': {'id': 1, 'secret_token': 'NEVER_PRINT', 'usage': {'secret': 'NEVER_PRINT'}}})
    monkeypatch.setattr(checks.httpx, 'get', get)
    monkeypatch.setattr(checks.httpx, 'post', lambda *a, **k: pytest.fail('No lookup POSTs'))
    checks.main([])
    assert calls == ['https://api.prospeo.io/account-information', 'https://api.hunter.io/v2/account', 'https://api.tomba.io/v1/me']
    output = capsys.readouterr().out
    assert output.count(': OK') == 3 and '2/50 used' in output
    assert 'fake-' not in output and 'NEVER_PRINT' not in output


@pytest.mark.parametrize('function', [checks.check_prospeo, checks.check_hunter, checks.check_tomba])
@pytest.mark.parametrize('payload', [None, [], {}, {'error': True}, {'data': {}}, {'response': {}}])
def test_malformed_200_never_reports_ok(monkeypatch, function, payload):
    for name in PROVIDERS:
        monkeypatch.setenv(name, 'fake-' + name)
    monkeypatch.setattr(checks.httpx, 'get', lambda *a, **k: httpx.Response(200, json=payload))
    assert ': OK' not in function()


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_refusal_bodies_are_never_printed(monkeypatch, capsys, status):
    for name in PROVIDERS:
        monkeypatch.setenv(name, 'fake-' + name)
    response = httpx.Response(status, text='NEVER_PRINT echoed credential fake-PROSPEO_API_KEY')
    monkeypatch.setattr(checks.httpx, 'get', lambda *a, **k: response)
    monkeypatch.setattr(checks.httpx, 'post', lambda *a, **k: response)
    checks.main(['--apollo-enrichment'])
    output = capsys.readouterr().out
    assert output.count('HTTP ' + str(status)) == 5
    assert 'NEVER_PRINT' not in output and 'fake-' not in output


def test_connection_failure_does_not_abort_other_checks_or_print_url(monkeypatch, capsys):
    monkeypatch.setenv('PROSPEO_API_KEY', 'fake-key')
    def fail(*args, **kwargs):
        raise httpx.ConnectError('https://api.example?api_key=fake-key')
    monkeypatch.setattr(checks.httpx, 'get', fail)
    checks.main([])
    output = capsys.readouterr().out
    assert 'connection or provider error' in output and 'Tomba:' in output
    assert 'fake-key' not in output and 'https://' not in output and 'Traceback' not in output


def test_echoed_secret_in_allowlisted_field_is_redacted(monkeypatch):
    monkeypatch.setenv('PROSPEO_API_KEY', 'test-secret-value')
    monkeypatch.setattr(checks.httpx, 'get', lambda *a, **k: httpx.Response(200, json={
        'response': {'current_plan': 'test-secret-value', 'remaining_credits': 0}}))
    result = checks.check_prospeo()
    assert 'test-secret-value' not in result and '[redacted]' in result
