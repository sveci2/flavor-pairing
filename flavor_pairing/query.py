"""Read-only query layer over a canonical sample package (CP8).

Loads one package directory (``data/sample/`` or any conforming copy) into
a read-only API snapshot and answers entity-centric questions with
deterministic ordering. Strictly read-only: files are opened for reading
only, nothing is ever written, and no SQLite connection is involved. (The
snapshot's internal indexes are ordinary private containers; the class
exposes no mutating operations.)

Identity and provenance fields are returned exactly as stored:

- ``observation_id`` identifies the normalized observation;
- ``source_record_id`` links it back to the parsed/raw source record
  (``parsed_source_rows`` and ``raw_source_rows`` are keyed by it together
  with ``source_id``; the schema has no separate parsed_row_id);
- ``source_id`` identifies the source.

Affinity members carry every column the schema stores for them —
``affinity_id``, ``member_order``, ``member_entity_id``,
``member_text_raw``, ``normalization_status`` — and nothing more: a member
row's own identifier is the composite ``(affinity_id, member_order)``, and
its source provenance flows through the parent group's
``(source_id, source_record_id)``.

Query semantics (approved CP8 design):

- Entity resolution is exact canonical-name lookup after
  ``strip().casefold()`` — nothing else. No fuzzy, plural, alias,
  substring, or partial matching; several entities sharing one folded name
  raise :class:`AmbiguousEntityError` rather than guessing.
- "Pairings for an entity" are two explicitly separated directions —
  ``as_subject`` and ``as_paired`` — never merged and never treated as
  symmetric. Reverse-pair evidence pairs the two directional observation
  lists without combining scores or inferring a canonical direction.
- Affinity groups remain distinct from binary pairings; an entity's
  affinity view separates groups it is the subject of from groups it is a
  member of.
- Stored NULLs stay ``None`` (project convention: a blank CSV cell is
  NULL); nothing is invented, defaulted, aggregated, or reinterpreted.
  Every observation is returned separately.

:class:`EntityQueryResult` is the single top-level model for one entity's
complete answer; every output format (plain text, JSON) must be rendered
from it so renderings cannot diverge.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flavor_pairing.validation import EXPECTED_HEADERS

__all__ = [
    "AffinityGroupResult",
    "AffinityMemberResult",
    "AffinityView",
    "AmbiguousEntityError",
    "AttributeResult",
    "EntityQueryResult",
    "EntityView",
    "FlavorPackage",
    "ObservationsView",
    "PairingResult",
    "QueryError",
    "ReversePairEvidence",
    "UnresolvedMapping",
]

# The package files this layer reads (a subset of the full package).
_QUERY_FILES = (
    "entities.csv",
    "entity_source_names.csv",
    "pairing_observations.csv",
    "entity_attributes.csv",
    "affinity_groups.csv",
    "affinity_members.csv",
)


class QueryError(Exception):
    """The package directory cannot be queried (missing/malformed files)."""


class AmbiguousEntityError(QueryError):
    """Several entities share the requested folded canonical name."""

    def __init__(self, name: str, candidates: Tuple[str, ...]):
        self.name = name
        self.candidates = candidates
        super().__init__(
            f"entity name {name!r} is ambiguous; candidates: {list(candidates)}"
        )


@dataclass(frozen=True)
class EntityView:
    entity_id: str
    canonical_name: Optional[str]
    display_name: Optional[str]
    entity_type: Optional[str]
    parent_entity_id: Optional[str]
    normalization_status: Optional[str]
    review_status: Optional[str]
    notes: Optional[str]


@dataclass(frozen=True)
class PairingResult:
    """One stored pairing observation, verbatim, with full provenance."""

    observation_id: str
    source_id: str
    source_record_id: str
    subject_entity_id: str
    paired_entity_id: Optional[str]
    paired_text_raw: Optional[str]
    strength_label: Optional[str]
    strength_score: Optional[int]
    strength_method: Optional[str]
    normalization_status: Optional[str]
    review_status: Optional[str]


@dataclass(frozen=True)
class ObservationsView:
    """The two pairing directions for one entity, kept strictly apart."""

    as_subject: Tuple[PairingResult, ...]
    as_paired: Tuple[PairingResult, ...]


@dataclass(frozen=True)
class AttributeResult:
    attribute_id: str
    source_id: str
    source_record_id: str
    entity_id: Optional[str]
    attribute_name: Optional[str]
    attribute_value_raw: Optional[str]
    attribute_value_normalized: Optional[str]
    normalization_method: Optional[str]
    review_status: Optional[str]


@dataclass(frozen=True)
class AffinityMemberResult:
    """One affinity member row — every column the schema stores for it.

    Identified by the composite (affinity_id, member_order); the schema
    defines no other member-level identifier, review, or provenance
    columns (source provenance lives on the parent group).
    """

    affinity_id: str
    member_order: int
    member_text_raw: Optional[str]
    member_entity_id: Optional[str]
    normalization_status: Optional[str]


@dataclass(frozen=True)
class AffinityGroupResult:
    affinity_id: str
    source_id: str
    source_record_id: str
    subject_entity_id: Optional[str]
    affinity_text_raw: Optional[str]
    review_status: Optional[str]
    members: Tuple[AffinityMemberResult, ...]


@dataclass(frozen=True)
class AffinityView:
    """Groups where the entity is the subject vs where it is a member."""

    as_subject: Tuple[AffinityGroupResult, ...]
    as_member: Tuple[AffinityGroupResult, ...]


@dataclass(frozen=True)
class ReversePairEvidence:
    """Both directions observed for one unordered entity pair.

    ``entity_a`` < ``entity_b``; each directional tuple keeps its own
    observations unmerged — no symmetry is assumed.
    """

    entity_a: str
    entity_b: str
    observations_a_to_b: Tuple[PairingResult, ...]
    observations_b_to_a: Tuple[PairingResult, ...]


@dataclass(frozen=True)
class UnresolvedMapping:
    source_name_id: str
    source_id: str
    source_text: str
    source_role: str
    normalization_status: Optional[str]
    notes: Optional[str]


@dataclass(frozen=True)
class EntityQueryResult:
    """The single top-level answer for one entity.

    Every CLI/output rendering must be produced from this model so plain
    text and JSON cannot diverge. The two unresolved sections are
    package-wide reports (an unresolved mapping has no entity to attach
    to), included so one query shows the full review surface.
    """

    entity: EntityView
    pairings: ObservationsView
    reverse_pairs: Tuple[ReversePairEvidence, ...]
    attributes: Tuple[AttributeResult, ...]
    affinities: AffinityView
    unresolved_mappings: Tuple[UnresolvedMapping, ...]
    unresolved_observations: Tuple[PairingResult, ...]


def _fold(name: str) -> str:
    """The one and only name-folding rule: trim + casefold, nothing more."""
    return name.strip().casefold()


def _null(value: Optional[str]) -> Optional[str]:
    """Project null convention: blank cell means NULL."""
    return value if value else None


def _read_table(path: Path, file_name: str) -> List[Dict[str, str]]:
    if not path.is_file():
        raise QueryError(
            f"{file_name}: required package file is missing from {path.parent}"
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        expected = list(EXPECTED_HEADERS[file_name])
        if header != expected:
            raise QueryError(
                f"{file_name}: header {header} does not match the specification {expected}"
            )
        rows: List[Dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            if row.get(None):
                raise QueryError(
                    f"{file_name} line {line_number}: row has more cells than the "
                    f"header declares; extra values {row[None]!r}"
                )
            rows.append({key: (value or "") for key, value in row.items() if key is not None})
    return rows


def _score(value: str, observation_id: str) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise QueryError(
            f"pairing_observations.csv: observation '{observation_id}' has "
            f"non-integer strength_score {value!r}"
        ) from None


class FlavorPackage:
    """A read-only API snapshot of one package directory."""

    def __init__(
        self,
        package_dir: Path,
        entities: Tuple[EntityView, ...],
        observations: Tuple[PairingResult, ...],
        attributes: Tuple[AttributeResult, ...],
        affinity_groups: Tuple[AffinityGroupResult, ...],
        mappings: Tuple[UnresolvedMapping, ...],
    ):
        self.package_dir = package_dir
        self.entities = entities
        self.observations = observations
        self.attributes = attributes
        self.affinity_groups = affinity_groups
        self._unresolved_mappings = mappings
        self._by_id: Dict[str, EntityView] = {}
        for entity in entities:
            if entity.entity_id in self._by_id:
                raise QueryError(
                    f"entities.csv: duplicate entity_id '{entity.entity_id}'"
                )
            self._by_id[entity.entity_id] = entity
        self._by_folded_name: Dict[str, List[str]] = {}
        for entity in entities:
            if entity.canonical_name:
                key = _fold(entity.canonical_name)
                self._by_folded_name.setdefault(key, []).append(entity.entity_id)

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, package_dir: Path) -> "FlavorPackage":
        package_dir = Path(package_dir)
        if not package_dir.is_dir():
            raise QueryError(f"package directory not found: {package_dir}")
        tables = {
            name: _read_table(package_dir / name, name) for name in _QUERY_FILES
        }

        entities = tuple(
            EntityView(
                entity_id=row["entity_id"],
                canonical_name=_null(row["canonical_name"]),
                display_name=_null(row["display_name"]),
                entity_type=_null(row["entity_type"]),
                parent_entity_id=_null(row["parent_entity_id"]),
                normalization_status=_null(row["normalization_status"]),
                review_status=_null(row["review_status"]),
                notes=_null(row["notes"]),
            )
            for row in sorted(tables["entities.csv"], key=lambda r: r["entity_id"])
        )

        observations = tuple(
            PairingResult(
                observation_id=row["observation_id"],
                source_id=row["source_id"],
                source_record_id=row["source_record_id"],
                subject_entity_id=row["subject_entity_id"],
                paired_entity_id=_null(row["paired_entity_id"]),
                paired_text_raw=_null(row["paired_text_raw"]),
                strength_label=_null(row["strength_label"]),
                strength_score=_score(row["strength_score"], row["observation_id"]),
                strength_method=_null(row["strength_method"]),
                normalization_status=_null(row["normalization_status"]),
                review_status=_null(row["review_status"]),
            )
            for row in sorted(
                tables["pairing_observations.csv"], key=lambda r: r["observation_id"]
            )
        )

        attributes = tuple(
            AttributeResult(
                attribute_id=row["attribute_id"],
                source_id=row["source_id"],
                source_record_id=row["source_record_id"],
                entity_id=_null(row["entity_id"]),
                attribute_name=_null(row["attribute_name"]),
                attribute_value_raw=_null(row["attribute_value_raw"]),
                attribute_value_normalized=_null(row["attribute_value_normalized"]),
                normalization_method=_null(row["normalization_method"]),
                review_status=_null(row["review_status"]),
            )
            for row in sorted(
                tables["entity_attributes.csv"], key=lambda r: r["attribute_id"]
            )
        )

        group_ids = {row["affinity_id"] for row in tables["affinity_groups.csv"]}
        members_by_group: Dict[str, List[AffinityMemberResult]] = {}
        for row in tables["affinity_members.csv"]:
            if row["affinity_id"] not in group_ids:
                raise QueryError(
                    f"affinity_members.csv: member (order {row['member_order']!r}) "
                    f"references unknown affinity_id '{row['affinity_id']}'"
                )
            try:
                member_order = int(row["member_order"])
            except ValueError:
                raise QueryError(
                    f"affinity_members.csv: member of '{row['affinity_id']}' has "
                    f"non-integer member_order {row['member_order']!r}"
                ) from None
            members_by_group.setdefault(row["affinity_id"], []).append(
                AffinityMemberResult(
                    affinity_id=row["affinity_id"],
                    member_order=member_order,
                    member_text_raw=_null(row["member_text_raw"]),
                    member_entity_id=_null(row["member_entity_id"]),
                    normalization_status=_null(row["normalization_status"]),
                )
            )
        affinity_groups = tuple(
            AffinityGroupResult(
                affinity_id=row["affinity_id"],
                source_id=row["source_id"],
                source_record_id=row["source_record_id"],
                subject_entity_id=_null(row["subject_entity_id"]),
                affinity_text_raw=_null(row["affinity_text_raw"]),
                review_status=_null(row["review_status"]),
                members=tuple(
                    sorted(
                        members_by_group.get(row["affinity_id"], []),
                        key=lambda member: member.member_order,
                    )
                ),
            )
            for row in sorted(
                tables["affinity_groups.csv"], key=lambda r: r["affinity_id"]
            )
        )

        mappings = tuple(
            UnresolvedMapping(
                source_name_id=row["source_name_id"],
                source_id=row["source_id"],
                source_text=row["source_text"],
                source_role=row["source_role"],
                normalization_status=_null(row["normalization_status"]),
                notes=_null(row["notes"]),
            )
            for row in sorted(
                tables["entity_source_names.csv"],
                key=lambda r: (r["source_id"], r["source_text"], r["source_role"]),
            )
            if not row["entity_id"]
        )

        return cls(
            package_dir, entities, observations, attributes, affinity_groups, mappings
        )

    # -- entity resolution ---------------------------------------------------

    def entity(self, entity_id: str) -> Optional[EntityView]:
        return self._by_id.get(entity_id)

    def resolve_entity(self, name: str) -> Optional[EntityView]:
        """Exact canonical-name lookup after strip().casefold(); no fuzz."""
        candidates = sorted(self._by_folded_name.get(_fold(name), []))
        if not candidates:
            return None
        if len(candidates) > 1:
            raise AmbiguousEntityError(name, tuple(candidates))
        return self._by_id[candidates[0]]

    # -- per-entity sections ---------------------------------------------------

    def observations_for(self, entity_id: str) -> ObservationsView:
        return ObservationsView(
            as_subject=tuple(
                o for o in self.observations if o.subject_entity_id == entity_id
            ),
            as_paired=tuple(
                o for o in self.observations if o.paired_entity_id == entity_id
            ),
        )

    def reverse_pairs_for(self, entity_id: str) -> Tuple[ReversePairEvidence, ...]:
        directed: Dict[Tuple[str, str], List[PairingResult]] = {}
        for observation in self.observations:
            if observation.paired_entity_id is None:
                continue
            key = (observation.subject_entity_id, observation.paired_entity_id)
            directed.setdefault(key, []).append(observation)

        evidence: List[ReversePairEvidence] = []
        counterparts = {
            paired for subject, paired in directed if subject == entity_id
        } | {subject for subject, paired in directed if paired == entity_id}
        for counterpart in sorted(counterparts):
            if counterpart == entity_id:
                continue  # self-pairs are never reverse pairs
            forward = directed.get((entity_id, counterpart))
            backward = directed.get((counterpart, entity_id))
            if not forward or not backward:
                continue
            entity_a, entity_b = sorted((entity_id, counterpart))
            evidence.append(
                ReversePairEvidence(
                    entity_a=entity_a,
                    entity_b=entity_b,
                    observations_a_to_b=tuple(directed[(entity_a, entity_b)]),
                    observations_b_to_a=tuple(directed[(entity_b, entity_a)]),
                )
            )
        evidence.sort(key=lambda item: (item.entity_a, item.entity_b))
        return tuple(evidence)

    def attributes_for(self, entity_id: str) -> Tuple[AttributeResult, ...]:
        return tuple(a for a in self.attributes if a.entity_id == entity_id)

    def affinities_for(self, entity_id: str) -> AffinityView:
        return AffinityView(
            as_subject=tuple(
                g for g in self.affinity_groups if g.subject_entity_id == entity_id
            ),
            as_member=tuple(
                g
                for g in self.affinity_groups
                if any(m.member_entity_id == entity_id for m in g.members)
            ),
        )

    # -- package-wide sections ---------------------------------------------------

    def unresolved_mappings(self) -> Tuple[UnresolvedMapping, ...]:
        return self._unresolved_mappings

    def unresolved_observations(self) -> Tuple[PairingResult, ...]:
        return tuple(o for o in self.observations if o.paired_entity_id is None)

    # -- the single top-level answer -----------------------------------------------

    def query(self, name: str) -> Optional[EntityQueryResult]:
        """Resolve ``name`` and assemble every section, or None if unknown."""
        entity = self.resolve_entity(name)
        if entity is None:
            return None
        return EntityQueryResult(
            entity=entity,
            pairings=self.observations_for(entity.entity_id),
            reverse_pairs=self.reverse_pairs_for(entity.entity_id),
            attributes=self.attributes_for(entity.entity_id),
            affinities=self.affinities_for(entity.entity_id),
            unresolved_mappings=self.unresolved_mappings(),
            unresolved_observations=self.unresolved_observations(),
        )
