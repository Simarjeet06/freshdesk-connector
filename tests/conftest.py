import pytest

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.config import Settings
from freshdesk_connector.mock import MOCK_API_KEY, MockFreshdesk
from freshdesk_connector.server import set_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeSleep:
    """Records requested sleeps instead of actually sleeping, so retry tests run instantly."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_settings(**overrides) -> Settings:
    base = dict(domain="demo-merchant", api_key=MOCK_API_KEY, mock=True, rate_limit_per_min=1000)
    return Settings(**(base | overrides))


@pytest.fixture
def fake_sleep():
    return FakeSleep()


@pytest.fixture
def mock_fd():
    return MockFreshdesk()


@pytest.fixture
def client(mock_fd, fake_sleep):
    c = FreshdeskClient(make_settings(), transport=mock_fd.transport(), sleep=fake_sleep)
    set_client(c)
    return c
