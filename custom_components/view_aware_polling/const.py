"""Constants for the View-Aware Polling integration."""
DOMAIN = "view_aware_polling"

MODULE_PATH = "/view_aware_polling/view_aware_polling.js"
MODULE_VERSION = "0.6.0"

# Used only when a target's native polling interval can't be determined
# (e.g. a push-based or non-coordinator integration).
FALLBACK_INTERVAL = 30

# Subentry types (one row per picked target).
SUB_INTEGRATION = "integration"
SUB_DEVICE = "device"
SUB_ENTITY = "entity"

# Subentry data keys.
CONF_TARGET = "target"
CONF_SCOPE = "scope"
CONF_INTERVAL = "interval"
CONF_ONLY_WHEN_SHOWN = "only_when_shown"
