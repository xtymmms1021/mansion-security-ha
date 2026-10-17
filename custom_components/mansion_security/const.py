"""Constants for the Mansion Security integration."""

from datetime import timedelta

DOMAIN = "mansion_security"
CONF_SESSION_CODE = "session_code"
CONF_PAIR_HASH = "pair_hash"
CONF_NICKNAME = "nickname"
CONF_ACCEPT_TERMS = "accept_terms"
CONF_NEW_PASSWORD = "new_password"
CONF_CONFIRM_PASSWORD = "confirm_password"
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)
PAIRING_RENEWAL_DAYS = 7
