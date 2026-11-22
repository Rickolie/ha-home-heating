"""Sensors: the results of Home Heating, and the selected value of every input."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import INPUTS
from .entity import HomeHeatingEntity


@dataclass(frozen=True)
class Description:
    key: str
    name: str
    value: Callable[[dict], object]
    attributes: Callable[[dict], dict] = lambda d: {}
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    icon: str | None = None
    measurement: bool = False


def _ff(d: dict) -> dict:
    return d.get("feedforward") or {}


def _boiler(d: dict) -> dict:
    return d.get("boiler") or {}


def _history(d: dict) -> dict:
    return d.get("history") or {}


def _water_fit(d: dict) -> dict:
    return (_history(d).get("fit") or {}).get("water_target_temp") or {}


SENSORS = [
    Description(
        "feedforward_water_temperature", "Feedforward water temperature", lambda d: _ff(d).get("water_temp"),
        lambda d: {k: _ff(d).get(k) for k in ("mode", "demand", "curve_source", "slope", "intercept", "expected_power",
                                              "expected_cop", "heating_day", "reason")},
        "°C", SensorDeviceClass.TEMPERATURE, measurement=True,
    ),
    Description(
        "feedforward_outside_temperature", "Feedforward outside temperature", lambda d: _ff(d).get("outside_temp_used"),
        lambda d: {"measured": _ff(d).get("outside_temp"), "source": _ff(d).get("outside_source"),
                   "forecast_temp": _ff(d).get("forecast_temp"), "forecast_bias": _ff(d).get("forecast_bias"),
                   "average_used": _ff(d).get("outside_temp_avg_used"), "measured_24h_average": d.get("outside_avg_24h")},
        "°C", SensorDeviceClass.TEMPERATURE, measurement=True,
    ),
    Description("feedforward_mode", "Heat pump mode", lambda d: _ff(d).get("mode") or None,
                lambda d: {"demand": _ff(d).get("demand"), **(_ff(d).get("details") or {})}, icon="mdi:heat-pump"),
    Description(
        "boiler", "Boiler", lambda d: _boiler(d).get("element_reason") or _boiler(d).get("reason") or None,
        lambda d: {k: _boiler(d).get(k) for k in ("element", "instant", "instant_reason", "valve_setpoint",
                                                  "valve_reason", "target_temp", "legionella", "holiday")},
        icon="mdi:water-boiler",
    ),
    Description(
        "boiler_price_plan", "Boiler price plan", lambda d: len((_boiler(d).get("price_plan") or {}).get("slots", [])),
        lambda d: _boiler(d).get("price_plan") or {}, icon="mdi:currency-eur",
    ),
    Description(
        "history", "Heat pump history", lambda d: _history(d).get("days_used"),
        lambda d: {**{k: v for k, v in _history(d).items() if k != "days_used"}, "error": d.get("history_error")},
        "days", icon="mdi:history",
    ),
    Description("history_water_slope", "History water slope", lambda d: _water_fit(d).get("slope"), unit="°C/°C",
                icon="mdi:slope-downhill"),
    Description("history_water_intercept", "History water intercept", lambda d: _water_fit(d).get("intercept"),
                unit="°C", device_class=SensorDeviceClass.TEMPERATURE),
    Description(
        "watchdog", "Watchdog", lambda d: (d.get("watchdog") or {}).get("state"),
        lambda d: {"checks": {k: v for k, v in ((d.get("watchdog") or {}).get("checks") or {}).items()
                              if v["state"] != "ok"}},
        icon="mdi:shield-check",
    ),
]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    controller = entry.runtime_data
    entities: list[SensorEntity] = [HomeHeatingSensor(controller, d) for d in SENSORS]
    entities += [InputSensor(controller, key) for key, (_l, _u, numeric, _n) in INPUTS.items() if numeric]
    weather = [e for e in dict.fromkeys(controller.s.get("weather_temperature_sensors") or []) if e]
    entities += [WeatherTemperatureSensor(controller, entity_id) for entity_id in weather]
    async_add_entities(entities)

    # Remove the sensors of weather entities that are no longer in the list
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_weather_"
    wanted = {f"{prefix}{entity_id}" for entity_id in weather}
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain == "sensor" and reg.unique_id.startswith(prefix) and reg.unique_id not in wanted:
            registry.async_remove(reg.entity_id)


class HomeHeatingSensor(HomeHeatingEntity, SensorEntity):
    def __init__(self, controller, description: Description) -> None:
        super().__init__(controller, description.key, description.name)
        self.description = description
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        self._attr_icon = description.icon
        if description.measurement:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        return self.description.value(self.data) if self.data else None

    @property
    def extra_state_attributes(self) -> dict:
        return self.description.attributes(self.data) if self.data else {}


class InputSensor(HomeHeatingEntity, SensorEntity):
    """The value used for an input, with the entity it came from (master or backup)."""

    def __init__(self, controller, key: str) -> None:
        label, unit, _numeric, _needed = INPUTS[key]
        super().__init__(controller, f"input_{key[3:]}", f"Input {label.lower()}")
        self.key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = SensorDeviceClass.TEMPERATURE if unit == "°C" else (
            SensorDeviceClass.POWER if unit == "W" else SensorDeviceClass.ENERGY if unit == "kWh" else None)
        self._attr_state_class = SensorStateClass.MEASUREMENT if unit != "kWh" else None
        self._attr_entity_registry_enabled_default = True

    @property
    def _reading(self) -> dict:
        return (self.data.get("inputs") or {}).get(self.key) or {}

    @property
    def native_value(self):
        return self._reading.get("value")

    @property
    def extra_state_attributes(self) -> dict:
        r = self._reading
        return {"source": r.get("source"), "status": r.get("status"), "problems": r.get("problems", [])}


class WeatherTemperatureSensor(HomeHeatingEntity, SensorEntity):
    """The current temperature of a weather entity, as a separate sensor (for example as a backup input).

    Unavailable when the weather entity is unavailable or has no temperature, so the watchdog reports it.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, controller, weather_entity: str) -> None:
        st = controller.hass.states.get(weather_entity)
        label = st.name if st else weather_entity.split(".", 1)[1].replace("_", " ")
        super().__init__(controller, f"weather_{weather_entity}", f"{label} temperature")
        self.weather_entity = weather_entity

    def _weather(self):
        st = self.hass.states.get(self.weather_entity)
        if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return st

    @property
    def available(self) -> bool:
        st = self._weather()
        return st is not None and isinstance(st.attributes.get("temperature"), (int, float))

    @property
    def native_value(self):
        st = self._weather()
        return st.attributes.get("temperature") if st else None

    @property
    def native_unit_of_measurement(self) -> str:
        st = self.hass.states.get(self.weather_entity)
        return (st.attributes.get("temperature_unit") if st else None) or UnitOfTemperature.CELSIUS

    @property
    def extra_state_attributes(self) -> dict:
        st = self.hass.states.get(self.weather_entity)
        return {"weather_entity": self.weather_entity, "condition": st.state if st else None}

    async def async_added_to_hass(self) -> None:
        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(async_track_state_change_event(self.hass, [self.weather_entity], _changed))
