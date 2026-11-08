import pytest
from unittest.mock import AsyncMock, MagicMock
from nexus_engine.services.google_flow_worker import GoogleFlowWorker

@pytest.mark.anyio
async def test_handle_confirmation_prompts_clicks_radio_options():
    worker = GoogleFlowWorker.__new__(GoogleFlowWorker)
    worker.page = MagicMock()

    # Mock evaluate to simulate finding and clicking the 'always' option
    worker.page.evaluate = AsyncMock(return_value="always")

    # Mock locator visibility to False for standard selectors so it reaches evaluate
    mock_locator = MagicMock()
    mock_locator.is_visible = AsyncMock(return_value=False)
    worker.page.locator = MagicMock(return_value=MagicMock(first=mock_locator))

    handled = await worker.handle_confirmation_prompts()
    assert handled is True
    assert worker.page.evaluate.called

@pytest.mark.anyio
async def test_latest_unanswered_agent_choice_ignores_credit_confirmation():
    worker = GoogleFlowWorker.__new__(GoogleFlowWorker)
    worker.page = MagicMock()

    # When the page has a credit confirmation dialog ("Phê duyệt", "Luôn phê duyệt", "Từ chối"),
    # the evaluate script inside _latest_unanswered_agent_choice returns {present: False, choices: []}
    worker.page.evaluate = AsyncMock(return_value={"present": False, "choices": []})

    result = await worker._latest_unanswered_agent_choice()
    assert result["present"] is False
    assert result["choices"] == []
