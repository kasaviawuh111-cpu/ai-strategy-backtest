from datetime import date

import pytest

from ashare_lab.application.local_minute_grid import _narrower_minute_bounds


@pytest.mark.parametrize('known,preceding,expected', [
    ([1, 2, 3, 4], False, None),
    ([], False, None),
    ([1, 3, 4], False, None),  # Internal holes cannot be hidden by shortening.
    ([2, 3, 4], False, (2, 4)),
    ([1, 2, 3], False, (1, 3)),
    ([2, 3], False, (2, 3)),
    ([2, 3, 4], True, (3, 4)),  # Reserve a sourced preceding session.
    ([4], True, None),
    ([1, 2, 3], True, (1, 3)),
])
def test_only_sourced_boundary_gaps_propose_a_range(known, preceding, expected):
    day = lambda n: date(2026, 9, n)
    result = _narrower_minute_bounds({day(n) for n in (1, 2, 3, 4)},
        {day(n) for n in known}, needs_preceding_session=preceding)
    assert result == (tuple(day(n) for n in expected) if expected else None)
