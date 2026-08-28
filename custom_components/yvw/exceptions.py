"""Exceptions raised by the Yarra Valley Water integration."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class YvwError(HomeAssistantError):
    """Base class for Yarra Valley Water errors."""


class YvwCannotConnect(YvwError):
    """The portal could not be reached."""


class YvwAuthError(YvwError):
    """The stored session is no longer valid and the user must sign in again."""


class YvwInvalidAuth(YvwError):
    """The supplied email address or password was rejected."""


class YvwInvalidCode(YvwError):
    """The supplied SMS verification code was rejected."""


class YvwApiError(YvwError):
    """The portal returned an unexpected response."""
