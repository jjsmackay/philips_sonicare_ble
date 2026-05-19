# custom_components/philips_sonicare/__init__.py
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    DOMAIN,
    CONF_ADDRESS,
    CONF_TRANSPORT_TYPE,
    TRANSPORT_ESP_BRIDGE,
    CONF_ESP_DEVICE_NAME,
    CONF_ESP_BRIDGE_ID,
    CONF_ESP_BRIDGES,
    CONF_AUTO_CONNECT_OVERRIDES,
    CHAR_SERVICE_MAP,
    auto_connect_key,
)
from .coordinator import PhilipsSonicareCoordinator
from .helpers import bridge_service_name, esphome_service_id
from .transport import BleakTransport, EspBridgeTransport, MultiSourceTransport


def get_configured_bridges(entry_data: dict) -> list[dict[str, str]]:
    """Return the de-duplicated list of bridges serving an ESP-bridge entry.

    The legacy single-bridge fields (CONF_ESP_DEVICE_NAME + CONF_ESP_BRIDGE_ID)
    are treated as the primary/first bridge for backwards compatibility.
    Additional bridges live under CONF_ESP_BRIDGES. Empty bridge_ids are normalised
    to "" so dedup is exact.
    """
    bridges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(device_name: str, bridge_id: str) -> None:
        device_name = (device_name or "").strip()
        bridge_id = (bridge_id or "").strip()
        if not device_name:
            return
        key = (device_name, bridge_id)
        if key in seen:
            return
        seen.add(key)
        bridges.append({"device_name": device_name, "bridge_id": bridge_id})

    _add(
        entry_data.get(CONF_ESP_DEVICE_NAME, ""),
        entry_data.get(CONF_ESP_BRIDGE_ID, ""),
    )
    for extra in entry_data.get(CONF_ESP_BRIDGES, []) or []:
        if isinstance(extra, dict):
            _add(extra.get("device_name", ""), extra.get("bridge_id", ""))
    return bridges

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
]

SERVICE_READ_CHARACTERISTIC = "read_characteristic"
SERVICE_WRITE_CHARACTERISTIC = "write_characteristic"
SERVICE_FORCE_WAKE = "force_wake"


def _get_coordinator(hass: HomeAssistant, entry_id: str | None):
    """Resolve coordinator from entry_id or use first available."""
    if entry_id and entry_id in hass.data[DOMAIN]:
        return hass.data[DOMAIN][entry_id]["coordinator"]
    first = next(iter(hass.data[DOMAIN].values()), None)
    return first["coordinator"] if first else None


def _resolve_esp_device_id(hass: HomeAssistant, esp_device_name: str) -> str | None:
    """Find the device-registry id of an ESPHome device by its service-id name."""
    dev_reg = dr.async_get(hass)
    target = esphome_service_id(esp_device_name)
    for esphome_entry in hass.config_entries.async_entries("esphome"):
        entry_name = esphome_service_id(esphome_entry.data.get("device_name", ""))
        if entry_name != target:
            continue
        esp_mac = esphome_entry.unique_id
        if not esp_mac:
            return None
        esp_device = dev_reg.async_get_device(
            connections={(dr.CONNECTION_NETWORK_MAC, esp_mac)}
        )
        return esp_device.id if esp_device else None
    return None


