"""Select for ViCare."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import logging
from typing import Any

from PyViCare.PyViCareDevice import Device as PyViCareDevice
from PyViCare.PyViCareDeviceConfig import PyViCareDeviceConfig
from PyViCare.PyViCareUtils import (
    PyViCareInvalidDataError,
    PyViCareNotSupportedFeatureError,
    PyViCareRateLimitError,
)
from requests.exceptions import ConnectionError as RequestConnectionError

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ViCareEntity
from .types import ViCareConfigEntry, ViCareDevice, ViCareRequiredKeysMixin
from .utils import get_device_serial, is_supported

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ViCareSelectEntityDescription(SelectEntityDescription, ViCareRequiredKeysMixin):
    """Describes ViCare select entity."""

    value_setter: Callable[[PyViCareDevice, str], Any] | None = None


DEVICE_ENTITY_DESCRIPTIONS: tuple[ViCareSelectEntityDescription, ...] = (
    ViCareSelectEntityDescription(
        key="dhw_operating_mode",
        translation_key="dhw_operating_mode",
        entity_category=EntityCategory.CONFIG,
        options=["comfort", "eco"],
        value_getter=lambda api: api.getProperty("heating.dhw.operating.modes.active")[
            "properties"
        ]["value"]["value"],
        value_setter=lambda api, mode: api.setDomesticHotWaterOperatingMode(mode),
    ),
)


def _build_entities(
    device_list: list[ViCareDevice],
) -> list[ViCareSelect]:
    """Create ViCare select entities for a device."""
    return [
        ViCareSelect(
            description,
            get_device_serial(device.api),
            device.config,
            device.api,
        )
        for device in device_list
        for description in DEVICE_ENTITY_DESCRIPTIONS
        if is_supported(description.key, description.value_getter, device.api)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ViCareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the ViCare select devices."""
    async_add_entities(
        await hass.async_add_executor_job(
            _build_entities,
            config_entry.runtime_data.devices,
        )
    )


class ViCareSelect(ViCareEntity, SelectEntity):
    """Representation of a ViCare select."""

    entity_description: ViCareSelectEntityDescription

    def __init__(
        self,
        description: ViCareSelectEntityDescription,
        device_serial: str | None,
        device_config: PyViCareDeviceConfig,
        device: PyViCareDevice,
    ) -> None:
        """Initialize the select."""
        super().__init__(description.key, device_serial, device_config, device)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._attr_current_option is not None

    def select_option(self, option: str) -> None:
        """Select a new option."""
        if self.entity_description.value_setter:
            self.entity_description.value_setter(self._api, option)
        self.schedule_update_ha_state()

    def update(self) -> None:
        """Update state of select."""
        try:
            with suppress(PyViCareNotSupportedFeatureError):
                value = self.entity_description.value_getter(self._api)
                self._attr_current_option = (
                    value if value in (self.entity_description.options or []) else None
                )
        except RequestConnectionError:
            _LOGGER.error("Unable to retrieve data from ViCare server")
        except ValueError:
            _LOGGER.error("Unable to decode data from ViCare server")
        except PyViCareRateLimitError as limit_exception:
            _LOGGER.error("Vicare API rate limit exceeded: %s", limit_exception)
        except PyViCareInvalidDataError as invalid_data_exception:
            _LOGGER.error("Invalid data from Vicare server: %s", invalid_data_exception)
