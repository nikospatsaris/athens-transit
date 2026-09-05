"""Polite client for the OASA telematics API (Athens public transport).

The API needs no key, which makes it easy to hammer. Everything here is throttled
to one request at a time with a minimum gap between them, retries only on
transport errors and 5xx, and identifies itself in the User-Agent.

Quirks this wraps:
  * a valid response can be the bare JSON literal `null` (no routes, no buses,
    stale line code) -- that is not an error, it means "nothing"
  * timestamps look like "Sep  4 2026 10:13:21:000PM": padded day, milliseconds
    joined by a colon, no timezone. They are Athens wall-clock time.
  * numeric fields arrive as strings
"""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("oasa.client")

BASE = "https://telematics.oasa.gr/api/"
DEFAULT_UA = "athens-transit/0.1 (portfolio project; +https://github.com/)"

_WS = re.compile(r"\s+")


class ApiError(RuntimeError):
    pass


def parse_api_datetime(raw):
    """'Sep  4 2026 10:13:21:000PM' -> naive datetime in Athens local time."""
    if not raw:
        return None
    text = _WS.sub(" ", str(raw).strip())
    for fmt in ("%b %d %Y %I:%M:%S:%f%p", "%b %d %Y %I:%M:%S%p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    log.warning("unparseable timestamp: %r", raw)
    return None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class OasaClient:
    def __init__(self, user_agent=DEFAULT_UA, min_interval=0.34, timeout=20.0,
                 retries=3, backoff=1.7):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._last_call = 0.0
        self.requests = 0
        self.errors = 0

    # -- transport ---------------------------------------------------------
    def _throttle(self):
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    def call(self, act, *params):
        query = {"act": act}
        for i, value in enumerate(params, start=1):
            query["p%d" % i] = value
        url = BASE + "?" + urllib.parse.urlencode(query)

        last_error = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            self.requests += 1
            request = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                self.errors += 1
                if exc.code < 500:                       # 4xx will not fix itself
                    raise ApiError("%s -> HTTP %s" % (act, exc.code)) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self.errors += 1
            if attempt < self.retries:
                delay = self.backoff ** attempt
                log.warning("%s attempt %d/%d failed (%s), retrying in %.1fs",
                            act, attempt, self.retries, last_error, delay)
                time.sleep(delay)
        else:
            raise ApiError("%s failed after %d attempts: %s" % (act, self.retries, last_error))

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("%s returned non-JSON (%d bytes)" % (act, len(body))) from exc
        return payload

    def call_list(self, act, *params):
        """Same as call(), but normalises the `null`/dict cases to a list."""
        payload = self.call(act, *params)
        if payload is None:
            return []
        if isinstance(payload, dict):
            return [payload]
        return payload

    # -- endpoints ---------------------------------------------------------
    def lines_with_masterline(self):
        """Every line joined to the master line + schedule-day code it belongs to."""
        return self.call_list("webGetLinesWithMLInfo")

    def routes(self, line_code):
        """Directions of a line. Usually two; loop lines have one."""
        return self.call_list("webGetRoutes", line_code)

    def stops(self, route_code):
        """Ordered stops of a route, with coordinates."""
        return self.call_list("webGetStops", route_code)

    def bus_locations(self, route_code):
        """Live vehicle positions currently assigned to a route."""
        return self.call_list("getBusLocation", route_code)

    def stop_arrivals(self, stop_code):
        """Predicted arrivals at a stop: btime2 is minutes away."""
        return self.call_list("getStopArrivals", stop_code)

    def schedule(self, ml_code, sdc_code, line_code):
        """Timetable for today's service pattern, split into 'go' and 'come'."""
        payload = self.call("getSchedLines", ml_code, sdc_code, line_code)
        if not isinstance(payload, dict):
            return {"go": [], "come": []}
        return {"go": payload.get("go") or [], "come": payload.get("come") or []}
