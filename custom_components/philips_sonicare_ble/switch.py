from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PhilipsSonicareCoordinator
from .const import (
    CONF_AUTO_CONNECT_OVERRIDES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
    auto_connect_key,
    supports_settings_write,
)
from .entity import PhilipsSonicareEntity
from .transport import EspBridgeTransport, MultiSourceTransport

_LOGGER = logging.getLogger(__name__)

SETTINGS_BIT_ADAPTIVE_INTENSITY = 0x1000
SETTINGS_BIT_SCRUBBING_FEEDBACK = 0x0800
SETTINGS_BIT_PRESSURE_FEEDBACK = 0x0200


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Philips Sonicare switch entities."""
    model = entry.data.get("model", "")
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list = []

    if entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE:
        transport = coordinator.transport
        children = (
            transport.children
            if isinstance(transport, MultiSourceTransport)
            else [transport]
        )
        multi = len(children) > 1
        entities.extend(
            SonicareBridgeAutoConnectSwitch(coordinator, entry, child, multi)
            for child in children
        )

    # Settings-bitmask switches only land on devices that accept the writes.
    if supports_settings_write(model) and coordinator.supports_writes:
        entities.extend([
            SonicareSettingsSwitch(
                coordinator, entry,
                "adaptive_intensity",
                SETTINGS_BIT_ADAPTIVE_INTENSITY,
                "mdi:auto-fix",
            ),
            SonicareSettingsSwitch(
                coordinator, entry,
                "scrubbing_feedback",
                SETTINGS_BIT_SCRUBBING_FEEDBACK,
                "mdi:vibrate",
            ),
            SonicareSettingsSwitch(
                coordinator, entry,
                "pressure_feedback",
                SETTINGS_BIT_PRESSURE_FEEDBACK,
                "mdi:gauge",
            ),
        ])

    if entities:
        async_add_entities(entities)


class SonicareSettingsSwitch(PhilipsSonicareEntity, SwitchEntity):
    """Switch entity for a single settings bit on characteristic 0x4420."""

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.coordinator.data:
            state = self.coordinator.data.get("brushing_state")
            if state == "on":
                return False
        return True

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
        key: str,
        bit_mask: int,
        icon: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_{key}"
        self._attr_translation_key = key
        self._attr_icon = icon
        self._bit_mask = bit_mask

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        settings = self.coordinator.data.get("settings_bitmask")
        if settings is None:
            return None
        return bool(settings & self._bit_mask)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_write_settings_bit(self._bit_mask, True)
        settings = self.coordinator.data.get("settings_bitmask", 0)
        self.coordinator.data["settings_bitmask"] = settings | self._bit_mask
        self.coordinator.async_set_updated_data(self.coordinator.data)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_write_settings_bit(self._bit_mask, False)
        settings = self.coordinator.data.get("settings_bitmask", 0)
        self.coordinator.data["settings_bitmask"] = settings & ~self._bit_mask
        self.coordinator.async_set_updated_data(self.coordinator.data)


class SonicareBridgeAutoConnectSwitch(PhilipsSonicareEntity, SwitchEntity):
    """Toggle a single bridge's auto-connect behaviour at runtime.

    Off = the bridge stops chasing the brush (won't connect on next advert).
    Power-cycling the ESP reverts to the YAML default; HA re-applies the
    user's intent via the info-event reconciliation in EspBridgeTransport.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:link-variant"

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
        child: EspBridgeTransport,
        multi: bool,
    ) -> None:
        super().__init__(coordinator, entry)
        self._child = child
        self._key = auto_connect_key(child.device_name, child.bridge_id)
        suffix = f"_{self._key.replace('|', '_')}" if multi else ""
        self._attr_unique_id = f"{self._device_id}_auto_connect{suffix}"
        self._attr_translation_key = "auto_connect"
        if multi:
            label = child.device_name + (f" / {child.bridge_id}" if child.bridge_id else "")
            self._attr_name = f"Auto Connect — {label}"
            self._attr_has_entity_name = False
        # Default state until the first info event populates child.auto_connect
        self._attr_is_on = True

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool | None:
        return self._child.auto_connect

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        await self._child.set_auto_connect(enabled)
        overrides = dict(self.entry.options.get(CONF_AUTO_CONNECT_OVERRIDES, {}) or {})
        overrides[self._key] = enabled
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_AUTO_CONNECT_OVERRIDES: overrides},
        )
        self.async_write_ha_state()
