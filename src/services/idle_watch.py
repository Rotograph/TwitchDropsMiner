"""
Idle watch service: when nothing can be mined, watch the channels the owner lists.

Each live listed channel gets one streak visit per broadcast, otherwise the highest
live listed channel is watched for channel points.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
from dataclasses import dataclass
from itertools import zip_longest
from typing import TYPE_CHECKING

import aiohttp

from src.config import GQL_OPERATIONS, MAX_CHANNELS, State, WebsocketTopic
from src.exceptions import MinerException
from src.i18n import _
from src.models.channel import Channel
from src.utils import chunk, task_wrapper


if TYPE_CHECKING:
    from src.config import GQLOperation, GQLQuery, GQLRawQuery, JsonType
    from src.core.client import Twitch


logger = logging.getLogger("TwitchDrops")

IDLE_VISIT_SECONDS = 420
IDLE_TICK_SECONDS = 30
STREAK_REASON = "WATCH_STREAK"
CLAIM_REASON = "CLAIM"


@dataclass
class Visit:
    channel_id: int
    broadcast_id: int
    started: float
    done: bool = False


class IdleWatchService:
    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self.visited: set[int] = set()
        self.visit: Visit | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        # logins track() gave up on (unknown, banned, over the cap) until the list changes
        self._skipped: set[str] = set()
        self._seen_list: list[str] | None = None
        self.balances: dict[int, int] = {}
        # claim ids claimed on the channel being watched; cleared when it changes
        self._claimed: set[str] = set()
        # claim ids with a request in flight, so a repeated message does not send twice
        self._claiming: set[str] = set()
        self._raw_logged = False
        # the channel the loop chose to watch, and the visit it last announced
        self.watched: Channel | None = None
        self._last_visit: Visit | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def allowed(self) -> bool:
        tw = self._twitch
        return (
            tw.is_mining_time()
            and tw._auth_state._logged_in.is_set()
            and bool(tw.settings.idle_channels)
        )

    def enter(self) -> None:
        if not self.active:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> asyncio.Task[None] | None:
        """Cancel the loop and return its task, so a caller can await the cancellation."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        return task

    def reset(self) -> None:
        """Forget the previous account's visits, balances, claims and lookups."""
        self.visited.clear()
        self.visit = None
        self.balances.clear()
        self._claimed.clear()
        self._skipped.clear()
        self._seen_list = None

    def poke(self) -> None:
        self._wake.set()

    def show_status(self) -> None:
        """Re-show "Idle watching <channel>" while the loop's channel is still watched."""
        if self.active and self.watched is not None and self.watched is self._twitch.watching_channel.get_with_default(None):
            self._show_status(self.watched)

    async def _run(self) -> None:
        tw = self._twitch
        logger.info("Idle watching started")
        # the last drop's progress panel does not belong to an idle channel
        tw.gui.clear_drop()
        self._last_visit = None
        self.watched = None
        while True:
            # cleared before the awaits, so a poke during the tick runs one more tick
            self._wake.clear()
            watching = tw.watching_channel.get_with_default(None)
            if watching is not None and tw.can_watch(watching):
                # Drops won: normal selection takes the channel over; the watch is not stopped.
                logger.info("Idle watching stopped: a drop channel can be watched")
                tw.change_state(State.CHANNEL_SWITCH)
                return
            if not self.allowed():
                tw.stop_watching()
                logger.info("Idle watching stopped")
                return
            try:
                await self._tick()
            except Exception:
                # one bad tick must not end idle watching; CancelledError still propagates
                logger.exception("Idle watching step failed, retrying")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), IDLE_TICK_SECONDS)

    async def _tick(self) -> None:
        tw = self._twitch
        try:
            await self.track()
        except (MinerException, aiohttp.ClientError, TimeoutError) as exc:
            # no response body: the exception type is enough to diagnose
            logger.warning("Idle channel lookup failed (%s), retrying", type(exc).__name__)
        channel = self.pick(self.live_channels(), time.monotonic())
        if self.visit is not self._last_visit:
            if self._last_visit is not None:
                logger.info("Idle visit ended: broadcast %s", self._last_visit.broadcast_id)
            if self.visit is not None:
                logger.info("Idle visit started: %s", channel.name if channel else "?")
            self._last_visit = self.visit
        if channel is None:
            tw.stop_watching()
        elif channel is not tw.watching_channel.get_with_default(None) or channel is not self.watched:
            if self.watched is not None:
                logger.info("Idle watching switched to %s", channel.name)
            if channel is not self.watched:
                self._claimed.clear()
            self.watched = channel
            tw.watch(channel, update_status=False)
            self._show_status(channel)
            await self.refresh_points(channel)

    def _show_status(self, channel: Channel) -> None:
        points = self.balances.get(channel.id)
        if points is None:
            text = _.t["status"]["idle_watching"].format(channel=channel.name)
        else:
            text = _.t["status"]["idle_watching_points"].format(channel=channel.name, points=points)
        self._twitch.gui.status.update(text)

    def _idle_watched(self, channel_id: object) -> Channel | None:
        """The channel being idle watched, if channel_id is it and the loop is running."""
        watched = self._twitch.watching_channel.get_with_default(None)
        if not self.active or watched is None or str(watched.id) != str(channel_id):
            return None
        # a channel drops can use is mined, not idle watched, even before the loop hands it over
        return None if self._twitch.can_watch(watched) else watched

    async def _gql(self, op: GQLOperation) -> JsonType | None:
        """Send op; failures are logged as one warning without the response body."""
        try:
            response = await self._twitch.gql_request(op)
        except (MinerException, aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("Channel points request %s failed (%s)", op["operationName"], type(exc).__name__)
            return None
        if not isinstance(response, dict) or response.get("errors"):
            logger.warning("Channel points request %s returned errors", op["operationName"])
            return None
        return response

    async def claim(self, channel: Channel, claim_id: str) -> None:
        if (
            not self._twitch.settings.claim_channel_points
            or claim_id in self._claimed
            or claim_id in self._claiming
        ):
            return
        self._claiming.add(claim_id)
        try:
            response = await self._gql(GQL_OPERATIONS["ClaimCommunityPoints"].with_variables(
                {"input": {"claimID": claim_id, "channelID": str(channel.id)}},
            ))
        finally:
            self._claiming.discard(claim_id)
        # a failed request stays unclaimed, so the next claim-available or refresh retries it
        if response is None:
            return
        try:
            result = response["data"]["claimCommunityPoints"]
            error = result.get("error") if isinstance(result, dict) else True
        except (KeyError, TypeError):
            result, error = None, True
        if not isinstance(result, dict) or error is not None:
            # the reply carries an error code (for example an already claimed bonus) instead of the claim
            code = error.get("code") if isinstance(error, dict) else error
            logger.warning(
                "Channel points request ClaimCommunityPoints was not accepted%s",
                f" ({code})" if isinstance(code, str) else "",
            )
            return
        self._claimed.add(claim_id)
        logger.info("Claimed channel points bonus on %s", channel.name)

    async def refresh_points(self, channel: Channel) -> None:
        """Read the balance of a newly watched channel and claim a bonus that is already open."""
        try:
            response = await asyncio.wait_for(
                self._gql(GQL_OPERATIONS["ChannelPointsContext"].with_variables({"channelLogin": channel.login})),
                15,
            )
        except TimeoutError:
            logger.warning("Channel points request ChannelPointsContext timed out")
            return
        try:
            points = response["data"]["community"]["channel"]["self"]["communityPoints"]  # type: ignore[index]
            balance, available = points["balance"], points["availableClaim"]
        except (KeyError, TypeError):
            return
        if isinstance(balance, int) and not isinstance(balance, bool):
            self.balances[channel.id] = balance
            self._show_status(channel)
        if isinstance(available, dict) and isinstance(available.get("id"), str):
            await self.claim(channel, available["id"])

    @task_wrapper
    async def process_points(self, user_id: int, message: JsonType) -> None:
        """
        Handle community-points websocket messages while idle watching.

        Shapes: {"type": "points-earned", "data": {"channel_id": "1", "point_gain": {"reason_code": "WATCH",
        "total_points": 10}, "balance": {"balance": 1510}}} and
        {"type": "claim-available", "data": {"claim": {"id": "x", "channel_id": "1"}}}
        """
        msg_type = message.get("type") if isinstance(message, dict) else None
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            return
        if msg_type == "points-earned":
            self._log_raw(message)
            await self._points_earned(data)
        elif msg_type == "claim-available":
            claim = data.get("claim")
            if not isinstance(claim, dict) or not isinstance(claim.get("id"), str):
                return
            if (channel := self._idle_watched(claim.get("channel_id"))) is not None:
                await self.claim(channel, claim["id"])

    def _log_raw(self, message: JsonType) -> None:
        if self._raw_logged:
            return
        self._raw_logged = True
        raw = copy.deepcopy(message)
        balance = raw["data"].get("balance")
        if isinstance(balance, dict):
            balance.pop("balance", None)
        logger.debug("Raw points-earned message: %s", raw)

    async def _points_earned(self, data: JsonType) -> None:
        channel_id = data.get("channel_id")
        if not self.active or not isinstance(channel_id, str) or not channel_id.isdigit():
            return
        gain, balance = data.get("point_gain"), data.get("balance")
        reason = gain.get("reason_code") if isinstance(gain, dict) else None
        watched = self._idle_watched(channel_id)
        listed = {c.login for c in self._twitch.channels.values() if str(c.id) == channel_id}
        points = balance.get("balance") if isinstance(balance, dict) else None
        if isinstance(points, int) and not isinstance(points, bool) and (
            watched is not None or listed & set(self._twitch.settings.idle_channels)
        ):
            self.balances[int(channel_id)] = points
            if watched is not None:
                self._show_status(watched)
        visit = self.visit
        if reason == STREAK_REASON and visit is not None and str(visit.channel_id) == channel_id:
            visit.done = True
            self.poke()
        elif reason == CLAIM_REASON and watched is not None:
            # claim() already logged the INFO line for this bonus
            logger.debug("Channel points bonus on %s: +%s", watched.name, gain.get("total_points"))  # type: ignore[union-attr]

    @staticmethod
    def _broadcast_id(channel: Channel) -> int:
        stream = channel._stream
        assert stream is not None  # live channels are online
        return stream.broadcast_id

    def pick(self, live: list[Channel], now: float) -> Channel | None:
        ids = {c.id: self._broadcast_id(c) for c in live}
        visit = self.visit
        if visit is not None:
            if (
                ids.get(visit.channel_id) == visit.broadcast_id
                and not visit.done
                and now - visit.started < IDLE_VISIT_SECONDS
            ):
                return next(c for c in live if c.id == visit.channel_id)
            self.visited.add(visit.broadcast_id)
            self.visit = None
        self.visited &= set(ids.values())
        for channel in live:
            if ids[channel.id] not in self.visited:
                self.visit = Visit(channel.id, ids[channel.id], now)
                return channel
        return live[0] if live else None

    def live_channels(self) -> list[Channel]:
        by_login = {c.login: c for c in self._twitch.channels.values()}
        channels = (by_login.get(login) for login in self._twitch.settings.idle_channels)
        return [c for c in channels if c is not None and c.online and not c.banned]

    async def track(self) -> list[Channel]:
        """Look up listed logins not tracked yet and add them; whole-request failures propagate."""
        twitch = self._twitch
        logins = list(self._twitch.settings.idle_channels)
        if logins != self._seen_list:
            self._seen_list = logins
            self._skipped.clear()
        known = {c.login for c in twitch.channels.values()}
        missing = [login for login in logins if login not in known and login not in self._skipped]
        new: list[Channel] = []
        resolved: dict[str, Channel] = {}
        for batch in chunk(missing, 20):
            ops: list[GQLOperation | GQLQuery | GQLRawQuery] = [GQL_OPERATIONS["GetStreamInfo"].with_variables({"channel": n}) for n in batch]
            response = await twitch.gql_request(ops)
            answers = response if isinstance(response, list) else [response]
            # a login a short reply did not answer is skipped like an unknown one
            for login, answer in zip_longest(batch, answers[: len(batch)]):
                data = answer.get("data") if isinstance(answer, dict) else None
                user = data.get("user") if isinstance(data, dict) else None
                if not user or answer.get("errors"):
                    logger.warning("Idle channel %s skipped: lookup failed or unknown", login)
                    self._skipped.add(login)
                    continue
                channel_id = int(user["id"])
                channel = twitch.channels.get(channel_id) or next(
                    (c for c in new if c.id == channel_id), None,
                )
                if channel is None:
                    channel = Channel(twitch, id=channel_id, login=login)
                    if user.get("stream"):
                        channel.external_update(user, [])
                    new.append(channel)
                resolved[login] = channel
        new = new[: max(MAX_CHANNELS - len(twitch.channels), 0)]
        banned: set[int] = set()
        if new:
            allowed = await twitch._channel_service.filter_banned_channels(new)
            banned = {c.id for c in new} - {c.id for c in allowed}
            new = allowed
        topics: list[WebsocketTopic] = []
        handlers = twitch._message_handler_service
        for channel in new:
            twitch.channels[channel.id] = channel
            twitch.gui.channels.display(channel, add=True)
            topics.append(WebsocketTopic(
                "Channel", "StreamState", channel.id, handlers.process_stream_state,
            ))
            topics.append(WebsocketTopic(
                "Channel", "StreamUpdate", channel.id, handlers.process_stream_update,
            ))
        if topics:
            twitch.websocket.add_topics(topics)
        by_login = {c.login: c for c in twitch.channels.values()} | resolved
        for login in logins:
            channel = by_login.get(login)
            if channel is not None and (channel.id in banned or channel.banned) and login not in self._skipped:
                logger.warning("Idle channel %s skipped: banned", login)
                self._skipped.add(login)
        self._skipped |= {login for login, c in resolved.items() if c.id not in twitch.channels}
        return [c for login in logins if (c := by_login.get(login)) is not None and c.id in twitch.channels]
