# Idle watch and channel points: design

Branch: `feat/idle-watch-points` (from `docs/open-reward-campaigns`). Status: draft for the owner.

## Intent

The owner runs this fork of TDM as a Docker container. When no wanted game has a mineable campaign the miner goes idle and watches nothing. The owner wants it to watch Twitch channels they choose during that time, so their watch streaks keep going and they earn channel points there, and to claim the channel points bonus chest when it shows up.

This is for the owner's own fork (Rotograph/TwitchDropsMiner) and deployment only. It is not meant as an upstream PR: upstream answered the same requests (#55 "Channel points", #57 "Add Idle Watch") by pointing to another fork, SimpliAj/twitchdropsminer (MIT), which has both features. That fork is used as a reference for ideas only.

## Goals

1. An ordered list of idle channels (Twitch logins) in Settings: add, remove and reorder. The order is the channel points priority (owner's decision).
2. Streaks for every listed channel (owner's decision): whenever the miner has nothing to mine (no wanted game, or no watchable channel for the wanted games) and it is inside mining time, each live listed channel gets one streak visit per stream. Between visits the miner watches the highest live channel on the list for points; when it goes offline the next live one takes over, and a higher listed channel that goes live takes over after its visit.
3. Drops always win. As soon as a wanted game has a watchable channel again, normal channel selection takes over, exactly as today.
4. When a channel points bonus becomes claimable on the channel being idle watched, it is claimed automatically. Only while idle watching, never while mining drops (owner's decision). A setting turns it off; default on.
5. The dashboard shows when the miner is idle watching (status line "Idle watching <channel>") and the points balance on the watched channel.

## Non-goals

Keeping streaks while drops are being mined (drops win, idle watching only runs when there is nothing to mine), predictions or betting, point redemptions, several accounts, watching more than one channel at a time (visits run one after another), raids, Discord or Telegram notifications for points, a watch streak counter (no known Twitch field), KickDropsMiner (handled in another session).

## Anti-goals (a failure even if it works)

- Idle watching delays or blocks drop mining in any way, for example by holding a channel slot, delaying the inventory refresh, or making the channel switch prefer an idle channel.
- The miner watches anything while paused, outside mining time, while logged out or when mining is turned off.
- The idle channel names or the points balances end up in anything shared: diagnostics dumps are treated like the existing account data in them (redacted or left out the same way as `games_to_watch`).

## Constraints

- Build on `docs/open-reward-campaigns` so reward-campaign mining stays. Follow `AGENTS.md` (architecture, services, i18n strings for new UI text, logging, tests with pytest, `ruff check src/`).
- Reuse what exists:
  - `WatchService.watch_loop` already keeps sending watch traffic for whatever `watching_channel` is set, as long as it is online, not banned and inside mining time. It does not need a drop.
  - `GQL_OPERATIONS["ChannelPointsContext"]` (balance and any open claim for a channel login) and `["ClaimCommunityPoints"]` (claim by `claimID` and `channelID`) are defined but unused.
  - `WEBSOCKET_TOPICS["User"]["CommunityPoints"]` (`community-points-user-v1`) is defined but not subscribed.
- Today every entry into `State.IDLE` calls `stop_watching()` (`src/core/client.py`, IDLE branch). IDLE is entered for several reasons: nothing wanted (CHANNELS_CLEANUP), no watchable channel (CHANNEL_SWITCH), outside mining time, and others such as logout. Only the first two may start idle watching.
- The persisted-query hashes of the two points operations may be stale. `ChannelPointsContext` can be checked live with a read-only probe before any code relies on it; `ClaimCommunityPoints` can only be checked by a real claim.
- Websocket stream events only reach channels in `Twitch.channels` (`process_stream_state` and `process_stream_update` drop unknown IDs). Idle channels must be in that dict while idle watching, and a full cleanup or the `MAX_CHANNELS` cap must never keep them at the expense of drop channels. They are only added when no wanted game has a channel, so the cap is not reached then.
- `on_channel_update` only switches to channels that pass `can_watch`, so idle channels need their own online and offline handling there.
- For a channel with no campaign the watch loop keeps sending watch traffic and logs "No active drop could be determined" once a minute at CALL level; no reward cutoff applies.

## Approaches

- **A. Own implementation in the fork (picked by the owner).** Small additions on the existing watch loop and GQL operations, reward campaigns kept, deployable from the owner's fork through the existing docker stack.
- **B. Run SimpliAj's fork instead.** Everything at once plus extras, but it drops the reward-campaign work in this fork and means running a large third-party codebase with the owner's Twitch session. Rejected.
- **C. Claim the bonus chest only, no idle watching.** Smallest, but misses the main request. Rejected.

## How (decided)

- **Settings:** `idle_channels: list[str]` (lowercase logins, unique, at most 20) and `claim_channel_points: bool = True`, with the same validation and save path as the other settings, and a Settings card: an input to add a login, the list with move up, move down and remove, and the toggle.
- **When idle watching runs:** on the two "nothing to mine" entries into IDLE, if mining time is open, the miner is not paused and the list is not empty. The IDLE branch keeps its current behaviour for every other reason.
- **Tracking:** look up the listed channels' stream state (the existing `GetStreamInfo` path by login), add them to `Twitch.channels` and subscribe their stream state topics like other tracked channels.
- **Streak visits:** a live listed channel whose current stream (broadcast ID) has not had a visit is due. Due channels are visited one at a time in list order via the existing `watch()`. A visit ends after 7 minutes, or earlier when a `points-earned` message for that channel carries the watch-streak reason code, or when the channel goes offline. Visited broadcast IDs are kept in memory only; after a restart a stream may get a second visit, which costs a few minutes and nothing else.
- **Points watch:** with no visit due, watch the highest live listed channel. Offline moves to the next live one; nothing live means watching nothing, as today.
- The 7 minutes and the reason code come from TwitchChannelPointsMiner's documentation and are not verified here. The live test logs one raw `points-earned` message to confirm the code; if no such code exists, visits simply run the full 7 minutes.
- **Back to drops:** the next inventory refresh or games update runs the normal flow. Because an idle channel never passes `can_watch`, `should_switch` already prefers any drop channel; the cleanup removes the idle channels' topics when they are not needed.
- **Points** (the `ChannelPointsContext` hash was checked live with a read-only probe: it answers without errors, with `data.community.channel.self.communityPoints.balance` and `availableClaim`, null when there is no bonus): subscribe the user's `CommunityPoints` topic at login next to the drops topic. On a `claim-available` message for the channel being idle watched (ignored while mining drops), send `ClaimCommunityPoints` when the setting is on; on `points-earned`, update the balance shown. When an idle watch starts, one `ChannelPointsContext` query reads the balance and claims an open bonus that was there before the watch started.
- **Logging:** one INFO line per claim (channel, points), one INFO line when idle watching starts, switches or starts or ends a streak visit; no line per points-earned message.

## Left to the builder

- The exact shape of `community-points-user-v1` messages and of `availableClaim` when a bonus is open: confirm them with a read-only probe or a DEBUG log of a real message before writing the parser, and test with those shapes.
- Where the idle state lives (a small service next to `WatchService`, or methods on it) and how the IDLE branch asks it, within the existing service layout.
- The dashboard layout of the new card and the balance, following the existing Settings cards and the channel panel.
- How the idle channels show in the dashboard channel list, if at all.

## How we know it works

- Unit tests:
  - settings validation and round trip;
  - visit order and one visit per broadcast ID, the 7-minute end and the early end on the streak reason code, the points channel by list priority, the offline fallback, and handing back to drops;
  - no idle watching when paused, outside mining time, logged out, with an empty list, or for the other IDLE reasons;
  - the claim handler with a fake GQL client (claims only with the setting on and only once per claim id);
  - the balance update.
- Live on the owner's deployment, with nothing mineable:
  - idle watching starts on the first live listed channel;
  - the balance on that channel rises within about 10 minutes;
  - a bonus chest is claimed within about 20 minutes;
  - the miner goes back to drops when a wanted campaign becomes mineable.
- Streaks: a streak-coded `points-earned` message during a visit, if Twitch sends one; otherwise the owner checks a streak on Twitch after a visited stream. Until one of those is seen, the docs call streaks untested.
