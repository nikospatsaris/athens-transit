"""Europe/Athens without the tzdata package.

Windows ships no IANA database, so `zoneinfo.ZoneInfo("Europe/Athens")` fails on a
plain CPython install. Greece has followed the EU rule since 1981 and the API only
ever returns recent timestamps, so the rule is implemented directly:

    EET  (UTC+2) in winter
    EEST (UTC+3) from 01:00 UTC on the last Sunday of March
                 to 01:00 UTC on the last Sunday of October
"""

from datetime import datetime, timedelta, timezone, tzinfo

_STD = timedelta(hours=2)
_DST = timedelta(hours=3)


def _last_sunday(year: int, month: int) -> datetime:
    """00:00 UTC on the last Sunday of the given month."""
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


class Athens(tzinfo):
    """Fixed-rule EET/EEST zone."""

    def utcoffset(self, dt):
        return _DST if self._is_dst(dt) else _STD

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "EEST" if self._is_dst(dt) else "EET"

    def _is_dst(self, dt) -> bool:
        if dt is None:
            return False
        # dt arrives as local wall time; compare against the local changeover
        # instants (03:00 local in spring, 04:00 local in autumn).
        start = _last_sunday(dt.year, 3) + timedelta(hours=3)
        end = _last_sunday(dt.year, 10) + timedelta(hours=4)
        naive = dt.replace(tzinfo=None)
        return start <= naive < end


ATHENS = Athens()


def to_epoch(local_naive: datetime) -> float:
    """Athens wall-clock datetime -> UTC epoch seconds."""
    return local_naive.replace(tzinfo=ATHENS).timestamp()


def from_epoch(epoch: float) -> datetime:
    """UTC epoch seconds -> Athens wall-clock datetime (naive)."""
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(ATHENS).replace(tzinfo=None)


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone(ATHENS).replace(tzinfo=None)
