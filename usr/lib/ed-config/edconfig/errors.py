class ConfigError(RuntimeError):
    """A deterministic, user-facing configuration error."""


class ApplyError(ConfigError):
    """A failure while applying a previously validated configuration."""

