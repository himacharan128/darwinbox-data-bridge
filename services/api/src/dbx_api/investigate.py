"""Go and look at the data before interrupting a person.

The pipeline is deterministic and knows nothing about models; this runs after it,
over the result, and only touches cases that are already open. Nothing here can
change a mapping, clean a value or send a record. The most it can do is add a
suggested answer to a question a person was going to be asked anyway, with a note
of what was checked to arrive at it.

That is the whole safety argument for letting it use a model freely: the output
is a suggestion on a case, and a case still needs a person.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dbx_agent import Lookup, investigate_case
from dbx_contracts import (
    Checked,
    EscalationClass,
    MigrationSchema,
    Option,
    ReviewCase,
)

#: Classes where looking at the data can plausibly settle something. A case about
#: two sources disagreeing is worth checking; one about a scan being unreadable is
#: not, because the answer is not in the data.
WORTH_CHECKING = {
    EscalationClass.AMBIGUOUS_MAPPING,
    EscalationClass.AMBIGUOUS_VALUE,
    EscalationClass.UNRESOLVED_REFERENCE,
    EscalationClass.CONFLICTING_FACTS,
    EscalationClass.UNMAPPED_REQUIRED,
    EscalationClass.VALIDATION_UNRESOLVED,
}

#: Model time is not free and a run can raise dozens of cases. The ones blocking
#: the most records are worth the most, so they go first.
MAX_CASES = 10

log = logging.getLogger("dbx.investigate")

LOOKUPS = [
    Lookup(
        name="values_in_column",
        description=(
            "The values a source column actually holds, with how often each occurs. "
            "Use it to see what a column really is rather than what it is called."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Exact file name"},
                "column": {"type": "string", "description": "Exact column name"},
            },
            "required": ["file", "column"],
        },
    ),
    Lookup(
        name="known_values",
        description=(
            "Every value the target schema will accept for a field: its allowed list, "
            "or the codes in the lookup table it references. Use it before suggesting "
            "a replacement for a value that was rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {"field": {"type": "string", "description": "Target field name"}},
            "required": ["field"],
        },
    ),
    Lookup(
        name="what_other_sources_say",
        description=(
            "What every source file says about one employee for one field. Use it when "
            "two sources disagree, to see which value the rest of the files support."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "employee": {"type": "string", "description": "The employee's key"},
                "field": {"type": "string", "description": "Target field name"},
            },
            "required": ["employee", "field"],
        },
    ),
    Lookup(
        name="columns_in_file",
        description=(
            "The column names of one source file, with a sample from each. Use it to "
            "see what else a file holds before deciding where a column belongs."
        ),
        input_schema={
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
    ),
]


def _tools_over(result: Any, schema: MigrationSchema, profiles: dict) -> Any:
    """Bind the read-only lookups to one run's data."""
    by_field = {f.name: f for f in schema.fields}

    def values_in_column(file: str, column: str) -> str:
        for (fname, header), profile in profiles.items():
            if fname == file and header == column:
                top = ", ".join(f"{v.value!r} x{v.count}" for v in profile.top_values[:12])
                return (
                    f"{profile.non_null} of {profile.total} rows filled, "
                    f"{profile.distinct} distinct. Values: {top or 'none'}"
                )
        return f"no column {column!r} in {file!r}"

    def known_values(field: str) -> str:
        spec = by_field.get(field)
        if spec is None:
            return f"{field!r} is not a field in this schema"
        if spec.allowed:
            return f"{field} accepts exactly: {', '.join(spec.allowed)}"
        ref = spec.reference_target
        if ref and ref[0] in result.lookups:
            codes = sorted(result.lookups[ref[0]])
            return f"{field} must be one of the {len(codes)} {ref[0]} codes: {', '.join(codes)}"
        bits = [f"type {spec.type.value}"]
        if spec.pattern:
            bits.append(f"pattern {spec.pattern}")
        return f"{field} has no fixed list of values ({', '.join(bits)})"

    def what_other_sources_say(employee: str, field: str) -> str:
        for record in result.records:
            if (record.natural_key or record.id) != employee:
                continue
            prov = record.provenance.get(field)
            here = f"the merged record has {record.values.get(field)!r}"
            if prov is None:
                return here
            return (
                f"{here}; it came from {prov.source.label()} as {prov.raw_value!r}. "
                f"This employee was built from: "
                f"{', '.join(r.label() for r in record.contributing)}"
            )
        return f"no employee {employee!r} in this run"

    def columns_in_file(file: str) -> str:
        found = [
            f"{header} (e.g. {', '.join(p.sample[:2]) or 'empty'})"
            for (fname, header), p in profiles.items() if fname == file
        ]
        return f"{file} has {len(found)} columns: " + "; ".join(found) if found else \
            f"no file named {file!r}"

    table = {
        "values_in_column": values_in_column,
        "known_values": known_values,
        "what_other_sources_say": what_other_sources_say,
        "columns_in_file": columns_in_file,
    }

    def run_lookup(name: str, args: dict) -> str:
        fn = table.get(name)
        if fn is None:
            return f"no lookup called {name!r}"
        try:
            return fn(**args)
        except TypeError:
            return f"{name} was called with the wrong arguments: {json.dumps(args)}"

    return run_lookup


def _question(case: ReviewCase) -> str:
    bits = [case.headline, case.detail]
    if case.record_key:
        bits.append(f"This is about employee {case.record_key}.")
    if case.target_field:
        bits.append(f"The target field involved is {case.target_field}.")
    if case.raw_values:
        bits.append(f"The value in the file is {case.raw_values[0]!r}.")
    if case.source_refs:
        bits.append(f"It came from {case.source_refs[0].label()}.")
    if case.options:
        choices = ", ".join(o.value or o.label for o in case.options if o.value)
        if choices:
            bits.append(f"The options already on the table are: {choices}.")
    return "\n".join(b for b in bits if b)


def enrich(result: Any, schema: MigrationSchema, profiles: dict, provider: Any) -> int:
    """Investigate the open cases worth investigating. Returns how many were checked."""
    run_lookup = _tools_over(result, schema, profiles)
    worth = [c for c in result.open_cases if c.klass in WORTH_CHECKING]
    worth.sort(key=lambda c: c.waiting_count, reverse=True)

    checked = 0
    for case in worth[:MAX_CASES]:
        try:
            finding, steps, _ = investigate_case(
                provider,
                question=_question(case),
                schema=schema,
                lookups=LOOKUPS,
                run_lookup=run_lookup,
            )
        except Exception as exc:  # noqa: BLE001 - an outage must not lose the queue
            log.warning("investigation failed for %s: %s", case.headline, exc)
            continue
        if not steps and finding is None:
            continue

        checked += 1
        case.checked = [Checked(looked_at=s.looked_at, found=s.found) for s in steps]
        if finding is None:
            continue
        case.found = finding.conclusion

        # A suggestion only becomes an option if it is one of the answers the case
        # already allows. The model proposes; the case decides what is offerable.
        if finding.settled and finding.suggestion:
            allowed = {o.value for o in case.options if o.value}
            if finding.suggestion in allowed:
                for option in case.options:
                    if option.value == finding.suggestion:
                        option.recommended = True
                        option.description = finding.conclusion
            else:
                case.options.append(Option(
                    label=f"Use {finding.suggestion}",
                    value=finding.suggestion,
                    description=finding.conclusion,
                    recommended=True,
                ))
    return checked
