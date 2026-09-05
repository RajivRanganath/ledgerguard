"""Provider construction errors.

One distinction lives here, and it matters: *not configured* is an expected,
documented way to run LedgerGuard on a subset of providers, while any other
construction failure is a defect. Both used to raise plain ``ValueError``, so
``build_chain`` could not tell them apart and quietly dropped a broken provider
as though the user had simply not set its key.

``ProviderNotConfigured`` stays a ``ValueError`` subclass so that callers which
name a provider explicitly keep failing the way they always did.
"""

from __future__ import annotations


class ProviderNotConfigured(ValueError):
    """No credentials for this provider, or its endpoint is not reachable.

    Expected. The provider is skipped silently and the chain carries on.
    """
