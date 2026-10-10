from types import SimpleNamespace

import pytest

from app.services.report_agent import ReportAgent, ReportOutline, ReportSection
from app.utils.llm_client import LLMClient, LLMResponseError
from app.utils.locale import t


class CompletionSequence:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _response(content, *, finish_reason="stop", include_choice=True):
    choices = []
    if include_choice:
        choices.append(
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        )
    return SimpleNamespace(choices=choices)


def _client_for(sequence):
    client = object.__new__(LLMClient)
    client.model = "compatible-model"
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=sequence)
    )
    return client


def _chat(client):
    return client.chat(messages=[{"role": "user", "content": "Write a section"}])


def test_chat_reports_truncation_when_content_is_missing():
    # Reasoning models can exhaust the budget before leaving reasoning_content,
    # which returns content=null together with finish_reason="length".
    client = _client_for(CompletionSequence(_response(None, finish_reason="length")))

    with pytest.raises(LLMResponseError) as captured:
        _chat(client)

    assert "truncated" in str(captured.value)
    assert captured.value.finish_reason == "length"


def test_chat_reports_truncation_for_partial_content():
    client = _client_for(
        CompletionSequence(
            _response("Final Answer: the first half of the sec", finish_reason="length")
        )
    )

    with pytest.raises(LLMResponseError) as captured:
        _chat(client)

    assert "truncated" in str(captured.value)
    assert captured.value.finish_reason == "length"


def test_chat_reports_unexpected_finish_reason():
    client = _client_for(
        CompletionSequence(_response("partial", finish_reason="content_filter"))
    )

    with pytest.raises(LLMResponseError, match="stopped unexpectedly") as captured:
        _chat(client)

    assert captured.value.finish_reason == "content_filter"


@pytest.mark.parametrize(
    "content",
    [None, "", "   ", "<think>still deciding how to answer</think>"],
)
def test_chat_rejects_empty_completion_even_when_finish_reason_is_stop(content):
    client = _client_for(CompletionSequence(_response(content)))

    with pytest.raises(LLMResponseError, match="empty text content") as captured:
        _chat(client)

    assert captured.value.finish_reason == "stop"


def test_chat_reports_missing_choices():
    client = _client_for(CompletionSequence(_response(None, include_choice=False)))

    with pytest.raises(LLMResponseError, match="no choices"):
        _chat(client)


def test_chat_returns_successful_completion_unchanged():
    client = _client_for(
        CompletionSequence(_response("Final Answer: full section body."))
    )

    assert _chat(client) == "Final Answer: full section body."


def test_chat_still_strips_reasoning_wrapper_from_successful_completion():
    client = _client_for(
        CompletionSequence(_response("<think>plan</think>\nFinal Answer: body."))
    )

    assert _chat(client) == "Final Answer: body."


class TruncatedChatClient:
    """Stand-in LLM client whose every completion hits the token limit."""

    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        raise LLMResponseError(
            "LLM text output was truncated at the token limit",
            finish_reason="length",
        )


def test_section_generation_degrades_to_an_explicit_failure_notice():
    agent = object.__new__(ReportAgent)
    agent.tools = {}
    agent.report_logger = None
    agent.simulation_requirement = "demo requirement"
    agent.llm = TruncatedChatClient()

    content = agent._generate_section_react(
        section=ReportSection(title="Key findings"),
        outline=ReportOutline(title="Demo report", summary="demo", sections=[]),
        previous_sections=[],
    )

    # The section must not be saved as a silently empty success.
    assert content == t('report.sectionGenFailedContent')
    # Five ReACT iterations plus the force-finish attempt.
    assert agent.llm.calls == 6


def test_chat_truncation_error_does_not_echo_partial_model_output():
    partial = "The informant named SENTINEL-SHOULD-NOT-LEAK said that"
    client = _client_for(CompletionSequence(_response(partial, finish_reason="length")))

    with pytest.raises(LLMResponseError) as captured:
        _chat(client)

    assert "SENTINEL-SHOULD-NOT-LEAK" not in str(captured.value)
    assert "SENTINEL-SHOULD-NOT-LEAK" not in repr(captured.value)
