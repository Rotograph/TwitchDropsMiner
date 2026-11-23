"""
Idle watch service: when nothing can be mined, watch the channels the owner lists.

Each live listed channel gets one streak visit per broadcast, otherwise the highest
live listed channel is watched for channel points.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.config import GQL_OPERATIONS, MAX_CHANNELS, WebsocketTopic
from src.models.channel import Channel
from src.utils import chunk


if TYPE_CHECKING:
    from src.config import GQLOperation, GQLQuery, GQLRawQuery
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
        known = {c.login for c in twitch.channels.values()}
        missing = [login for login in logins if login not in known]
        new: list[Channel] = []
        resolved: dict[str, Channel] = {}
        for batch in chunk(missing, 20):
            ops: list[GQLOperation | GQLQuery | GQLRawQuery] = [GQL_OPERATIONS["GetStreamInfo"].with_variables({"channel": n}) for n in batch]
            response = await twitch.gql_request(ops)
            answers = response if isinstance(response, list) else [response]
            for login, answer in zip(batch, answers, strict=False):
                data = answer.get("data") if isinstance(answer, dict) else None
                user = data.get("user") if isinstance(data, dict) else None
                if not user or answer.get("errors"):
                    logger.warning("Idle channel %s skipped: lookup failed or unknown", login)
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
        if new:
            new = await twitch._channel_service.filter_banned_channels(new)
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
        return [c for login in logins if (c := by_login.get(login)) is not None and c.id in twitch.channels]
