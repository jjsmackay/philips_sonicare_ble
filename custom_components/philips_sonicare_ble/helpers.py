"""Shared helpers for the Philips Sonicare BLE integration."""


def esphome_service_id(device_name: str) -> str:
    """Normalize an ESPHome device name to its Home Assistant service prefix.

    ESPHome stores ``device_name`` in mDNS form (e.g. ``atom-lite``) in
    its config entry, but HA registers each ESPHome user-service with
    underscores (e.g. ``atom_lite_ble_read_char``). Run any name that
    came from ``entry.data["device_name"]``, an mDNS hostname, or
    free-text user input through this helper before using it as a
    prefix for ``hass.services.has_service`` /
    ``hass.services.async_services`` lookups, otherwise the lookup
    silently misses every device whose name contains a hyphen.
    """
    return device_name.replace("-", "_")


def bridge_service_name(device_name: str, action: str, bridge_id: str = "") -> str:
    """Build a per-bridge ESPHome service name (e.g. ``atom_lite_ble_unpair_kids``).

    The ``device_name`` must already be in service-id form (run through
    ``esphome_service_id`` if it came from user input or mDNS).
    """
    base = f"{device_name}_{action}"
    return f"{base}_{bridge_id}" if bridge_id else base
