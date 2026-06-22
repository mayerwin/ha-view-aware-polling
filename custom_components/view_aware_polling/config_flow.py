"""Config flow for View-Aware Polling.

A single integration entry holds one **subentry per target**. There are three
subentry types - integration, device, entity - and each subentry stores its own
target, scope ("any" dashboard vs only one that shows it) and optional interval
override. The integration page then shows one readable row per target.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector
from homeassistant.loader import async_get_integration

from .const import (
    CONF_INTERVAL,
    CONF_ONLY_WHEN_SHOWN,
    CONF_SCOPE,
    CONF_TARGET,
    DOMAIN,
    SUB_DEVICE,
    SUB_ENTITY,
    SUB_INTEGRATION,
)

_INTERVAL_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=2, max=3600, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s"
    )
)


def _title(name: str, scope: str, interval: int | None) -> str:
    """Human-readable subentry row title, e.g. 'Proxmox VE (only when shown, 45s)'."""
    bits: list[str] = []
    if scope == "visible":
        bits.append("only when shown")
    if interval:
        bits.append(f"{interval}s")
    return f"{name} ({', '.join(bits)})" if bits else name


class _TargetSubentryFlow(ConfigSubentryFlow):
    """Shared add/edit flow for a single target. Subclasses pick the target type."""

    subentry_type: str

    def _target_selector(self) -> selector.Selector:
        """The selector used to pick the target. Overridden per type."""
        raise NotImplementedError

    async def _resolve_name(self, target: str) -> str:
        """Friendly display name for the picked target. Overridden per type."""
        return target

    def _form(self, defaults: dict[str, Any], include_target: bool) -> vol.Schema:
        schema: dict[Any, Any] = {}
        if include_target:
            schema[vol.Required(CONF_TARGET)] = self._target_selector()
        schema[vol.Required(CONF_ONLY_WHEN_SHOWN, default=defaults.get(CONF_ONLY_WHEN_SHOWN, False))] = (
            selector.BooleanSelector()
        )
        schema[vol.Optional(CONF_INTERVAL, description={"suggested_value": defaults.get(CONF_INTERVAL)})] = (
            _INTERVAL_SELECTOR
        )
        return vol.Schema(schema)

    @staticmethod
    def _data_from_input(target: str, user_input: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {
            CONF_TARGET: target,
            CONF_SCOPE: "visible" if user_input.get(CONF_ONLY_WHEN_SHOWN) else "any",
        }
        interval = user_input.get(CONF_INTERVAL)
        if interval:
            data[CONF_INTERVAL] = int(interval)
        return data

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            target = user_input[CONF_TARGET]
            for existing in self._get_entry().subentries.values():
                if existing.subentry_type == self.subentry_type and existing.data.get(CONF_TARGET) == target:
                    return self.async_abort(reason="already_configured")
            data = self._data_from_input(target, user_input)
            name = await self._resolve_name(target)
            return self.async_create_entry(
                title=_title(name, data[CONF_SCOPE], data.get(CONF_INTERVAL)), data=data
            )
        return self.async_show_form(step_id="user", data_schema=self._form({}, include_target=True))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        target = sub.data[CONF_TARGET]
        if user_input is not None:
            data = self._data_from_input(target, user_input)
            name = await self._resolve_name(target)
            return self.async_update_and_abort(
                self._get_entry(),
                sub,
                title=_title(name, data[CONF_SCOPE], data.get(CONF_INTERVAL)),
                data=data,
            )
        defaults = {
            CONF_ONLY_WHEN_SHOWN: sub.data.get(CONF_SCOPE) == "visible",
            CONF_INTERVAL: sub.data.get(CONF_INTERVAL),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._form(defaults, include_target=False),
            description_placeholders={"name": await self._resolve_name(target)},
        )


class IntegrationSubentryFlow(_TargetSubentryFlow):
    subentry_type = SUB_INTEGRATION

    def _target_selector(self) -> selector.Selector:
        domains = sorted({entry.domain for entry in self.hass.config_entries.async_entries()})
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=domains,
                mode=selector.SelectSelectorMode.DROPDOWN,
                custom_value=True,
                sort=True,
            )
        )

    async def _resolve_name(self, target: str) -> str:
        try:
            return (await async_get_integration(self.hass, target)).name
        except Exception:  # noqa: BLE001 - unknown/uninstalled domain, fall back to the slug
            return target


class DeviceSubentryFlow(_TargetSubentryFlow):
    subentry_type = SUB_DEVICE

    def _target_selector(self) -> selector.Selector:
        return selector.DeviceSelector()

    async def _resolve_name(self, target: str) -> str:
        device = dr.async_get(self.hass).async_get(target)
        if device is None:
            return target
        return device.name_by_user or device.name or target


class EntitySubentryFlow(_TargetSubentryFlow):
    subentry_type = SUB_ENTITY

    def _target_selector(self) -> selector.Selector:
        return selector.EntitySelector()

    async def _resolve_name(self, target: str) -> str:
        state = self.hass.states.get(target)
        if state and state.attributes.get("friendly_name"):
            return state.attributes["friendly_name"]
        entry = er.async_get(self.hass).async_get(target)
        if entry:
            return entry.name or entry.original_name or target
        return target


class ViewAwarePollingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance main flow; all real config lives in subentries."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {
            SUB_INTEGRATION: IntegrationSubentryFlow,
            SUB_DEVICE: DeviceSubentryFlow,
            SUB_ENTITY: EntitySubentryFlow,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="View-Aware Polling", data={})
        return self.async_show_form(step_id="user")
