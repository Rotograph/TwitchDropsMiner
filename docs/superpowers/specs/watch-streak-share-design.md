# Watch streak sharing: design

Addition to `idle-watch-channel-points-design.md`. Branch `feat/idle-watch-points`.

## Intent

Twitch lets a viewer share a watch streak once it reaches a share threshold, and the owner wants the miner to do that for the idle channels because a share gives bonus channel points (third-party guides name 300 to 450 points; not confirmed by Twitch).

## Goals

- While idle watching, share every watch streak of a live listed channel that Twitch marks as shareable, once per milestone.
- A setting turns it off; default on (owner's request).

## Non-goals

Sharing while mining drops (same rule as the bonus claim), sharing for channels not on the idle list, the `viewer-milestones` websocket topic (its message shapes are unverified and the topic budget is tight), recovering lost streaks.

## Anti-goals

- Sharing a milestone twice or retrying a refused share in a loop.
- Any delay to drop mining.

## Facts and their status

- Read, checked live with a read-only probe: raw query `query($login:String!){ user(login:$login){ channel{ id self{ viewerMilestones{ id category value state shareStatus }}}}}` returns a list per channel, for example `category: WATCH_STREAK`, `value: "9"` (string), `state: ACTIVE`, `shareStatus: CAN_NOT_SHARE` or `SHARED`; `viewerMilestones` is null for a channel without a streak.
- Share, unverified (from twitch.tv's bundles via a third-party client that never ran it): raw mutation `mutation($input: ShareViewerMilestoneInput!) { shareViewerMilestone(input: $input) { milestone { id } error { code } } }` with `{"input": {"channelID": "<id>", "milestoneID": "<id>"}}`.
- A share makes Twitch post a visible `viewermilestone` notice ("... watched N consecutive streams ...") in the streamer's chat under the owner's name (Twitch IRC tag docs; Streamer.bot docs).

## How

- Setting `share_watch_streaks: bool = True`, validated like `claim_channel_points`, toggle in the idle channel card.
- While the idle loop runs, at most every 5 minutes, and right after a `points-earned` message with the `WATCH_STREAK` reason for a listed channel, read the milestones of the live tracked listed channels in one batched request.
- For each `WATCH_STREAK` milestone with `state` `ACTIVE` and `shareStatus` `CAN_SHARE`, send the share. Success: `data.shareViewerMilestone` is an object with `error` null. Then one INFO line: "Shared watch streak on <channel> (<value> streams)".
- A refused or failed share logs one WARNING with the error code (no body) and is tried at most once more in the next check; after that it is left alone until the miner restarts.
- The live test confirms the mutation; until then the docs call sharing unverified.
