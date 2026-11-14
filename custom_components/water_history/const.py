"""Constants for Consommation eau (domain: water_history)."""
from datetime import timedelta

DOMAIN = "water_history"

CONF_LOGIN = "login"
CONF_PASSWORD = "password"
CONF_CONTRACT = "contract_id"

WINDOW_DAYS = 10  # days re-read at each poll (late publications are picked up)
MAX_CATCHUP_DAYS = 90  # upper bound when HA was off for a long time
CHUNK_DAYS = 30  # one API request never spans more than this
UPDATE_INTERVAL = timedelta(hours=6)  # 4 polls per day
FIRST_POLL_DELAY = 120  # seconds after setup
ANCHOR_RETRY_DELAY = 600  # seconds, when the sensor has no live statistics yet
AUTH_FAILURES_BEFORE_REAUTH = 3
