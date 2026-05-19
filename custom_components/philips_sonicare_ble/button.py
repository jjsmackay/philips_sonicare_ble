"""Button entities for the Philips Sonicare integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
    auto_connect_key,
)
from .coordinator import PhilipsSonicareCoordinator
from .entity import PhilipsSonicareEntity
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
    multi = len(children) > 1
    async_add_entities(
        SonicareBridgeDisconnectButton(coordinator, entry, child, multi)
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

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
        child: EspBridgeTransport,
        multi: bool,
    ) -> None:
        super().__init__(coordinator, entry)
        self._child = child
        key = auto_connect_key(child.device_name, child.bridge_id)
        suffix = f"_{key.replace('|', '_')}" if multi else ""
        self._attr_unique_id = f"{self._device_id}_disconnect{suffix}"
        self._attr_translation_key = "disconnect"
        if multi:
            label = child.device_name + (f" / {child.bridge_id}" if child.bridge_id else "")
            self._attr_name = f"Disconnect — {label}"
            self._attr_has_entity_name = False

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self._child.force_disconnect()
