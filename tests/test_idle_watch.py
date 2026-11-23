"""Idle watch: pick logic for streak visits and points, lookup and tracking of listed channels."""

import asyncio
import inspect
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_CHANNELS, State, WebsocketTopic
from src.core.client import Twitch
from src.services import idle_watch as idle_mod
from src.services.idle_watch import IDLE_VISIT_SECONDS, IdleWatchService
from src.services.message_handlers import MessageHandlerService


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


# --- loop and client wiring ---


def loop_service(live, *, mining=True, idle=("c1",)):
    svc, tw = service(idle=idle)
    tw.is_mining_time = MagicMock(side_effect=lambda: mining() if callable(mining) else mining)
    tw._auth_state = SimpleNamespace(_logged_in=SimpleNamespace(is_set=lambda: True))
    tw.watching_channel = SimpleNamespace(
        get_with_default=lambda d: tw._watching or d, set=lambda c: setattr(tw, "_watching", c),
    )
    tw._watching = None
    tw.watch = MagicMock(side_effect=lambda c, update_status=True: setattr(tw, "_watching", c))
    tw.stop_watching = MagicMock()
    tw.gui.status = MagicMock()
    svc.track = AsyncMock(return_value=live)
    svc.live_channels = lambda: live
    return svc, tw


@pytest.mark.asyncio
async def test_idle_stops_outside_mining_time():
    svc, tw = loop_service([fake(1, 10)], mining=False)
    svc.enter()
    await asyncio.wait_for(svc._task, 1)
    tw.stop_watching.assert_called_once()
    tw.watch.assert_not_called()
    assert not svc.active


@pytest.mark.asyncio
async def test_loop_watches_pick_with_idle_status(caplog, monkeypatch):
    monkeypatch.setattr(idle_mod, "IDLE_TICK_SECONDS", 0.01)
    monkeypatch.setattr(idle_mod, "IDLE_VISIT_SECONDS", 0.05)
    a, b = fake(1, 10, login="c1"), fake(2, 20, login="c2")
    a.name, b.name = "A", "B"
    svc, tw = loop_service([a, b], idle=("c1", "c2"))
    with caplog.at_level(logging.INFO, logger="TwitchDrops"):
        svc.enter()
        svc.enter()  # second call must not start a second task
        await asyncio.sleep(0.03)
        assert tw.watch.call_args_list[0].args == (a,)
        assert tw.watch.call_args_list[0].kwargs == {"update_status": False}
        assert svc.active
        await asyncio.sleep(0.2)
        svc.stop()
    assert [c.args[0] for c in tw.watch.call_args_list][:2] == [a, b]
    # no re-watch while the pick is unchanged
    assert len(tw.watch.call_args_list) <= 3
    assert any("Idle watching A" in c.args[0] for c in tw.gui.status.update.call_args_list)
    text = caplog.text
    assert text.count("Idle watching started") == 1
    assert "Idle visit started" in text and "Idle visit ended" in text
    assert "Idle watching switched" in text
    assert not svc.active


@pytest.mark.asyncio
async def test_track_failure_is_logged_and_retried(caplog, monkeypatch):
    monkeypatch.setattr(idle_mod, "IDLE_TICK_SECONDS", 0.01)
    a = fake(1, 10)
    a.name = "A"
    svc, tw = loop_service([a])
    svc.track = AsyncMock(side_effect=[TimeoutError("secret body"), [a]])
    with caplog.at_level(logging.INFO, logger="TwitchDrops"):
        svc.enter()
        await asyncio.sleep(0.1)
        svc.stop()
    assert svc.track.await_count >= 2
    tw.watch.assert_called()
    assert "secret body" not in caplog.text
    assert "Idle channel lookup failed" in caplog.text


def test_stop_keeps_visit_state():
    svc, _ = service()
    svc.pick([fake(1, 10)], 0)
    svc.stop()
    assert svc.visit is not None


def client_stub(*, flag, mining=True, logged_in=True, idle=("c1",)):
    client = object.__new__(Twitch)
    client._state = State.IDLE
    client._state_change = MagicMock()
    client._idle_watch_allowed = flag
    client._idle_watch = MagicMock()
    client._idle_watch.allowed.return_value = mining and logged_in and bool(idle)
    client.stop_watching = MagicMock()
    return client


@pytest.mark.parametrize("flag,ok", [(True, True), (False, False)])
def test_idle_branch_enters_idle_watch_only_when_allowed(flag, ok):
    client = client_stub(flag=flag)
    client._idle_watch_step()
    assert client._idle_watch.enter.called is ok
    assert client.stop_watching.called is (not ok)


