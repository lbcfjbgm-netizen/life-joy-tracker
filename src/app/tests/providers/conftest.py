"""Keep the provider short circuit from leaking between tests.

A network failure marks a provider as down for 60 seconds in the cache. The
provider tests that talk to the real APIs (and fail, because those hosts are
unreachable from this machine) would otherwise leave the flag behind and make
the following, fully mocked, test fail for the wrong reason.
"""

import pytest
from django.core.cache import cache

from app.models import Sources


def _clear_short_circuit():
    """Delete the short-circuit flag of every provider."""
    for source in Sources:
        cache.delete(f"provider_down:{source.value}")


@pytest.fixture(autouse=True)
def _isolate_provider_short_circuit():
    """Start and end every test with no provider marked as down."""
    _clear_short_circuit()
    yield
    _clear_short_circuit()
