"""Viessmann ViCare water_heater device."""

from __future__ import annotations

from contextlib import suppress
from datetime import timedelta
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
SERVICE_SET_DHW_CIRCULATION_PUMP_SCHEDULE_ATTR_SCHEDULE = "schedule"

SERVICE_SET_DHW_SCHEDULE = "set_dhw_schedule"
SERVICE_SET_DHW_SCHEDULE_ATTR_SCHEDULE = "schedule"

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


def _normalize_time_input(time_input: str | timedelta) -> str:
    """Convert time input (string or timedelta) to HH:MM string format."""
    if isinstance(time_input, timedelta):
        # Convert timedelta to HH:MM format
        total_seconds = int(time_input.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        # Handle 24:00 case for end of day
        if hours == 24 and minutes == 0:
            return "24:00"
        time_str = f"{hours:02d}:{minutes:02d}"
    elif isinstance(time_input, str):
        # Validate string format
        if ":" not in time_input:
            raise vol.Invalid(f"Invalid time format: {time_input}. Expected HH:MM")
        try:
            h, m = map(int, time_input.split(":"))
            # Allow 24:00 as special case for end of day, otherwise 0-23
            if h == 24 and m == 0:
                time_str = "24:00"  # Valid end-of-day time
            elif not (0 <= h <= 23 and 0 <= m <= 59):
                raise vol.Invalid(
                    f"Invalid time: {time_input}. Hours must be 0-23 (or 24:00 for end of day), minutes 0-59"
                )
            else:
                time_str = f"{h:02d}:{m:02d}"
        except ValueError as err:
            raise vol.Invalid(
                f"Invalid time format: {time_input}. Expected HH:MM"
            ) from err
    else:
        raise vol.Invalid(f"Time must be string or timedelta, got {type(time_input)}")

    # ViCare-specific validations
    _validate_vicare_time_constraints(time_str)

    return time_str


def _validate_vicare_time_constraints(time_str: str) -> None:
    """Validate ViCare-specific time constraints."""
    if time_str == "24:00":
        return  # 24:00 is always valid

    h, m = map(int, time_str.split(":"))

    # Check for 10-minute increments
    if m % 10 != 0:
        raise vol.Invalid(
            f"Invalid time: {time_str}. ViCare only accepts 10-minute increments "
            f"(e.g., 08:00, 08:10, 08:20, 08:30, 08:40, 08:50)"
        )

    # Check for times that round to midnight (00:00) and cause issues
    # ViCare rounds to nearest 10-minute increment:
    # - 23:55, 23:56, 23:57, 23:58, 23:59 → round UP to 00:00 (next day) ❌
    # - 23:50, 23:51, 23:52, 23:53, 23:54 → round DOWN to 23:50 (same day) ✅
    if h == 23 and m >= 55:
        raise vol.Invalid(
            f"Invalid time: {time_str}. ViCare rounds times 23:55-23:59 up to 00:00 "
            f"(next day), which causes scheduling issues. Use 24:00 for end of day "
            f"or 23:50 for the latest reliable time."
        )


def _validate_time_field(value):
    """Validate and normalize time field."""
    return _normalize_time_input(value)


# Define schedule validation schema
SCHEDULE_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required("start"): _validate_time_field,
        vol.Required("end"): _validate_time_field,
        vol.Required("mode"): vol.In(["on"]),
        vol.Required("position"): vol.All(
            int, vol.Range(min=0, max=3)
        ),  # 0-3 for 4 slots
    }
)


# ViCare allows maximum 4 time entries per day
def _validate_max_entries(entries):
    """Validate maximum 4 entries per day."""
    if len(entries) > 4:
        raise vol.Invalid("Maximum 4 time entries allowed per day")
    return entries


SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("mon", default=[]): vol.All(
            [SCHEDULE_ENTRY_SCHEMA], _validate_max_entries
        ),
        vol.Optional("tue", default=[]): vol.All(
            [SCHEDULE_ENTRY_SCHEMA], _validate_max_entries
        ),
        vol.Optional("wed", default=[]): vol.All(
            [SCHEDULE_ENTRY_SCHEMA], _validate_max_entries
        ),
        vol.Optional("thu", default=[]): vol.All(
            [SCHEDULE_ENTRY_SCHEMA], _validate_max_entries
        ),
        vol.Optional("fri", default=[]): vol.All(
            [SCHEDULE_ENTRY_SCHEMA], _validate_max_entries
        ),
        vol.Optional("sat", default=[]): vol.All(
            [SCHEDULE_ENTRY_SCHEMA], _validate_max_entries
        ),
        vol.Optional("sun", default=[]): vol.All(
            [SCHEDULE_ENTRY_SCHEMA], _validate_max_entries
        ),
    }
)


