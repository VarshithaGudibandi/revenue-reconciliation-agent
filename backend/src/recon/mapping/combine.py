"""Combine what the rules say with what the model says about the same file.

The rules are cheap, deterministic and have never been confidently wrong on the generated
exports, so a confident rule is not overruled by a model. A model that is sure where the rules
are not is followed as a suggestion, still marked as a question. Two confident answers that
disagree are a conflict, and the user decides.
"""

from collections.abc import Sequence

from recon.mapping.proposal import (
    FieldChoice,
    FileProposal,
    MappingProposal,
    ValueChoice,
    field_choice,
    value_choice,
)


def _merge(
    rules: tuple[str | None, bool, tuple[str, ...], tuple[str, ...]],
    model: tuple[str | None, bool, tuple[str, ...], tuple[str, ...]],
) -> tuple[str | None, bool, tuple[str, ...], tuple[str, ...]]:
    r_value, r_sure, r_alts, r_why = rules
    m_value, m_sure, m_alts, m_why = model

    def others(chosen: str | None, *more: Sequence[str | None]) -> tuple[str, ...]:
        seen = dict.fromkeys(x for group in more for x in group if x is not None and x != chosen)
        return tuple(seen)

    if r_value == m_value:
        why = (*r_why, *(w for w in m_why if w not in r_why))
        return r_value, r_sure or m_sure, others(r_value, r_alts, m_alts), why
    if r_sure and m_sure:
        both = others(None, (r_value, m_value), r_alts, m_alts)
        return None, False, both, ("the rules and the model disagree", *r_why, *m_why)
    if r_sure:
        return r_value, True, others(r_value, (m_value,), r_alts, m_alts), (*r_why, *m_why)
    if m_sure:
        return (
            m_value,
            False,
            others(m_value, (r_value,), r_alts, m_alts),
            ("suggested by the model; the rules could not settle it", *m_why),
        )
    chosen = m_value if m_value is not None else r_value
    return chosen, False, others(chosen, (r_value, m_value), r_alts, m_alts), (*r_why, *m_why)


def merge_field(rules: FieldChoice, model: FieldChoice) -> FieldChoice:
    value, sure, alternatives, reasons = _merge(
        (rules.column, rules.confident, rules.alternatives, rules.reasons),
        (model.column, model.confident, model.alternatives, model.reasons),
    )
    return field_choice(value, sure, alternatives, reasons)


def merge_value(rules: ValueChoice, model: ValueChoice) -> ValueChoice:
    value, sure, alternatives, reasons = _merge(
        (rules.value, rules.confident, rules.alternatives, rules.reasons),
        (model.value, model.confident, model.alternatives, model.reasons),
    )
    return value_choice(value, sure, alternatives, reasons)


def combine_file(rules: FileProposal, model: FileProposal) -> FileProposal:
    names = dict.fromkeys((*rules.fields, *model.fields))
    return FileProposal(
        fields={n: merge_field(rules.choice(n), model.choice(n)) for n in names},
        date_format=merge_value(rules.date_format, model.date_format),
        decimal_separator=merge_value(rules.decimal_separator, model.decimal_separator),
    )


def combine(rules: MappingProposal, model: MappingProposal) -> MappingProposal:
    return MappingProposal(
        invoices=combine_file(rules.invoices, model.invoices),
        payments=combine_file(rules.payments, model.payments),
    )
