"""Turn a local wall time into UTC seconds.

Shared by the reminder time parser and the repeat rules, so both read
a local time with the same daylight saving rules. No Discord or
database code here.
"""

import datetime
import zoneinfo


def local_to_utc(
    date: datetime.date, clock: datetime.time, zone: zoneinfo.ZoneInfo
) -> int:
    """Return the UTC seconds of a local date and clock time.

    fold=0 handles daylight saving. A time that occurs twice uses the
    first occurrence. A time that does not exist is read with the
    offset from before the change, which lands that far past it:
    2:30 AM on the spring-forward day becomes 3:30 AM.
    """
    local = datetime.datetime.combine(
        date, clock.replace(tzinfo=None, fold=0), tzinfo=zone
    )
    return int(local.timestamp())
