# Idle Watch and Channel Points Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When TDM has nothing to mine, watch the owner's listed Twitch channels: one streak visit per stream for every listed channel, otherwise the highest live listed channel for channel points, and claim the points bonus while doing so.

**Architecture:** A new `IdleWatchService` (`src/services/idle_watch.py`) owns the idle list state: it looks the listed logins up, adds them to `Twitch.channels`, decides which one to watch (streak visit or points watch) and runs a small loop while the miner sits in `State.IDLE` for a "nothing to mine" reason. It watches through the existing `Twitch.watch()` and `WatchService.watch_loop`. A `community-points-user-v1` websocket handler on the same service claims bonuses and tracks balances, only while idle watching.

**Tech Stack:** Python 3 asyncio, aiohttp GQL client already in TDM, pydantic `SettingsUpdate`, vanilla JS dashboard (`web/static/app.js`, `web/index.html`), pytest with the repository's node-based frontend helpers.

**Spec:** `docs/superpowers/specs/2026-11-23-idle-watch-channel-points-design.md`

## Global Constraints

- Branch `feat/idle-watch-points`, based on `docs/open-reward-campaigns`. Fork-only feature by owner decision; `AGENTS.md` "Project Scope" lists channel points mining as not supported upstream: leave that text alone, do not open an upstream PR.
- Read `AGENTS.md` (Architecture, Translation System, Testing, CI) and `CONTRIBUTING.md` before editing. Follow the services layout, logging levels and i18n rules there.
- New user-visible strings go in `lang/English.json` and the TypedDicts in `src/i18n/translator.py`; other languages fall back to English. If a test enforces key parity across languages, add the English text to the other files too.
- Drops always win: nothing in this plan may change channel selection, `can_watch`, `should_switch`, inventory refresh timing or `MAX_CHANNELS` handling for drop channels.
- Idle watching and bonus claims run only when all hold: state is `State.IDLE` entered for "nothing to mine", `is_mining_time()` is true (covers pause and mining hours), the user is logged in, `idle_channels` is not empty.
- Visit length `IDLE_VISIT_SECONDS = 420`; loop tick `IDLE_TICK_SECONDS = 30`; at most 20 idle channels; logins match `^[a-z0-9_]{1,25}$` after `strip().lower()`.
- Streak reason code `"WATCH_STREAK"`, bonus reason code `"CLAIM"` (from TwitchChannelPointsMiner, unverified here; Task 6 confirms live).
- Logs: INFO for idle start, switch, visit start, visit end, and each claim; nothing per `points-earned`. Never log message bodies or balances above DEBUG.
- Diagnostics: counts and flags only (`idle_channels_total`, `claim_channel_points`), never logins or balances.
- Commit messages: Conventional Commits in English, no AI attribution trailers. Do not push.
- Tests: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/ --ignore=tests/test_gui.py -q` (Windows; the suite has a few pre-existing symlink failures on Windows, compare against the base branch before blaming the change). Lint: `.venv/Scripts/python -m ruff check src/` and `.venv/Scripts/python -m mypy src` if CI runs it.

## Review Focus

1. A listed login that does not exist, is banned for the account, or returns a GQL error: skipped with one WARNING, the other channels still work, no crash. (Task 3 test `test_lookup_skips_unknown_and_errors`.)
2. A listed channel that is also a drop channel already in `Twitch.channels`: the existing object is reused, no duplicate, and its drop role is untouched. (Task 3 test `test_enter_reuses_tracked_channel`.)
3. Pause or mining hours closing while idle watching: watching stops, the loop ends, no claim is sent. (Task 4 test `test_idle_stops_outside_mining_time`, Task 5 test `test_claim_ignored_when_not_idle`.)
4. The owner removes a channel from the list while it is being visited or watched: the next pick drops it. (Task 2 test `test_removed_channel_is_dropped`.)
5. A points message for another channel, a malformed message, or a message while mining drops: ignored without a claim or an exception. (Task 5 tests `test_claim_only_for_idle_watched_channel`, `test_malformed_points_message_is_ignored`.)

---

### Task 1: Settings, API validation and diagnostics

**Files:**
- Modify: `src/config/settings.py` (defaults dict, `Settings` annotations, a new `IdleChannelSettings` next to `MiningHoursSettings`)
- Modify: `src/web/app.py` (`SettingsUpdate`)
- Modify: `src/web/managers/settings.py` (validation, apply, `should_trigger_update`, `get_settings`)
- Modify: `src/diagnostics.py` (settings section near `games_to_watch_total`)
- Test: `tests/test_idle_watch_settings.py`

**Interfaces:**
- Produces: `IdleChannelSettings.normalize(value: Any) -> list[str]` (raises `ValueError("invalid_idle_channels")`); settings attributes `settings.idle_channels: list[str]` (default `[]`) and `settings.claim_channel_points: bool` (default `True`); API keys `idle_channels`, `claim_channel_points` in the settings payload.

- [ ] **Step 1: Write the failing tests** in `tests/test_idle_watch_settings.py`, following `tests/test_reward_priority.py` and the `prefer_followed_channels` tests for fixtures:
  - `test_normalize_lowercases_strips_and_dedupes`: `normalize([" Foo ", "foo", "bar_1"]) == ["foo", "bar_1"]`.
  - `test_normalize_rejects_bad_values`: each of `"foo"`, `[1]`, `["bad-name"]`, `[""]`, `["a" * 26]`, `["x%d" % i for i in range(21)]` raises `ValueError("invalid_idle_channels")`.
  - `test_defaults`: a fresh `Settings` has `idle_channels == []` and `claim_channel_points is True`.
  - `test_settings_update_saves_and_triggers_games_update`: posting `{"idle_channels": ["Foo"], "claim_channel_points": False}` through the settings manager stores `["foo"]` and `False`, sets `should_trigger_update` only when `idle_channels` changed, and the response includes both keys.
  - `test_claim_channel_points_requires_strict_bool`: `"false"` is rejected like `prefer_followed_channels`.
  - `test_diagnostics_report_counts_only`: with `idle_channels = ["secretname"]` the diagnostics settings section has `idle_channels_total == 1`, a `claim_channel_points` flag, and the string `secretname` appears nowhere in the dump.
- [ ] **Step 2: Run** `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_idle_watch_settings.py -q`. Expected: FAIL (no `IdleChannelSettings`).
- [ ] **Step 3: Implement** by following every place `grep -rn prefer_followed_channels src` finds for the bool, and `reward_priority` in `src/web/managers/settings.py` for the list (normalize on request, compare with the previous value, set `should_trigger_update = True` on change, `_log_change` with the count only, not the names).
- [ ] **Step 4: Run** the same test file. Expected: PASS. Then `ruff check src/`. Expected: no findings.
- [ ] **Step 5: Commit** `feat: add idle channel and channel points settings`.

### Task 2: Idle pick logic

**Files:**
- Create: `src/services/idle_watch.py`
- Test: `tests/test_idle_watch.py`

**Interfaces:**
- Produces in `src/services/idle_watch.py`:
  - constants `IDLE_VISIT_SECONDS = 420`, `IDLE_TICK_SECONDS = 30`, `STREAK_REASON = "WATCH_STREAK"`, `CLAIM_REASON = "CLAIM"`;
  - `@dataclass class Visit: channel_id: int; broadcast_id: int; started: float; done: bool = False`;
  - `class IdleWatchService` with `__init__(self, twitch: Twitch)`, attributes `visited: set[int]`, `visit: Visit | None`, and `pick(self, live: list[Channel], now: float) -> Channel | None`.
- `live` is the listed channels that are tracked, online and not banned, in `settings.idle_channels` order; `Channel._stream.broadcast_id` identifies a stream.

- [ ] **Step 1: Write the failing tests** with small fake channels (an object with `id`, `online`, `banned`, `_stream.broadcast_id`):
  - `test_first_due_channel_gets_a_visit`: live `[a(b=1), b(b=2)]`, empty `visited` → returns `a`, `visit.channel_id == a.id`.
  - `test_visit_holds_until_420_seconds`: same `pick` at `now + 419` returns `a`; at `now + 420` it returns `b` and `1 in visited`.
  - `test_done_visit_ends_early`: setting `visit.done = True` makes the next pick move on and adds the broadcast to `visited`.
  - `test_visit_ends_when_channel_offline_or_new_broadcast`: visited channel missing from `live`, or present with another `broadcast_id`, ends the visit.
  - `test_points_watch_on_highest_live_after_visits`: all broadcasts visited → returns `live[0]`; empty `live` → `None`.
  - `test_removed_channel_is_dropped`: the visited channel no longer in `live` (removed from settings) is not returned.
  - `test_visited_is_pruned`: after a pick, `visited` holds only broadcast IDs of channels in `live`.
- [ ] **Step 2: Run** `... -m pytest tests/test_idle_watch.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `pick` with these rules, in order:
  1. If `visit` is set and its channel is in `live` with the same `broadcast_id`, not `done`, and `now - visit.started < IDLE_VISIT_SECONDS`: return that channel.
  2. Else if `visit` is set: add its `broadcast_id` to `visited`, clear `visit`.
  3. Prune `visited` to the broadcast IDs in `live`.
  4. The first channel in `live` whose `broadcast_id` is not in `visited`: start `Visit(channel.id, broadcast_id, now)` and return it.
  5. Else return `live[0]` if `live` else `None`.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: pick idle channels for streak visits and points`.

### Task 3: Idle channel lookup and tracking

**Files:**
- Modify: `src/services/idle_watch.py`
- Test: `tests/test_idle_watch.py`

**Interfaces:**
- Consumes: `IdleWatchService`, `settings.idle_channels` (Task 1, 2).
- Produces: `async def track(self) -> list[Channel]` (returns the listed channels in list order that are now in `Twitch.channels`), `def live_channels(self) -> list[Channel]`.

- [ ] **Step 1: Write the failing tests** with a fake `gql_request` returning `GetStreamInfo`-shaped answers (`{"data": {"user": {"id": "...", "login": ..., "stream": {...} | None}}}`, as parsed by `ChannelService.bulk_check_online`):
  - `test_lookup_skips_unknown_and_errors`: one login returns `user: None`, one returns `errors`; the third is tracked; one WARNING without the response body.
  - `test_enter_reuses_tracked_channel`: a login whose user id is already a key of `Twitch.channels` keeps the existing object.
  - `test_track_subscribes_stream_topics_and_respects_cap`: new channels get `StreamState` and `StreamUpdate` topics exactly like `CHANNELS_FETCH` does, and no more are added than `MAX_CHANNELS - len(Twitch.channels)`.
  - `test_live_channels_follow_list_order_and_skip_banned_offline`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** `track`: one batched `gql_request` of `GetStreamInfo` ops for the logins not yet tracked (by login), build `Channel(twitch, id=user["id"], login=login)` and call `channel.external_update(user, [])` when `stream` is set (same as `bulk_check_online`), add to `Twitch.channels` and `gui.channels` the way `CHANNELS_FETCH` does, subscribe the two topics. `live_channels` maps `settings.idle_channels` through the tracked channels by login.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: track idle channels and their stream state`.

