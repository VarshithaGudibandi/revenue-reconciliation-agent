from recon.mapping.combine import combine_file, merge_field, merge_value
from recon.mapping.proposal import FileProposal, field_choice, value_choice


def test_agreement_is_confident_if_either_side_is() -> None:
    assert merge_field(field_choice("a", True), field_choice("a", False)).confident
    assert merge_field(field_choice("a", False), field_choice("a", True)).confident
    assert not merge_field(field_choice("a", False), field_choice("a", False)).confident


def test_a_confident_rule_is_not_overruled_by_a_model() -> None:
    merged = merge_field(
        field_choice("gross", True, reasons=("rule",)),
        field_choice("net", False, ("gross",), reasons=("model",)),
    )

    assert merged.column == "gross"
    assert merged.confident
    assert merged.alternatives == ("net",)
    assert merged.reasons == ("rule", "model")


def test_a_model_that_is_sure_where_the_rules_are_not_becomes_a_suggestion() -> None:
    merged = merge_field(field_choice(None, False, ("a", "b")), field_choice("b", True))

    assert merged.column == "b"
    assert not merged.confident
    assert merged.alternatives == ("a",)
    assert "suggested by the model" in merged.reasons[0]


def test_two_confident_answers_that_disagree_are_a_question_with_both_offered() -> None:
    merged = merge_field(field_choice("a", True), field_choice("b", True))

    assert merged.column is None
    assert not merged.confident
    assert merged.alternatives == ("a", "b")
    assert "disagree" in merged.reasons[0]


def test_when_neither_is_sure_the_models_pick_is_offered_first() -> None:
    merged = merge_field(field_choice("a", False), field_choice("b", False))

    assert merged.column == "b"
    assert not merged.confident
    assert merged.alternatives == ("a",)


def test_no_column_agreed_by_both_is_a_confident_no_column() -> None:
    assert merge_field(field_choice(None, True), field_choice(None, True)).confident


def test_settings_merge_the_same_way() -> None:
    sure_rule = merge_value(value_choice(",", True), value_choice(".", False))
    open_order = merge_value(
        value_choice(None, False, ("%d/%m/%Y", "%m/%d/%Y")), value_choice("%d/%m/%Y", False)
    )

    assert sure_rule.value == "," and sure_rule.confident
    assert open_order.value == "%d/%m/%Y"
    assert not open_order.confident
    assert open_order.alternatives == ("%m/%d/%Y",)


def test_a_field_only_one_side_describes_is_kept() -> None:
    rules = FileProposal(fields={"reference": field_choice("ref", True)})
    model = FileProposal(fields={"amount": field_choice("total", True)})

    combined = combine_file(rules, model)

    assert combined.choice("reference").column == "ref"
    assert combined.choice("amount").column == "total"
