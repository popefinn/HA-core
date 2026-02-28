"""Viessmann ViCare sensor device."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import logging

from PyViCare.PyViCareDevice import Device as PyViCareDevice
from PyViCare.PyViCareDeviceConfig import PyViCareDeviceConfig
from PyViCare.PyViCareHeatingDevice import (
    HeatingDeviceWithComponent as PyViCareHeatingDeviceComponent,
)
from PyViCare.PyViCareUtils import (
    PyViCareInvalidDataError,
    PyViCareNotSupportedFeatureError,
    PyViCareRateLimitError,
)
import requests

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ViCareEntity
from .types import ViCareConfigEntry, ViCareDevice, ViCareRequiredKeysMixin
from .utils import (
    get_burners,
    get_circuits,
    get_compressors,
    get_device_serial,
    is_supported,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ViCareBinarySensorEntityDescription(
    BinarySensorEntityDescription, ViCareRequiredKeysMixin
):
    """Describes ViCare binary sensor entity."""

    value_getter: Callable[[PyViCareDevice], bool]


CIRCUIT_SENSORS: tuple[ViCareBinarySensorEntityDescription, ...] = (
    ViCareBinarySensorEntityDescription(
        key="circulationpump_active",
        translation_key="circulation_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getCirculationPumpActive(),
    ),
    ViCareBinarySensorEntityDescription(
        key="frost_protection_active",
        translation_key="frost_protection",
        value_getter=lambda api: api.getFrostProtectionActive(),
    ),
    ViCareBinarySensorEntityDescription(
        key="heating_mode_active",
        translation_key="heating_mode_active",
        value_getter=lambda api: api.getProperty(
            f"heating.circuits.{api.circuit}.operating.modes.heating"
        )["properties"]["active"]["value"],
    ),
    ViCareBinarySensorEntityDescription(
        key="cooling_mode_active",
        translation_key="cooling_mode_active",
        value_getter=lambda api: api.getProperty(
            f"heating.circuits.{api.circuit}.operating.modes.cooling"
        )["properties"]["active"]["value"],
    ),
)

BURNER_SENSORS: tuple[ViCareBinarySensorEntityDescription, ...] = (
    ViCareBinarySensorEntityDescription(
        key="burner_active",
        translation_key="burner",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getActive(),
    ),
)

COMPRESSOR_SENSORS: tuple[ViCareBinarySensorEntityDescription, ...] = (
    ViCareBinarySensorEntityDescription(
        key="compressor_active",
        translation_key="compressor",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getActive(),
    ),
    ViCareBinarySensorEntityDescription(
        key="compressor_crankcase_heater_active",
        translation_key="compressor_crankcase_heater_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_getter=lambda api: api.getProperty(
            f"heating.compressors.{api.compressor}.heater.crankcase"
        )["properties"]["active"]["value"],
        entity_registry_enabled_default=False,
    ),
)

GLOBAL_SENSORS: tuple[ViCareBinarySensorEntityDescription, ...] = (
    ViCareBinarySensorEntityDescription(
        key="primary_valve_active",
        translation_key="primary_valve_active",
        device_class=BinarySensorDeviceClass.OPENING,
        value_getter=lambda api: api.getProperty(
            "heating.primaryCircuit.valves.fourThreeWay"
        )["properties"]["active"]["value"],
    ),
    ViCareBinarySensorEntityDescription(
        key="internal_pump_active",
        translation_key="internal_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getProperty("heating.boiler.pumps.internal")[
            "properties"
        ]["status"]["value"]
        == "on",
    ),
    ViCareBinarySensorEntityDescription(
        key="solar_pump_active",
        translation_key="solar_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getSolarPumpActive(),
    ),
    ViCareBinarySensorEntityDescription(
        key="charging_active",
        translation_key="domestic_hot_water_charging",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getDomesticHotWaterChargingActive(),
    ),
    ViCareBinarySensorEntityDescription(
        key="dhw_circulationpump_active",
        translation_key="domestic_hot_water_circulation_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getDomesticHotWaterCirculationPumpActive(),
    ),
    ViCareBinarySensorEntityDescription(
        key="dhw_pump_active",
        translation_key="domestic_hot_water_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getDomesticHotWaterPumpActive(),
    ),
    ViCareBinarySensorEntityDescription(
        key="one_time_charge",
        translation_key="one_time_charge",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getOneTimeCharge(),
    ),
    ViCareBinarySensorEntityDescription(
        key="device_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_getter=lambda api: len(api.getDeviceErrors()) > 0,
    ),
    ViCareBinarySensorEntityDescription(
        key="identification_mode",
        translation_key="identification_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_getter=lambda api: api.getIdentification(),
        entity_registry_enabled_default=False,
    ),
    ViCareBinarySensorEntityDescription(
        key="mounting_mode",
        translation_key="mounting_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_getter=lambda api: api.getMountingMode(),
        entity_registry_enabled_default=False,
    ),
    ViCareBinarySensorEntityDescription(
        key="child_safety_lock_mode",
        translation_key="child_safety_lock_mode",
        value_getter=lambda api: api.getChildLock() == "active",
        entity_registry_enabled_default=False,
    ),
    ViCareBinarySensorEntityDescription(
        key="valve",
        translation_key="valve",
        device_class=BinarySensorDeviceClass.DOOR,
        value_getter=lambda api: api.isValveOpen(),
    ),
    ViCareBinarySensorEntityDescription(
        key="outdoor_defrosting",
        translation_key="outdoor_defrosting",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getProperty("heating.outdoor.defrosting")[
            "properties"
        ]["active"]["value"],
    ),
    ViCareBinarySensorEntityDescription(
        key="heating_rod_active",
        translation_key="heating_rod_active",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_getter=lambda api: api.getProperty("heating.heatingRod")["properties"][
            "active"
        ]["value"],
    ),
    ViCareBinarySensorEntityDescription(
        key="condensate_pan_active",
        translation_key="condensate_pan_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_getter=lambda api: api.getProperty("heating.heater.condensatePan")[
            "properties"
        ]["active"]["value"],
        entity_registry_enabled_default=False,
    ),
    ViCareBinarySensorEntityDescription(
        key="fan_ring_active",
        translation_key="fan_ring_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_getter=lambda api: api.getProperty("heating.heater.fanRing")[
            "properties"
        ]["active"]["value"],
        entity_registry_enabled_default=False,
    ),
    ViCareBinarySensorEntityDescription(
        key="secondary_heat_generator_active",
        translation_key="secondary_heat_generator_active",
        value_getter=lambda api: api.getProperty("heating.secondaryHeatGenerator")[
            "properties"
        ]["active"]["value"],
    ),
    ViCareBinarySensorEntityDescription(
        key="ventilation_frost_protection",
        translation_key="ventilation_frost_protection",
        value_getter=lambda api: api.getHeatExchangerFrostProtectionActive(),
    ),
)


def _build_entities(
    device_list: list[ViCareDevice],
) -> list[ViCareBinarySensor]:
    """Create ViCare binary sensor entities for a device."""

    entities: list[ViCareBinarySensor] = []
    for device in device_list:
        # add device entities
        entities.extend(
            ViCareBinarySensor(
                description,
                get_device_serial(device.api),
                device.config,
                device.api,
            )
            for description in GLOBAL_SENSORS
            if is_supported(description.key, description.value_getter, device.api)
        )
        # add component entities
        for component_list, entity_description_list in (
            (get_circuits(device.api), CIRCUIT_SENSORS),
            (get_burners(device.api), BURNER_SENSORS),
            (get_compressors(device.api), COMPRESSOR_SENSORS),
        ):
            entities.extend(
                ViCareBinarySensor(
                    description,
                    get_device_serial(device.api),
                    device.config,
                    device.api,
                    component,
                )
                for component in component_list
                for description in entity_description_list
                if is_supported(description.key, description.value_getter, component)
            )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ViCareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the ViCare binary sensor devices."""
    async_add_entities(
        await hass.async_add_executor_job(
            _build_entities,
            config_entry.runtime_data.devices,
        )
    )


class ViCareBinarySensor(ViCareEntity, BinarySensorEntity):
    """Representation of a ViCare sensor."""

    entity_description: ViCareBinarySensorEntityDescription

    def __init__(
        self,
        description: ViCareBinarySensorEntityDescription,
        device_serial: str | None,
        device_config: PyViCareDeviceConfig,
        device: PyViCareDevice,
        component: PyViCareHeatingDeviceComponent | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            description.key, device_serial, device_config, device, component
        )
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._attr_is_on is not None

    def update(self) -> None:
        """Update state of sensor."""
        try:
            with suppress(PyViCareNotSupportedFeatureError):
                self._attr_is_on = self.entity_description.value_getter(self._api)
        except requests.exceptions.ConnectionError:
            _LOGGER.error("Unable to retrieve data from ViCare server")
        except ValueError:
            _LOGGER.error("Unable to decode data from ViCare server")
        except PyViCareRateLimitError as limit_exception:
            _LOGGER.error("Vicare API rate limit exceeded: %s", limit_exception)
        except PyViCareInvalidDataError as invalid_data_exception:
            _LOGGER.error("Invalid data from Vicare server: %s", invalid_data_exception)
