"""the dev auth bypass must never coexist with a production env.

``DEV_AUTH_BYPASS`` disables every credential check and resolves all
traffic to ``uid='dev-user'``; it exists only for the local self-hosted
bundle. These tests pin the two-layer guard:

1. :class:`OrchestratorSettings` refuses to *validate* when
   ``dev_auth_bypass`` is true and ``env`` is ``prod``.
2. :func:`create_app` re-asserts the same invariant at
   app-construction time (defense in depth for hand-built settings).

Dev / staging with the bypass on stay allowed; a prod env with the
bypass off stays allowed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quantra_common.settings import Environment
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings


def test_bypass_in_prod_rejected_by_settings() -> None:
    with pytest.raises(ValidationError, match="DEV_AUTH_BYPASS"):
        OrchestratorSettings(env=Environment.PROD, dev_auth_bypass=True)


@pytest.mark.parametrize("env", [Environment.DEV, Environment.STAGING])
def test_bypass_allowed_outside_prod(env: Environment) -> None:
    settings = OrchestratorSettings(env=env, dev_auth_bypass=True)
    assert settings.dev_auth_bypass is True
    assert settings.env is env


def test_prod_without_bypass_allowed() -> None:
    settings = OrchestratorSettings(env=Environment.PROD, dev_auth_bypass=False)
    assert settings.dev_auth_bypass is False
    assert settings.env is Environment.PROD


def test_create_app_refuses_bypass_in_prod() -> None:
    # Build a settings object that dodges the model validator (e.g. a future
    # caller that mutates the field post-construction) and confirm the app
    # factory still refuses to start.
    settings = OrchestratorSettings(env=Environment.DEV, dev_auth_bypass=True)
    object.__setattr__(settings, "env", Environment.PROD)
    with pytest.raises(RuntimeError, match="Refusing to start"):
        create_app(settings)
