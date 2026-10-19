"""Closed contracts. Structural validation never establishes source fidelity."""

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")]
Cost = Annotated[int, Field(ge=-10**9, le=10**9)]
Amount = Annotated[int, Field(ge=0, le=10**9)]
Unit = Annotated[str, Field(min_length=1, max_length=80)]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Gap(ClosedModel):
    tasks: list[Identifier] = Field(min_length=1)
    resources: list[Identifier] = Field(min_length=1)
    costs: list[list[Cost]]
    consumption: list[list[Amount]]
    capacities: list[Amount]
    cost_unit: Unit
    resource_unit: Unit

    @model_validator(mode="after")
    def dimensions(self):
        m, n = len(self.resources), len(self.tasks)
        if m * n > 10000:
            raise ValueError("at most 10000 task-resource pairs")
        if len(set(self.tasks)) != n or len(set(self.resources)) != m:
            raise ValueError("task/resource identifiers must be unique within each list")
        if len(self.capacities) != m:
            raise ValueError("capacities must have one value per resource")
        for name in ("costs", "consumption"):
            matrix = getattr(self, name)
            if len(matrix) != m or any(len(row) != n for row in matrix):
                raise ValueError(f"{name} must have {m} resource rows and {n} task columns")
        if not self.cost_unit.strip() or not self.resource_unit.strip():
            raise ValueError("units cannot be blank")
        return self


class Extraction(ClosedModel):
    disposition: Literal["ready", "clarify", "unsupported", "invalid"]
    problem: Gap | None
    issues: list[str]
    provenance: dict[str, str]

    @model_validator(mode="after")
    def disposition_rules(self):
        if self.disposition == "ready":
            if self.problem is None or self.issues:
                raise ValueError("ready requires a complete problem and no issues")
            if set(self.provenance) != set(Gap.model_fields):
                raise ValueError("provenance requires exactly every GAP field")
        elif self.problem is not None or not self.issues or any(not x.strip() for x in self.issues):
            raise ValueError("non-ready requires no problem and nonempty issues")
        return self

    def check_source(self, source: str):
        if self.disposition == "ready":
            for field, quote in self.provenance.items():
                if not quote.strip() or quote not in source:
                    raise ValueError(f"provenance for {field} must quote the unchanged source exactly")


class Assignment(ClosedModel):
    task: Identifier
    resource: Identifier


class Candidate(ClosedModel):
    assignments: list[Assignment]
    objective: int


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError(f"nonfinite JSON number: {value}")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        _nonfinite(value)
    return parsed


def load_json(text: str):
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_nonfinite,
                      parse_float=_finite_float)


def read_gap(text: str) -> Gap:
    return Gap.model_validate(load_json(text))


def read_extraction(text: str, source: str) -> Extraction:
    result = Extraction.model_validate(load_json(text))
    result.check_source(source)
    return result
