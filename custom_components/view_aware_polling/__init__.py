"""View-Aware Polling.

Poll selected integrations ONLY while their entities/devices are actually being
looked at in a browser, instead of on a fixed background timer. See README.

Configuration is UI-only: a single integration entry holds one **subentry per
target** (a whole integration, a device, or an entity), each with its own scope
("any" dashboard vs only a dashboard that shows it) and an optional interval
override. Each target refreshes (while viewed) at its OWN native polling interval:
disabling polling via pref_disable_polling does not clear a coordinator's
`update_interval`, so we read it back from the live entity's coordinator.
"""
from __future__ import annotations

import logging
import os

import voluptuous as vol

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.start import async_at_start

from .const import (
    CONF_INTERVAL,
    CONF_SCOPE,
    CONF_TARGET,
    DOMAIN,
    FALLBACK_INTERVAL,
    MODULE_PATH,
    MODULE_VERSION,
    SUB_DEVICE,
    SUB_ENTITY,
    SUB_INTEGRATION,
)

_LOGGER = logging.getLogger(__name__)


def _native_interval(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a target's native polling interval (seconds) from its live coordinator.

    pref_disable_polling stops the coordinator scheduling but keeps update_interval,
    so this still works while polling is disabled. Returns None if not determinable
    (push-based or non-coordinator entity, or not loaded yet).
    """
    comp = hass.data.get(DATA_INSTANCES, {}).get(entity_id.split(".", 1)[0])
    if comp is None:
        return None
    entity = comp.get_entity(entity_id)
    coordinator = getattr(entity, "coordinator", None) if entity is not None else None
    interval = getattr(coordinator, "update_interval", None) if coordinator is not None else None
    try:
        return interval.total_seconds() if interval else None
    except (AttributeError, TypeError):
        return None


def _unit_interval(hass: HomeAssistant, rep: str, override: int | None) -> int:
    if override:
        return max(2, int(override))
    native = _native_interval(hass, rep)
    if native and native >= 2:
        return int(round(native))
    return FALLBACK_INTERVAL


def _build_payload(hass: HomeAssistant, entry: ConfigEntry | None) -> tuple[dict, set[str]]:
    """Expand the entry's subentries into the frontend payload + target config entries."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # target -> (scope, interval override); device/entity dicts let an explicit
    # device/entity subentry win over one inherited from an integration subentry.
    device_cfg: dict[str, tuple[str, int | None]] = {}
    entity_cfg: dict[str, tuple[str, int | None]] = {}
    target_entries: set[str] = set()

    subentries = list(entry.subentries.values()) if entry is not None else []
    # Integration subentries first, so a more specific device/entity subentry overrides.
    subentries.sort(key=lambda s: s.subentry_type != SUB_INTEGRATION)

    for sub in subentries:
        target = sub.data.get(CONF_TARGET)
        if not target:
            continue
        scope = sub.data.get(CONF_SCOPE, "any")
        override = sub.data.get(CONF_INTERVAL)
        if sub.subentry_type == SUB_INTEGRATION:
            for owner in hass.config_entries.async_entries(target):
                target_entries.add(owner.entry_id)
                for ent in er.async_entries_for_config_entry(ent_reg, owner.entry_id):
                    if ent.device_id:
                        device_cfg.setdefault(ent.device_id, (scope, override))
                    else:
                        entity_cfg.setdefault(ent.entity_id, (scope, override))
        elif sub.subentry_type == SUB_DEVICE:
            device_cfg[target] = (scope, override)
        elif sub.subentry_type == SUB_ENTITY:
            entity_cfg[target] = (scope, override)

    units: list[dict] = []
    device_reps: dict[str, str] = {}
    eid_rep: dict[str, str] = {}

    for dev_id, (scope, override) in device_cfg.items():
        ents = [
            e.entity_id
            for e in er.async_entries_for_device(ent_reg, dev_id, include_disabled_entities=False)
        ]
        if not ents:
            continue
        rep = ents[0]
        device_reps[dev_id] = rep
        for e in ents:
            eid_rep[e] = rep
        units.append({"rep": rep, "scope": scope, "entities": ents, "interval": _unit_interval(hass, rep, override)})
        dev = dev_reg.async_get(dev_id)
        if dev:
            target_entries.update(dev.config_entries)

    for eid, (scope, override) in entity_cfg.items():
        units.append({"rep": eid, "scope": scope, "entities": [eid], "interval": _unit_interval(hass, eid, override)})
        eid_rep[eid] = eid
        ent = ent_reg.async_get(eid)
        if ent and ent.config_entry_id:
            target_entries.add(ent.config_entry_id)

    payload = {
        "units": units,
        "eid_rep": eid_rep,
        "device_reps": device_reps,
        "has_visible": any(u["scope"] == "visible" for u in units),
        "fallback_interval": FALLBACK_INTERVAL,
    }
    return payload, target_entries


async def _register_frontend(hass: HomeAssistant) -> None:
    """Register the websocket command + the global frontend module once."""
    data = hass.data[DOMAIN]
    if data.get("_registered"):
        return

    @websocket_api.websocket_command({vol.Required("type"): "view_aware_polling/config"})
    @callback
    def _ws_config(hass: HomeAssistant, connection, msg) -> None:
        # Built on demand so native intervals are current (targets fully loaded).
        entry_id = hass.data[DOMAIN].get("entry_id")
        entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        payload, _ = _build_payload(hass, entry)
        connection.send_result(msg["id"], payload)

    websocket_api.async_register_command(hass, _ws_config)

    module_file = os.path.join(os.path.dirname(__file__), "frontend", "view_aware_polling.js")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(MODULE_PATH, module_file, False)]
    )
    frontend.add_extra_js_url(hass, f"{MODULE_PATH}?v={MODULE_VERSION}")
    data["_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry_id"] = entry.entry_id

    await _register_frontend(hass)

    async def _disable_polling(_hass: HomeAssistant) -> None:
        _, target_entries = _build_payload(hass, entry)
        for entry_id in target_entries:
            target = hass.config_entries.async_get_entry(entry_id)
            if target and not target.pref_disable_polling:
                hass.config_entries.async_update_entry(target, pref_disable_polling=True)
                _LOGGER.info("Disabled background polling for %s", target.title)
                hass.async_create_task(hass.config_entries.async_reload(entry_id))

    async_at_start(hass, _disable_polling)

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if hass.data.get(DOMAIN, {}).get("entry_id") == entry.entry_id:
        hass.data[DOMAIN]["entry_id"] = None
    return True
