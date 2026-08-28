"""Shared entity base for the Yarra Valley Water integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import YvwCoordinator


class YvwEntity(CoordinatorEntity[YvwCoordinator]):
    """An entity backed by one water meter."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: YvwCoordinator, key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.meter_serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.meter_serial)},
            manufacturer="Yarra Valley Water",
            model="Digital water meter",
            name=coordinator.address,
            serial_number=coordinator.meter_serial,
            configuration_url="https://myaccount.yvw.com.au/myaccount/s/usage",
        )