### Task 4: Run idle watching from the client state machine

**Files:**
- Modify: `src/core/client.py` (`change_state`, IDLE branch, CHANNELS_CLEANUP no-games branch, CHANNEL_SWITCH no-channel branch, service creation next to `_watch_service`)
- Modify: `src/services/idle_watch.py` (`enter`, `stop`, `poke`, loop)
- Modify: `src/services/message_handlers.py` (`on_channel_update`)
- Modify: `lang/English.json`, `src/i18n/translator.py` (`status.idle_watching`)
- Test: `tests/test_idle_watch.py`, plus a client-level test next to `tests/test_client_state_requests.py`

**Interfaces:**
- Consumes: Task 2 `pick`, Task 3 `track`, `live_channels`.
- Produces: `Twitch.change_state(self, state: State, *, idle_watch: bool = False) -> None`; `Twitch._idle_watch: IdleWatchService`; `IdleWatchService.enter() -> None` (starts the loop task once), `stop() -> None` (cancels it, keeps `visited` and `visit`), `poke() -> None` (wakes the loop), `active: bool` (true while the loop runs); status string `_.t["status"]["idle_watching"] = "Idle watching {channel}"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_nothing_to_mine_entries_allow_idle_watch`: the CHANNELS_CLEANUP branch with no wanted games and the CHANNEL_SWITCH branch with no watchable channel call `change_state(State.IDLE, idle_watch=True)`; the logout path (`self._state = State.IDLE`), the mining-hours close (`change_state(State.IDLE)` near line 612) and the schedule close keep `idle_watch` false.
  - `test_idle_branch_enters_idle_watch_only_when_allowed`: IDLE with the flag, mining time open, logged in and a non-empty list calls `enter()` and not `stop_watching()`; any condition false calls `stop_watching()` as today.
  - `test_leaving_idle_stops_the_loop`: `change_state(State.INVENTORY_FETCH)` calls `stop()`.
  - `test_idle_stops_outside_mining_time`: a loop tick with `is_mining_time()` false calls `stop_watching()` and ends the loop.
  - `test_loop_watches_pick_with_idle_status`: a tick calls `Twitch.watch(channel, update_status=False)` only when the pick differs from `watching_channel`, sets the status to `Idle watching <name>`, and logs one INFO line per start, switch, visit start and visit end.
  - `test_idle_channel_online_event_pokes`: `on_channel_update` for an idle-listed channel going online while `active` calls `poke()`, and does not change behaviour for drop channels.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** `change_state` stores `self._idle_watch_allowed = idle_watch and state is State.IDLE` and calls `self._idle_watch.stop()` when `state is not State.IDLE`. The IDLE branch keeps its status update and, when allowed and the conditions hold, `await self._idle_watch.enter()` instead of `stop_watching()`. The loop: `track()`, then every `IDLE_TICK_SECONDS` or on `poke()`: exit with `stop_watching()` if the conditions no longer hold, else `pick(live_channels(), monotonic())` and watch it, or `stop_watching()` when it returns `None`. A watched idle channel going offline already reaches CHANNEL_SWITCH and comes back to IDLE with the flag; do not add a second path for that.
