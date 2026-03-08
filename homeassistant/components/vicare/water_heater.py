"""Viessmann ViCare water_heater device."""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import Any

from PyViCare.PyViCareDevice import Device as PyViCareDevice
from PyViCare.PyViCareDeviceConfig import PyViCareDeviceConfig
from PyViCare.PyViCareHeatingDevice import HeatingCircuit as PyViCareHeatingCircuit
from PyViCare.PyViCareUtils import (
    PyViCareCommandError,
    PyViCareInvalidDataError,
    PyViCareNotSupportedFeatureError,
    PyViCareRateLimitError,
)
import requests
import voluptuous as vol

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ViCareEntity
from .types import ViCareConfigEntry, ViCareDevice
from .utils import get_circuits, get_device_serial

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_DHW_CIRCULATION_PUMP_SCHEDULE = "set_dhw_circulation_pump_schedule"
SERVICE_SET_DHW_SCHEDULE = "set_dhw_schedule"

VICARE_MODE_DHW = "dhw"
VICARE_MODE_HEATING = "heating"
VICARE_MODE_DHWANDHEATING = "dhwAndHeating"
VICARE_MODE_DHWANDHEATINGCOOLING = "dhwAndHeatingCooling"
VICARE_MODE_FORCEDREDUCED = "forcedReduced"
VICARE_MODE_FORCEDNORMAL = "forcedNormal"
VICARE_MODE_OFF = "standby"

VICARE_TEMP_WATER_MIN = 10
VICARE_TEMP_WATER_MAX = 60

OPERATION_MODE_ON = "on"
OPERATION_MODE_OFF = "off"

VICARE_TO_HA_HVAC_DHW = {
    VICARE_MODE_DHW: OPERATION_MODE_ON,
    VICARE_MODE_DHWANDHEATING: OPERATION_MODE_ON,
    VICARE_MODE_DHWANDHEATINGCOOLING: OPERATION_MODE_ON,
    VICARE_MODE_HEATING: OPERATION_MODE_OFF,
    VICARE_MODE_FORCEDREDUCED: OPERATION_MODE_OFF,
    VICARE_MODE_FORCEDNORMAL: OPERATION_MODE_ON,
    VICARE_MODE_OFF: OPERATION_MODE_OFF,
}

HA_TO_VICARE_HVAC_DHW = {
    OPERATION_MODE_OFF: VICARE_MODE_OFF,
    OPERATION_MODE_ON: VICARE_MODE_DHW,
}


def _validate_time(value: str) -> str:
    """Validate HH:MM time string and reject times that cause ViCare midnight rollover."""
    if not isinstance(value, str) or ":" not in value:
        raise vol.Invalid(f"Invalid time format: {value!r}. Expected HH:MM")
    try:
        h, m = map(int, value.split(":"))
    except ValueError as err:
        raise vol.Invalid(f"Invalid time format: {value!r}. Expected HH:MM") from err
    if h == 24 and m == 0:
        return "24:00"
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise vol.Invalid(
            f"Invalid time: {value!r}. Hours must be 0-23 (or 24:00 for end of day),"
            " minutes 0-59"
        )
    # ViCare rounds 23:55-23:59 up to 00:00 (next day), corrupting the schedule.
    if h == 23 and m >= 55:
        raise vol.Invalid(
            f"Invalid time: {value!r}. Times from 23:55 to 23:59 are rounded up to"
            " 00:00 (next day) by ViCare, which corrupts the schedule. Use 24:00 for"
            " end of day or 23:50 as the latest reliable time."
        )
    return f"{h:02d}:{m:02d}"


def _validate_day_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate entries for a single day: max 4, no overlaps, no duplicate positions."""
    if len(entries) > 4:
        raise vol.Invalid("Maximum 4 time entries allowed per day")
    positions = [e["position"] for e in entries]
    if len(positions) != len(set(positions)):
        raise vol.Invalid(
            "Duplicate position values. Each time slot must have a unique position (0-3)"
        )
    for i, entry in enumerate(entries):
        for other in entries[i + 1 :]:
            if _times_overlap(entry, other):
                raise vol.Invalid(
                    f"Overlapping time slots: {entry['start']}-{entry['end']}"
                    f" and {other['start']}-{other['end']}"
                )
    return entries


def _times_overlap(entry1: dict[str, Any], entry2: dict[str, Any]) -> bool:
    """Return True if two schedule entries overlap."""

    def to_minutes(t: str) -> int:
        h, m = map(int, t.split(":"))
        return 24 * 60 if (h == 24 and m == 0) else h * 60 + m

    return not (
        to_minutes(entry1["end"]) <= to_minutes(entry2["start"])
        or to_minutes(entry2["end"]) <= to_minutes(entry1["start"])
    )


_SCHEDULE_ENTRY = vol.Schema(
    {
        vol.Required("start"): _validate_time,
        vol.Required("end"): _validate_time,
        vol.Required("mode"): vol.In(["on"]),
        vol.Required("position"): vol.All(int, vol.Range(min=0, max=3)),
    }
)

_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(day, default=[]): vol.All([_SCHEDULE_ENTRY], _validate_day_entries)
        for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
)


def _build_entities(
    device_list: list[ViCareDevice],
) -> list[ViCareWater]:
    """Create ViCare domestic hot water entities for a device."""

    return [
        ViCareWater(
            get_device_serial(device.api),
            device.config,
            device.api,
            circuit,
        )
        for device in device_list
        for circuit in get_circuits(device.api)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ViCareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ViCare water heater platform."""
    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        SERVICE_SET_DHW_CIRCULATION_PUMP_SCHEDULE,
        cv.make_entity_service_schema(
            {vol.Required("schedule"): _SCHEDULE_SCHEMA}
        ),
        SERVICE_SET_DHW_CIRCULATION_PUMP_SCHEDULE,
    )

    platform.async_register_entity_service(
        SERVICE_SET_DHW_SCHEDULE,
        cv.make_entity_service_schema(
            {vol.Required("schedule"): _SCHEDULE_SCHEMA}
        ),
        SERVICE_SET_DHW_SCHEDULE,
    )

    async_add_entities(
        await hass.async_add_executor_job(
            _build_entities,
            config_entry.runtime_data.devices,
        )
    )


