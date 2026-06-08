"""Format-type registry. Each video format is a first-class TYPE that owns its structure,
retention rules, caption strategy, cut cadence, and ad-beat placement — instead of the legacy
monolith stamping every video out the same way."""
from .types import FORMATS, FormatSpec, CaptionStrategy, AdBeat, get_format

__all__ = ["FORMATS", "FormatSpec", "CaptionStrategy", "AdBeat", "get_format"]
