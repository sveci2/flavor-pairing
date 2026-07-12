"""Normalize layer: entity/source-name resolution under the decision-table
merge rule, derived attribute and pairing-observation rebuilds (CP5), and
derived affinity-group/member rebuilds (CP6) —
docs/DATA_FOUNDATION_PLAN.md §6, §9, §12, §13.

Public API re-exported from the submodules. Duplicate detection lives in
:mod:`flavor_pairing.dupes`; sample regeneration and CI remain out of scope
— see ``docs/DATA_FOUNDATION_PLAN.md`` §20-21.
"""

from flavor_pairing.normalize.affinities import (
    AffinityOutcome,
    affinity_id_for,
    normalize_affinities,
)
from flavor_pairing.normalize.attributes import (
    AttributeSource,
    EntityAttributeRow,
    attribute_id_for,
    build_entity_attribute_rows,
)
from flavor_pairing.normalize.entities import (
    ENTITY_TYPES,
    ENTITY_TYPE_UNKNOWN,
    MACHINE_MAPPING_STATUSES,
    NORMALIZATION_STATUS_AUTO_MAPPED,
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    NORMALIZATION_STATUS_UNRESOLVED,
    REVIEW_STATUSES,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_NEEDS_REVIEW,
    REVIEW_STATUS_REJECTED,
    ROLE_AFFINITY_MEMBER,
    ROLE_PAIRING_ENTRY,
    ROLE_SUBJECT,
    SOURCE_ROLES,
    NameRequest,
    NameResolution,
    NormalizeError,
    ResolutionCounts,
    create_reviewed_entity,
    is_machine_mapping_status,
    resolve_source_names,
    source_name_id_for,
)
from flavor_pairing.normalize.observations import (
    ObservationSource,
    PairingObservationRow,
    build_pairing_observation_rows,
    observation_id_for,
)
from flavor_pairing.normalize.pipeline import NormalizeOutcome, normalize_source

__all__ = [
    "AffinityOutcome",
    "affinity_id_for",
    "normalize_affinities",
    "AttributeSource",
    "EntityAttributeRow",
    "attribute_id_for",
    "build_entity_attribute_rows",
    "ENTITY_TYPES",
    "ENTITY_TYPE_UNKNOWN",
    "MACHINE_MAPPING_STATUSES",
    "NORMALIZATION_STATUS_AUTO_MAPPED",
    "NORMALIZATION_STATUS_HUMAN_MAPPED",
    "NORMALIZATION_STATUS_UNRESOLVED",
    "REVIEW_STATUSES",
    "REVIEW_STATUS_APPROVED",
    "REVIEW_STATUS_NEEDS_REVIEW",
    "REVIEW_STATUS_REJECTED",
    "ROLE_AFFINITY_MEMBER",
    "ROLE_PAIRING_ENTRY",
    "ROLE_SUBJECT",
    "SOURCE_ROLES",
    "NameRequest",
    "NameResolution",
    "NormalizeError",
    "ResolutionCounts",
    "create_reviewed_entity",
    "is_machine_mapping_status",
    "resolve_source_names",
    "source_name_id_for",
    "ObservationSource",
    "PairingObservationRow",
    "build_pairing_observation_rows",
    "observation_id_for",
    "NormalizeOutcome",
    "normalize_source",
]