@pytest.mark.parametrize("kw", [{"mining": False}, {"logged_in": False}, {"idle": ()}])
def test_idle_branch_stops_when_a_condition_fails(kw):
    client = client_stub(flag=True, **kw)
    client._idle_watch_step()
    client._idle_watch.enter.assert_not_called()
    client.stop_watching.assert_called_once()


def test_leaving_idle_stops_the_loop():
    client = client_stub(flag=True)
    client.change_state(State.INVENTORY_FETCH)
    client._idle_watch.stop.assert_called_once()
    assert client._idle_watch_allowed is False
    client.change_state(State.IDLE, idle_watch=True)
    assert client._idle_watch_allowed is True
    client.change_state(State.IDLE)
    assert client._idle_watch_allowed is False


def test_nothing_to_mine_entries_allow_idle_watch():
    src = inspect.getsource(Twitch._run)
    calls = re.findall(r"self\.change_state\(State\.IDLE(?:, idle_watch=True)?\)", src)
    flagged = [c for c in calls if "idle_watch" in c]
    assert len(flagged) == 2
    # no-games cleanup branch and the no-channel switch branch carry the flag
    assert re.search(r'collapse_key="status.no_campaign",\s+\)\s+self\.change_state\(State\.IDLE, idle_watch=True\)', src)
    assert re.search(r'_t\["status"\]\["no_channel"\]\)\s+self\.change_state\(State\.IDLE, idle_watch=True\)', src.replace("_.t", "_t"))
    assert len(calls) == 3  # the mining-hours close stays unflagged; the schedule loop is another method
    assert "self._state = State.IDLE" in inspect.getsource(Twitch)


def test_idle_channel_online_event_pokes():
    tw = MagicMock()
    tw.settings.idle_channels = ["c1"]
    tw._idle_watch.active = True
    tw.watching_channel.get_with_default.return_value = None
    tw.can_watch.return_value = False
    handler = MessageHandlerService(tw)
    listed = SimpleNamespace(login="c1", name="C1", display=MagicMock())
    other = SimpleNamespace(login="zz", name="ZZ", display=MagicMock())
    handler.on_channel_update(listed, None, object())
    tw._idle_watch.poke.assert_called_once()
    handler.on_channel_update(other, None, object())
    tw._idle_watch.poke.assert_called_once()
    tw._idle_watch.active = False
    handler.on_channel_update(listed, None, object())
    tw._idle_watch.poke.assert_called_once()


def test_idle_entry_without_flag_stops_a_running_loop():
    client = client_stub(flag=True)
    client.change_state(State.IDLE)
    client._idle_watch.stop.assert_called_once()
    client._idle_watch.stop.reset_mock()
    client._idle_watch_allowed = False
    client._idle_watch_step()
    client._idle_watch.stop.assert_called_once()


@pytest.mark.asyncio
async def test_stale_channel_object_with_same_id_is_replaced(monkeypatch):
    monkeypatch.setattr(idle_mod, "IDLE_TICK_SECONDS", 0.01)
    old, new = fake(1, 10), fake(1, 10)
    old.name = new.name = "A"
    svc, tw = loop_service([new])
    tw._watching = old
    svc.enter()
    await asyncio.sleep(0.03)
    svc.stop()
    assert tw.watch.call_args_list[0].args == (new,)
    tw.gui.status.update.assert_called()
    assert tw.watch.call_count == 1


@pytest.mark.asyncio
async def test_unknown_login_is_looked_up_once_until_the_list_changes(caplog):
    answer = {"data": {"user": None}}
    gql = AsyncMock(return_value=[answer])
    svc, tw = service(idle=["typo"], gql=gql)
    with caplog.at_level(logging.WARNING, logger="TwitchDrops"):
        await svc.track()
        await svc.track()
    assert gql.await_count == 1
    assert caplog.text.count("skipped") == 1
    tw.settings.idle_channels = ["typo", "other"]
    gql.return_value = [answer, answer]
    await svc.track()
    assert gql.await_count == 2


@pytest.mark.asyncio
async def test_whole_request_failure_is_not_a_skip():
    gql = AsyncMock(side_effect=[TimeoutError(), [{"data": {"user": None}}]])
    svc, _ = service(idle=["a"], gql=gql)
    with pytest.raises(TimeoutError):
        await svc.track()
    await svc.track()
    assert gql.await_count == 2
