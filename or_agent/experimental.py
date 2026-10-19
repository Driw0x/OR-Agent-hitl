"""Opt-in multiple_excerpts/structured_diagnostics research contracts. Literal evidence is never semantic proof."""

from typing import Annotated
import re

from pydantic import Field, ValidationError

from .models import Extraction, Gap, load_json

Excerpt = Annotated[str, Field(min_length=1)]
Excerpts = Annotated[list[Excerpt], Field(min_length=1)]


class MultiExtraction(Extraction):
    """One or more literal excerpts per field; no implicit splitting or joining."""

    provenance: dict[str, Excerpts]

    def check_source(self, source: str):
        if self.disposition == 'ready':
            for field, excerpts in self.provenance.items():
                for index, quote in enumerate(excerpts):
                    if not quote.strip() or quote not in source:
                        raise ValueError(f'provenance for {field}[{index}] must quote the unchanged source exactly')


def read_multi(text, source):
    extraction = MultiExtraction.model_validate(load_json(text))
    extraction.check_source(source)
    return extraction


def diagnose(text, source, provenance='single_excerpt', locked=False):
    """Collect independent schema and quote errors without an evaluation reference.

    Dependent model validators can still stop at one invariant. Do not invent
    downstream diagnostics when types/dimensions make them uninterpretable.
    """
    if provenance not in ('single_excerpt', 'multiple_excerpts'):
        raise ValueError('provenance must be single_excerpt or multiple_excerpts')
    errors = []

    def add(category, field, value, rule, action, data_locked=False, repairability='conditional'):
        errors.append(dict(category=category, field=field, value=value, rule=rule,
                           allowed_action=action, locked=data_locked,
                           repairability=repairability, blocking=True))

    try:
        parsed = load_json(text)
    except (ValueError, TypeError) as error:
        add('syntax', '$', None, str(error), 'Return complete JSON from the unchanged source; never invent values.')
        return errors
    shape_errors = {}
    data = parsed.get('problem') if isinstance(parsed, dict) else None
    if isinstance(data, dict) and isinstance(data.get('tasks'), list) and isinstance(data.get('resources'), list):
        m, n = len(data['resources']), len(data['tasks'])
        capacities = data.get('capacities')
        if isinstance(capacities, list) and len(capacities) != m:
            shape_errors['problem.capacities'] = 'capacities must have one value per resource'
        for field in ('costs', 'consumption'):
            matrix = data.get(field)
            if isinstance(matrix, list) and (len(matrix) != m or
                    any(isinstance(row, list) and len(row) != n for row in matrix)):
                shape_errors[f'problem.{field}'] = f'{field} must have {m} resource rows and {n} task columns'
    contract = Extraction if provenance == 'single_excerpt' else MultiExtraction
    try:
        contract.model_validate(parsed)
    except ValidationError as error:
        for detail in error.errors(include_url=False, include_context=False):
            if detail['msg'] == 'Value error, provenance requires exactly every GAP field':
                continue  # Report the missing/extra field names individually below.
            if detail['msg'] in ('Value error, ' + message for message in shape_errors.values()):
                continue
            location = detail['loc']
            field = '.'.join(str(part) for part in location) or '$'
            data_locked = locked and bool(location) and location[0] == 'problem'
            missing = detail['type'] == 'missing'
            value = detail.get('input')
            if not isinstance(value, (str, int, float, bool, type(None))):
                value = None
            add('schema', field, value, detail['msg'],
                ('Add only missing source-supported data; preserve every parsed leaf. '
                 'If existing data must change, retain them and abstain.') if data_locked else
                'Repair schema only; preserve every problem datum and user constraint.',
                data_locked, 'conditional' if missing or not data_locked else 'locked_or_additive_only')
    for field, rule in shape_errors.items():
        add('schema', field, None, rule,
            'Add only missing source-supported entries; never change or delete parsed data. '
            'If additive repair is impossible, retain data and abstain.', locked,
            'locked_or_additive_only' if locked else 'conditional')
    if not isinstance(parsed, dict) or parsed.get('disposition') != 'ready':
        return errors
    quotes = parsed.get('provenance')
    if not isinstance(quotes, dict):
        return errors
    for field in sorted(set(quotes) - set(Gap.model_fields)):
        add('provenance', field, None, 'Only GAP field names are allowed in ready provenance.',
            'Remove only the unknown provenance key; preserve every problem datum and constraint.')
    for field in Gap.model_fields:
        if field not in quotes:
            add('provenance', field, None, 'Every GAP field needs provenance.',
                'Add exact source excerpts for this field; keep problem data unchanged.')
            continue
        values = [quotes[field]] if provenance == 'single_excerpt' else quotes[field]
        if not isinstance(values, list):
            continue  # The schema diagnostic already identifies the wrong type.
        for index, value in enumerate(values):
            if isinstance(value, str) and (not value.strip() or value not in source):
                add('provenance', field if provenance == 'single_excerpt' else f'{field}[{index}]', value,
                    'Excerpt must be nonblank and occur literally, with unchanged case and whitespace.',
                    'Replace the excerpt with exact source text for this field; keep problem data unchanged.')
    return errors


