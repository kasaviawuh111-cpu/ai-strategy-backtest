from copy import deepcopy

from ashare_lab.application.execution_feedback import untriggered_combination_note


def test_combination_feedback_requires_complete_valid_evidence():
    rows = [dict(session_date="2026-09-10", condition_ref="all", triggered=False,
                 children=[dict(condition_ref="a", triggered=True), dict(condition_ref="b", triggered=False)]),
            dict(session_date="2026-09-11", condition_ref="all", triggered=False,
                 children=[dict(condition_ref="a", triggered=False), dict(condition_ref="b", triggered=True)])]
    dates = [r["session_date"] for r in rows]
    before = deepcopy(rows)
    assert "各项条件曾分别满足" in untriggered_combination_note(rows, dates, [])
    assert untriggered_combination_note(rows[:1], dates, []) is None
    assert untriggered_combination_note(rows, dates, [{"kind": "order"}]) is None
    rows[0]["children"][0]["triggered"] = None
    assert untriggered_combination_note(rows, dates, []) is None
    rows = deepcopy(before)
    rows[0]["children"][1]["triggered"] = True
    assert untriggered_combination_note(rows, dates, []) is None


def test_never_satisfied_child_is_not_described_as_previously_satisfied():
    rows = [dict(session_date="2026-09-11", condition_ref="all", triggered=False,
                 children=[dict(condition_ref="a", triggered=False), dict(condition_ref="b", triggered=True)])]
    note = untriggered_combination_note(rows, ["2026-09-11"], [])
    assert "曾分别满足" not in note
    assert "没有在同一天全部满足" in note
