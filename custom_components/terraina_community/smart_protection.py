"""Smart protection logic: rain hold, temperature limit, weather forecast check."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

_LOGGER = logging.getLogger(__name__)

_FORECAST_RAIN_STATES = frozenset({
    "rainy", "pouring", "lightning", "lightning-rainy", "exceptional",
})
_RAIN_STOP_DELAY = timedelta(minutes=30)
_CHECK_INTERVAL = timedelta(minutes=10)


class SmartProtectionManager:
    """Watches external sensors and computes whether mowing is safe.

    Checks (priority order):
      1. Rain hold — calculated from precipitation sensor after rain stops
      2. Forecast rain — via weather.get_forecasts service (proactive, N hours ahead)
      3. High temperature — temperature sensor vs configurable threshold
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        on_update: Callable[[bool, str, datetime | None], None],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._on_update = on_update
        self._unsubs: list[Callable] = []
        self._tasks: list[asyncio.Task] = []

        # Rain detection state
        self._last_rain_time: datetime | None = None
        self._last_precip_value: float | None = None
        self._peak_precip_value: float | None = None
        self._rain_hold_until: datetime | None = None

        # Forecast check result (updated async every _CHECK_INTERVAL)
        self._forecast_blocked: bool = False
        self._forecast_reason: str = ""

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @property
    def _opts(self) -> dict:
        return self._entry.options

    @property
    def _precip_sensor(self) -> str | None:
        v = self._opts.get("precipitation_sensor", "")
        return v if v else None

    @property
    def _forecast_entity(self) -> str | None:
        v = self._opts.get("forecast_entity", "")
        return v if v else None

    @property
    def _forecast_hours_ahead(self) -> int:
        return int(self._opts.get("forecast_hours_ahead", 2))

    @property
    def _temp_sensor(self) -> str | None:
        v = self._opts.get("temperature_sensor", "")
        return v if v else None

    @property
    def _max_temp(self) -> float:
        return float(self._opts.get("max_temperature", 32))

    @property
    def _base_hours(self) -> float:
        return float(self._opts.get("rain_hold_base_hours", 0))

    @property
    def _rain_factor(self) -> float:
        return float(self._opts.get("rain_factor_mm", 5))

    @property
    def _step_hours(self) -> float:
        return float(self._opts.get("rain_hold_step_hours", 1))

    @property
    def _max_hours(self) -> float:
        return float(self._opts.get("rain_hold_max_hours", 24))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def restore(self, rain_hold_until: datetime | None) -> None:
        """Restore persisted rain hold from the last HA session."""
        if rain_hold_until and rain_hold_until > datetime.now(timezone.utc):
            self._rain_hold_until = rain_hold_until
            _LOGGER.debug("SmartProtection restored rain_hold_until=%s", rain_hold_until.isoformat())

    def start(self) -> None:
        """Register state listeners and periodic check."""
        if self._precip_sensor:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass, [self._precip_sensor], self._on_precipitation_change
                )
            )
            state = self._hass.states.get(self._precip_sensor)
            if state:
                try:
                    v = float(state.state)
                    self._last_precip_value = v
                    self._peak_precip_value = v
                except (TypeError, ValueError):
                    pass

        # Temperature sensor: state changes are meaningful (sync check)
        if self._temp_sensor:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass, [self._temp_sensor], self._on_sensor_change
                )
            )

        # Periodic check: rain-stopped detection + forecast service call
        self._unsubs.append(
            async_track_time_interval(self._hass, self._periodic_check, _CHECK_INTERVAL)
        )

        # Run forecast check immediately on start
        if self._forecast_entity:
            self._tasks.append(self._hass.async_create_task(self._check_forecast()))

        _LOGGER.debug("SmartProtectionManager started (entry …%s)", self._entry.entry_id[-6:])

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Safe-to-mow computation (synchronous — uses cached forecast result)
    # ------------------------------------------------------------------

    def compute_safe(self) -> tuple[bool, str]:
        """Return (is_safe, human-readable reason)."""
        now = datetime.now(timezone.utc)

        if self._rain_hold_until and now < self._rain_hold_until:
            delta = self._rain_hold_until - now
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            return False, f"Rain hold: {h}h {m}m remaining"

        if self._forecast_blocked:
            return False, self._forecast_reason

        if self._temp_sensor:
            state = self._hass.states.get(self._temp_sensor)
            if state:
                try:
                    temp = float(state.state)
                    if temp >= self._max_temp:
                        return False, f"Too hot: {temp:.1f}°C ≥ {self._max_temp:.0f}°C"
                except (TypeError, ValueError):
                    pass

        return True, ""

    # ------------------------------------------------------------------
    # Async forecast check via weather.get_forecasts service
    # ------------------------------------------------------------------

    async def _check_forecast(self) -> None:
        """Call weather.get_forecasts and cache whether rain is expected.

        Tries hourly first; falls back to daily if the entity does not support hourly.
        """
        if not self._forecast_entity:
            return

        forecasts: list[dict] = []
        forecast_type = "hourly"

        try:
            response = await self._hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self._forecast_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            forecasts = (response or {}).get(self._forecast_entity, {}).get("forecast", [])
        except Exception:
            # Entity does not support hourly — try daily
            try:
                response = await self._hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": self._forecast_entity, "type": "daily"},
                    blocking=True,
                    return_response=True,
                )
                forecasts = (response or {}).get(self._forecast_entity, {}).get("forecast", [])
                forecast_type = "daily"
                _LOGGER.debug(
                    "Forecast: %s does not support hourly, using daily", self._forecast_entity
                )
            except Exception as err:
                _LOGGER.debug(
                    "Forecast service call failed for %s: %s", self._forecast_entity, err
                )
                self._notify()
                return

        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=self._forecast_hours_ahead)

        self._forecast_blocked = False
        self._forecast_reason = ""

        for entry in forecasts:
            dt_str = entry.get("datetime", "")
            condition = entry.get("condition", "")
            try:
                dt = datetime.fromisoformat(dt_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                if forecast_type == "daily":
                    # Daily entries cover a full day — check if the day overlaps our window
                    if dt > cutoff:
                        break
                    if condition in _FORECAST_RAIN_STATES:
                        self._forecast_blocked = True
                        self._forecast_reason = f"Rain forecast today ({condition})"
                        _LOGGER.info(
                            "Forecast check: daily rain on %s (%s)", dt_str, condition
                        )
                        break
                else:
                    if dt > cutoff:
                        break
                    if condition in _FORECAST_RAIN_STATES:
                        hours_from_now = max(0, int((dt - now).total_seconds() / 3600))
                        self._forecast_blocked = True
                        self._forecast_reason = (
                            f"Rain forecast in {hours_from_now}h ({condition})"
                        )
                        _LOGGER.info(
                            "Forecast check: rain expected at %s (%s)", dt_str, condition
                        )
                        break
            except (TypeError, ValueError):
                continue

        if not self._forecast_blocked:
            _LOGGER.debug(
                "Forecast check: no rain in next %dh (%s)", self._forecast_hours_ahead, forecast_type
            )

        self._notify()

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    @callback
    def _on_precipitation_change(self, event) -> None:
        new_state = event.data.get("new_state")
        if not new_state:
            return
        try:
            new_val = float(new_state.state)
        except (TypeError, ValueError):
            return

        prev = self._last_precip_value
        self._last_precip_value = new_val

        if prev is None:
            self._peak_precip_value = new_val
            return

        # Midnight reset: sensor goes from high to near-zero — keep hold state
        if new_val < 1.0 and prev > 5.0:
            _LOGGER.debug(
                "Precipitation midnight reset (%.1f→%.1f) — hold state preserved", prev, new_val
            )
            self._last_rain_time = None
            self._peak_precip_value = None
            return

        # Rain increasing: record time and peak value for hold calculation
        if new_val > (prev + 0.05):
            self._last_rain_time = datetime.now(timezone.utc)
            self._peak_precip_value = new_val
            _LOGGER.debug("Rain detected: %.2f mm today", new_val)

        self._notify()

    @callback
    def _on_sensor_change(self, event) -> None:
        self._notify()

    @callback
    def _periodic_check(self, _now=None) -> None:
        now = datetime.now(timezone.utc)

        # Rain-stopped detection: no increase for 30 min → calculate hold
        if (
            self._last_rain_time is not None
            and self._peak_precip_value is not None
            and (now - self._last_rain_time) >= _RAIN_STOP_DELAY
        ):
            extra_hours = self._base_hours + (
                int(self._peak_precip_value / self._rain_factor) * self._step_hours
            )
            hold = min(timedelta(hours=extra_hours), timedelta(hours=self._max_hours))
            self._rain_hold_until = now + hold
            _LOGGER.info(
                "Rain stopped. Daily precipitation: %.1f mm → "
                "base=%.0fh + floor(%.1f/%.0f)×%.0fh = %.1fh hold (until %s)",
                self._peak_precip_value,
                self._base_hours,
                self._peak_precip_value,
                self._rain_factor,
                self._step_hours,
                extra_hours,
                self._rain_hold_until.isoformat(),
            )
            self._last_rain_time = None
            self._peak_precip_value = None

        # Trigger async forecast check
        if self._forecast_entity:
            self._tasks.append(self._hass.async_create_task(self._check_forecast()))
        else:
            self._notify()

    def _notify(self) -> None:
        safe, reason = self.compute_safe()
        self._on_update(safe, reason, self._rain_hold_until)
