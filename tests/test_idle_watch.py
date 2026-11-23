"""Idle watch: pick logic for streak visits and points, lookup and tracking of listed channels."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_CHANNELS, WebsocketTopic
from src.services.idle_watch import IDLE_VISIT_SECONDS, IdleWatchService


def fake(cid, broadcast, *, online=True, banned=False, login=None):
    return SimpleNamespace(
        id=cid, login=login or f"c{cid}", online=online, banned=banned,
        _stream=SimpleNamespace(broadcast_id=broadcast),
    )


def service(channels=None, idle=(), gql=None):
    handlers = SimpleNamespace(process_stream_state=object(), process_stream_update=object())
    twitch = SimpleNamespace(
        channels=dict(channels or {}),
        settings=SimpleNamespace(idle_channels=list(idle)),
        gql_request=gql or AsyncMock(),
        gui=SimpleNamespace(channels=MagicMock(), login=MagicMock()),
        websocket=MagicMock(),
        _message_handler_service=handlers,
        _channel_service=SimpleNamespace(filter_banned_channels=AsyncMock(side_effect=list)),
    )
    return IdleWatchService(twitch), twitch


def test_first_due_channel_gets_a_visit():
    svc, _ = service()
    a, b = fake(1, 10), fake(2, 20)
    assert svc.pick([a, b], 0) is a
    assert svc.visit.channel_id == 1


def test_visit_holds_until_420_seconds():
    svc, _ = service()
    a, b = fake(1, 10), fake(2, 20)
    svc.pick([a, b], 0)
    assert svc.pick([a, b], IDLE_VISIT_SECONDS - 1) is a
    assert svc.pick([a, b], IDLE_VISIT_SECONDS) is b
    assert 10 in svc.visited


def test_done_visit_ends_early():
    svc, _ = service()
    a, b = fake(1, 10), fake(2, 20)
    svc.pick([a, b], 0)
    svc.visit.done = True
    assert svc.pick([a, b], 5) is b
    assert 10 in svc.visited


def test_visit_ends_when_channel_offline_or_new_broadcast():
    svc, _ = service()
    a, b = fake(1, 10), fake(2, 20)
    svc.pick([a, b], 0)
    assert svc.pick([b], 5) is b
    svc2, _ = service()
    svc2.pick([a, b], 0)
    a2 = fake(1, 11)
    assert svc2.pick([a2, b], 5) is a2  # new broadcast is due again
    assert svc2.visit.broadcast_id == 11


def test_points_watch_on_highest_live_after_visits():
    svc, _ = service()
    a, b = fake(1, 10), fake(2, 20)
    svc.visited = {10, 20}
    assert svc.pick([a, b], 0) is a
    assert svc.pick([], 1) is None


def test_removed_channel_is_dropped():
    svc, _ = service()
    a, b = fake(1, 10), fake(2, 20)
    svc.pick([a, b], 0)
    assert svc.pick([b], 1) is b


def test_visited_is_pruned():
    svc, _ = service()
    a = fake(1, 10)
    svc.visited = {10, 20, 99}
    svc.pick([a], 0)
    assert svc.visited == {10}


def user(uid, login, live=True):
    return {"data": {"user": {
        "id": str(uid), "login": login,
        "stream": {"id": str(uid * 100), "viewersCount": 5, "type": "live",
                   "game": None, "broadcaster": {"id": str(uid), "login": login}} if live else None,
    }}}


@pytest.mark.asyncio
async def test_lookup_skips_unknown_and_errors(caplog):
    gql = AsyncMock(return_value=[
        {"data": {"user": None}}, {"errors": [{"message": "secret body"}]},
        {"data": None, "errors": [{"message": "secret body"}]}, user(3, "ok", live=False),
    ])
    svc, twitch = service(idle=["gone", "bad", "null", "ok"], gql=gql)
    with caplog.at_level(logging.WARNING, logger="TwitchDrops"):
        tracked = await svc.track()
    assert [c.login for c in tracked] == ["ok"]
    assert set(twitch.channels) == {3}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert all("secret body" not in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_enter_reuses_tracked_channel():
    existing = fake(3, 1, login="Other")
    gql = AsyncMock(return_value=[user(3, "ok", live=False)])
    svc, twitch = service({3: existing}, idle=["ok"], gql=gql)
    tracked = await svc.track()
    assert tracked == [existing]
    assert twitch.channels[3] is existing


@pytest.mark.asyncio
async def test_track_subscribes_stream_topics_and_respects_cap():
    gql = AsyncMock(return_value=[user(1, "a", live=False), user(2, "b", live=False)])
    filler = {1000 + i: fake(1000 + i, 1) for i in range(MAX_CHANNELS - 1)}
    svc, twitch = service(filler, idle=["a", "b"], gql=gql)
    tracked = await svc.track()
    assert len(twitch.channels) == MAX_CHANNELS
    assert [c.login for c in tracked] == ["a"]
    topics = twitch.websocket.add_topics.call_args.args[0]
    assert {str(t) for t in topics} == {
        WebsocketTopic.as_str("Channel", "StreamState", 1),
        WebsocketTopic.as_str("Channel", "StreamUpdate", 1),
    }
    twitch.gui.channels.display.assert_called_once()


def test_live_channels_follow_list_order_and_skip_banned_offline():
    chans = {1: fake(1, 1, login="a"), 2: fake(2, 2, login="b", banned=True),
             3: fake(3, 3, login="c", online=False), 4: fake(4, 4, login="d")}
    svc, _ = service(chans, idle=["d", "b", "c", "a", "zz"])
    assert [c.login for c in svc.live_channels()] == ["d", "a"]


@pytest.mark.asyncio
async def test_banned_new_channel_is_not_added_and_gets_no_topics():
    gql = AsyncMock(return_value=[user(1, "a", live=False), user(2, "b", live=False)])
    svc, twitch = service(idle=["a", "b"], gql=gql)
    twitch._channel_service.filter_banned_channels = AsyncMock(
        side_effect=lambda chans: [c for c in chans if c.id != 2],
    )
    tracked = await svc.track()
    assert set(twitch.channels) == {1}
    assert [c.login for c in tracked] == ["a"]
    topics = twitch.websocket.add_topics.call_args.args[0]
    assert {str(t) for t in topics} == {
        WebsocketTopic.as_str("Channel", "StreamState", 1),
        WebsocketTopic.as_str("Channel", "StreamUpdate", 1),
    }
