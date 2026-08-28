"""Fixtures for the Yarra Valley Water tests."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest_plugins = "pytest_homeassistant_custom_component"

# The recorder logs every statement it runs, which drowns out the actual output.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@pytest.fixture
def custom_integration(enable_custom_integrations):
    """Make the custom component loadable.

    Deliberately not autouse: it pulls in the hass fixture, and recorder tests
    have to set the database up before hass exists.
    """
    return


@pytest.fixture
def portal_tz() -> ZoneInfo:
    """Return the timezone the portal reports readings in."""
    return ZoneInfo("Australia/Melbourne")