- [ ] **Step 4: Run** the new tests and the whole suite. Expected: new tests PASS; no test that passed on the base branch fails.
- [ ] **Step 5: Commit** `feat: watch idle channels when nothing can be mined`.

### Task 5: Channel points: balance and bonus claims

**Files:**
- Modify: `src/services/idle_watch.py` (`process_points`, `claim`, `refresh_points`)
- Modify: `src/core/client.py` (topic at login next to `"User", "Drops"`)
- Modify: `lang/English.json`, `src/i18n/translator.py` (`status.idle_watching_points = "Idle watching {channel}: {points} channel points"`)
- Test: `tests/test_idle_points.py`

**Interfaces:**
- Consumes: `active`, `visit`, `poke()` (Task 4); `settings.claim_channel_points` (Task 1).
- Produces: `async def process_points(self, user_id: int, message: JsonType) -> None`, `async def refresh_points(self, channel: Channel) -> None`, `balances: dict[int, int]`.
- Message shapes used in tests (from TwitchChannelPointsMiner, confirmed live in Task 6): `{"type": "points-earned", "data": {"channel_id": "123", "point_gain": {"reason_code": "WATCH", "total_points": 10}, "balance": {"balance": 1510, "channel_id": "123"}}}` and `{"type": "claim-available", "data": {"claim": {"id": "abc", "channel_id": "123"}}}`. `ChannelPointsContext` answer: `data.community.channel.self.communityPoints.balance` (int) and `.availableClaim` (null or an object with `id`), checked live on 2026-11-23.

