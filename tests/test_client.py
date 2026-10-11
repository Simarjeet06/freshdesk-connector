import httpx
import pytest

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.config import normalize_domain
from freshdesk_connector.errors import AuthError, ConfigError, NotFoundError, RateLimitedError, UpstreamError

from conftest import make_settings

pytestmark = pytest.mark.anyio


async def test_sends_api_key_as_basic_auth(client, mock_fd):
    await client.get_json("/agents/me")
    # base64("mock-api-key:X")
    assert mock_fd.requests[0].headers["authorization"] == "Basic bW9jay1hcGkta2V5Olg="


async def test_wrong_api_key_maps_to_auth_error(mock_fd, fake_sleep):
    c = FreshdeskClient(make_settings(api_key="wrong"), transport=mock_fd.transport(), sleep=fake_sleep)
    with pytest.raises(AuthError):
        await c.get_json("/agents/me")


async def test_429_waits_for_retry_after_then_succeeds(client, mock_fd, fake_sleep):
    mock_fd.fail_with_429 = 2
    mock_fd.retry_after = 7
    data = await client.get_json("/agents/me")
    assert data["id"] == 7001
    assert fake_sleep.calls == [7, 7]
    assert client.rate_state.throttled_requests == 2


async def test_429_with_long_retry_after_fails_fast_with_hint(client, mock_fd, fake_sleep):
    mock_fd.fail_with_429 = 1
    mock_fd.retry_after = 120  # longer than max_retry_wait_seconds (30)
    with pytest.raises(RateLimitedError) as exc:
        await client.get_json("/agents/me")
    assert exc.value.retry_after == 120
    assert fake_sleep.calls == []  # did not block the agent for two minutes


async def test_429_gives_up_after_max_retries(client, mock_fd):
    mock_fd.fail_with_429 = 10
    with pytest.raises(RateLimitedError):
        await client.get_json("/agents/me")
    assert len(mock_fd.requests) == 1 + client.settings.max_retries


async def test_5xx_is_retried_with_backoff(fake_sleep):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    c = FreshdeskClient(make_settings(), transport=httpx.MockTransport(handler), sleep=fake_sleep)
    assert await c.get_json("/x") == {"ok": True}
    assert len(fake_sleep.calls) == 2


async def test_network_errors_surface_as_upstream_error(fake_sleep):
    def handler(request):
        raise httpx.ConnectError("boom")

    c = FreshdeskClient(make_settings(), transport=httpx.MockTransport(handler), sleep=fake_sleep)
    with pytest.raises(UpstreamError):
        await c.get_json("/x")


async def test_404_maps_to_not_found(client):
    with pytest.raises(NotFoundError):
        await client.get_json("/tickets/424242")


async def test_pagination_follows_link_header(client):
    first = await client.get_page("/tickets", {"per_page": 5}, 1)
    assert len(first.items) == 5 and first.next_page == 2
    last = await client.get_page("/tickets", {"per_page": 5}, 3)
    assert len(last.items) == 2 and last.next_page is None


async def test_tracks_rate_limit_headers(client):
    await client.get_json("/agents/me")
    assert client.rate_state.total == 50
    assert client.rate_state.remaining == 49


async def test_token_bucket_paces_requests(mock_fd, fake_sleep):
    c = FreshdeskClient(make_settings(rate_limit_per_min=2), transport=mock_fd.transport(), sleep=fake_sleep)
    for _ in range(3):
        await c.get_json("/agents/me")
    # third request exceeds a bucket of 2 and must wait for a refill
    assert fake_sleep.calls and fake_sleep.calls[0] > 0


@pytest.mark.parametrize("raw", ["acme", "acme.freshdesk.com", "https://ACME.freshdesk.com/"])
def test_domain_normalization(raw):
    assert normalize_domain(raw) == "acme"


def test_domain_rejects_other_hosts():
    with pytest.raises(ConfigError):
        normalize_domain("evil.com/acme")
