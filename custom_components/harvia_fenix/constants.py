DOMAIN = "harvia_fenix"

# existing endpoints config
CONF_ENDPOINTS_URL = "endpoints_url"
DEFAULT_ENDPOINTS_URL = "https://api.harvia.io/endpoints"

# Options keys
CONF_DATA_POLL_INTERVAL = "data_poll_interval"       # stored as label e.g. "30s"
CONF_DEVICE_POLL_INTERVAL = "device_poll_interval"   # stored as label e.g. "2min"

# Dropdown labels -> seconds
POLL_INTERVAL_OPTIONS = {
    "30s": 30,
    "1min": 60,
    "2min": 120,
    "5min": 300,
}

# Default labels (what we store)
DEFAULT_DATA_POLL_LABEL = "30s"
DEFAULT_DEVICE_POLL_LABEL = "2min"

# Optimistic on/off entities + post-command forced-refresh timing (options)
CONF_OPTIMISTIC = "optimistic"
DEFAULT_OPTIMISTIC = True

CONF_FORCED_REFRESH_DELAYS = "forced_refresh_delays"
DEFAULT_FORCED_REFRESH_DELAYS = ["10", "25"]  # seconds after a command
# Preset seconds offered in the options multi-select (custom values allowed too).
FORCED_REFRESH_DELAY_OPTIONS = ["5", "10", "15", "20", "25", "30", "40", "60"]

# Optimistic state is held for (last forced delay + this) seconds.
OPTIMISTIC_TIMEOUT_MARGIN = 5


def parse_forced_delays(value) -> tuple[int, ...]:
    """Parse a list ['10','25'] or a '10,25' string into a sorted tuple of positive ints."""
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = []
    try:
        delays = sorted({int(str(x).strip()) for x in items if str(x).strip()})
    except (ValueError, AttributeError):
        delays = []
    delays = [d for d in delays if d > 0]
    if not delays:
        delays = [int(x) for x in DEFAULT_FORCED_REFRESH_DELAYS]
    return tuple(delays)


SERVICE_DEVICE_COMMAND = "device_command"

ATTR_DEVICE_ID = "device_id"
ATTR_COMMAND = "command"
ATTR_PAYLOAD = "payload"

DEVICE_COORDINATOR = "device_coordinator"
DATA_COORDINATOR = "data_coordinator"
