"""Parse Health Auto Export "Sleep Analysis" payloads into a SleepSummary.

Health Auto Export (see the API JSON format docs:
https://github.com/Lybron/health-auto-export/wiki/API-Export---JSON-Format)
POSTs a payload shaped like:

    {
      "data": {
        "metrics": [
          {
            "name": "sleep_analysis",
            "units": "hr",
            "data": [
              {
                "date": "2026-07-16 06:45:00 +0000",
                "totalSleep": 7.3,
                "asleep": 7.3,
                "core": 4.2,
                "deep": 1.3,
                "rem": 1.8,
                "awake": 0.35,
                "sleepStart": "2026-07-15 23:02:00 +0000",
                "sleepEnd":   "2026-07-16 06:45:00 +0000",
                "inBed": 8.1,
                "source": "Apple Watch"
              }
            ]
          },
          {"name": "heart_rate_variability", "units": "ms",        "data": [{"date": "...", "qty": 68.0}]},
          {"name": "resting_heart_rate",     "units": "count/min", "data": [{"date": "...", "qty": 52.0}]}
        ]
      }
    }

Sleep stage/total durations arrive in *hours*; we convert stages to minutes for
the summary. HRV and resting heart rate arrive as separate scalar metrics.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

QualityFlag = Literal["good", "short", "fragmented", "poor"]

# Metric names Health Auto Export uses for the data we care about.
SLEEP_METRIC = "sleep_analysis"
HRV_METRIC = "heart_rate_variability"
RESTING_HR_METRIC = "resting_heart_rate"

# HAE date format, e.g. "2026-07-15 23:02:00 +0000".
_HAE_DATE_FMT = "%Y-%m-%d %H:%M:%S %z"


def _parse_hae_datetime(value: str) -> datetime:
    return datetime.strptime(value, _HAE_DATE_FMT)


# --- Pydantic models matching the HAE payload -------------------------------


class SleepEntry(BaseModel):
    """One night's `sleep_analysis` record (durations in hours)."""

    date: datetime
    total_sleep: float = Field(alias="totalSleep")
    asleep: Optional[float] = None
    core: float = 0.0
    deep: float = 0.0
    rem: float = 0.0
    awake: float = 0.0
    sleep_start: datetime = Field(alias="sleepStart")
    sleep_end: datetime = Field(alias="sleepEnd")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @field_validator("date", "sleep_start", "sleep_end", mode="before")
    @classmethod
    def _coerce_dt(cls, v):
        return _parse_hae_datetime(v) if isinstance(v, str) else v


class QuantityEntry(BaseModel):
    """A simple scalar metric sample (HRV, resting heart rate)."""

    date: Optional[datetime] = None
    qty: float

    model_config = {"extra": "ignore"}

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_dt(cls, v):
        return _parse_hae_datetime(v) if isinstance(v, str) else v


class Metric(BaseModel):
    name: str
    units: Optional[str] = None
    # Kept as raw dicts so we can validate the right entry model per metric.
    data: list[dict] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class MetricsBody(BaseModel):
    metrics: list[Metric] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class HealthExportPayload(BaseModel):
    data: MetricsBody

    model_config = {"extra": "ignore"}


# --- Domain output ----------------------------------------------------------


class SleepSummary(BaseModel):
    """The parsed, unit-normalized view the rest of Restore consumes."""

    total_hours: float
    deep_minutes: float
    rem_minutes: float
    awake_minutes: float
    sleep_start: datetime
    sleep_end: datetime
    resting_hr: Optional[float] = None
    hrv_ms: Optional[float] = None
    quality_flag: QualityFlag


# --- Quality thresholds -----------------------------------------------------
# Tuned for a typical adult; adjust freely. Every comparison is simple and
# deterministic so the resulting flag is easy to reason about.
#
#   SHORT_HOURS          6.0   below this: not enough total sleep
#   POOR_HOURS           5.5   below this AND shallow/stressed: worst tier
#   MIN_DEEP_MINUTES    45     below this: inadequate deep (restorative) sleep
#   MAX_AWAKE_MINUTES   45     above this: night was fragmented
#   ELEVATED_RESTING_HR 65     resting HR at/above this after a short night
#                              signals poor overnight recovery
#
# Precedence (first match wins): poor -> short -> fragmented -> good
SHORT_HOURS = 6.0
POOR_HOURS = 5.5
MIN_DEEP_MINUTES = 45.0
MAX_AWAKE_MINUTES = 45.0
ELEVATED_RESTING_HR = 65.0


def _quality_flag(
    total_hours: float,
    deep_minutes: float,
    awake_minutes: float,
    resting_hr: Optional[float],
) -> QualityFlag:
    stressed = resting_hr is not None and resting_hr >= ELEVATED_RESTING_HR
    if total_hours < POOR_HOURS and (deep_minutes < MIN_DEEP_MINUTES or stressed):
        return "poor"
    if total_hours < SHORT_HOURS:
        return "short"
    if awake_minutes > MAX_AWAKE_MINUTES:
        return "fragmented"
    return "good"


def _first_qty(body: MetricsBody, name: str) -> Optional[float]:
    """Return the first scalar sample for `name`, or None if absent/unparseable.

    HRV and resting HR are optional context — a malformed sample is logged and
    skipped rather than failing the whole wake.
    """
    for metric in body.metrics:
        if metric.name == name and metric.data:
            try:
                return QuantityEntry.model_validate(metric.data[0]).qty
            except Exception:
                log.warning("Could not parse %s entry: %r", name, metric.data[0])
                return None
    return None


def parse_sleep(payload: dict) -> SleepSummary:
    """Parse a Health Auto Export payload into a SleepSummary.

    Raises ValueError if the payload contains no usable `sleep_analysis` data,
    and pydantic ValidationError if the payload shape is wrong. We surface both
    loudly rather than returning a partial/empty summary.
    """
    parsed = HealthExportPayload.model_validate(payload)

    sleep_metric = next(
        (m for m in parsed.data.metrics if m.name == SLEEP_METRIC and m.data),
        None,
    )
    if sleep_metric is None:
        raise ValueError(
            f"No '{SLEEP_METRIC}' metric with data found in payload; "
            f"got metrics: {[m.name for m in parsed.data.metrics]}"
        )

    # A payload may carry several nights; use the most recent (latest sleepEnd).
    entries = [SleepEntry.model_validate(d) for d in sleep_metric.data]
    entry = max(entries, key=lambda e: e.sleep_end)

    resting_hr = _first_qty(parsed.data, RESTING_HR_METRIC)
    hrv_ms = _first_qty(parsed.data, HRV_METRIC)

    deep_minutes = entry.deep * 60.0
    rem_minutes = entry.rem * 60.0
    awake_minutes = entry.awake * 60.0

    summary = SleepSummary(
        total_hours=round(entry.total_sleep, 2),
        deep_minutes=round(deep_minutes, 1),
        rem_minutes=round(rem_minutes, 1),
        awake_minutes=round(awake_minutes, 1),
        sleep_start=entry.sleep_start,
        sleep_end=entry.sleep_end,
        resting_hr=resting_hr,
        hrv_ms=hrv_ms,
        quality_flag=_quality_flag(entry.total_sleep, deep_minutes, awake_minutes, resting_hr),
    )
    log.info(
        "Parsed sleep: %.2fh total, deep=%.0fm rem=%.0fm awake=%.0fm rhr=%s flag=%s",
        summary.total_hours,
        summary.deep_minutes,
        summary.rem_minutes,
        summary.awake_minutes,
        summary.resting_hr,
        summary.quality_flag,
    )
    return summary
