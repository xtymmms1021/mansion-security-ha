"""Constants for the Mansion Security integration."""

from datetime import timedelta

DOMAIN = "mansion_security"
CONF_SESSION_CODE = "session_code"
CONF_ALLOW_UNLOCK_MODES = "allow_unlock_modes"
CONF_PAIR_HASH = "pair_hash"
CONF_NICKNAME = "nickname"
CONF_ACCEPT_TERMS = "accept_terms"
CONF_NEW_PASSWORD = "new_password"
CONF_CONFIRM_PASSWORD = "confirm_password"
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)
AUTO_OFF_TIMEOUT = timedelta(hours=2)
AUTO_OFF_RETRY_INTERVAL = timedelta(seconds=15)
CONF_MODE_DEADLINE = "mode_deadline"
CONF_TRACKED_MODE = "tracked_mode"
PAIRING_RENEWAL_DAYS = 7