def _async_link_via_esp_device(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Link the Sonicare device (and per-bridge sub-devices) to their ESPs.

    The brush root device is linked to the first configured bridge's ESPHome
    device (HA's ``via_device_id`` is singular). Each per-bridge "Connection"
    sub-device is linked to *its own* ESP so the device tree mirrors the
    multi-bridge topology.
    """
    from .entity import bridge_subdevice_id

    dev_reg = dr.async_get(hass)
    bridges = get_configured_bridges(entry.data)
    if not bridges:
        return
    first_device_name = bridges[0]["device_name"]
    device_id = entry.data.get(CONF_ADDRESS) or first_device_name

    first_esp_id = _resolve_esp_device_id(hass, first_device_name)
    if first_esp_id:
        sonicare_device = dev_reg.async_get_device(
            identifiers={(DOMAIN, device_id)}
        )
        if sonicare_device:
            dev_reg.async_update_device(
                sonicare_device.id, via_device_id=first_esp_id
            )

    for bridge in bridges:
        esp_device_id = _resolve_esp_device_id(hass, bridge["device_name"])
        if not esp_device_id:
            _LOGGER.debug(
                "ESPHome device for '%s' not in registry", bridge["device_name"]
            )
            continue
        sub_id = bridge_subdevice_id(
            device_id, bridge["device_name"], bridge["bridge_id"]
        )
        sub_device = dev_reg.async_get_device(identifiers={(DOMAIN, sub_id)})
        if sub_device:
            dev_reg.async_update_device(sub_device.id, via_device_id=esp_device_id)


# Suffixes that moved from the legacy `_bridge` sub-device to per-bridge
# `_bridge_<key>` sub-devices in entry-version 2. Used by the v1→v2 entity
# registry migration to rename existing unique-ids in-place so single-bridge
# installations keep their entity state and history across the upgrade.
_V2_MIGRATED_SUFFIXES = (
    "bridge_version",
    "bridge_boot_time",
    "esp_bridge_alive",
    "ble_connected",
    "adapter",
    "adapter_type",
    "last_seen",
)


def _migrate_v1_to_v2_esp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rename the legacy `_bridge` sub-device + its entities to the v2 scheme.

    v2 stops special-casing the first bridge. Every Connection sub-device gets
    the same ``{device_id}_bridge_<key>`` identifier, and every per-bridge
    entity's unique_id is sub-device-prefixed. For single-bridge entries that
    existed pre-migration this means a one-time rename.
    """
    from .entity import bridge_subdevice_id

    data = dict(entry.data)
    legacy_device = data.get(CONF_ESP_DEVICE_NAME, "")
    legacy_bid = data.get(CONF_ESP_BRIDGE_ID, "") or ""

    # Ensure CONF_ESP_BRIDGES already contains the legacy bridge.
    bridges = list(data.get(CONF_ESP_BRIDGES, []) or [])
    if legacy_device and not any(
        b.get("device_name") == legacy_device
        and (b.get("bridge_id", "") or "") == legacy_bid
        for b in bridges
    ):
        bridges.insert(0, {"device_name": legacy_device, "bridge_id": legacy_bid})
        data[CONF_ESP_BRIDGES] = bridges
        hass.config_entries.async_update_entry(entry, data=data)

    if not legacy_device:
        return

    device_id = data.get(CONF_ADDRESS) or legacy_device
    new_sub_id = bridge_subdevice_id(device_id, legacy_device, legacy_bid)

    dev_reg = dr.async_get(hass)
    old_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{device_id}_bridge")}
    )
    if old_device:
        new_identifiers = {
            i for i in old_device.identifiers if i != (DOMAIN, f"{device_id}_bridge")
        }
        new_identifiers.add((DOMAIN, new_sub_id))
        try:
            dev_reg.async_update_device(
                old_device.id, new_identifiers=new_identifiers
            )
            _LOGGER.info(
                "Migrated bridge sub-device identifier %s_bridge → %s",
                device_id, new_sub_id,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to migrate bridge sub-device: %s", err)

    ent_reg = er.async_get(hass)
    old_uids = {f"{device_id}_{s}": s for s in _V2_MIGRATED_SUFFIXES}
    for registry_entry in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
        suffix = old_uids.get(registry_entry.unique_id)
        if suffix is None:
            continue
        new_uid = f"{new_sub_id}_{suffix}"
        try:
            ent_reg.async_update_entity(
                registry_entry.entity_id, new_unique_id=new_uid
            )
            _LOGGER.info(
                "Migrated %s unique_id %s → %s",
                registry_entry.entity_id, registry_entry.unique_id, new_uid,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to migrate %s: %s", registry_entry.entity_id, err
            )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries when VERSION changes.

    v1 → v2: per-bridge Connection sub-devices stop special-casing the first
    bridge. Single-bridge ESP entries need their `_bridge` device and the
    seven moved entity unique_ids renamed to the new ``_bridge_<key>`` scheme.
    """
    if entry.version >= 2:
        return True

    if entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE:
        _migrate_v1_to_v2_esp(hass, entry)

    hass.config_entries.async_update_entry(entry, version=2)
    _LOGGER.info("Migrated config entry %s to v2", entry.entry_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Philips Sonicare from a config entry."""
    address = entry.data.get("address", "")
    transport_type = entry.data.get(CONF_TRANSPORT_TYPE)

    if transport_type == TRANSPORT_ESP_BRIDGE:
        bridges = get_configured_bridges(entry.data)
        if not bridges:
            _LOGGER.error("ESP-bridge entry %s has no bridges configured", entry.entry_id)
            return False
        children = [
            EspBridgeTransport(hass, address, b["device_name"], b["bridge_id"])
            for b in bridges
        ]
        overrides = entry.options.get(CONF_AUTO_CONNECT_OVERRIDES, {}) or {}
        for child, bridge_def in zip(children, bridges):
            key = auto_connect_key(bridge_def["device_name"], bridge_def["bridge_id"])
            if key in overrides:
                child.set_desired_auto_connect(bool(overrides[key]))
        if len(children) == 1:
            transport = children[0]
        else:
            transport = MultiSourceTransport(children)
            _LOGGER.info(
                "Multi-bridge mode: %d bridges serving %s",
                len(children), address,
            )
    else:
        transport = BleakTransport(hass, address)

    coordinator = PhilipsSonicareCoordinator(hass, entry, transport)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}

    # Non-blocking first refresh — the toothbrush sleeps most of the time,
    # so blocking startup for a device that may not be reachable is not worth it.
    # Sensors will show "Unknown" briefly until the device wakes up.
    coordinator.async_set_updated_data(coordinator.data or {})

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Link device to ESP bridge in device registry
    if transport_type == TRANSPORT_ESP_BRIDGE:
        _async_link_via_esp_device(hass, entry)

    # Start polling/live monitoring after platforms are registered
    await coordinator.async_start()

    # Register debug services (only once)
    _char_schema = vol.Schema({
        vol.Required("characteristic_uuid"): vol.Any(str, [str]),
        vol.Optional("entry_id"): str,
    })

    async def _read_uuids(coord, raw_input) -> tuple[str, dict[str, dict]]:
        """Read one or more characteristics."""
        if isinstance(raw_input, list):
            uuids = [u.strip().lower() for u in raw_input]
        else:
            uuids = [u.strip().lower() for u in raw_input.split(",")]

        if not coord.transport.is_connected:
            return "not_connected", {u: {"value": None, "bytes": 0} for u in uuids}

        results = {}
        for char_uuid in uuids:
            try:
                raw = await coord.transport.read_char(char_uuid)
            except Exception as e:
                _LOGGER.error("Failed to read characteristic %s: %s", char_uuid, e)
                results[char_uuid] = {"value": None, "bytes": 0, "error": str(e)}
                continue
            if raw is None:
                entry_result: dict[str, Any] = {"value": None, "bytes": 0}
                error = getattr(coord.transport, "pop_read_error", lambda u: None)(char_uuid)
                if error:
                    entry_result["error"] = error
                results[char_uuid] = entry_result
            else:
                results[char_uuid] = {"value": raw.hex(), "bytes": len(raw), "_raw": raw}
        has_errors = any("error" in r for r in results.values())
        has_data = any(r.get("value") is not None for r in results.values())
        if has_errors:
            status = "partial" if has_data else "error"
        else:
            status = "ok"
        return status, results

    if not hass.services.has_service(DOMAIN, SERVICE_READ_CHARACTERISTIC):
        async def handle_read_characteristic(call: ServiceCall) -> ServiceResponse:
            """Read GATT characteristics and return parsed values."""
            coord = _get_coordinator(hass, call.data.get("entry_id"))
            if not coord:
                return {"status": "no_device", "results": {}, "parsed": {}}

            status, results = await _read_uuids(coord, call.data["characteristic_uuid"])

            # Parse requested characteristics in isolation
            to_parse = {uuid: r["_raw"] for uuid, r in results.items() if "_raw" in r}
            parsed = {}
            if to_parse:
                saved_data = coord.data
                try:
                    coord.data = {}
                    parsed_data = coord._process_results(to_parse)
                finally:
                    coord.data = saved_data
                for key, val in parsed_data.items():
                    if key == "last_seen":
                        continue
                    parsed[key] = val

            clean = {uuid: {k: v for k, v in r.items() if k != "_raw"} for uuid, r in results.items()}
            return {"status": status, "results": clean, "parsed": parsed}

        hass.services.async_register(
            DOMAIN, SERVICE_READ_CHARACTERISTIC, handle_read_characteristic,
            schema=_char_schema, supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_WRITE_CHARACTERISTIC):
        async def handle_write_characteristic(call: ServiceCall) -> ServiceResponse:
            """Write a hex value to a BLE GATT characteristic."""
            coord = _get_coordinator(hass, call.data.get("entry_id"))
            if not coord:
                return {"status": "no_device"}

            raw_uuid = call.data["characteristic_uuid"]
            char_uuid = raw_uuid.strip().lower() if isinstance(raw_uuid, str) else raw_uuid
            hex_value = call.data["value"].replace(" ", "")

            if not coord.transport.is_connected:
                return {"status": "not_connected", "characteristic": char_uuid}

            try:
                payload = bytes.fromhex(hex_value)
            except ValueError:
                return {"status": "error", "error": f"Invalid hex value: {hex_value}"}

            try:
                await coord.transport.write_char(char_uuid, payload)
            except Exception as e:
                _LOGGER.error("Failed to write characteristic %s: %s", char_uuid, e)
                return {"status": "error", "characteristic": char_uuid, "error": str(e)}

            return {
                "status": "ok",
                "characteristic": char_uuid,
                "written": hex_value,
                "bytes": len(payload),
            }

        hass.services.async_register(
            DOMAIN, SERVICE_WRITE_CHARACTERISTIC, handle_write_characteristic,
            schema=vol.Schema({
                vol.Required("characteristic_uuid"): str,
                vol.Required("value"): str,
                vol.Optional("entry_id"): str,
            }),
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_WAKE):
        async def handle_force_wake(call: ServiceCall) -> ServiceResponse:
            """Manually trigger the coordinator wake path.

            Stand-in for the BlueZ D-Bus RSSI listener on transports that
            can't fire it (stock bluetooth_proxy without a parallel hci0).
            Used to exercise the reconnect / live-monitoring path during
            debugging when the device's static advertisements get
            deduplicated and the natural wake-via-ADV doesn't fire.
            """
            coord = _get_coordinator(hass, call.data.get("entry_id"))
            if not coord:
                return {"status": "no_device"}
            already_connected = coord.transport.is_connected
            _LOGGER.info(
                "%s: manual wake via force_wake service "
                "(connected=%s)",
                coord.address,
                already_connected,
            )
            coord._handle_wake()
            return {
                "status": "ok",
                "address": coord.address,
                "was_connected": already_connected,
            }

        hass.services.async_register(
            DOMAIN, SERVICE_FORCE_WAKE, handle_force_wake,
            schema=vol.Schema({
                vol.Optional("entry_id"): str,
            }),
            supports_response=SupportsResponse.ONLY,
        )

    _LOGGER.info("Philips Sonicare integration loaded - device: %s", address)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Philips Sonicare config entry."""
    _LOGGER.info("Unloading Philips Sonicare integration started")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    coordinator = hass.data[DOMAIN].pop(entry.entry_id)["coordinator"]
    await coordinator.async_shutdown()

    # Remove services if no more entries
    if not hass.data[DOMAIN]:
        for svc in (
            SERVICE_READ_CHARACTERISTIC,
            SERVICE_WRITE_CHARACTERISTIC,
            SERVICE_FORCE_WAKE,
        ):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)

    # Allow re-discovery for direct BLE devices
    if entry.data.get(CONF_TRANSPORT_TYPE) != TRANSPORT_ESP_BRIDGE:
        from homeassistant.components.bluetooth import async_rediscover_address
        async_rediscover_address(hass, entry.data["address"])

    _LOGGER.info("Unloading Philips Sonicare integration finished")
    return True


async def _unpair_bridge(
    hass: HomeAssistant,
    esp_device_name: str,
    bridge_id: str,
    *,
    wait: bool = True,
) -> None:
    """Issue ble_unpair on a single bridge.

    Best-effort: offline bridges are skipped with a log message; service-call
    failures and missing confirmations are logged but do not raise — callers
    must not let bond cleanup block entry removal. When ``wait=False`` the
    call is fire-and-forget (used after a mis-pair where we just want the
    bond gone, no confirmation needed).
    """
    svc_name = bridge_service_name(esp_device_name, "ble_unpair", bridge_id)

    if not hass.services.has_service("esphome", svc_name):
        _LOGGER.info(
            "ESP bridge %s offline — skipping ble_unpair", esp_device_name
        )
        return

    if not wait:
        try:
            await hass.services.async_call("esphome", svc_name, {}, blocking=False)
        except Exception:  # noqa: BLE001
            pass
        return

    unpair_done = asyncio.Event()

    @callback
    def _on_status(event) -> None:
        if event.data.get("status") != "unpaired":
            return
        if event.data.get("bridge_id", "") != bridge_id:
            return
        # Multi-bridge: also disambiguate by ESP name in case two ESPs share
        # a bridge_id. Older firmware (no device_name in payload) wildcards.
        event_device = event.data.get("device_name", "") or ""
        if event_device and esphome_service_id(event_device) != esp_device_name:
            return
        unpair_done.set()

    unsub = hass.bus.async_listen(
        "esphome.philips_sonicare_ble_status", _on_status
    )
    try:
        await hass.services.async_call(
            "esphome", svc_name, {}, blocking=True,
        )
        try:
            await asyncio.wait_for(unpair_done.wait(), timeout=4.0)
            _LOGGER.info("Removed bond on ESP bridge %s", esp_device_name)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "ble_unpair on %s did not confirm within 4s — bridge may "
                "need a manual reboot to recover",
                esp_device_name,
            )
    except Exception as err:  # noqa: BLE001 — removal must not fail
        _LOGGER.warning(
            "ble_unpair on %s failed: %s", esp_device_name, err
        )
    finally:
        unsub()


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release the device-side bond when the entry is permanently removed.

    Distinct from ``async_unload_entry`` (which fires on every reload /
    restart). Only ``async_remove_entry`` reflects the user's intent to
    delete the entry for good — the right hook for un-bonding the brush
    so the next setup attempt sees a clean slate.

    Two transport-specific paths:

    - **ESP bridge**: call the bridge's ``ble_unpair`` ESPHome service.
      That wipes the BLE bond and the NVS-persisted identity on the
      ESP, returning the bridge to ``pair_capable=true``.
    - **Direct BLE**: drop the host-side BlueZ bond via the same
      ``async_pair_and_trust``-companion D-Bus call we already use for
      stale-bond cleanup during pairing. Symmetric to the auto-pair path.

    Both branches are best-effort. If the ESP is offline or D-Bus is
    unreachable we log and return quietly — HA will delete the entry
    regardless of what this returns.
    """
    transport = entry.data.get(CONF_TRANSPORT_TYPE)

    if transport == TRANSPORT_ESP_BRIDGE:
        bridges = get_configured_bridges(entry.data)
        if not bridges:
            return
        await asyncio.gather(
            *(
                _unpair_bridge(
                    hass,
                    esphome_service_id(b["device_name"]),
                    b["bridge_id"],
                )
                for b in bridges
            ),
            return_exceptions=True,
        )
        return

    # Direct BLE — release host-side BlueZ bond.
    address = entry.data.get(CONF_ADDRESS)
    if not address:
        return
    from .dbus_pairing import async_remove_device, is_dbus_available
    if not is_dbus_available():
        _LOGGER.debug(
            "D-Bus unavailable — skipping host-side unpair for %s", address
        )
        return
    try:
        await async_remove_device(address)
    except Exception as err:  # noqa: BLE001 — removal must not fail
        _LOGGER.warning(
            "Host-side unpair failed during entry removal for %s: %s",
            address,
            err,
        )