def _validate_day_entries(day: str, entries: list[dict[str, Any]]) -> None:
    """Validate entries for a single day."""
    if len(entries) <= 1:
        return

    # Check for overlapping times
    _check_overlapping_times(day, entries)

    # Check for duplicate positions
    _check_duplicate_positions(day, entries)


def _check_overlapping_times(day: str, entries: list[dict[str, Any]]) -> None:
    """Check for overlapping time slots in a day."""
    for i, entry in enumerate(entries):
        for other_entry in entries[i + 1 :]:  # Fixed: removed unused 'j' variable
            if _times_overlap(entry, other_entry):
                raise HomeAssistantError(
                    f"Overlapping time slots found for {day}: "
                    f"{entry['start']}-{entry['end']} and "
                    f"{other_entry['start']}-{other_entry['end']}"
                )


def _check_duplicate_positions(day: str, entries: list[dict[str, Any]]) -> None:
    """Check for duplicate position values in a day."""
    positions = [entry["position"] for entry in entries]
    if len(positions) != len(set(positions)):
        raise HomeAssistantError(
            f"Duplicate position values found for {day}. "
            "Each time slot must have a unique position (0-3)."
        )


def _validate_schedule_format(schedule: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize schedule format."""
    # Check if user pasted JSON-like structure
    if isinstance(schedule, str):
        raise HomeAssistantError(
            "Schedule must be in YAML format, not JSON string. "
            "Remove quotes around the entire schedule."
        )

    try:
        # Validate against schema (this will normalize time formats)
        validated_schedule = SCHEDULE_SCHEMA(schedule)

        # Additional validation: check for overlapping times and position conflicts
        for day, entries in validated_schedule.items():
            _validate_day_entries(day, entries)

    except vol.Invalid as err:
        # Convert voluptuous errors to user-friendly messages
        error_msg = str(err)
        if "Maximum 4 time entries allowed per day" in error_msg:
            raise HomeAssistantError(
                f"Too many time slots for {err.path[0] if err.path else 'a day'}. "
                "ViCare allows maximum 4 time entries per day."
            ) from err
        if "ViCare only accepts 10-minute increments" in error_msg:
            raise HomeAssistantError(f"Invalid time format: {error_msg}") from err
        if "ViCare misinterprets this as 00:00" in error_msg:
            raise HomeAssistantError(f"Time validation error: {error_msg}") from err
        if "Times after 23:50 may cause issues" in error_msg:
            raise HomeAssistantError(f"Time validation warning: {error_msg}") from err
        if "required key not provided" in error_msg:
            raise HomeAssistantError(
                f"Missing required field in schedule: {err.path[-1]}. "
                "Each time slot must have: start, end, mode, position"
            ) from err
        if "Invalid time format" in error_msg or "Invalid time:" in error_msg:
            raise HomeAssistantError(
                f"Invalid time format: {error_msg}. Use HH:MM format with 10-minute increments (e.g., '08:00', '08:10')"
            ) from err
        if "not a valid value" in error_msg and "mode" in str(err.path):
            raise HomeAssistantError("Mode must be either 'on' or 'off'") from err
        if "not in range" in error_msg and "position" in str(err.path):
            raise HomeAssistantError(
                "Position must be between 0 and 3 (ViCare supports 4 time slots per day)"
            ) from err
        raise HomeAssistantError(f"Invalid schedule format: {err}") from err
    else:
        return validated_schedule


def _times_overlap(entry1: dict, entry2: dict) -> bool:
    """Check if two time entries overlap."""

    def time_to_minutes(time_str: str) -> int:
        h, m = map(int, time_str.split(":"))
        # Convert 24:00 to 1440 minutes (end of day)
        if h == 24 and m == 0:
            return 24 * 60  # 1440 minutes
        return h * 60 + m

    start1 = time_to_minutes(entry1["start"])
    end1 = time_to_minutes(entry1["end"])
    start2 = time_to_minutes(entry2["start"])
    end2 = time_to_minutes(entry2["end"])

    return not (end1 <= start2 or end2 <= start1)


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

    # Use cv.make_entity_service_schema for proper validation【1】
    platform.async_register_entity_service(
        SERVICE_SET_DHW_CIRCULATION_PUMP_SCHEDULE,
        cv.make_entity_service_schema(
            {
                vol.Required(
                    SERVICE_SET_DHW_CIRCULATION_PUMP_SCHEDULE_ATTR_SCHEDULE
                ): SCHEDULE_SCHEMA
            }
        ),
        SERVICE_SET_DHW_CIRCULATION_PUMP_SCHEDULE,
    )

    platform.async_register_entity_service(
        SERVICE_SET_DHW_SCHEDULE,
        cv.make_entity_service_schema(
            {vol.Required(SERVICE_SET_DHW_SCHEDULE_ATTR_SCHEDULE): SCHEDULE_SCHEMA}
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
        try:
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
                circulation_schedule = (
                    self._api.getDomesticHotWaterCirculationSchedule()
                )
                self._attributes["dhw_circulation_schedule"] = circulation_schedule

            with suppress(PyViCareNotSupportedFeatureError):
                dhw_schedule = self._api.getDomesticHotWaterSchedule()
                self._attributes["dhw_time_programme"] = dhw_schedule

        except requests.exceptions.ConnectionError:
            _LOGGER.error("Unable to retrieve data from ViCare server")
        except PyViCareRateLimitError as limit_exception:
            _LOGGER.error("Vicare API rate limit exceeded: %s", limit_exception)
        except ValueError:
            _LOGGER.error("Unable to decode data from ViCare server")
        except PyViCareInvalidDataError as invalid_data_exception:
            _LOGGER.error("Invalid data from Vicare server: %s", invalid_data_exception)

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
        if self._current_mode is None:
            return None
        return VICARE_TO_HA_HVAC_DHW.get(self._current_mode, None)

    def set_dhw_circulation_pump_schedule(self, schedule: dict[str, Any]) -> None:
        """Service function to set schedule for dhw circulation pump directly."""
        # Validation already done by service registration schema
        # Just call API directly since data is pre-validated
        try:
            self._api.setDomesticHotWaterCirculationSchedule(schedule)
        except PyViCareCommandError as error:
            _LOGGER.error("ViCare API command failed: %s", error)
            if "VALIDATION_ERROR" in str(error):
                raise HomeAssistantError(
                    "ViCare API rejected the schedule format. Please check that all "
                    "time slots have valid start/end times, don't overlap, and each day "
                    "has maximum 4 time entries."
                ) from error
            raise HomeAssistantError(f"ViCare API error: {error}") from error
        except PyViCareNotSupportedFeatureError as error:
            _LOGGER.error("DHW circulation pump schedule not supported: %s", error)
            raise HomeAssistantError(
                "DHW circulation pump scheduling is not supported on this device"
            ) from error
        except PyViCareRateLimitError as error:
            _LOGGER.error("ViCare API rate limit exceeded: %s", error)
            raise HomeAssistantError(
                f"ViCare API rate limit exceeded: {error}"
            ) from error
        except PyViCareInvalidDataError as error:
            _LOGGER.error("Invalid schedule data: %s", error)
            raise HomeAssistantError(f"Invalid schedule data: {error}") from error
        except requests.exceptions.ConnectionError as error:
            _LOGGER.error("Unable to connect to ViCare server: %s", error)
            raise HomeAssistantError(
                f"Unable to connect to ViCare server: {error}"
            ) from error
        except ValueError as error:
            _LOGGER.error("Unable to decode data from ViCare server: %s", error)
            raise HomeAssistantError(
                f"Unable to decode data from ViCare server: {error}"
            ) from error

    def set_dhw_schedule(self, schedule: dict[str, Any]) -> None:
        """Service function to set schedule for dhw time programme directly."""
        # Validation already done by service registration schema
        # Just call API directly since data is pre-validated
        try:
            self._api.setProperty(
                "heating.dhw.schedule", "setSchedule", {"newSchedule": schedule}
            )
        except PyViCareCommandError as error:
            _LOGGER.error("ViCare API command failed: %s", error)
            if "VALIDATION_ERROR" in str(error):
                raise HomeAssistantError(
                    "ViCare API rejected the schedule format. Please check that all "
                    "time slots have valid start/end times, don't overlap, and each day "
                    "has maximum 4 time entries."
                ) from error
            raise HomeAssistantError(f"ViCare API error: {error}") from error
        except PyViCareNotSupportedFeatureError as error:
            _LOGGER.error("DHW schedule not supported: %s", error)
            raise HomeAssistantError(
                "DHW time programme scheduling is not supported on this device"
            ) from error
        except PyViCareRateLimitError as error:
            _LOGGER.error("ViCare API rate limit exceeded: %s", error)
            raise HomeAssistantError(
                f"ViCare API rate limit exceeded: {error}"
            ) from error
        except PyViCareInvalidDataError as error:
            _LOGGER.error("Invalid schedule data: %s", error)
            raise HomeAssistantError(f"Invalid schedule data: {error}") from error
        except requests.exceptions.ConnectionError as error:
            _LOGGER.error("Unable to connect to ViCare server: %s", error)
            raise HomeAssistantError(
                f"Unable to connect to ViCare server: {error}"
            ) from error
        except ValueError as error:
            _LOGGER.error("Unable to decode data from ViCare server: %s", error)
            raise HomeAssistantError(
                f"Unable to decode data from ViCare server: {error}"
            ) from error
