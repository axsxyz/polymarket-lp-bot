"""Tests for the ``TwoSidedQuotes.is_two_sided`` property (Issue 2 + 4).

Invariant: a market with ASK=0 OR BID=0 on every book MUST be reported
as one-sided so it is excluded from Q-score reporting and classified as
ONE_SIDED in the per-cycle market state map.
"""

from __future__ import annotations

from core.quoter import Quote, TwoSidedQuotes


def _quote(token_id: str = "t", bid_size: float = 100.0, ask_size: float = 100.0) -> Quote:
    return Quote(
        token_id=token_id,
        bid_price=0.49,
        bid_size=bid_size,
        ask_price=0.51,
        ask_size=ask_size,
        mid=0.50,
        spread_used=0.01,
    )


class TestIsTwoSided:
    def test_both_books_full_is_two_sided(self):
        q = TwoSidedQuotes(
            condition_id="c1",
            yes_quote=_quote("y", bid_size=100, ask_size=100),
            no_quote=_quote("n", bid_size=100, ask_size=100),
        )
        assert q.is_two_sided is True

    def test_one_book_full_is_two_sided(self):
        """Only YES has both sizes — still counts as two-sided."""
        q = TwoSidedQuotes(
            condition_id="c1",
            yes_quote=_quote("y", bid_size=100, ask_size=100),
            no_quote=_quote("n", bid_size=100, ask_size=0),
        )
        assert q.is_two_sided is True

    def test_yes_ask_zero_no_bid_zero_is_one_sided(self):
        """No single book has both → one-sided."""
        q = TwoSidedQuotes(
            condition_id="c1",
            yes_quote=_quote("y", bid_size=100, ask_size=0),
            no_quote=_quote("n", bid_size=0, ask_size=100),
        )
        assert q.is_two_sided is False

    def test_all_zero_is_one_sided(self):
        q = TwoSidedQuotes(
            condition_id="c1",
            yes_quote=_quote("y", bid_size=0, ask_size=0),
            no_quote=_quote("n", bid_size=0, ask_size=0),
        )
        assert q.is_two_sided is False

    def test_both_quotes_none_is_one_sided(self):
        q = TwoSidedQuotes(condition_id="c1", yes_quote=None, no_quote=None)
        assert q.is_two_sided is False

    def test_one_quote_none_other_full_is_two_sided(self):
        q = TwoSidedQuotes(
            condition_id="c1",
            yes_quote=None,
            no_quote=_quote("n", bid_size=100, ask_size=100),
        )
        assert q.is_two_sided is True
