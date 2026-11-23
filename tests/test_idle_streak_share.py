"""Idle watch: sharing watch streaks."""

import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.exceptions import MinerException
from src.services.idle_watch import STREAK_SHARE_CHECK_SECONDS, IdleWatchService


def milestone(mid="m1", *, category="WATCH_STREAK", state="ACTIVE", share="CAN_SHARE", value="9"):
    return {"id": mid, "category": category, "value": value, "state": state, "shareStatus": share}


def read(rows):
    return {"data": {"user": {"channel": {"id": "123", "self": {"viewerMilestones": rows}}}}}


SHARE_OK = {"data": {"shareViewerMilestone": {"milestone": {"id": "m1"}, "error": None}}}


def make_gql(reads, shares=None):
    """Answers a batch of reads with `reads` (one per login) and a share with the next of `shares`."""
    shares = list(shares or [SHARE_OK])

    async def gql(op):
        if isinstance(op, list):
            return reads
        assert "shareViewerMilestone" in op["query"]
        item = shares.pop(0) if len(shares) > 1 else shares[0]
        if isinstance(item, Exception):
            raise item
        return item

    return AsyncMock(side_effect=gql)


def setup(rows=None, *, share=True, active=True, gql=None, online=True):
    channel = SimpleNamespace(id=123, name="A", login="a", online=online, banned=False)
    tw = SimpleNamespace(
        channels={123: channel},
        settings=SimpleNamespace(idle_channels=["a"], claim_channel_points=True, share_watch_streaks=share),
        gql_request=gql or make_gql([read(rows if rows is not None else [milestone()])]),
        gui=SimpleNamespace(status=MagicMock()),
        watching_channel=SimpleNamespace(get_with_default=lambda d: channel),
        can_watch=MagicMock(return_value=False),
    )
    svc = IdleWatchService(tw)
    svc._task = MagicMock(done=lambda: not active)
    svc.poke = MagicMock()
    return svc, tw


def shares(tw):
    return [c.args[0] for c in tw.gql_request.call_args_list if not isinstance(c.args[0], list)]


def reads(tw):
    return [c.args[0] for c in tw.gql_request.call_args_list if isinstance(c.args[0], list)]


@pytest.mark.asyncio
async def test_shares_a_can_share_streak_once(caplog):
    svc, tw = setup()
    with caplog.at_level(logging.INFO, logger="TwitchDrops"):
        await svc.share_streaks()
    [op] = shares(tw)
    assert op["variables"] == {"input": {"channelID": "123", "milestoneID": "m1"}}
    assert "shareViewerMilestone" in op["query"]
    [batch] = reads(tw)
    assert batch[0]["variables"] == {"login": "a"}
    assert "viewerMilestones" in batch[0]["query"]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        "Shared watch streak on A (9 streams)",
    ]
    # a later check, even with the id still reported as shareable, sends nothing
    svc._share_checked -= STREAK_SHARE_CHECK_SECONDS
    await svc.share_streaks()
    assert len(shares(tw)) == 1
    assert len(reads(tw)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [milestone(share="CAN_NOT_SHARE")],
    [milestone(share="SHARED")],
    [milestone(state="SUSPENDED")],
    [milestone(category="SUBSCRIPTION_STREAK")],
    None,
    [],
    [{"category": "WATCH_STREAK", "state": "ACTIVE", "shareStatus": "CAN_SHARE"}],
    [{**milestone(), "id": ""}],
    [{**milestone(), "id": 5}],
    ["junk", None, 3, []],
    "junk",
])
async def test_no_share_for_other_rows(rows):
    svc, tw = setup()
    tw.gql_request = make_gql([read(rows)])
    await svc.share_streaks()
    assert shares(tw) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, {}, {"data": None}, {"data": {"user": None}}, "x", {"errors": [{}], **read([milestone()])}])
