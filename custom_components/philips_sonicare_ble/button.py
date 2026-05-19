"""Button entities for the Philips Sonicare integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ESP_BRIDGE_ID,
    CONF_ESP_DEVICE_NAME,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)
from .coordinator import PhilipsSonicareCoordinator
from .entity import (
    PhilipsSonicareEntity,
    bridge_subdevice_id,
    bridge_subdevice_name,
)
from .transport import EspBridgeTransport, MultiSourceTransport

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_TRANSPORT_TYPE) != TRANSPORT_ESP_BRIDGE:
        return

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    transport = coordinator.transport
    children = (
        transport.children
        if isinstance(transport, MultiSourceTransport)
        else [transport]
    )
    async_add_entities(
        SonicareBridgeDisconnectButton(coordinator, entry, child)
        for child in children
    )


class SonicareBridgeDisconnectButton(PhilipsSonicareEntity, ButtonEntity):
    """One-shot disconnect of a single bridge's current BLE link.

    No-op when the bridge is idle. If auto-connect is on, the bridge will try
    to reconnect on the next advertisement; combine with the auto-connect
    switch off to keep the brush free (e.g. so the official app can use it).
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:lan-disconnect"
    _attr_translation_key = "disconnect"

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
        child: EspBridgeTransport,
    ) -> None:
        super().__init__(coordinator, entry)
        self._child = child
        primary_key = (
            entry.data.get(CONF_ESP_DEVICE_NAME, ""),
            entry.data.get(CONF_ESP_BRIDGE_ID, ""),
        )
        sub_id = bridge_subdevice_id(
            self._device_id, child.device_name, child.bridge_id, primary_key
        )
        self._attr_unique_id = f"{sub_id}_disconnect"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, sub_id)},
            manufacturer="Espressif",
            name=bridge_subdevice_name(
                child.device_name, child.bridge_id, primary_key
            ),
        )

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self._child.force_disconnect()
