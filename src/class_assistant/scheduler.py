from datetime import datetime, time
from zoneinfo import ZoneInfo


def _local(value, zone):
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(zone))
    return value.astimezone(ZoneInfo(zone))


def scheduled_slot(value, zone="Asia/Shanghai"):
    local = _local(value, zone)
    if local.time() < time(8):
        return None
    hour = 20 if local.time() >= time(20) else 8
    return local.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat(timespec="minutes")


def catch_up_slot(now, completed_slots, zone="Asia/Shanghai"):
    slot = scheduled_slot(now, zone)
    if slot is None or slot in set(completed_slots):
        return None
    return slot