async def test_malformed_answers_do_not_raise(answer):
    svc, tw = setup()
    tw.gql_request = make_gql([answer])
    await svc.share_streaks()
    assert shares(tw) == []


@pytest.mark.asyncio
async def test_refused_share_retries_once_then_stops(caplog):
    refused = {"data": {"shareViewerMilestone": {"milestone": None, "error": {"code": "NOT_ALLOWED"}}}}
    svc, tw = setup(gql=make_gql([read([milestone()])], [refused]))
    with caplog.at_level(logging.WARNING, logger="TwitchDrops"):
        for _ in range(3):
            await svc.share_streaks()
            svc._share_checked -= STREAK_SHARE_CHECK_SECONDS
    assert len(shares(tw)) == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Watch streak share on A was not accepted (NOT_ALLOWED)"] * 2


@pytest.mark.asyncio
async def test_transport_failure_uses_up_an_attempt(caplog):
    svc, tw = setup(gql=make_gql([read([milestone()])], [MinerException("boom")]))
    with caplog.at_level(logging.WARNING, logger="TwitchDrops"):
        for _ in range(3):
            await svc.share_streaks()
            svc._share_checked -= STREAK_SHARE_CHECK_SECONDS
    assert len(shares(tw)) == 2
    assert "boom" not in caplog.text


@pytest.mark.asyncio
async def test_failure_then_success_marks_done():
    refused = {"data": {"shareViewerMilestone": None}}
    svc, tw = setup(gql=make_gql([read([milestone()])], [refused, SHARE_OK]))
    for _ in range(3):
        await svc.share_streaks()
        svc._share_checked -= STREAK_SHARE_CHECK_SECONDS
    assert len(shares(tw)) == 2


@pytest.mark.asyncio
async def test_setting_off_or_not_active_or_not_live_sends_nothing():
    for kwargs in ({"share": False}, {"active": False}, {"online": False}):
        svc, tw = setup(**kwargs)
        await svc.share_streaks()
        assert tw.gql_request.call_count == 0, kwargs


@pytest.mark.asyncio
async def test_cadence_and_early_check():
    svc, tw = setup()
    await svc.share_streaks()
    await svc.share_streaks()
    assert len(reads(tw)) == 1
    # a WATCH_STREAK points-earned for a listed channel asks for an early check and wakes the loop
    await svc.process_points(1, {"type": "points-earned", "data": {
        "channel_id": "123", "point_gain": {"reason_code": "WATCH_STREAK", "total_points": 450},
        "balance": {"balance": 10}}})
    assert svc._share_now is True
    svc.poke.assert_called()
    assert len(reads(tw)) == 1  # the handler does no network work itself
    await svc.share_streaks()
    assert len(reads(tw)) == 2
    assert svc._share_now is False
    # other reasons and unlisted channels do not ask for one
    await svc.process_points(1, {"type": "points-earned", "data": {
        "channel_id": "123", "point_gain": {"reason_code": "WATCH", "total_points": 10}, "balance": {"balance": 10}}})
    await svc.process_points(1, {"type": "points-earned", "data": {
        "channel_id": "999", "point_gain": {"reason_code": "WATCH_STREAK", "total_points": 10}, "balance": {"balance": 10}}})
    assert svc._share_now is False
    # the interval alone also triggers a check
    svc._share_checked = time.monotonic() - STREAK_SHARE_CHECK_SECONDS - 1
    await svc.share_streaks()
    assert len(reads(tw)) == 3


@pytest.mark.asyncio
async def test_tick_runs_the_check_only_while_the_loop_is_active():
    svc, tw = setup(active=False)
    svc.track = AsyncMock()
    svc.pick = MagicMock(return_value=None)
    tw.stop_watching = MagicMock()
    await svc._tick()
    assert tw.gql_request.call_count == 0
    svc._task = MagicMock(done=lambda: False)
    await svc._tick()
    assert len(reads(tw)) == 1
    assert len(shares(tw)) == 1
