"""crm_insights' elicitation rubric: the shared soundness checks from tests/tasks/conftest.py.

No per-item pin against a generator table here (unlike compliance_review) -- crm_insights' hidden
facts are policy prose (tie-break order, filter direction), not a data table with a canonical source
to drift from. The soundness check is what matters: none of the 5 items may be readable from
schema_reference.md or the redacted spec, since that's exactly the leak this task was reshaped to
close.
"""
from __future__ import annotations

from tests.tasks.conftest import assert_rubric_is_sound

TASK = "crm_insights"


def test_the_rubric_is_sound():
    assert_rubric_is_sound(TASK)
