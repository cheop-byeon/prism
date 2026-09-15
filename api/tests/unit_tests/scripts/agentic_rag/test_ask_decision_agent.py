from types import SimpleNamespace

import pytest

from scripts.agentic_rag.ask_decision_agent import _summarize_usage


def _fake_usage(*, prompt_tokens, completion_tokens, total_tokens, total_price, latency, currency="USD"):
    """A duck-typed stand-in for dify_graph's LLMUsage -- _summarize_usage
    only ever reads these attributes, so a real LLMUsage (which needs
    Decimal fields and pydantic validation) isn't necessary here.
    """
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        total_price=total_price,
        latency=latency,
        currency=currency,
    )


class TestSummarizeUsage:
    def test_empty_log_returns_zeroed_totals(self):
        result = _summarize_usage([])

        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0
        assert result["total_tokens"] == 0
        assert result["total_price"] == 0
        assert result["latency"] == 0.0
        assert result["currency"] == "USD"

    def test_sums_every_llm_call_in_the_turn(self):
        # Mirrors a real turn: several tool-calling iterations plus one
        # verify_answer call, each its own LLMUsage appended via
        # _invoke_llm_with_retry's usage_sink.
        usage_log = [
            _fake_usage(prompt_tokens=100, completion_tokens=20, total_tokens=120, total_price=1, latency=0.5),
            _fake_usage(prompt_tokens=200, completion_tokens=30, total_tokens=230, total_price=2, latency=0.8),
            _fake_usage(prompt_tokens=50, completion_tokens=10, total_tokens=60, total_price=1, latency=0.3),
        ]

        result = _summarize_usage(usage_log)

        assert result["prompt_tokens"] == 350
        assert result["completion_tokens"] == 60
        assert result["total_tokens"] == 410
        assert result["total_price"] == 4
        assert result["latency"] == pytest.approx(1.6)

    def test_currency_takes_the_last_call_seen(self):
        # Every call in one ask() turn uses the same model_instance, so in
        # practice currency never actually varies mid-log -- this documents
        # the (arbitrary but harmless) tie-breaking behavior rather than
        # asserting a scenario that can't occur.
        usage_log = [
            _fake_usage(
                prompt_tokens=1, completion_tokens=1, total_tokens=2, total_price=0, latency=0.1, currency="USD"
            ),
            _fake_usage(
                prompt_tokens=1, completion_tokens=1, total_tokens=2, total_price=0, latency=0.1, currency="EUR"
            ),
        ]

        result = _summarize_usage(usage_log)

        assert result["currency"] == "EUR"
