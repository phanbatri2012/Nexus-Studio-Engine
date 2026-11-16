import datetime


META_MIN_SCHEDULE_LEAD_MINUTES = 20
META_MAX_SCHEDULE_AHEAD_DAYS = 29
META_SCHEDULE_MAX_BUFFER_MINUTES = 5


class MetaScheduleWindowError(ValueError):
    """Raised when a requested Facebook schedule is outside Meta's window."""


class MetaScheduleCapacityError(MetaScheduleWindowError):
    """Raised when every safe Meta schedule slot is already occupied."""


def _as_utc(now_utc: datetime.datetime | None = None) -> datetime.datetime:
    value = now_utc or datetime.datetime.now(datetime.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def meta_schedule_window_epochs(
    *,
    now_utc: datetime.datetime | None = None,
    minimum_lead_minutes: int = META_MIN_SCHEDULE_LEAD_MINUTES,
) -> tuple[int, int]:
    """Return the inclusive safe epoch window accepted by Meta scheduling."""
    current = _as_utc(now_utc)
    lead_minutes = max(META_MIN_SCHEDULE_LEAD_MINUTES, int(minimum_lead_minutes))
    earliest = current + datetime.timedelta(minutes=lead_minutes)
    latest = current + datetime.timedelta(
        days=META_MAX_SCHEDULE_AHEAD_DAYS,
        minutes=-META_SCHEDULE_MAX_BUFFER_MINUTES,
    )
    return int(earliest.timestamp()), int(latest.timestamp())


def validate_meta_schedule_epoch(
    value: int | float,
    *,
    now_utc: datetime.datetime | None = None,
    minimum_lead_minutes: int = META_MIN_SCHEDULE_LEAD_MINUTES,
) -> int:
    """Validate and return a Facebook schedule epoch inside the safe Meta window."""
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise MetaScheduleWindowError("Thời gian đặt lịch Facebook không hợp lệ") from exc

    earliest, latest = meta_schedule_window_epochs(
        now_utc=now_utc,
        minimum_lead_minutes=minimum_lead_minutes,
    )
    if epoch < earliest:
        raise MetaScheduleWindowError(
            f"Lịch Facebook phải cách hiện tại ít nhất {max(META_MIN_SCHEDULE_LEAD_MINUTES, int(minimum_lead_minutes))} phút"
        )
    if epoch > latest:
        raise MetaScheduleWindowError(
            f"Lịch Facebook phải nằm trong {META_MAX_SCHEDULE_AHEAD_DAYS} ngày tới"
        )
    return epoch
