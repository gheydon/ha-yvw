"""Sensors summarising the most recent Yarra Valley Water readings.

These are for display only. Historical consumption reaches the Water dashboard
through the external statistics written by ``statistics.py``; pointing the
dashboard at these entities as well would count the same water twice.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import YvwConfigEntry, YvwCoordinator
from .entity import YvwEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YvwConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Yarra Valley Water sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LatestHourlyUsageSensor(coordinator),
            LastFullDayUsageSensor(coordinator),
            LastReadingSensor(coordinator),
            LastKeepaliveSensor(coordinator),
        ]
    )


class LatestHourlyUsageSensor(YvwEntity, SensorEntity):
    """Consumption during the most recent hour the meter reported."""

    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: YvwCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "latest_hourly_usage")

    @property
    def native_value(self) -> float | None:
        """Return the litres used in that hour."""
        latest = self.coordinator.data.latest
        return latest.litres if latest else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return which hour the reading covers."""
        latest = self.coordinator.data.latest
        if latest is None:
            return None
        return {
            "hour_starting": latest.start.isoformat(),
            "hour_ending": latest.end.isoformat(),
        }


class LastFullDayUsageSensor(YvwEntity, SensorEntity):
    """Consumption across the most recent day the meter reported in full."""

    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: YvwCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "last_full_day_usage")

    @property
    def native_value(self) -> float | None:
        """Return the litres used that day."""
        return self.coordinator.data.last_full_day_litres

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return which day the total covers."""
        day = self.coordinator.data.last_full_day
        if day is None:
            return None
        return {"date": day.isoformat()}


class LastReadingSensor(YvwEntity, SensorEntity):
    """When the meter last reported."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YvwCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "last_reading")

    @property
    def native_value(self) -> datetime | None:
        """Return when the meter last reported.

        That is the end of the hour it covers, not the start: the reading for
        the 23:00 hour is taken at midnight.
        """
        latest = self.coordinator.data.latest
        if latest is None:
            return None
        return latest.end


class LastKeepaliveSensor(YvwEntity, SensorEntity):
    """When the portal was last touched to stop the session going idle.

    Mostly of interest while measuring how long a session survives, where the
    gap between these is the thing being tested.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YvwCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "last_keepalive")

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last successful ping."""
        return self.coordinator.last_keepalive

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return what the measurement has found so far."""
        return {
            "interval": str(self.coordinator.keepalive_interval),
            "calibrating": str(self.coordinator.calibrating),
            "measurement": self.coordinator.probe_state.summary,
        }