class ViCareWater(ViCareEntity, WaterHeaterEntity):
    """Representation of the ViCare domestic hot water device."""

    _attr_precision = PRECISION_TENTHS
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = VICARE_TEMP_WATER_MIN
    _attr_max_temp = VICARE_TEMP_WATER_MAX
    _attr_operation_list = list(HA_TO_VICARE_HVAC_DHW)
    _attr_translation_key = "domestic_hot_water"
    _current_mode: str | None = None
    _dhw_active: bool | None = None

    def __init__(
        self,
        device_serial: str | None,
        device_config: PyViCareDeviceConfig,
        device: PyViCareDevice,
        circuit: PyViCareHeatingCircuit,
    ) -> None:
        """Initialize the DHW water_heater device."""
        super().__init__(circuit.id, device_serial, device_config, device)
        self._circuit = circuit
        self._attributes: dict[str, Any] = {}

    def update(self) -> None:
        """Let HA know there has been an update from the ViCare API."""
        with self.vicare_api_handler():
            with suppress(PyViCareNotSupportedFeatureError):
                self._attr_current_temperature = (
                    self._api.getDomesticHotWaterStorageTemperature()
                )

            with suppress(PyViCareNotSupportedFeatureError):
                self._attr_target_temperature = (
                    self._api.getDomesticHotWaterDesiredTemperature()
                )

            with suppress(PyViCareNotSupportedFeatureError):
                self._current_mode = self._circuit.getActiveMode()

            with suppress(PyViCareNotSupportedFeatureError):
                self._dhw_active = self._api.getDomesticHotWaterActive()

            with suppress(PyViCareNotSupportedFeatureError):
                self._attributes["dhw_circulation_schedule"] = (
                    self._api.getDomesticHotWaterCirculationSchedule()
                )

            with suppress(PyViCareNotSupportedFeatureError):
                self._attributes["dhw_time_programme"] = (
                    self._api.getDomesticHotWaterSchedule()
                )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity specific state attributes."""
        return self._attributes

    def set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperatures."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._api.setDomesticHotWaterTemperature(temp)
            self._attr_target_temperature = temp

    @property
    def current_operation(self) -> str | None:
        """Return current operation ie. heat, cool, idle."""
        if self._dhw_active is not None:
            return OPERATION_MODE_ON if self._dhw_active else OPERATION_MODE_OFF
        if self._current_mode is None:
            return None
        return VICARE_TO_HA_HVAC_DHW.get(self._current_mode)

    def set_dhw_circulation_pump_schedule(self, schedule: dict[str, Any]) -> None:
        """Set schedule for the DHW circulation pump."""
        try:
            self._api.setDomesticHotWaterCirculationSchedule(schedule)
        except PyViCareCommandError as err:
            raise HomeAssistantError(
                "ViCare API rejected the schedule. Check that time slots are valid,"
                " do not overlap, and each day has at most 4 entries."
            ) from err
        except PyViCareNotSupportedFeatureError as err:
            raise HomeAssistantError(
                "DHW circulation pump scheduling is not supported on this device"
            ) from err
        except (PyViCareRateLimitError, PyViCareInvalidDataError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
        except requests.exceptions.ConnectionError as err:
            raise HomeAssistantError(
                f"Unable to connect to ViCare server: {err}"
            ) from err

    def set_dhw_schedule(self, schedule: dict[str, Any]) -> None:
        """Set the DHW time programme."""
        try:
            self._api.setProperty(
                "heating.dhw.schedule", "setSchedule", {"newSchedule": schedule}
            )
        except PyViCareCommandError as err:
            raise HomeAssistantError(
                "ViCare API rejected the schedule. Check that time slots are valid,"
                " do not overlap, and each day has at most 4 entries."
            ) from err
        except PyViCareNotSupportedFeatureError as err:
            raise HomeAssistantError(
                "DHW time programme scheduling is not supported on this device"
            ) from err
        except (PyViCareRateLimitError, PyViCareInvalidDataError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
        except requests.exceptions.ConnectionError as err:
            raise HomeAssistantError(
                f"Unable to connect to ViCare server: {err}"
            ) from err
