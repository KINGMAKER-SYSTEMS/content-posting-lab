"""Configuration errors raised before any outbound call.

A service whose origin comes from the environment must fail closed when that
variable is unset: no Lab code falls back to a production URL. Jobs and CLIs
raise ``ConfigError``; routes translate it to HTTP 503 "<service> not
configured".
"""


class ConfigError(RuntimeError):
    """A required setting is unset or invalid; nothing was sent anywhere."""