- [ ] **Step 1: Write the failing tests** with a fake `gql_request`:
  - `test_claim_only_for_idle_watched_channel`: `claim-available` for the watched idle channel sends one `ClaimCommunityPoints` with `{"input": {"claimID": "abc", "channelID": "123"}}`; for another channel nothing is sent.
  - `test_claim_ignored_when_not_idle`: `active` false (mining drops, paused) → nothing sent.
  - `test_claim_respects_setting_and_dedupes`: `claim_channel_points` false → nothing sent; the same claim id twice → one request.
  - `test_points_earned_updates_balance_and_status`: balance stored for idle channels only, status shows the points string for the watched one.
  - `test_streak_reason_ends_visit`: `points-earned` with `"WATCH_STREAK"` for the visited channel sets `visit.done` and calls `poke()`; for another channel it does not.
  - `test_claim_reason_logs_one_info`: `"CLAIM"` logs one INFO line with channel name and points.
  - `test_refresh_points_reads_balance_and_claims_open_bonus`: a `ChannelPointsContext` answer with `availableClaim: {"id": "x"}` stores the balance and claims `x` once.
  - `test_malformed_points_message_is_ignored`: missing keys or wrong types raise nothing and send nothing.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** using `GQL_OPERATIONS["ClaimCommunityPoints"]` and `["ChannelPointsContext"]`; subscribe `WebsocketTopic("User", "CommunityPoints", auth_state.user_id, self._idle_watch.process_points)` at login; call `refresh_points` when the loop starts watching a new idle channel. Log one raw `points-earned` message per run at DEBUG with balances removed, so Task 6 can confirm the shapes.
