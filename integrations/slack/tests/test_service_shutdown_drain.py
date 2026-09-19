"""Service shutdown must drain in-flight turns, not drop their answers.

A verdict answered right before shutdown leaves the turn's committed answer
text still in flight; cancelling the turn task outright drops it from the
thread. Shutdown therefore grants in-flight turns a bounded grace to finish
delivering, while a turn parked past the grace is still cancelled promptly
and posts the restart notice. The 50 ms gap between resolution and answer
emission models the timing skew that made the drop intermittent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent_slack.approvals import Verdict
from test_service import (
    ApprovalClient,
    FakeOmnigentClient,
    FakeSlackClient,
    _card_elicitation_id,
    _card_session_id,
    _configure_user,
    _form_elicitation_event,
    _service,
    _store,
    _wait_any,
    _wait_for_card,
    _wait_for_resolved,
)


class DelayedCommittedAnswerClient(FakeOmnigentClient):
    """PreambleThenCommittedAnswerClient's stream shape, with the post-answer
    message emitted 50 ms after elicitation resolution.
    """

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__(final_text="")
        self._event = event

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
        host_type: str = "external",
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        yield {"type": "response.output_text.delta", "delta": "Here's a demo."}
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Here's a demo."}],
            },
        }
        yield self._event
        await _wait_any(self.resolve_signal)
        yield {"type": "response.elicitation_resolved", "elicitation_id": "elicit_form"}
        # The server delivers the committed answer shortly after the verdict,
        # not atomically with it — the skew behind the intermittent CI failure.
        await asyncio.sleep(0.05)
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "You picked A. Full summary here."}],
            },
        }
        yield {"type": "session.status", "status": "idle", "response_id": "resp_1"}


async def test_post_answer_text_survives_shutdown(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = DelayedCommittedAnswerClient(_form_elicitation_event())
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> demo"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True, content={"store": "A"})
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    assert any("You picked A. Full summary here." in s.text for s in slack.streams), (
        "the answer committed after the verdict was dropped by service shutdown"
    )


async def test_parked_turn_is_cancelled_after_grace(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_card(slack)

    # The turn stays parked on the unanswered card, so the grace elapses and
    # shutdown must still cancel it promptly and post the restart notice.
    started = asyncio.get_running_loop().time()
    await service.shutdown(grace=0.05)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2.0, f"shutdown was not bounded by the grace: {elapsed:.2f}s"
    assert not service._turn_tasks
    assert any("restarted while working" in str(p.get("text", "")) for p in slack.posts), (
        "the cancelled turn did not post the restart notice"
    )
