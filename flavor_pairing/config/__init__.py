"""Configuration loading for the flavour-pairing data foundation.

Public API re-exported from :mod:`flavor_pairing.config.loaders`.
"""

from flavor_pairing.config.loaders import (
    AffinitySplitRule,
    AttributeLabel,
    ColumnMapping,
    ConfigError,
    ProjectConfig,
    Source,
    StrengthMapping,
    load_affinity_split_rules,
    load_attribute_labels,
    load_config,
    load_import_mappings,
    load_sources,
    load_strength_mappings,
)

__all__ = [
    "AffinitySplitRule",
    "AttributeLabel",
    "ColumnMapping",
    "ConfigError",
    "ProjectConfig",
    "Source",
    "StrengthMapping",
    "load_affinity_split_rules",
    "load_attribute_labels",
    "load_config",
    "load_import_mappings",
    "load_sources",
    "load_strength_mappings",
]