- [ ] **Step 4: Run** the tests and the whole suite. Expected: PASS, no new failures.
- [ ] **Step 5: Commit** `feat: claim channel points bonus while idle watching`.

### Task 6: Dashboard settings card and docs

**Files:**
- Modify: `web/index.html` (a Settings card next to `prefer-followed-channels`), `web/static/app.js` (load, render, save), `lang/English.json`, `src/i18n/translator.py` (`gui.settings.idle_channels`, `idle_channels_help`, `idle_channels_add`, `idle_channels_remove`, `idle_channels_up`, `idle_channels_down`, `idle_channels_save_error`, `claim_channel_points`, `claim_channel_points_help`, `claim_channel_points_save_error`)
- Modify: `docs/usage.md` (a short "Idle watching" section: what runs when, that streaks are unverified until seen live, that claims only happen while idle watching)
- Test: `tests/test_idle_watch_frontend.py`

**Interfaces:**
- Consumes: settings keys `idle_channels`, `claim_channel_points` (Task 1).

- [ ] **Step 1: Write the failing tests** with `tests/javascript_helpers.py`, in the style of `tests/test_reward_priority_frontend.py`:
  - `test_idle_list_renders_in_order_with_escaped_names`: names render as text, not HTML.
  - `test_add_move_remove_save_the_new_order`: each control sends the full list in the new order; a failed save rolls back and shows the translated error.
  - `test_add_rejects_invalid_login_client_side`: `bad-name` shows the error and sends nothing.
  - `test_claim_toggle_saves_like_prefer_followed`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** reusing the reward-priority row and move pattern and `saveBooleanSetting`.
- [ ] **Step 4: Run** the frontend tests, the whole suite and `ruff check src/`. Expected: PASS, no new failures.
- [ ] **Step 5: Commit** `feat: idle channel settings in the dashboard`.

### Task 7: Live check (main session, after the owner approves push and deploy)

- [ ] Push `feat/idle-watch-points` to the owner's fork only after the owner says so; point the miner stack's build context at the branch and `stack twitch-drops-miner up`.
- [ ] With nothing mineable and two live listed channels: idle watching starts on the first, a visit runs, then the points watch takes over; the balance rises within about 10 minutes; one bonus is claimed within about 20 minutes; the DEBUG `points-earned` line confirms the message shape and whether `WATCH_STREAK` appears.
- [ ] Making a wanted campaign mineable hands the miner back to drops at the next refresh.
- [ ] Record what ran live and what did not in the handoff; streaks stay "untested" until a streak code or the owner's check is seen.