STRUCTURED_DIRECTIVE = 'Assign each task exactly once; minimize total cost within capacities.'


def structured_gap(source):
    """Reconstruct the entire closed line grammar from source alone.

    No prose, comments, extra constraints or implicit matrix orientation are
    accepted. Row labels identify resources; ordered pair labels identify tasks.
    """
    lines = source.splitlines()
    if not lines or lines[0] != STRUCTURED_DIRECTIVE:
        raise ValueError('structured_source_unrecognized')
    remaining = lines[1:]
    evidence = {}

    def declaration(field, label, pattern):
        matches = [line for line in remaining if re.fullmatch(label + pattern, line)]
        if len(matches) != 1:
            raise ValueError(f'structured_declaration_missing_or_ambiguous:{field}')
        line = matches[0]
        remaining.remove(line)
        evidence[field] = [line]
        return line[len(label):-1]

    identifier = r'[A-Za-z][A-Za-z0-9_-]{0,31}'
    identifiers = identifier + r'(?:, ' + identifier + r')*\.'
    tasks = declaration('tasks', 'Tasks: ', identifiers).split(', ')
    resources = declaration('resources', 'Resources: ', identifiers).split(', ')
    data = dict(tasks=tasks, resources=resources)
    for field, label in (('cost_unit', 'Cost unit: '), ('resource_unit', 'Resource unit: ')):
        data[field] = declaration(field, label, r'[A-Za-z][A-Za-z0-9_-]{0,79}\.')
    for field in ('costs', 'consumption', 'capacities'):
        data[field], evidence[field] = [], []
        for resource in resources:
            label = resource + (' capacity: ' if field == 'capacities' else f' {field}: ')
            matches = [line for line in remaining if line.startswith(label)]
            if len(matches) != 1:
                raise ValueError(f'structured_row_missing_or_ambiguous:{field}:{resource}')
            line = matches[0]
            integer = r'(-?(?:0|[1-9][0-9]*))' if field == 'costs' else r'(0|[1-9][0-9]*)'
            pattern = integer if field == 'capacities' else ', '.join(re.escape(task) + '=' + integer for task in tasks)
            match = re.fullmatch(re.escape(label) + pattern + r'\.', line)
            if match is None:
                raise ValueError(f'structured_row_unrecognized:{field}:{resource}')
            values = [int(value) for value in match.groups()]
            data[field].append(values[0] if field == 'capacities' else values)
            evidence[field].append(line)
            remaining.remove(line)
    if remaining:
        raise ValueError('structured_source_has_unrecognized_lines')
    return Gap.model_validate(data), evidence


def evidence_status(extraction, source):
    """Literal authenticity and a limited, source-only structured proof."""
    extraction.check_source(source)
    status = dict(literal_authenticity=True, coverage='unassessed', fidelity='unassessed',
                  operationally_admissible=extraction.disposition != 'ready')
    if extraction.disposition != 'ready':
        return status
    try:
        reconstructed, evidence = structured_gap(source)
    except ValueError as error:
        return dict(status, reason=str(error))
    for field, lines in evidence.items():
        # Field-specific complete lines are required, not a whole-request quote.
        cited = [line for quote in extraction.provenance[field] for line in quote.splitlines()]
        if set(cited) != set(lines):
            return dict(status, reason=f'structured_field_evidence_incomplete:{field}')
    status['coverage'] = 'verified'
    for field in Gap.model_fields:
        if getattr(extraction.problem, field) != getattr(reconstructed, field):
            return dict(status, reason=f'structured_data_mismatch:{field}')
    return dict(status, fidelity='verified', operationally_admissible=True,
                scope='structured_gap_v1_only')


def check_fidelity(problem: Gap, source: str):
    """Source-only proof using the entire closed structured grammar, without quote coverage.

    Unknown context is unassessed, including apparently explicit data embedded in
    prose. No experimental reference, unit conversion or partial parser is used.
    Compare labelled data so consistent row/column permutations remain faithful.
    """
    report = dict(status='unassessed', coverage='none', checked_fields=[], differences=[])
    try:
        reconstructed, evidence = structured_gap(source)
    except ValueError as error:
        return dict(report, reason=str(error))

    def labelled(data):
        return dict(tasks=sorted(data.tasks), resources=sorted(data.resources),
                    capacities=dict(zip(data.resources, data.capacities)),
                    costs={r: dict(zip(data.tasks, row)) for r, row in zip(data.resources, data.costs)},
                    consumption={r: dict(zip(data.tasks, row)) for r, row in zip(data.resources, data.consumption)},
                    cost_unit=data.cost_unit, resource_unit=data.resource_unit)

    expected, actual = labelled(reconstructed), labelled(problem)
    differences = [dict(field=field, expected=expected[field], actual=actual[field], excerpts=evidence[field])
                   for field in Gap.model_fields if expected[field] != actual[field]]
    return dict(report, status='mismatch' if differences else 'verified', coverage='complete',
                checked_fields=list(Gap.model_fields), differences=differences,
                scope='structured_gap_v1_only')


def diagnostic_feedback(errors):
    """Keep every error and action; omit repeated raw values and constant flags."""
    return [{key: value for key, value in error.items() if key not in ('value', 'blocking')}
            for error in errors]
