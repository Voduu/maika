# Discord Bot Design Notes

Status: planning. Start date undecided (possibly Thanksgiving break or free time before winter break). Last updated: 2026-11-01.

Bot account name: Maika. The account was created on September 30, 2020, has existed in the friend server since, and has been offline for that whole time.

Two servers: a private server (feeds, reminders, personal tools, logs, persona notes) and the friend server (cat photos, later conversation).
Stack: Python, discord.py, SQLite, Docker on Unraid. Slash commands plus conversational equivalents for selected commands.
Authorship: the bot is built with Claude Code. Jido remains hand-written by the owner.

Many values below are marked "tune later". They live in the settings table (section 3.18; full list in section 10) so they can change without redeploying.

---

## 1. Build Layers

Each layer works without the ones above it. Finish one layer before starting the next.

1. **Layer 1: Feeds, reminders, permissions, cat photos.** The original bot. Useful on its own.
2. **Layer 2: Behavior systems without an LLM.** Mood, relationships, traits, nicknames, memories (manual), canned responses, cooldowns, presence, typos, self-correction, one-time events.
3. **Layer 3: LLM conversation.** Persona prompt assembly, retrieval memory, automatic memory extraction, model-assisted trait tagging, flexible conversational commands, local model.

Scope target: Layer 1 and Layer 2, with the minimum go-live set in section 7 (steps 1 through 9 plus the return event from step 12) as the first milestone. Layer 3 is the next project.

### 1.1 Data scope

- Global (keyed on the Discord user ID alone, shared across every server the bot is in): mood, relationships, memories, traits, nicknames, persona notes, cats and photos, one-time events, per-person settings such as timezone (9.11).
- Single values for the whole account: bot status (online, idle, do not disturb), sleep schedule, current mood.
- Per channel or per server: feed subscriptions, listen channels, addressing sessions, the reply queue, repeat counters. Cooldowns carry a scope (user, channel, server, or global).
- Hooks for a larger group, each a column with a neutral default until used: `guild_id` on permission overrides (`0` means everywhere; a multi-server override is one row per server), `guild_id` on settings (`0` means global), and source server, source channel, and a visibility field (default global) on memories. The bot has no plan to leave the personal server and the friend server. The hooks cost little and avoid a data migration if that changes. They also address a privacy question in a larger group: something said in one server could otherwise surface in another.

### 1.2 Command registry and permission keys

**Registry.** Each command is defined once with: name, description, parameters, permission key, default minimum level, help text with an example, category, an owner-only-fixed flag, and a protected flag (always allowed, see below). One wrapper around every command does the permission check, logs usage, and registers the command for `/help` and `/perm keys`. Conversational commands (3.17), buttons, and reactions that trigger the same action use the same key.

**Keys.** A key is a stable internal name for one permission, in the form `area.action` (lowercase, one key per subcommand). Override rows store the key, so renaming a command does not break stored overrides. Keys also cover features that are not commands, such as `chat.talk` (default level 0), which controls whether the bot replies to a person at all.

**Check order** when a person uses a command:
1. The owner always passes.
2. A protected key passes for everyone.
3. Blocks: a `chat` block refuses `chat.talk`; an `all` block refuses every key.
4. Overrides: exact key, then `area.*`, then `*`. The first row found decides, allow or deny. When a server-specific row and a global row match the same key, the server-specific row wins.
5. The person's level against the key's default level. No row at any earlier step means the level decides.

A refusal in conversation is silence. A refused slash command returns an ephemeral line from the fixed `denied` pool (3.9); the default text, used until that pool is built, is "You don't have permission for that." An override row holds user, key, allow or deny, and an optional server (9.3).

**Commands (owner-only):** `/perm level` (set a person's level, 2.3), `/perm grant` (set allow), `/perm deny` (set deny), `/perm reset` (remove the override, returning the key to the person's level), `/perm show` (the expanded result for every key and what decided it), `/perm history` (every change made to one person), `/perm keys` (lists every key with autocomplete), `/block` and `/unblock` (see Blocks below).

**Wildcards.** `feed.*` covers every key beginning with `feed.`, including keys added later. The latest command overwrites earlier settings for every key it covers: a wildcard grant, deny, or reset removes the rows beneath it (for `*`, the exact-key rows and the `area.*` rows), and the bot's reply lists what it cleared ("Cleared 1 exception: `feed.notify` deny"). For "everything except one", grant the wildcard first, then deny the single key. The rule is broad command first, exceptions after. Owner-only fixed keys and protected keys ignore wildcards, including `*`. Every change goes to the audit log.

**Initial keys (draft):**

| Key | Command | Default level |
|---|---|---|
| `feed.subscribe` | `/feed subscribe`, `/feed unsubscribe`, `/feed filter add\|remove\|show` | 1 |
| `feed.notify` | `/feed notify`, `/feed pingwords add\|remove\|show` | 0 |
| `feed.list` | `/feed list` | 0 |
| `feed.create` | creating a new source | 1 |
| `feed.remove` | removing a source | 3 |
| `cat.view` | `/cat` | 0 |
| `cat.add` | `/addcat` (a new cat name asks for confirmation), the "Add as cat photo" message menu, removing own photos | 1 |
| `cat.retag` | `/retag`, `/removecat`, removing others' photos | 2 |
| `cat.ingest` | folder ingest | 3 |
| `remind.create` | `/remind`, own reminders | 0 |
| `remind.list` | `/remind list` (own) | 0 |
| `remind.cancel` | `/remind cancel` (own) | 0 |
| `prefs.view` | `/timezone show` | 0 |
| `prefs.set` | `/timezone set`, `/birthday set` | 0 |
| `memory.view` | `/memories` (own) | protected |
| `memory.add` | `/remember` (about yourself or another person) | 1 |
| `memory.forget` | `/forget`, `/forget all` (own) | protected |
| `memory.manage` | view and delete other people's memories | 3 |
| `relationship.view` | `/relationship` | 0 |
| `mood.view` | `/mood` (the named mood) | 0 |
| `pet.use` | `/pet` | 0 |
| `nickname.set` | `/nickname add`, `remove`, `list` (own list) | 0 |
| `nickname.manage` | change other people's nicknames | 3 |
| `help.view` | `/help` | 0 |
| `chat.talk` | the bot replies to the person at all | 0 |

Owner-only and fixed (cannot be granted): `perm.*` (which includes `/block` and `/unblock`), `config.*`, `event.*`, `persona.*`, `mood.set`, `mood.raw`, `relationship.set`, `relationship.raw`, `sleep.*`, `presence.*`, `say.*` (the `/say` command), `state.reset` (the `/reset` command).

**Protected keys.** A key with the protected flag is allowed for everyone, and no override, block, or level removes it. The first protected keys are `memory.view` and `memory.forget`, so a person can always see and delete what the bot stored about them. The commands themselves restrict a person to their own memories; naming another person requires `memory.manage` (the owner always has it, Admin by default). `/perm deny` on a protected key is refused with an explanation, and wildcards skip protected keys. Replies from these commands are ephemeral (only the person sees them), so repeated use does not fill a channel. Any other key can be flagged protected later with one line. `help.view` is not protected.

**Blocks.** A block has a kind. `chat` stops the bot from conversing with the person and leaves commands working. `all` stops conversation and every command except protected keys. Blocks sit in their own table (9.4) and never change override rows, so `/unblock` restores the person exactly as they were.

### 1.3 Message pipeline

**Entry points.**
- Slash command: registry wrapper (permission check, log) to command function to result to flat reply.
- Message: intake to response plan to channel queue to worker to send.
- Reaction or button: small handlers (feed notify, confirm buttons).
- Timers (feeds, reminders, heartbeat, sleep, scheduled typo fixes and corrections): run as their own tasks and never pass through message intake, so a bot-sent message cannot trigger a reply.

**Intake** (rules only, no model calls):
1. Guards: ignore bot authors; only channels the bot listens in; drop messages from a person whose `chat.talk` is refused, whether by a block or by an override, with no writes (9.4).
2. Classify: addressing score, name mentions, conversational command patterns, intent patterns (greeting, thanks), topic-avoidance check, one-time event triggers.
3. Write mood and relationship events.
4. Batching: wait for more messages from the same person (3.2).
5. Gating: cooldowns, repeat ladder, sleep and idle state.
6. Output a response plan: ignore, reaction only, canned reply, model reply, or a command result to react to.

**Worker** (one per channel, so replies within a channel go in order; channels run in parallel):
1. Presence: status change and reading delay (3.11).
2. Render: a canned reply, or a `generate()` call running in parallel with the delay.
3. Post-check, split, typo and correction scheduling.
4. Send, then update state.

In Layer 2 the render step reads the canned dictionary only. Layer 3 changes that step to call `generate()` for plans that need a model. The other steps stay the same.

**Simultaneous messages.** Addressed messages in a channel are answered in arrival order through the channel queue. Generation for a later message can start while an earlier reply is sending, when another endpoint is free; sends stay in order. A later reply skips the idle-to-online delay and uses the short in-conversation pause. Queue limits (`queue.max_depth` and `queue.max_age_seconds`) are settings and apply to the live queue only; messages sent during sleep are read from channel history at wake (3.12). Batching groups one person's messages; the queue orders different people's.

**`generate()`.** The single place the bot asks a model for text. A request carries: purpose (reply, interjection, extraction, trait batch, command parse, self-correction), prompt parts, token cap, priority, response format (text or JSON), timeout, and a cancel handle. The result carries the text or JSON, the endpoint used, latency, and a fallback flag. Each endpoint runs one generation at a time. Interactive replies take priority over background batches, which prefer the server CPU. Cancelling closes the request to the endpoint (used for "never mind" and regeneration). A maximum total wait (`llm.max_wait_seconds`, 20 seconds to start) ends in a canned reply. Every call is logged (endpoint, purpose, latency, outcome); the log supports decisions such as whether to install the 1070 (4.4). Endpoint list and fallback chain: 4.5.

**Delivery after a crash.** Reminders, feed posts, and one-time events send first and record afterward, so a crash between the two can produce one duplicate message on restart. That is accepted, because a missed reminder or alert costs more than a repeat. Reminders that came due during downtime are delivered when the bot returns (the loop selects every pending reminder whose due time has passed), feed items are posted after the next poll, and calendar events use the catch-up window (3.15). One-time events delete leftover `pending` claims at startup (9.17), so an event can fire a second time in the same rare case.

**Rules for testability.**
- Decision functions are pure: addressing score, disposition, trigger matching, and gating take plain data in and return a decision. The Discord layer gathers inputs and carries out outputs, so replay scripts run without Discord.
- Command functions return a result object (success, data, a plain default message, and for a failure a kind: `denied`, `not_found`, `already_exists`, `missing_argument`, or `other`) and never final text. The command lane renders it flat: a success shows the plain message, and a failure of one of the four named kinds shows a fixed line from the matching pool in the data file (3.9), with the plain default message as the fallback. The social commands (3.9) are the exception for successes: their success line comes from a mood- and tier-keyed pool. The conversational lane passes the result to the personality renderer for an in-character reaction.
- All database access goes through one module; tests can use an in-memory database. Schema changes use numbered migration scripts from the start.

### 1.4 Reply style

- Default: a plain message.
- Use Discord's reply feature (quoting the message) when another person has posted in the channel since the message she is answering, when the bot is answering from the queue out of order, or when the reply targets an older message (for example a reaction to a typo comment). Elapsed time alone does not trigger it, so a slow model in a two-person back-and-forth still gets a plain message.
- The number of intervening messages that triggers a reply reference is a setting (default 1 message from someone else).
- Reply references do not ping the author (`mention_author` off).
- In split replies, only the first part carries the reply reference (3.11).

### 1.5 Reply visibility

A command that changes what the channel sees replies publicly: Discord shows "Sam used /command" above the reply, and the room can follow along. A command that changes a personal setting or shows personal data replies ephemerally.

| Reply | Commands |
| --- | --- |
| Public | `/cat`, `/addcat`, the "Add as cat photo" menu, `/removephoto`, `/retag`, `/removecat`, `/feed` (subscribe, unsubscribe, filter, list), `/remember`, `/nickname`, `/relationship`, `/pet`, `/mood` |
| Ephemeral | `/remind`, `/timezone`, `/birthday`, `/memories`, `/forget`, `/help`, `/feed notify`, `/feed pingwords`, and the owner commands (`/say` answers privately, then posts its message publicly) |

- Confirm prompts (a new cat, a new feed source, a photo removal) are ephemeral to the person who ran the command. Conversational confirmations are normal channel messages (3.17).
- A refusal is ephemeral. Other failures keep the visibility of the command that failed.
- The owner-only parts of a public command (`/mood set`, the raw mood numbers, `/relationship set`) reply ephemerally. `relationship.ephemeral` (default off) makes `/relationship` ephemeral for everyone.
- A public reply that names a change (a removal, a retag, a feed edit) says who made the change.

---

## 2. Layer 1: Feeds, Reminders, Permissions, Cat Photos

### 2.1 Feed framework

Pattern: poll each source on a schedule, store every item that appears, and post each stored item to every subscribed channel that has not received it yet.

**Structure** (five tables, designs in 9.12)
- `sources`: one row per thing watched (type, parameter such as an app ID, display name, optional type-specific `extra` JSON, enabled flag, poll health). Each source is polled once, however many channels use it. The poll interval per source type is a setting.
- `feed_items`: every item the poll found, one row each, with the title, link, summary, publish time, and an optional `event_at` (the start time of a maintenance window). A unique rule on source and item ID stops the same item from being stored twice. A daily task deletes items older than a retention period (a setting).
- `subscriptions`: one row per channel and source, with a `start_at` time, a `filters` field, and an enabled flag. A subscription receives only items first seen after its `start_at` (the moment it was created), so a new subscription posts no backlog. Filters are in 2.1 below.
- `feed_notify`: one row per user and subscription, with an on/off switch and an optional list of personal ping words. A post mentions only the users whose entry is on and whose ping words match (below), using `allowed_mentions` limited to exactly those user IDs. With nobody to ping the post is silent. Nobody is added automatically.
- `feed_posts`: one row per message the bot sent for an item (kind `item`, or `reminder` for the one-hour-before maintenance reminder). It prevents double posting and maps a message ID back to its subscription for the ping controls below.
- A subscription finds its items through the shared `source_id`: the poll stores items under a source, and every subscription to that source reads the same rows.
- When a poll finds several new items at once, the bot posts all of them.
- Pings skip a user with an `all` block. A user with a `chat` block is still pinged (1.2).
- The full table designs are in 9.12.

**Filters.** A subscription's `filters` field holds a keyword list (empty or missing means every item passes). English keywords match whole words and phrases; Japanese keywords match as text anywhere. Matching covers the title and summary. Anyone with `feed.subscribe` can edit the filters of a subscription. An edit applies to new items only: it resets the subscription's `start_at` (9.12). 

**Ping words.** The channel filter decides whether an item is posted at all, and everyone in the channel sees it. A person's ping words (`/feed pingwords`) decide only whether that person is pinged for items that were posted.
- Pings on with no words: pinged for every posted item. Pings on with words: pinged only when the item matches at least one word. Pings off: never pinged, and any stored words are kept.
- Ping words narrow within the channel filter. They cannot bring back an item the filter blocked.
- Matching uses the same function as the channel filter (English whole words and phrases, Japanese as text anywhere, title and summary). The one-hour maintenance reminder applies the same rule to the original item's text.
- Adding a ping word turns pings on. Removing the last word turns pings off for that subscription, and the reply says so. The only way to be pinged for every item is `/feed notify` with an empty word list.
- A cap on words per person per subscription is a setting (default 10).

**Commands**
- `/feed subscribe type target`: `type` is a dropdown (FFXIV, Steam, Anime news, Games) and `target` autocompletes from existing sources of that type. Parameter names appear as labeled slots in Discord and are not typed.
- A target not yet followed is validated with a test request, created as a new source, and subscribed after a confirm button ("Add Helldivers 2 news to this channel?"). A source that already exists is reused.
- `/feed unsubscribe` (autocompletes from the channel's subscriptions), `/feed list` (what the channel follows, with nothing about individual pings), `/feed notify` (a toggle; the reply states the new state).
- `/feed filter add`, `/feed filter remove`, `/feed filter show`: edit the keyword list of this channel's subscription to a source (autocomplete picks the subscription).
- `/feed pingwords add`, `/feed pingwords remove`, `/feed pingwords show`: edit your own ping words. Like `/feed filter`, the commands are scoped to the channel where they are run and take a required `feed` option that autocompletes from that channel's subscriptions. In a DM they return a plain message. `/feed notify feed:<subscription>` flips your on/off switch, and the reply states the new state. `/feed pingwords show` lists your entries for this channel as on or off with their words, and `/feed list` shows only what the channel follows. All use `feed.notify` (level 0).
- Friends can create sources (`feed.create`, level 1). Limits: validation before saving, a cap on sources created per person (a setting), and the owner can remove any source.
- Ping controls on each post: check and X reactions, or Notify and Stop buttons that reply privately to the person who clicked. They switch pings on and off (stored ping words are kept). Both get built on the test bot and compared. `/feed notify` stays as the typed version.
- Visibility: `subscribe`, `unsubscribe`, `filter`, and `list` reply publicly with a line naming what changed and who changed it ("Sam subscribed this channel to FFXIV Maintenance."). `notify` and `pingwords` reply privately, so nobody sees another person's ping settings. The confirm button for a new source is private (1.5).

**Source types**

| Source | Notes |
|---|---|
| FFXIV Lodestone | Each category (maintenance, notices, topics, updates, status) is its own source. No official API or RSS; community JSON services exist (lodestonenews, `lodestone-news` npm package) and can break. Plan: scrape the Lodestone news pages with httpx + BeautifulSoup; a failed fetch retries next cycle. Maintenance: post when announced, then a reminder one hour before start. Daily and weekly reset reminders and a chore list can be added later. Universalis API covers market board prices if wanted. |
| Steam news | Per game, by app ID. Add by store link (the game name comes from the link) or a bare ID with an optional name. `ISteamNews/GetNewsForApp/v2` needs no key and takes `appid`, `count`, `maxlength`, `enddate`, and `feeds`. One unofficial reference also lists a `tags` filter that Valve's page does not, so test before relying on it. Per-subscription filters: a keyword filter (patch, update, hotfix) and a choice between the game's own announcements and outside press articles (confirm the response fields with a real call). Name search comes later. Auto-following owned games needs a Steam API key and a public game-details profile (`GetOwnedGames`). |
| Anime news | News from RSS sources. Anime News Network says it offers RSS and Atom feeds for its article types; the feed URLs are unconfirmed. A subscription holds a keyword list typed as comma-separated text, mixing English and Japanese names (for example "toaru, a certain magical index, railgun"). English keywords match whole words and phrases; Japanese keywords match as text anywhere. Matching covers title and summary. Phrases avoid false matches on generic words. Optional announcement check: AniList media entries have a relations field, and a new sequel entry under a franchise could flag an announcement (verify with a test query). AniList has no news; it supplies titles and airing times. |
| Upcoming games | IGDB. Free for non-commercial use, 4 requests per second, needs a Twitch account with 2FA for a Client ID and Secret. Follow a specific title for release-date changes and release day, or filter by platform. |

Anime episode alerts are covered by Sonarr's Discord connection (Parking Lot).

### 2.2 Reminders

- `reminders` table (9.10): one row per reminder, with the due time, message, owner, delivery (DM or channel), status, and an optional repeat rule. `user_settings` (9.11) holds each person's timezone.
- One loop selects rows with status `pending` and a due time in the past.
- `/remind when message` for entry (`/remind 2h submit assignment`). Options choose DM or this channel, and a `repeat` dropdown of common patterns (daily, weekly, every 2 weeks, monthly, last day of the month, and similar). `/remind list` shows the person's pending reminders, plus reminders that could not be delivered in the last 7 days (marked "not delivered", with their text), as a private reply, and `/remind cancel` removes one.
- **Timezones.** Times are stored as UTC seconds. `/timezone set` stores a person's timezone (an IANA name such as `America/Los_Angeles`, with autocomplete) in `user_settings`, and `/timezone show` displays it. Clock times such as "tomorrow 9am" are read in the person's timezone, and a person without one set is asked to set it. Durations such as `2h` need no timezone. Confirmations use Discord timestamps (`<t:unix:F>`, `<t:unix:R>`), which each viewer sees in their own timezone.
- **Repeating reminders.** The rule is stored as RFC 5545 recurrence text (`FREQ=WEEKLY;BYDAY=TU`, `FREQ=MONTHLY;BYMONTHDAY=-1` for the last day of the month) and computed with python-dateutil in the reminder's timezone, so a 9 a.m. reminder stays at 9 a.m. across daylight saving changes. After a delivery, the same row moves to the next occurrence and stays `pending`. A setting (`reminders.min_repeat_minutes`) sets the shortest allowed repeat interval.
- Delivery is a DM by default. If the DM fails (the person turned off DMs from server members, or blocked the bot), the bot posts a short notice in the channel where the reminder was set, mentioning the person and saying a reminder could not be delivered ("I couldn't DM you a reminder. Check `/remind list`."). The notice never contains the reminder text, so a private reminder is not exposed. The status becomes `failed`, and the text stays readable in `/remind list`. If the notice cannot be posted either (a reminder set in a DM, or the bot cannot post there), the status is still `failed`. A repeating reminder whose DM fails posts the same notice, moves to its next due time, and stays `pending`. A reminder created with the `channel` delivery option posts its text in that channel by choice.
- A one-time reminder keeps its row after delivery (status `delivered`, with the delivery time and route). Cancelled and failed rows stay too.
- Blocks do not affect reminders already set: they continue to arrive by DM. A person with an `all` block cannot create new ones, because `remind.create` is refused.
- Reminders live in Discord only, with no tasks app sync.

### 2.3 Permission levels

Levels are the defaults. Per-person overrides by key sit on top of them (1.2). The level names (Everyone, Member, Trusted, Admin, and the owner above them) differ from the relationship tier names (3.4), because the two measure different things: permission is what a person may do, and the tier is how Maika feels about them.

| Level | Name | Examples |
|---|---|---|
| 0 | Everyone | `/cat`, own reminders, `/relationship`, `/nickname`, `/memories` (own), `/help` |
| 1 | Member | `/addcat`, `/feed subscribe`, creating feed sources, `/remember` |
| 2 | Trusted | `/retag`, remove others' photos |
| 3 | Admin | `feed.remove`, folder ingest, `memory.manage`, `nickname.manage` |

- The owner sits above the ladder, outside the levels. The owner ID comes from a config file, is never stored in `users`, and passes every check, including the fixed owner-only keys (`/perm`, `/config`, and the rest).
- Level 3 (Admin) is held by nobody at first. An Admin cannot reach the fixed owner-only keys, so cannot change permissions or settings.
- `users` table (9.2): holds levels 1 to 3 only. A missing row means level 0, and setting a person to 0 deletes the row. Levels follow a user across servers. The owner-only `/perm level` command sets them.
- The registry wrapper (1.2) checks every command, including conversational commands (3.17).
- Every level, override, block, and settings change is recorded in the `audit_log` table (9.5).

### 2.4 Cat photos (friend server)

Why not Google Photos: share links open a web page, not an image. The Library API only manages app-created photos since March 31, 2025 (`photoslibrary.readonly` removed).

Design: store the original image files on an array share, metadata in SQLite.

- Three tables (9.13): `cats` (name, who created it, when), `photos` (one row per image file), and `photo_cats` (links a photo to each cat in it). A photo can show more than one cat, so a photo can have several links. The SHA-256 hash of the file's bytes is unique and also names the stored file (`<hash>.<ext>`), so an exact duplicate is recognized. A resized or re-saved copy has a different hash and counts as a new photo (near-duplicate detection is on the Parking Lot).
- **Folder ingest (owner-only, `cat.ingest`):** photos go in one folder per cat (`cats/buttons/` and one per friend's cat). The command reads the folders, adds each photo under its folder's cat, and asks to create any cat that does not exist yet. A file that already exists (same hash) gets a link to the folder's cat instead of a second copy, so one photo placed in two cat folders becomes one photo with two cats. Sources: photos of the owner's cats downloaded from Google Photos, and albums friends send.
- `/addcat` with an attachment and a cat name (autocomplete from `cats`): the bot downloads the file, hashes it, stores the original, and inserts the rows. A name the bot does not know shows a confirm button ("No cat named Buttonz. Create it?"), which catches typos. Anyone with `cat.add` (level 1) can create cats this way. If the hash already exists, the bot adds a link to the named cat when the photo lacks one and otherwise replies that the photo is already stored. When a photo or a link is added, the reply posts the stored photo with a caption (photo ID and cat names, as for `/cat`) so the room sees what was added, with a short line from the social-command pool (3.9) above it; the "already stored" reply is text only, and a folder ingest posts nothing.
- **Add as cat photo (message menu, `cat.add`):** right-click a message with an image, choose Apps, then "Add as cat photo". This adds a photo people have already shared in chat. The bot answers privately (ephemeral) with a select menu of cats and a "New cat" button that asks for the name and then confirms. A message with several images shows a chooser first. After the add, the bot replies publicly to that message with a short caption (photo ID and cat names) and does not re-post the image, since it sits just above. The hash, duplicate, and `photo_cats` rules are the same as for `/addcat`. A select menu holds 25 options, so with more than 25 cats it lists the 25 most recently used, and `/addcat` reaches the rest. The conversational version ("Maika, add that as Buttons", 3.17) arrives at step 10.
- `/cat name:buttons` returns a random photo that has a link to that cat. `name` has autocomplete. Blank picks any cat. The caption shows the photo's ID and the names of all its cats. The reply is public.
- `/retag photo cat` adds a cat to a photo or removes one. A photo must keep at least one cat. The reply is public: the photo posted again with its updated caption and a line saying who changed it.
- **Removing a photo** (`/removephoto photo`): the person who uploaded it can remove it (`cat.add`), and removing someone else's photo needs `cat.retag`. The ID comes from the caption of a `/cat` or `/addcat` post. A confirm prompt comes first, private to the person removing, and it shows the photo. Removal deletes the `photos` row (its links go with it) and the file, and writes one `audit_log` entry (kind `photo`) with the actor, the uploader, the hash, and the cat names. There is no restore command, and the same file can be added again later. The result is a public line naming the photo ID, its cats, who removed it, and whose photo it was ("Sam removed photo #214 (Buttons), uploaded by Alex"). The line does not repeat the photo.
- `/removecat` deletes a cat that has no photos (`cat.retag`). The database refuses to delete a cat that still has links. The reply is public.
- Posting: send the original when it fits the upload limit; otherwise generate a resized copy in a cache folder, which can be deleted and regenerates on demand. Discord's free-user attachment limit was reported at 20 MB in August 2026; whether that applies to bots is unconfirmed, so test.
- No history import. There is no cat channel, and cat photos are mixed with other images in the chat channel.
- Possible extras: cat of the day, reaction-based `/topcats`, pet counters.
- Expected volume: hundreds of photos across 6 or 7 cats.
- Cat photo milestones (100th, 500th, 1,000th photo) are one-time events (3.15). The count is the number of rows in `photos`. A milestone fires once and its state is stored with the event, so removing a photo and adding another does not announce it again.

### 2.5 Manual memory commands (built at step 13)

Full memory design is in 3.7. These commands belong to Layer 2 and are built at step 13 with the `memories` table. Only the `memory.*` permission keys are registered at step 1, so overrides and `/perm keys` list them early. This section is the manual path:

- `/remember` and a message context-menu command ("Remember this"). Level 1 and up. `/remember` takes the text, an optional `about` person (default yourself), and an optional `tags` field. The person a memory is about can always see and delete it, whoever created it. The context menu saves without tags. The `/remember` reply is public and shows the memory text, so anyone can see what was saved, and the person it is about can `/forget` it right away.
- `/memories` lists your memories, paginated with Previous and Next buttons (3 to 10 per page, default 5), each with a Delete button. Pressing Delete opens a confirmation showing the memory text. Buttons stop working after a time-out (a setting), and running `/memories` again brings them back. Replies are ephemeral. `/memories` is expected to be one of the most used commands.
- `/forget <id>` removes one memory after a confirmation that shows its text (the confirmation is a trial: `memory.confirm_forget` turns it off). The ID is unique across all memories, so no user field is needed. A person can remove only memories about themselves. If the ID does not exist or belongs to someone else, the reply is the same generic message ("No memory with ID 14 found among yours"). Deletion is permanent in the live database (a hard delete); copies age out of backups (section 5).
- `/forget all` removes every memory about you, including pending and rejected ones from the review queue, after a confirm button. It leaves relationship numbers unchanged. It also deletes your nicknames (3.6), your traits, owner trait rows and signal counters (3.5), and your `recent_uses` rows (3.10). Your `timezone` and `birthday` in `user_settings` stay, because you set them for yourself and can change them with `/timezone` and `/birthday`.
- Managers (`memory.manage`, owner always, Admin by default): `/memories user:@x` lists someone's memories with Delete buttons, `/forget id:n` works on any ID, and `/forget all` takes an optional user. Replies are ephemeral.
- Every deletion writes an `audit_log` row (kind `memory`: who deleted, whose memory, the ID, type, and source). The memory text is not kept there. The row is not posted to a channel. A memory deleted by mistake can be restored from the daily backup (section 5).
- `/help` tells friends that the owner can view memories.
- `memory.view` and `memory.forget` are protected keys (1.2): no override or block removes them. Another person's memories need `memory.manage` (owner always, Admin by default).

### 2.6 Help command

Discord shows each slash command's description and each parameter's description in its picker, and those descriptions are the first layer of help. The prefix-command framework in discord.py includes a default `!help`; slash commands have no automatic help page, so `/help` is built.

- `/help` lists the commands the person can use. The list comes from the registry (1.2) and runs the same permission check as the commands, so individually granted keys appear and denied keys do not.
- **First screen:** a short intro, one line per category, and the category dropdown. It carries a one-line note that the owner can view memories. Opening a category shows its commands. Page buttons handle long categories.
- **Categories:** Cats, Reminders and time, Feeds, About me, Fun, Manage, and Owner. A person sees only the categories that contain a command they can use. Everyone sees the first five. Manage appears at level 2 or 3 (or with a granted key), and Owner only for the owner.
  - Cats: `/cat`, `/addcat`, the "Add as cat photo" message menu, `/removephoto`.
  - Reminders and time: `/remind`, `/timezone`, `/birthday`.
  - Feeds: `/feed` (list, notify, pingwords, subscribe, unsubscribe, filters, creating a source).
  - About me: `/memories`, `/forget`, `/remember`, `/nickname`, `/relationship`.
  - Fun: `/pet`, `/mood`.
  - Manage: `/retag`, `/removecat`, removing others' photos, folder ingest, removing a feed source, `/memories user:@x`, `/forget` on any ID, changing others' nicknames.
  - Owner (fixed, cannot be granted): `/perm`, `/block`, `/unblock`, `/config`, `/event`, `/persona`, `/mood set`, `/relationship set`, `/sleep`, `/wake`, `/presence`, `/say`, `/reset`.
- **Entries:** one entry per top-level command with its subcommands listed underneath, about 30 entries in all. For each: what it does, an example, and the permission it needs. The "About me" category carries the full memory-privacy note next to `/memories` and `/forget`, including that deleted memories can remain in backups for up to 30 days (offsite copies up to 4 weeks).
- `/help command:<name>` jumps to one command, with autocomplete.
- Replies are ephemeral, so `/help` never fills a channel.
- Help is generated from the registry, so it stays accurate when commands change. Conversational versions of commands (3.17) are not listed.
- Intro and category wording are plain for now. Whether they use Maika's voice (and whether mood changes the intro) is decided when `/help` is built.

---

## 3. Layer 2: Behavior Systems

### 3.1 Two lanes

- **Command lane:** slash commands only. Permission check, then execute and respond immediately with a plain response. Mood, affinity, delays, typos, splits, corrections, cooldowns on replies, and repeat counters do not apply. A bot in its worst mood still answers `/addcat` instantly for a user at the lowest tier. Failures and refusals use fixed lines from a data file (3.9). Those lines never depend on mood, tier, or time, so the guarantee holds: the same instant behavior in every mood, with only the wording in her voice. The social commands (`/pet`, `/mood`, `/nickname`, `/relationship`, `/remember`, and the success reply of `/addcat`) are the exception for successes: their reply comes from a mood- and tier-keyed pool (3.9), still instant, never silenced by cooldowns or sleep, and without typing delay, typos, or splits.
- **Conversational lane:** everything else, including conversational commands (natural-language versions of slash commands, 3.17). All behavior systems below apply here.
- Conversational commands are recognized by the conversational lane's intake and call the same underlying function and permission check as the slash command. The state change is identical. The reply goes through the personality pipeline (mood, relationship, delays, nickname), so the bot can react to the action in character. Example: a new nickname from a close friend gets a warm reaction, and one from someone the bot is cool toward gets a cooler one. The slash-command version of the same action returns a flat confirmation. Permission changes are slash-only (3.17).
- Reaction text never changes the outcome. Code performs the action and checks the permission first; the personality only decides how the bot talks about it.
- Message handler guard: `if message.author.bot: return`. This covers the bot's own messages and every other bot.
- Scheduled actions (typo fixes, self-corrections, split parts) are issued by code and never pass through the handler, so no reply can trigger another reply.

**Direct messages.**
- Conversation in DMs is off at launch (`dm.chat_enabled`, default off). The setting exists because friends talking to her in DMs would compete for the same local model, and because DM chat needs privacy rules (below) that are not worth building until it is wanted.
- A person who DMs her anyway gets one short line saying she only chats in servers, shaped by the relationship (a pool of canned lines by tier, no model call), at most once per person per day.
- Slash commands work in DMs, except commands tied to a server or channel (feed subscriptions, folder ingest), which return a short plain message. Permission levels are global. Owner commands work in a DM as a private console.
- She never starts a DM apart from reminders (2.2).
- If `dm.chat_enabled` is turned on later: memories created from a DM get `visibility = 'dm'` (9.16) and are never mentioned in a server channel, the person can still see and delete them, and `#decisions` and `#bot-log` record only metadata for a DM (who, when, what action), never message text.

### 3.2 Addressing detection

Decides whether a message in a channel is addressed to the bot.

**Listen channels.** The bot reads messages and converses only in the channels on a per-server list, set with `/config`. A channel that is not on the list gets no conversation from her, including no reply to an @mention or to a reply to her message. A server with no saved list and no `.env` value gets no conversation at all, and neither does a server whose saved list is empty: an empty list the owner saved is a decision to stop listening, while a missing list falls back to the `.env` value. The test bot starts with one test channel (the `.env` value). Slash commands are not affected by this list; Discord's Integrations settings restrict them by channel (section 5). A dedicated chat channel (`addressing.chat_channels`, empty by default) is a listen channel where every message counts as addressed. A channel in that list needs no entry in the listen list.

**Strong signals (always reply):**
- `@mention`, or a Discord reply to one of the bot's messages, in a listen channel.
- A dedicated chat channel, where every message is addressed to the bot.

**Channel-level sessions.** One session per channel, holding start time, time of last bot reply, set of participants (everyone the bot has answered), and keywords from the last exchange. A second person can follow up on an answer the bot gave someone else.

**Adjacency.** A message is likely a follow-up when the bot's reply is the last message in the channel or one or two messages back. Messages from other humans in between lower the likelihood. A gap lowers it.

**Scoring signals** (start with strong signals plus adjacency; add scoring after reviewing the decision log):
- Raise: Discord reply to the bot; within about 20 to 30 seconds of the bot's reply (decaying); author is a session participant; question mark, second-person word, or short reaction ("really?", "why?"); keyword overlap with the bot's last reply.
- Lower: Discord reply to a human; `@mention` of another user; other humans' messages in between; long gap; no keyword overlap and no question.

**Time limits:** session expires about 90 seconds after the bot's last message. The person she just answered stays a follow-up candidate longer (180 seconds to start), and while that person's typing indicator is active during the window the window extends, so a slow typist in a busy channel is not missed. Hard cap of about 5 follow-ups, from anyone in the session, without an explicit mention or reply. An explicit address restarts the session.

**Name list.** The spellings that count as her name are an owner-editable setting (`/config`), starting with "Maika" (case-insensitive) and the Japanese spelling. `#decisions` logs messages that score as addressed through an unlisted spelling, so new spellings can be added after watching how friends refer to her.

**Name-based heuristics** (message contains the bot's name): name at the start or end with punctuation, second-person words nearby, and greeting patterns ("hey Maika") indicate address. Name mid-sentence with third-person phrasing indicates discussion. Specific one-time event triggers (3.15) bypass this rule.

**Name mention with no strong signal (interjection candidate).** A message that contains the bot's name but has no @mention, no reply to the bot, and is not in a dedicated channel counts as a candidate for jumping into the conversation. Steps:
1. Require at least 3 earlier messages of channel context.
2. Gating checks run first and cost nothing: cooldown for unprompted replies in that channel, a probability scaled by mood (sleepy or grumpy lower), the topics-to-avoid check (3.16), and the sleep schedule (3.12).
3. If gating passes, send the name mention plus recent context to the model (Layer 3) with an instruction to write a short interjection or return `NONE`. `NONE` or an empty result means silence.
4. In Layer 2 (no model), this path does nothing, or at most adds an emoji reaction.
The interjection counts toward the per-channel cooldown, so a name mentioned repeatedly does not produce repeated interjections.

**Batching.** People often send one question as several short messages. After an addressed message arrives, the bot waits while that person's typing indicator is active (Discord's typing event), plus a short grace period after it stops (setting, with a maximum total wait), and treats the messages as one batch. A person who is not typing triggers no wait. Batching groups one person's messages; the channel queue (1.3) orders different people's.

**Idle state and interjections.** An interjection candidate passes through the same status rules as any reply (3.11): while the bot is idle the chance is lower, an interjection returns it to online, and during sleep there are none.

**Uncertain band.** Messages scoring between the low and high cutoffs get no reply and no reaction. A missed follow-up costs less than interrupting a conversation, and friends can reply directly to the bot's message or mention her to bring it back in. `#decisions` shows how often this mattered. A reaction can be added later with one setting.

**Alternatives:** a thread per conversation (removes ambiguity; changes the feel), or an LLM classifier on the uncertain band only (Layer 3, only if rules prove insufficient).

**Practical notes:** requires the Message Content intent. Per-channel cooldown and per-user rate limit. Log each decision (message, score, action) to `#decisions` during testing.

### 3.3 Mood

- **State.** Two values (energy and happiness), each a decimal from -5 to +5, stored in `bot_state` (9.8) together with a baseline for each and the time of the last update.
- **Named mood**, derived from the two values and checked in this order: energy ≤ -3 is **sleepy**; happiness ≤ -2 is **grumpy**; happiness ≥ +2 and energy ≥ +2 is **excited**; happiness ≥ +2 is **cheerful**; anything else is **neutral**. The thresholds are settings. Five names to start. A **relaxed** mood (positive happiness, low energy) and an **annoyed** mood (mild unhappiness) can be added later as a new rule and a new set of canned lines with no table change. Intensity (mild or strong flavor, from the distance to the baseline) is another way to add variety without multiplying line sets. Both are on the Parking Lot.
- **Baseline and drift.** The baseline is the resting point the mood returns to. Events move the current value away from it, and time moves it back: `baseline + (stored − baseline) × 0.5^(hours ÷ half_life)`, computed when the state is read (half-life 3 hours to start). The baseline changes at each **reroll**, which runs at wake time and again every N hours while the bot is awake (N is a setting, 6 to start). There is no reroll during sleep. Each reroll draws a new baseline for each axis from a weighted list. Happiness: -2 at 5%, -1 at 10%, 0 at 20%, +1 at 30%, +2 at 25%, +3 at 10%. The energy weights are decided at build time. A reroll writes a `mood_events` row with the old and new baselines in `note`.
- **Energy** changes from chat activity (+0.2 per reply the bot sends, capped at +1.0 per hour), a pre-sleep wind-down (the energy baseline drops by up to 2 over the last two hours before the sleep window, reading the sleep schedule in 3.12), and early wake. Drift shape and these sources are revisited at implementation.
- **Events.** Rules (keyword and pattern matching) write events. Starting values, all settings:

| Reason | Trigger | Happiness | Energy | Affinity (raw) | Scaled by relationship |
|---|---|---|---|---|---|
| `rude_message` | message classified rough (3.4) | -1.0 | 0 | -1.0 | yes |
| `spam` | many messages to the bot in a short burst | -0.7 | 0 | -0.3 | yes |
| `bad_bot` | "bad bot" | -1.0 | 0 | -0.5 | yes |
| `good_bot` | "good bot" | +1.0 | 0 | +0.15 | no |
| `thanks` | thanks | +0.5 | 0 | +0.10 | no |
| `pet` | the `/pet` command (any channel, `pet.use`) | +0.7 | +0.2 | +0.10 | no |
| `kind_message` | praise, a good morning or goodnight greeting, an apology, or asking how she is (3.4) | 0 | 0 | +0.05 | no |
| `chat_neutral` | an addressed neutral message that she answers (3.4) | 0 | 0 | +0.05 | no |
| `chat_activity` | each reply the bot sends | 0 | +0.2 | 0 | no |
| `early_wake` | woken inside the sleep window (3.12) | -0.5 | -1.5 | 0 | no |
| `long_outage` | startup after downtime (heartbeat comparison) | -0.5 per hour down, capped at -3 | 0 | 0 | no |
| `reroll` | wake time or the reroll interval | baseline change, logged in `note` | | | no |
| `owner_set` | `/mood set` | sets exact values | | | no |

Affinity gains stop at the daily limits and the neutral cap in 3.4.

- **Relationship scaling.** Negative events are multiplied by a factor from the person's tier (for example owner 0, closest tier 0.25, friend 0.5, acquaintance 0.75, stranger and `disliked` 1.0). Tier names and factors are decided with the relationship tiers (3.4, step 7). Positive events are not scaled.
- **Griefing protection.** Each person and reason has a cooldown (about a minute to two minutes, a setting), kept in the `cooldowns` table with key `mood_event:<reason>`. An event during the cooldown changes nothing. The check-and-start statement in 9.7 claims the cooldown atomically.
- **Catch-up messages.** Messages answered in the wake-time catch-up (3.12) cause no mood or affinity events.
- **Sleep and mood.** After waking, the first 5 replies to people carry the sleepy flavor (the count is a setting). The state lives in `bot_state` as a counter plus an expiry time (60 minutes after waking, a setting), so a quiet morning does not keep the bot sleepy. While the counter is above zero the named mood is sleepy regardless of the two values.
- **"Why are you grumpy"** queries `mood_events` for the largest drop in the last 6 hours (ties go to the most recent) and answers with the reason, the person's name, and a message link. Code supplies those; the phrasing comes from a mood template (9.14).
- **Commands.** `/mood` shows the current named mood to anyone (`mood.view`, level 0). The raw numbers are owner-only (`mood.raw`). `/mood set` is owner-only (`mood.set`). `/pet` works in any channel (`pet.use`, level 0), with a per-person cooldown. `/pet` and `/mood` replies are public and use the social-command pools (3.9). The cooldown limits the mood and affinity effect, and the reply still posts.
- **Startup.** The heartbeat row is updated each minute (stored in `bot_state`, 9.8). On startup the bot compares it with the current time to compute downtime and writes a `long_outage` event.
- **Retention.** A daily task deletes `mood_events` rows older than 30 days (a setting). Lasting moments are saved as memories (3.7).
- **Settings.** All mood settings are listed in 10.3.
- Mood sets tone and delivery. Relationship (3.4) sets warmth and content.

### 3.4 Relationship

Per-user relationship with the bot.

**`relationships` table fields** (9.15):
- `affinity`: a number from -5 to +10 (placeholders). How Maika feels about the person, based on how they treat her. It rises through the gains in "Gaining affinity" and falls through negative events; both are rows in the event log (`d_affinity`, 9.14).
- `baseline`: resting affinity that recovery returns to. It rises automatically to the affinity minimum of the highest tier the person has reached (see Recovery), and the owner can set it by hand.
- `familiarity`: a decimal from 0 to a ceiling (100). Counts visits and fades after a long absence (below).
- `peak_familiarity`: the highest familiarity the person reached. It sets the decay floor.
- `last_negative_at`: timestamp for the recovery cooldown.
- `last_counted_at`: time of the last visit that counted toward familiarity.
- `updated_at`: time the stored values were last written.
- There is no `notes` field. Remembered moments are memories (3.7).

**Ceiling and floor.** Affinity runs from -5 to +10 (placeholders), familiarity from 0 to 100. Values live in settings.

**Teasing depends on affinity.** Classify incoming messages into kind, neutral, and rough in code. The relationship decides the cost of a rough event:
- Close friend: little or no penalty; the exchange can count as banter that adds familiarity.
- Acquaintance: mild penalty.
- Low affinity or stranger: full penalty.
- Overdone: more than a set number of rough events by one person in a window (for example more than 3 in 10 minutes) removes the discount. The count comes from the `mood_events` rows for that person (9.14), which are written even when the applied effect is zero.
- Convention for friends who tease and for repeat greetings (3.10): no affinity penalty for the first few repeats; a small capped mood nudge is fine.

**Mood and affinity calculated together.** One candidate formula: `disposition = affinity + mood_value × mood_weight(affinity)`, where `mood_weight` falls as affinity rises and rises again for strongly negative affinity.
- Bad mood with low affinity: very dismissive (additive).
- Bad mood with high affinity: the mood is barely noticed.
- Disposition picks the response bucket. Mood supplies flavor (groggy, grumpy) at an intensity scaled the same way.
- To be examined in depth when built, and tested thoroughly. A test script replays scripted event sequences against the pure function and prints the numbers: friend teases twice, friend teases ten times, stranger teases once.

**Gaining affinity.** Affinity rises through messages and events only. Visits raise familiarity and nothing else.
- **Neutral exchange** (`chat_neutral`): an addressed message from the person that she answers, classified neutral. It gives +0.05, only while affinity is at or above 0 and below the neutral cap (1.5, a little above the acquaintance minimum), so ordinary talk alone ends at acquaintance. A message she stays silent on (the repeat ladder), an ignored message, and a command do not count.
- **Positive events** (placeholders; each keeps its mood effect from 3.3): `kind_message` +0.05, `thanks` +0.10, `pet` +0.10, `good_bot` +0.15. They apply at any affinity and carry a person into the friend and close friend tiers.
- **Kind messages** are matched by code rules and aimed at her by name or mention: praise ("you're the best"), a good morning or goodnight greeting, an apology after a rough message, and asking how she is. Each message maps to one reason, so it never earns two gains. Layer 2 matches plain forms and accepts some sarcasm errors, because the gains are small and a miss costs little. Layer 3 can judge context later.
- **Daily limits**, per person over a rolling 24 hours and read from `mood_events` (9.14): neutral gain at most 0.25 (about 5 exchanges), and all positive gain together at most 0.5. A gain that would pass a limit is cut to what remains.
- **Placeholder pace** (regular chatting, 5 or more exchanges a day, 1 or 2 visits a day): acquaintance after about 4 days. With some thanks and praise, friend after 9 to 25 days and close friend after about 3 to 7 weeks. A person who only chats stays an acquaintance, and talking less takes longer.

**Recovery.**
- Below baseline: affinity drifts back up to baseline. Computed on read: `affinity = min(baseline, stored + rate × hours_elapsed)`. A moment of rudeness recovers in about two to three days (tune later).
- Cooldown: recovery starts an hour or more after the last negative event (tune later).
- At or above baseline: no decay.
- Ceiling prevents scores from ratcheting up forever.
- **Baseline ratchet.** When a write leaves the person in a tier above stranger, `baseline` is raised to that tier's affinity minimum (acquaintance 1, friend 3, close friend 6) if it is lower. It never rises above that minimum and never falls on its own. A rude message can push affinity below the tier minimum, so the tier shows lower for a while, and recovery brings it back to the minimum over the usual two to three days. A hand-set baseline above the minimum stays.

**Familiarity.** How well Maika knows a person, counted in visits.
- A **visit** counts when an addressed interaction happens at least 6 hours (setting) after the last counted one, and adds 1 (ceiling 100). Forty messages in one evening count as one visit.
- **Decay.** Nothing changes for 30 days after the last counted visit (grace period). After that, every 60 days halves the gap between the current value and the floor. The floor is the larger of 25% of `peak_familiarity` and the familiarity minimum of the tier one below the tier the peak reached (0 below acquaintance). Absence alone therefore lowers a person by at most one tier: a close friend settles at friend, a friend at acquaintance, and an acquaintance can fall to stranger. The grace period, the half-life, the percentage, and the tier minimums are settings.
- Decay is computed when the value is read. The database keeps the stored number and its timestamp until the person returns, then the bot applies the decay, adds 1, and updates the peak. Example with a peak of 40: 40 for the first 30 days, 31.2 at 60 days, 25.0 at 90 days, 17.5 at 150 days, 10.6 at one year, 10.0 at the floor.
- Affinity events and visits both write to `relationships` (9.15). Blocked people get no writes.

**Tiers.**
- A person's tier is computed from affinity and familiarity together: the highest tier whose affinity minimum and familiarity minimum are both met. `disliked` depends on affinity alone (at or below -3). Otherwise the person is a stranger. Placeholder minimums (affinity, familiarity): acquaintance (1, 3), friend (3, 10), close friend (6, 30). Tier names, descriptions, and minimums are decided at step 7 and live in settings. Display names: stranger, acquaintance, friend, close friend, disliked, and the owner's chosen name (all settings). A short fixed description per tier, written in her voice, is shown by `/relationship` (3.4 Commands). A tier can fall through fading familiarity or through negative affinity.
- Lowest tier is named `disliked`, defined by behavior: short replies, no teasing, no jokes, no mood flavor, no callbacks, nickname replaced by the server display name. Reply pool is a short list of flat lines. **Open item, revisit:** the earlier rule was no aggressive lines and nothing like "don't talk to me." The owner prefers that she be blunt with someone in this tier, including saying she doesn't like them. The earlier rule stays as the default until the question is revisited. The tier's display name is **Disliked**, and its description says she doesn't like them.
- Negative tiers stay minimal: the floor, one `disliked` tier, the recovery rule. The group is small and negative interactions are unlikely.
- Tiers take weeks to climb (the pace is under Gaining affinity), so regulars settle into the friend and close friend tiers over time. Distinct treatment between friends comes from traits (3.5), memories (3.7), familiarity, and nickname (3.6).

**Owner tier.**
- Internal key `owner`, keyed to the owner's user ID in config. Uses its own reply pool and skips the disposition math.
- Display name defaults to `owner` and is a setting changeable through `/config`. Alternative names (Creator, Maker, Root, Sudo, Sensei, Kami, a bot-lore name tied to the 6-year gap, or several picked at random) can be set later.
- Unique interactions: private jokes, own greeting, access to behind-the-scenes commands such as asking why the bot responded a certain way.

**Commands.**
- `/relationship` shows your own tier and description. `/relationship user:@someone` shows another person's. Anyone can use both.
- Raw numbers are owner-only (`relationship.raw`).
- **Display:** the tier's display name and a fixed description, in a plain layout with no progress hints. Descriptions are written in her voice, once per tier, and do not change with mood. A person in the lowest tier sees its own name (Disliked) and a description that says she doesn't like them. Placeholder descriptions are rewritten in the owner's words at build.
- Replies are public, so everyone in the channel sees them. A setting (`relationship.ephemeral`, default off) switches them to ephemeral. Anyone can look up anyone.
- Owner-only `/relationship set` adjusts baselines, affinity, and trait tags by hand. Each change is written to `audit_log` as kind `relationship`.

### 3.5 Traits

Observed habits per person, separate from affinity.

- Two tables (9.20). `user_traits` holds one row per person, trait, and source (`keyword`, `model`, or `owner`) with a confidence from 0 to 1. `trait_signals` holds running counters per person (messages, laughs, teasing, and similar), each decayed with a 14-day half-life.
- Traits come from a fixed list in a data file. Model output is filtered to that list, and anything else is discarded. Traits describe communication style and interests only.
- Starting list: joker, effusive, terse, chatty, curious, sarcastic, supportive, teaser, formal, enthusiastic, quiet, meme-poster, cat-photo-sharer, gamer, night-owl. Add more from observation. A person can hold several.
- Layer 2 detection: rolling keyword and counter signals on every message (laughter and emoji frequency, praise and thanks, teasing the bot, questions versus statements, message length). No model calls.
- Layer 3 detection: once a day or after a long session, send a capped sample of each active person's recent messages (for example the last 50, allowed channels only) to the model with surrounding context, as an overnight low-priority batch. The model returns tags with confidence. Sarcasm is judged in context. Compare model tags with keyword tags and a small hand-labeled test set before trusting them. The sample is fetched when the batch runs and discarded afterward; no message text is stored.
- Owner overrides always win: an owner row of 1.0 forces a trait on and 0.0 forces it off. The effective confidence is the owner row if one exists, otherwise the model row, otherwise the keyword row, and only traits above a minimum confidence count. Formulas and minimums are set at build.
- Traits are visible and deletable by the person: `/memories` has a second view, "what I've noticed", listing their traits (names only) with Delete buttons. `/forget all` clears their traits, owner rows, and counters.
- Prompt and canned-reply use: inject the top two or three traits by confidence. Combinations can get their own lines later.
- Deviation behavior: a joker who writes something serious, or an effusive friend with no praise for several days, can trigger a noticing line ("no joke this time?"). The deviation lines are on the Parking Lot.

### 3.6 Nicknames

- Up to 3 nicknames per person (a setting), in the `nicknames` table (9.18). `/nickname add`, `/nickname remove`, and `/nickname list` work on your own list (`nickname.set`, level 0). The bot also learns from "call me X" and "you can call me X" and the three-zone rule decides whether a confirmation button appears (3.17). A full list asks the person to remove one first. Replies are public and use the social-command pools (3.9).
- The person, the owner, and Admins (`nickname.manage`, level 3) can change a list. Friends cannot set each other's. Changes to someone else's list are written to `audit_log` (kind `nickname`).
- Used in some replies only, a random nickname from the list. Frequency scales with affinity. The `disliked` tier and people without a nickname get their display name in that server.
- Validation: 1 to 40 characters and at most 5 words (both settings); no line breaks, links, `@`, `#`, backticks, or other Discord markdown characters; a blocklist in a text file. Replies use `allowed_mentions` set to none by default (4.1), so a nickname cannot ping.
- `/forget all` deletes the person's nicknames.
- Cooldown: a minimum gap between replies that use the nickname (3.10).

### 3.7 Memories

**`memories` table** (9.16): the text and tags, who it is about, who created it, type (`fact`, `preference`, `event`, `running_joke`), importance 1 to 5, source (`manual`, `auto`, `observed`), status (`active`, `pending`, `rejected`, for the review queue), source message link, source server and channel, visibility (default global), created date, last-used date, optional expiry, pinned flag.

**Creation paths:**
1. **Manual (Layer 2, step 13):** `/remember` (level 1; about yourself or another person, with optional tags), "Remember this" context menu, conversational "remember that X said Y" (3.17).
2. **Automatic extraction (Layer 3):** after a conversation session ends, send the exchange to the model asking for durable facts, preferences, events, and running jokes, returned as JSON (constrained output through Ollama). Runs as a low-priority batch, never per message.
3. **Observed:** typo callbacks, mood events, and one-time events write memories.

**Controls on automatic extraction:**
- Minimum importance threshold and a daily cap per person.
- Duplicate check against existing memories (keyword similarity) before saving.
- Review queue: new automatic memories post to `#memory-review` with approve and reject reactions; only approved ones become active. Once quality looks reliable, low-importance ones can save automatically.
- Sensitive topics (health, family problems, anything a friend would not want a bot to remember) are excluded by the extraction prompt and a keyword filter on the output.
- `/memories` shows what the bot remembers about you. `/forget` removes entries permanently. Friends see and delete every memory about them, whoever created it, and nothing can remove that access (protected keys, 1.2). Commands and manager access are in 2.5. A deletion writes an `audit_log` row without the memory text.

**Retrieval for a reply:** rank candidate memories for the speaker by relevance to the current message, then importance, then recency. Inject the top 3 to 5. Each retrieval refreshes the last-used date. Retrieval and unprompted callbacks skip memories with `visibility = 'dm'` outside a DM (9.16).

**Search method.** SQLite FTS5 (keyword search ranked by BM25) over memory text plus tags. FTS5 matches words, so "kitten" does not find a memory that says "cat"; tags and a testing pass cover the common gaps. The default tokenizer splits on spaces and does not fit Japanese. The trigram tokenizer (substring matching) was tested: it finds words of 3 or more characters, including Japanese, and misses 2-character words, which a `LIKE` scan covers (9.16). A synonym list edited by the owner expands search words. Embedding search is added only if testing shows misses. Embeddings go in a separate table keyed by memory ID and model name, so adding them changes nothing in the `memories` table and switching models means re-embedding without touching memories. Costs of embeddings: an extra model in the path, weak vectors for short casual text, relevance without suitability (a topic match that does not fit the moment), poor handling of names and Japanese terms, and scores that are harder to debug than keyword matches.

**Expiry and repetition control (all tune later):**
- Expiry by type: running jokes and events short (weeks to a couple of months), facts and preferences longer, pinned never. Expiry should be long enough that the bot remembers things most people have forgotten, but short enough that a joke from 8 months ago does not shape current replies.
- Freshness weighting: retrieval score drops with age.
- Callback cooldown: after the bot uses a memory unprompted, it cannot raise it unprompted again for a set number of days. When the person mentions the memory or asks about it directly, the cooldown does not apply, and that use restarts the cooldown.

### 3.8 Persona notes channel

A private channel on the personal server for writing to the bot's personality from a phone.

- Only the owner can post. Enforce by author ID in code as well as channel permissions.
- Each message becomes a row in `persona_notes` (9.19). Format: a leading `fact:` or `leaning:` sets the kind (no prefix means leaning); `topic = value, because ...` sets the topic and the short answer for a fact; `#words` become tags. Example: `fact: favorite animal = duck, because they look unbothered by everything #animals #duck`. The format is revisited at build.
- `on_message_edit` updates the row and `on_message_delete` deletes it. The channel is the editable source of truth.
- Note types: **facts** (settled answers, for example `fact: favorite animal = duck, because they look unbothered by everything`) and **leanings** (softer guidance the bot can vary, for example "wary of dogs, thinks they are too eager").
- Retrieval: SQLite FTS5 keyword search with `#tags` controlled by the owner (`#animals #duck #waterfowl`). Embedding search through a small Ollama embedding model is optional, later.
- Notes override lazy-invented opinions (4.2). A note on a topic where the bot already invented an answer marks the invented row superseded. Only one note per topic is active at a time: a newer note supersedes the older one, and deleting a channel message deletes its row and, when that note was the active one, reactivates the note it had replaced. The bot can acknowledge the change in character.
- `/persona list`, `/persona edit`, and `/persona reroll` (owner-only, `persona.*`). A reroll supersedes an invented row so she invents a new answer.
- In Layer 2, key-value facts (`favorite:animal = duck`) answer direct questions through patterns and mood-flavored canned lines. The same notes feed the LLM prompt in Layer 3.
- Other uses: backstory (including the 6-year gap), speech habits, opinions about friends, tuning notes for mood and typo behavior.

### 3.9 Canned responses

- Dictionary keyed by intent, mood, and relationship tier: `responses["greeting"]["grumpy"]["friend"] = [...]`. Variants keyed by trait where useful.
- Intent matching by simple patterns (greeting, thanks, good bot, bad bot).
- Mood-flavored confirmations in the conversational lane, plus mood- and tier-keyed success lines for the social commands in the command lane (below).
- Feed alerts can carry mood (a sleepy maintenance notice). Alert content itself stays accurate.
- **Command-failure pools (command lane).** Fixed pools for four failure kinds: `denied`, `not_found`, `already_exists`, and `missing_argument` (the generic line). A pool is keyed by the failure kind only, with no mood or tier, so the command lane stays instant and identical in every mood (3.1). Each pool holds several lines, and the no-repeat rule (3.10) picks one. Lines can use slots such as `{name}` and `{target}` and are written in the owner's words, starting from the old bot's ("You can't tell me what to do.", "I couldn't find a file named **X**.", "Please learn how to use that command before trying again."). A refusal stays ephemeral, and other failures keep the visibility the command already has. Other failures, and the successes of commands that are not social commands, keep the plain default message; pools for them can be added later in the same data file with no code change. Until step 8 builds the pools, every failure and every success uses the plain message.
- **Social-command pools (command lane).** `/pet`, `/mood`, `/nickname`, `/relationship`, `/remember`, and the success reply of `/addcat` are the commands where she answers in company, so their success lines come from pools keyed by command, mood, and relationship tier (`responses["cmd_pet"]["grumpy"]["friend"]`). A lookup falls back from mood-and-tier, to mood only, to a generic line for the command, so a first set can be written at mood level and tier lines added where they matter (`/pet`, `/relationship`). No model call is involved. The reply stays in the command lane: instant, never suppressed by cooldowns or sleep, with no delay, typos, or splits. A cooldown on `/pet` limits its mood and affinity effect, and the reply still posts. The facts in a reply (the mood name, the memory text, the nickname list, the cat names and photo) come from the command's result and always appear unchanged; the pool supplies the line around them. `/relationship` keeps its fixed tier description (3.4) and adds a mood-keyed lead-in line. The no-repeat rule (3.10) applies per person. A first set is about four lines for each of the five moods per command (roughly 120 lines in all), written by the owner or drafted for review.

### 3.10 Cooldowns and repetition

**Cooldown table.** `cooldowns`: scope (user, channel, server, or global), key, expiry (9.7). A behavior is on cooldown when a row exists and its expiry is empty or in the future. One statement checks and starts a cooldown together, so two simultaneous triggers cannot both pass. Durations live in settings. Every cooldown in this table is time-based.

Uses:
- Canned lines: a line cannot repeat for a user until several other lines in its pool have been used (counts uses, so it is not a time cooldown). Lines carry stable IDs in the response file, and the `recent_uses` table (9.21) holds the recent picks per user and pool.
- Memory callbacks (per-memory).
- Nickname use.
- Typos (`typo.cooldown_minutes`). Teasing, jokes, and mood phrases get their own cooldown keys and settings when those behaviors are built.
- Split replies and self-corrections (per-channel).

**Layer 3 repetition control:** include the last three or four bot replies to that person in the prompt with an instruction not to repeat their phrasing. The post-check compares a new reply with recent ones (word overlap) and regenerates if too similar.

**Repeat counter.** Normalize each addressed message (lowercase, strip punctuation and emoji). Map to an intent when a pattern matches (hello, hi, hey all become `greeting`); otherwise use normalized text with a word-overlap check. Counter per user, per channel, per intent, with a sliding window (about 10 minutes since the last message of that intent). A quiet period resets it.
The repeat counter lives in memory only and resets when the bot restarts.

Escalation ladder:

| Repeat | Behavior |
|---|---|
| 1 to 3 | Normal reply, varied by cooldowns |
| 4 | Meta line naming the repetition ("that's four hellos") |
| 5 | Reaction emoji only |
| 6+ | Silence until the counter resets |

Affinity and mood adjust the ladder: close friends get a longer, more playful run; `disliked` users get the short version; a grumpy bot reaches the meta line sooner. The check runs before any generation, so a silenced repeat costs no LLM time. A first person to reach silence can be a one-time event.

**Group greetings (optional, later):** count by channel as well as by user. When several people greet within about a minute, the bot answers once for the group.

### 3.11 Presence

Applies to conversational replies only. Commands run at any status.

**Status rules.** Discord status belongs to the bot account as a whole, so one status applies across both servers and all channels. Bots have no automatic idle timer, so the bot sets its own.
- **Online** while people are talking to it, about it, or using commands. Any of these resets the idle timer: a message containing its name, an @mention, a reply to it, or a slash command.
- **Idle** after 10 minutes (setting) with none of those.
- While idle the bot answers direct addresses and has a lower chance of interjecting. An interjection returns it to online.
- **Do not disturb** during sleep (3.12). Sleep overrides both other states, with no interjections.
- The bot changes status only at state transitions, because Discord rate-limits presence updates.

**Sequence for the first reply after idle:**
1. Message arrives; status is "idle".
2. Reading delay: a pause before the typing indicator appears, simulating that the bot noticed and read the message. Mood-scaled, about 3 to 15 seconds.
3. Status switches to "online". Wait 1 to 2 seconds.
4. Typing indicator for `min(words ÷ speed, cap)`. Speed baseline is about 100 words per minute (the owner's typing speed). The cap starts at 5 to 6 seconds and gets tuned by testing; a longer wait is acceptable. A 30-word reply therefore shows about 5 to 6 seconds of typing (an effective rate near 300 words per minute), and only long replies reach the cap. The cap and the speed are separate settings.
5. Send.

- Inside an active conversation the reading delay is 0.5 to 2 seconds.
- Generation starts when the message is accepted, in parallel with the delays. The reply sends when both the delays and the generation have finished, so the total wait is `max(reading delay + typing time, generation time)`. A 2-second generation fits inside the delays and adds no wait.
- Delay values live in settings; owner-only `/presence` tunes them.

**Split replies.**
- The model writes one normal reply; code checks its length afterward. Short replies never split.
- Split afterward in code (default for the first build): break at sentence or clause boundaries into two or three parts.
- Model-directed split (Layer 3 test): the prompt asks for short messages separated by a marker, and code splits on the marker. Code validates the output and falls back to the code split when the marker is missing or wrong. Both versions are compared during Layer 3 testing, and the length check decides whether code acts on the model's markers.
- Each part runs through the presence function. The first part uses the reading delay and typing time. Later parts show a short typing indicator with no pause before it, and a short gap adds a beat. Short parts go fast, long parts take longer.
- Trigger: reply above a length threshold, with probability scaled by mood and affinity. `disliked` never splits.
- Per-channel split cooldown: 30 minutes to an hour or more (setting). Splitting is meant to be a rare quirk, not her normal way of typing; loosen it only if it feels too rare in testing.
- **Eligible:** model replies above the length threshold, and the short follow-up part for a second intent. **Never split:** canned lines, confirmations, command results, reminders, feed alerts, and the `disliked` tier.
- **Length safety net.** Discord rejects messages over 2,000 characters. The persona prompt asks for short, chat-sized replies and the model's output length is capped, so this should be rare. A reply that still exceeds the limit is broken at the nearest paragraph or sentence boundary into consecutive messages, never cut mid-word. This has no probability, cooldown, or extra typing delay.
- The Discord reply reference goes on the first part only. A typo lands in one part; the edit targets that message's ID.
- Two intents in one message (for example a return reaction plus a goodbye): answer the primary intent first, send the secondary as a short follow-up part. At most two intents per reply. A one-time event flagged "primary only" waits for a message where it has the stage to itself.

**Interruption during a split sequence.**
- A new message from the same person: finish the sequence, then handle the new message from the per-channel queue (default). Alternative by setting: stop the loop, drop the remaining parts, and answer the new message.
- "Never mind" (or deleting or editing the message she is answering) after part 1 is sent: the remaining parts are cancelled and the parts already sent stay. The follow-up uses the cancel rule below: a short acknowledgment for a friendly mood or close tier, silence for a bad mood or low tier.

**Cancel on "never mind."**
- While a reply to a user is pending, check new messages from that user against a small pattern list ("never mind", "nvm", "forget it", "ignore that", "cancel", "scratch that"). False positives cost little because the bot just stays quiet.
- On match, cancel the pending task. Closing the HTTP request to the endpoint normally stops generation; otherwise discard the finished text.
- Mood and affinity decide the follow-up: silence for low affinity or a bad mood, a short acknowledgment ("alright") for a friendly one.
- Deleting or editing the triggering message while the reply is pending cancels the same way.
- Requires the model call to be asynchronous so the event loop keeps receiving messages during generation.
- After a reply has been sent, "never mind" is an ordinary message.

### 3.12 Sleep schedule

Sleep affects conversation only. Commands, feeds, reminders, and alerts run at all hours.

1. A sleep window (for example midnight to 8 a.m.) defines when sleep can begin.
2. Inside the window, after a quiet period (about 20 minutes since the last message in watched channels), the bot goes to do not disturb.
3. Waking happens a set duration after sleep starts (about 8 hours). A latest-wake setting (for example 10 a.m.) caps the wake time, so a chat that runs until 3 a.m. does not push waking to 11 a.m.
4. A message during sleep gets no reply while she sleeps. **Catch-up at wake:** when she wakes (at the wake time or early), she reads the history of the listen channels from a watermark and replies to the messages addressed to her (a mention, a reply to her, or her name, using the same addressing function as live messages). The watermark is the `bot_state` key `sleep.catchup_from` (9.8). It is set to the current time each time she falls asleep and moved forward after each catch-up, so after an early wake and a second sleep only messages from the second sleep are read, and a message she has already replied to is skipped. Rules: newest first, at most `sleep.catchup_max_replies` replies in all and one per person per channel; messages older than `sleep.catchup_max_age_hours` are ignored; each reply uses Discord's reply reference to the old message and carries the sleepy flavor; a conversational command in the backlog is not carried out, and the reply asks the person to ask again; catch-up messages cause no mood or affinity events (3.3). `sleep.catchup_enabled` turns the feature off. The messages are read from channel history, so the live queue and `queue.max_age_seconds` are not involved. The first 5 replies after waking carry the sleepy flavor, ending sooner if 60 minutes pass (3.3).
5. Early wake: two mentions within a minute, or any message from the owner. The bot answers groggily with a small mood penalty. The same 20-minute quiet rule puts it back to sleep, and the original wake time stays unchanged.
6. Schedule values live in settings. Owner-only `/sleep` changes or disables the schedule, `/sleep off` disables it, and `/wake` keeps the bot awake until the next scheduled sleep window.

**Conversational sleep (owner only).** The owner can say "go to sleep" in conversation. The bot pushes back depending on mood (the owner tier has maximum affinity, so only mood matters), and a second request within a short window goes through. The slash version stays flat and instant. The conversational version is owner-only through the permission check, and the permission rule remains the final authority (3.1).

### 3.13 Typo events

- Probability about 1 to 2 percent per reply, with a time-based cooldown (a setting, starting at 60 minutes).
- Skip replies carrying names, numbers, links, or data from memory or feeds.
- Generated in code: swap two adjacent letters or use a keyboard-neighbor letter in one word of 4+ letters, never the first word.
- Mood-weighted: sleepy or excited more often, focused less.
- Owner-only config switch to disable.

States: LIVE (posted, fix scheduled), FIXED (edited), LEFT (bot chose to leave it).

- Store: message ID, typo word, correct word, state, commented flag.
- Fix method: edit the message after 2 to 5 seconds (shows "(edited)"), or send a `*word` follow-up.
- Comment detection: reply to the typo message, message containing the typo word, correct word, or a keyword ("typo", "spelling", "misspelled"), a lone `*word`, or a laughing reaction within a minute. Only the first comment counts.
- Comment while LIVE: cancel the scheduled fix task, reply in character, then fix or leave based on mood.
- Comment after FIXED: acknowledgment line ("yeah, caught it") or deflection.
- Comment after LEFT: defensive or doubling-down line.
- Teasing can write a mood event, scaled by the relationship (3.4) and by the person's `teaser` trait (3.5), so a known teaser's banter lands lighter.
- Optional: store the typo as a memory for later callbacks.
- Edge cases: comment and fix landing in the same second (cancel first, then decide); restart with LIVE events (resolve on startup).

### 3.14 Self-correction

Rare, conversational lane only, mood-weighted (sleepy or excited more often), with a cooldown and a kill switch.

Versions:
1. **Value slip:** when a reply states a number, date, or name taken from data (cat photo count, days since an event, a nickname), the code occasionally decides to slip. Code picks the wrong value and knows the right value. Layer 2 uses templates ("wait, no, 14"). Layer 3 replaces the templates: the model receives the wrong value, the right value, and the persona, and returns one structured response with two fields, `first_attempt` (contains the wrong value) and `correction` (contains the right value, phrased naturally). Code validates that `first_attempt` contains the wrong value and `correction` contains the right one, then sends them as two parts with presence timing. If validation fails, the bot sends the plain correct reply. The correct value always comes from code and is never left to the model.
2. **Restart:** an inserted phrase ("actually, scratch that") followed by a rephrased second half, sent as two parts.
3. **Edit:** send the message, then edit the earlier word after a couple of seconds, with "(edited)" visible.

Rules:
- The correction is scheduled at the same moment the slip is created, so normal operation never leaves a wrong statement standing, and the corrected version ends with the correct information. A crash or restart between the two parts can leave the first part standing, or leave a multi-part message incomplete. That is accepted: the feature fires in well under 1 percent of eligible replies, and crashes are rare. Persistent recovery is in the Parking Lot.
- Never applied to anything someone might act on: feed alerts, reminders, maintenance times, `/relationship` output, command results.
- Well under 1 percent of eligible replies.

Occasions for restart-style corrections: naming the wrong cat or friend and fixing it from persona or memory data; a groggy opener ("what was I... right"); hedge-and-commit openers ("hmm, actually") before the real answer.

### 3.15 One-time events

A reply that fires once and never again.

**Table:** `one_time_events` (9.17): one row per event and person, written when an event is claimed. Event definitions (key, kind, priority, scope, trigger, lines) live in a data file (`events.yaml`) that the bot reads at startup and when the owner runs `/event reload`. Code holds only the trigger logic that needs the bot's internal state (counters, behavior). A phrase-based event is added by editing the file, with no code change.

**States and claiming.** An unfired event has no row. A row is `pending` (claimed, message being sent), `fired` (sent), or `disabled` (switched off with `/event disable`, or a milestone passed during a folder ingest). Each definition has `per_person` (each person triggers it at most once) and `max_total` (how many people can trigger it in all). A claim is a transaction: it deletes `pending` rows older than a few minutes, then inserts a `pending` row only when no `disabled` row exists for the key and fewer than `max_total` `pending` or `fired` rows exist. Zero rows inserted means the claim was refused, so two simultaneous triggers produce one reply. When the send succeeds the row becomes `fired` with the message link and the variant used; when it fails the row is deleted. At startup leftover `pending` rows are deleted. `user_id` is the person who triggered the event (0 when no person is involved, as with milestones and calendar events).

**Collisions:** each event has a priority; only the highest fires, the others stay unfired. A secondary event can send as a follow-up part (3.11) when its own trigger matched. Events flagged "primary only" wait for a clear moment.

**Kinds:**
- **Reaction firsts:** return after absence, first "good bot", first "bad bot".
- **Behavior firsts:** first typo someone comments on, first early wake-up, first time someone reaches silence on the repeat ladder, first full day with no mood events.
- **Per-user firsts:** first time each friend uses `/relationship`, sets a nickname, or adds a cat photo.
- **Milestones (counter-based):** 100th, 500th, 1,000th cat photo; 1,000th message answered. A milestone fires when the count is at or above the threshold and the claim succeeds, so a jump past it still works. A folder ingest writes `disabled` rows for the thresholds it passes, so only later thresholds fire. Removing a photo and adding another does not announce again. No per-user interaction-count milestones.
- **Calendar:** the bot's account anniversary (September 30; the account was created in 2020), New Year's, and friends' birthdays (entered by the friend with `/birthday set`). Mechanics below.
- **Second-time variants** ("that's twice now") are separate event keys whose definition lists the first event as a prerequisite. Candidates: a second bad bot, a second early wake-up, a second typo comment, a nickname changed again. None are written at first.

**Writing:** three to five variants per important event, each tagged by mood and carrying a stable ID (recorded in the `variant` column when it fires). A variant is `attached` (written to follow a normal reply as a second part) or `standalone` (written to work as a message by itself). Per-user events use the nickname and a memory slot.

**Calendar events.** The definition holds a month and day plus a key suffix, so the stored key carries the year (`anniversary:2027`) and the same entry works every year.
- At midnight in the events timezone (a setting, default the owner's) the bot builds an in-memory list of calendar events due that day that have no `fired` row. Replies check only that list, so no database query runs per message.
- **Anniversary (September 30):** attached as a second part to the first reply she gives that day. If it is still unclaimed at 9 p.m. (a setting), a scheduled task claims it and posts a standalone line in the announce channel.
- **New Year's and birthdays:** posted at midnight in the announce channel. A birthday uses the birthday person's own timezone from `user_settings` when they have set one, otherwise the events timezone. Birthdays come from `/birthday set` (month and day only, stored in `user_settings`).
- The announce channel is a per-server setting (`events.announce_channel`) set with `/config`. A server without one gets no announcements.
- Each event has a `during_sleep` field: `wait` (the default) posts when she wakes, and `post` posts at the exact time. A task checks once a minute for due events. If the bot is offline at the due time, it posts within a catch-up window (6 hours, a setting) after it returns or wakes; after that the day is skipped.
- These announcements are the exception to the rule against unprompted posts (the first-boot rule below is unchanged).

**Welcome back after a long absence.** This one repeats, so it is not a row in `one_time_events`. When a person's last counted visit (3.4) was 60 or more days ago, the next reply may carry a welcome-back line, guarded by a 30-day `cooldowns` entry. The bot checks the gap before it updates their visit time. Both numbers are settings.

**Return after absence (key: `return_after_absence`).** Friends will likely react to the bot coming online after about 6 years ("Maika's back", "Maika is alive?").
- Trigger: the bot's name plus at least one reaction word (back, alive, still, years, remember, ghost, "it's been so long"), or a bare "Maika?" / "Maika!!" shortly after the bot comes online. Require name plus reaction word, because a false positive on a once-ever event costs more than a miss.
- Do not post unprompted on first boot. Wait for the first matching message. The real Maika stays offline in the friend server until this event is ready (section 7).
- Gap measurement: scan server history once for the last message from the old Maika account (`channel.history` filtered by author, needs Read Message History) and store the date in `bot_state` as `last_seen_old_account` (9.8). Fallback: enter that key by hand.
- Tone is open (groggy, unsure what year it is, amused to be remembered). Mood can shape it.
- A `first_boot` record stores when the bot returned, so later lines can say how long it has been back.
- Several friends may react at once. The event allows up to 3 people (`max_total: 3`, `per_person`), each claiming their own row. The definition has slot variants: first person, second person ("you too?"), third person.

**Owner controls:** `/event list` (state, when fired, who, which variant), `/event reset <key>`, `/event fire <key>` (testing), `/event disable <key>`, and `/event reload` (re-reads `events.yaml`). A dry-run mode logs "would have fired" to `#events` without claiming a row or posting.

**Testing:** replay sample messages against trigger patterns with cases that should match and cases that should not ("Maika's back" versus "I'll be back" and "Maika is back in stock"). Trigger patterns for once-ever events get the most careful testing.

### 3.16 Topics to avoid

- Rule layer in front of the model: a keyword and category check runs first; a match returns a mood-flavored deflection from the canned dictionary, so the model never sees the message.
- The prompt and post-check enforce the same list as a second layer.
- List lives in settings (`guardrails.topics`), editable through `/config`.
- **Starting list:** politics, religion, medical, legal, and financial advice, sexual content, other friends' private details (addresses, workplaces, health) and people outside the server, slurs and harassment aimed at someone, and her own configuration (tokens, prompts, settings). The decision log extends it.
- **Keyword layer:** a short list of unambiguous terms, because a keyword match skips the model entirely ("trump card" in a card game, or "election" in a game's story mode, must not trigger politics). Gray areas are left to the prompt and the post-check, where the model reads context.
- **Post-check (4.1):** the finished reply is scanned for the blocked topics before it is sent. A failing reply is regenerated once with a reminder added, and then falls back to a canned line.
- **Repeated pushing** reuses the repeat ladder (3.10): the deflections escalate to silence the same way repeated greetings do.
- **Owner exemption:** a setting lets the owner bypass the guardrails for testing. The default is off, so testing shows what everyone else sees.
- Mood shapes the deflection (cheerful jokes its way out, grumpy says "no"); the avoided topic stays the same.
- **Crisis messages (open item, decided at step 13).** A deflection is the wrong response to a sincere message about self-harm or crisis. Direction so far: narrow triggers (Layer 2 uses a short list of unambiguous first-person statements, Layer 3 flags only likely-serious cases using conversation context, and venting, swearing, and joking trigger nothing); a short check-in shaped by the relationship ("hey, you okay?" for a close friend, a shorter and flatter version in the disliked tier); no resource lists, hotline numbers, or pamphlet text by default; the event logged for the owner; a switch to turn the handler off. The owner wants the lines used sparingly, so the exact wording and thresholds are chosen carefully at build.

### 3.17 Conversational commands

Slash commands and sentences are two front ends to one function (for example `add_cat(user, message, name)`), with the same permission check.

- Layer 2: patterns matched with regular expressions for simple phrases ("call me X", "remind me in 2 hours to Y", "remember that Z"). The "remember that" pattern arrives with the memory system at step 13.
- Layer 3: the model reads the message and returns JSON naming an action from a fixed allowlist plus parameters, or "none". Code validates the action, checks the user's level, and fills in context.
- Resolving "the picture from my last message": the speaker's most recent message in the channel with an attachment, or the message they replied to. If neither exists, the bot asks.
- Whether an action that changes stored data asks first ("Adding this photo as Buttons, right?") is decided by the three zones below. Read-only requests never ask. The confirmation wording goes through the personality pipeline (3.1).
- Reactions to actions: after a conversational command executes, the reply can carry a reaction shaped by relationship and mood. Examples: a nickname from a close friend gets a warm reaction, and one from someone the bot is cool toward gets a cooler one. Code performs the action and checks the permission first; the personality only decides how the bot talks about it.
- **Three zones.** Each candidate command gets a decision from three inputs: the addressing score (3.2), whether a command pattern matched, and a fit result for what the pattern captured (`valid`, `questionable`, or `unusable`):
  - Pattern not matched, or addressing below `commands.ask_cutoff`: ignore (ordinary chat, or not meant for her).
  - `unusable` fit (no action can be built): ask a clarifying question in character if addressing is at or above `commands.act_cutoff`; otherwise ignore. There are two reasons: `unreadable` (a value that does not parse, such as a reminder time) and `missing_argument` (the pattern matched but a required part is empty, such as "Maika, add" or "remind me" with nothing after it).
  - `valid` fit with addressing at or above `commands.act_cutoff`: act, then say what she did.
  - Anything else (addressing in the middle zone, or a `questionable` fit): ask for confirmation.
- Fit checks per command: nickname (passes the validation rules in 3.6), reminder (the time parses to a future moment and the task text is not empty), remember (the text is long enough to mean something). `questionable` covers unusual but buildable actions, such as a very long nickname.
- **Missing arguments.** A `missing_argument` result is answered with a line that asks for the missing part ("What am I supposed to be adding?"). The lines come from the canned dictionary (3.9), keyed by command, mood, and tier, with a generic mood-flavored template ("What am I supposed to {verb}?") for a command that has no lines of its own. Repeats from the same person count on the repeat ladder (3.10). The reply changes neither mood nor affinity. Slash commands cannot reach this case, because Discord requires the fields before it sends the command.
- The decision is a pure function (1.3 convention) tested with sample messages: "Maika call me Sam" acts, "call me when you're free" with no name or session is ignored, "Maika remind me in banana to do laundry" asks when, and "Maika remind me" with nothing after it asks what to remind you about.
- The confirmation is a normal channel message with Yes and No buttons (an ephemeral reply is not possible for a plain message). Only the person who sent the original message can press them, and they stop working after a time-out.
- Settings in `/config`: `commands.ask_cutoff` and `commands.act_cutoff`, `commands.act_without_asking` (off: every command asks for confirmation), and `commands.conversational_enabled` (off: conversational commands are disabled entirely). Cutoffs start cautious and are tuned from `#decisions`.
- Candidate commands: nickname, reminders, remember, cat photos, relationship lookups.
- Permission changes and the other owner-only commands (`/perm`, `/block`, `/unblock`, `/config`, `/reset`, `/relationship set`, `/event`, `/presence`, `/say`) stay slash-only. `/sleep` and `/wake` also have a conversational version for the owner (3.12).
- Once a command is recognized, it calls the same function as the slash command. The reply stays in the conversational lane (3.1); only slash commands use the flat command lane.

### 3.18 Owner controls and logs

**Settings table.** Every tunable value lives in `settings`: defaults in code, overrides in the database. Each setting has one definition in code: name, category, type, default, valid range, level (Basic or Advanced), whether a server may override it, description. `/config list category:presence` shows a category. `/config set` autocompletes the setting name and rejects out-of-range values. `/help` and `/config` generate from these definitions. Settings carry a `guild_id` (`0` means global). Values are stored as text and converted by the definition's type when read (lists as JSON text). Table details: 9.6. Owner-only `/config get`, `/config set`, `/config reset <key>` (deletes the override so the default applies again), `/config list` change delays, typo rate, sleep hours, recovery rate, cooldown lengths, ceiling and floor, topic list, tier display names, and anything else, without redeploying. A web dashboard goes in the Parking Lot in case `/config` becomes unwieldy. The full list of definitions is section 10. Each definition is marked Basic or Advanced: `/config list` shows Basic settings by default and every setting with the `all` option. `/config set` also runs cross-checks between settings (for example tier minimums must rise from acquaintance to close friend) and refuses a change that breaks one, with a reply that says why and changes nothing.

**Channel lists (owner-only, `config.*`).** `/config channels add list:<Listen|Chat> channel:<picker>`, `/config channels remove list:<Listen|Chat> channel:<autocomplete>`, and `/config channels show [list]` edit `addressing.listen_channels` and `addressing.chat_channels` for the server where the command runs, using Discord's channel picker so no IDs are typed. `remove` autocompletes from the channels currently on the list. The command checks that the channel is a text channel or thread in that server where the bot can read and send, reports a duplicate add or a missing remove without changing anything, and warns when the last listen channel is removed ("She will not converse in this server"). `show` labels values that come from `.env` when `/config` has none. No saved row means the `.env` value applies. A saved empty list, which `remove` writes when the last channel is removed, turns listening off for that server. `/config reset addressing.listen_channels` deletes the row and restores the `.env` fallback. Each change writes the same `settings` row, audit entry (kind `setting`, old and new list), and `#bot-log` post as `/config set`. A channel on the chat list also counts as a listen channel. `/config set` with raw IDs still works.

**Direct state commands (owner-only):** `/mood set`, `/relationship set`, `/reset` (mood to neutral, affinity to baseline, typo events cleared), plus `/mood` (anyone sees the named mood; the raw numbers are owner-only, `mood.raw`) and `/relationship` for inspecting state.

**`/say` (owner-only, `say.*`).** `/say message:<text>` makes her post the text as an ordinary message in the channel where the command ran. An optional `channel:<picker>` posts in another text channel of the same server where she can send (Discord's picker lists only the channels of the server where the command runs, so the command is run in the server where she should speak). `reply_to:<message ID>` makes the post a Discord reply to that message, and `allow_mentions` (off by default) lets the text ping people, roles, or everyone. The interaction is answered with an ephemeral "Sent." (a failure uses the failure pools, 3.9; a channel she cannot send in gets the plain default message), so the room sees only her message and no "used /say" header; this is confirmed on the test bot at step 5. The post is verbatim and in the command lane (3.1): no delay, typos, or splits. It does not count as a reply she generated: no mood energy, no reply cooldowns or repeat counters, no relationship events, and no persona notes or memories. It stays in the channel history she reads for context. Each use writes an `audit_log` row (kind `say`: the actor, the server, the channel in `note`, the text in `new_value`) and a `#bot-log` line with the channel and an excerpt.

**Kill switches:** a boolean per optional behavior (typos, presence delays, sleep, split replies, self-correction, repeat ladder, LLM replies) through `/config`.

**Change log:** every settings change is written to `audit_log` (kind `setting`, with old and new value) and posted to `#bot-log`.

**Log channels (private server).** Eight log streams, each mapped to a channel by a setting. A stream with no channel set falls back to `#bot-log`, so channels can be created as their features arrive, and merged or split later with no code change. Each stream has a log-level setting.
- `#bot-log`: startup and shutdown, settings changes, command usage.
- `#errors`: exceptions and failed fetches.
- `#permissions`: level, override, and block changes (a readable copy of the `audit_log` entries).
- `#feeds`: what was posted and where.
- `#decisions` (testing period): for each message in a listened channel, the addressing signals, score, and action. Read-only. A wrong call is fixed by adjusting settings with `/config`, and the logged signals are replayed through the decision function to confirm.
- `#persona-log`: changes to persona notes, and opinions the bot invented when asked (with notes that superseded them).
- `#events`: one-time event firings, collisions, and dry-run lines.
- `#memory-review`: the approval queue for automatic memories (Layer 3, step 14).

Logs hold excerpts and IDs, not full message content. Trim the decision log after the testing period. The audit log is kept permanently.

---

## 4. Layer 3: LLM Conversation

### 4.1 Architecture

- Code decides, the model only phrases. All functions (feeds, reminders, permissions, photos) stay in plain code.
- Model output goes to the channel as text only. `allowed_mentions` is set to none as the bot-wide default and turned on per message only for deliberate pings (the feed ping list, a reply that should ping the person). Text the bot repeats from a user also gets `@everyone` and `@here` neutralized.
- Multi-message questions from one person are batched before generation (3.2).
- Router in front of the model: greetings, thanks, "good bot" and similar get canned mood replies and never reach the LLM.
- Post-check in code: reject and regenerate (or fall back to a canned line) on excess length, forbidden phrases ("as an AI language model"), a missing required name, or similarity to recent replies.
- Token cap on generation, two to three sentence limit in the prompt, per-user rate limit and cooldown.
- Off-topic requests: allowed, answered in character with a short deflection.
- Test with a fixed list of about 20 prompts (greetings, favorites, off-topic, injection attempts) after every prompt change.

### 4.2 Persona facts

- Fixed facts in SQLite: favorite cat (Buttons), favorite game, foods, dislikes, short backstory. Written mainly through the persona notes channel (3.8).
- Keyword scan of the incoming message selects matching rows to inject, keeping the prompt short.
- Lazy opinions: when a question asks for a favorite or opinion and no row exists (favorite anime, for example), the bot picks one once, saves it as a persona row marked invented, and answers from the row afterward, so the answer stays consistent. `/persona edit` and `/persona reroll` (owner-only) change or replace rows. Owner notes override invented rows (3.8).

### 4.3 Prompt assembly

- System prompt: voice, length limit, in-character rule.
- Mood line: "Current mood: grumpy. You answer briefly and a little sarcastically."
- Relationship line: tier and disposition for the speaker.
- Trait lines: top two or three traits for the speaker.
- Nickname to use (or none this time).
- Persona rows matching the message.
- Retrieved memories (top 3 to 5).
- Short-term window: last 6 to 10 messages of the channel or thread. Optional one-line summary of older messages.
- The bot's last few replies to this person, with a no-repeat instruction.

### 4.4 Hardware

Three machines can run a model. Each is a separate endpoint (4.5).

| Machine | GPU | OS | Role |
|---|---|---|---|
| Main PC | RX 9070 XT (16 GB) | Nobara Linux | Fastest and largest VRAM. Shared with gaming. |
| Second PC | RTX 3080 (10 GB) | Windows | Not used for gaming. Sleeps when idle, woken with Wake-on-LAN. |
| Home server | i7-8700, 16 GB DDR4-2400 (2 × 8 GB, dual-channel), AVX2; GTX 1070 (8 GB) not installed | Unraid 7.3.4 | CPU fallback. Small-model GPU fallback only if the 1070 gets installed. |

**RX 9070 XT (main PC).** 16 GB GDDR6, 640 GB/s memory bandwidth, 304 W total board power.
- One owner reported Qwen3 8B at about 82 tokens per second and a 14B coder model at about 55 through Ollama's ROCm backend. The llama.cpp Vulkan backend measured about 100 and 60 tokens per second. These come from a single report; verify on the actual machine.
- 16 GB fits 14B-class models at 4-bit with room for context.
- ROCm on RDNA4 works from version 6.4 onward and Ollama supports it. Reports conflict on workarounds (one needed `HSA_OVERRIDE_GFX_VERSION`, another found it crashes on newer ROCm). Vulkan is the easier fallback. Expect an evening of setup on Linux.

**RTX 3080 (second PC, 10 GB).** Estimated 60 to 70 tokens per second on an 8B model, from memory bandwidth (estimate, not a benchmark). 10 GB fits 8B-class models at 4-bit to 8-bit with context; 12B to 14B at 4-bit is tight and may need partial offload. Mature NVIDIA drivers, and Ollama on Windows is the simplest install of the three. The model runs on this PC and the bot reaches it over HTTP on the home network, so nothing leaves the house.

**Home server CPU (i7-8700).** Generation speed on CPU is limited by memory bandwidth. Dual-channel DDR4-2400 gives a theoretical 38.4 GB/s. Estimates (not benchmarks):

| Model size (4-bit) | Generation | Prompt processing |
|---|---|---|
| 8B | about 5.5 to 7 tokens/s | about 30 to 40 tokens/s |
| 3B | about 11 to 14 tokens/s | about 70 to 90 tokens/s |
| 1B | about 22 to 30 tokens/s | about 200 tokens/s |

A 10-word reply is about 15 tokens. With a 3B model that is roughly 1 to 2 seconds of generation plus prompt processing, which depends on prompt length. Each endpoint has a prompt token budget, smaller for the CPU endpoint, because prompt processing dominates there. The integrated GPU is not part of the plan.

**GTX 1070 (home server, install deferred).**
- With the 1070: 7B to 8B at 4-bit fits. One calculator site estimates about 21 tokens per second for an 8B model (estimate, not a benchmark); a measured 1080 Ti result of about 46 tokens per second at twice the bandwidth supports a figure in the 20s.
- Decision: wait. The call log (4.5) records how often replies fall to the server CPU and how long they take. Install the card if the data shows the two PCs are unavailable often enough to matter.
- Driver risk: NVIDIA 580 branch is the last for Pascal; 590 dropped it; CUDA 13 dropped Pascal. NVIDIA lists quarterly security updates until October 2028.
- Unraid: install the Nvidia-Driver plugin (ich777), set a static driver version (a 580.x release) instead of "latest", add `--runtime=nvidia` and the card UUID to the Ollama container. The plugin only lists recent builds; a specific version can be requested in its `settings.cfg` if a build exists for the installed Unraid version. The plugin author has said he does not want to maintain a legacy branch.
- Power: idle draw of a 1070 is often around 10 W (about 88 kWh per year, about $26 per year at the owner's rate); up to about 150 W under load. Measure with a plug-in meter and the GPU Statistics plugin.

**General.**
- Run the model call outside the main event loop (Ollama as a separate service, async HTTP). One generation at a time per endpoint.
- Test plan: install Ollama, pull a small model, check tokens per second with `--verbose`. Rough guide: 5 or more tokens per second suits the typing effect; 2 to 5 works with short replies; below 2 is too slow.

### 4.5 Model endpoints and fallback chain

All model calls go through one function, `generate(request)`. Layers 1 and 2 never call a model directly, so the backend can change without touching them.

**Endpoint list (settings table, editable with `/config`):** an ordered list of endpoints. Each has a name, base URL, model name, a status-check URL, an optional wake method (Wake-on-LAN MAC address), and an enabled flag. Default order is undecided (see Open Questions). Each endpoint also has a prompt token budget (4.4). The call log records endpoint, purpose, latency, and outcome for every request.

**Walk-down procedure for each reply that needs a model:**
1. Check endpoints in priority order. Checks run in parallel with a short timeout (about 1 second) so the whole list costs a few seconds at worst.
2. Take the highest-priority endpoint that reports available.
3. If the chosen endpoint is asleep and has a wake method, send Wake-on-LAN and wait for it (about 10 to 20 seconds including model load). This hides inside the presence delay (3.11) for the first reply after a quiet stretch.
4. If every endpoint is unreachable or busy, use a canned reply (3.9) and write a line to `#bot-log`.
5. Any call failure or timeout moves to the next endpoint.

**Gaming and shared GPU.** The main PC shares its GPU with games.
- A small status script on the main PC answers a status request with "available" or "busy". It checks for a running game process (Steam game processes, or GameMode status if used). Busy, timeout, or no answer moves the chain to the next endpoint.
- VRAM: a game at 1440p can use 8 to 12 GB and an 8B model at 4-bit uses about 5 GB plus context, so both together on 16 GB can overflow into system memory. Compute: each reply is a few seconds of full GPU load, which shows up as a stutter in a game.
- Ollama `keep_alive` set short (one to two minutes) frees VRAM soon after a conversation ends. The game-launch side of the status script can also tell Ollama to unload the model immediately.
- Because the second PC (3080) is never used for gaming, it is the more predictable primary. The main PC can lead the list when the owner rarely games during the hours the friends chat. Order is a config value.

**Second PC power.** Wake-on-LAN wakes it only when a reply needs it. Auto-sleep after an idle timeout (15 to 30 minutes). Ollama's model-unload timeout is configurable. Power cost during extended use is a concern to measure.

**Fallback and kill switch.** A global LLM kill switch (3.18) forces canned replies. Layer 2 behavior works with no endpoint reachable.

### 4.6 Local versus hosted model

- A hosted API (Claude, ChatGPT) can match or exceed a local model on tone, sarcasm, and personality in the moment.
- A hosted model has no memory across conversations. Affinity, per-person memory, and consistent invented opinions all live in this bot's own stored data (3.4, 3.7, 3.8, 4.2) whichever model phrases the reply.
- Decision: stay self-hosted. Sending friend-group messages to a third party is unwanted.
- Hybrid option, tabled: storage stays local and only the assembled prompt for one reply goes to a hosted API. The single `generate()` function (4.5) allows adding a hosted endpoint later, controlled by a setting, without redesign. Revisit after the local models have been tested.

---

## 5. Unraid Layout

- Bot container (Python, discord.py) from a short Dockerfile. Code and SQLite database in `appdata` on the cache SSD.
- Ollama container on the server runs the CPU fallback model (and the 1070 if it gets installed). Other Ollama instances run on the main PC and the second PC and are reached by address (4.5).
- Photos on an array share, mounted into the bot container. Originals are kept (2.4).
- Include `appdata` in Unraid's backups (to be verified on the server). It holds permissions, personality, memories, relationships, and mood data. The bot also makes its own database backups (below).

### Database backups

- A scheduled task backs up the database once a day, at 4 a.m. by default, using SQLite's backup function. Copying the live database file can miss recent changes (WAL mode keeps them in a separate file) or produce an unreadable copy.
- Files: `backups/daily/maika-YYYY-MM-DD.db`. Copies older than 30 days are deleted. The backup time and the number of days kept are settings.
- Each new backup gets `PRAGMA integrity_check`. Success posts to `#bot-log` and failure to `#errors`.
- Before applying a migration (9.9) the bot makes a backup in a separate folder, using SQLite's backup function (the same method as the daily backup, never a plain file copy). The last 10 are kept.
- The backups folder sits inside `appdata`, so Unraid's backup includes it.
- Restore: stop the bot, copy the chosen backup over the database file, start the bot.
- A weekly copy of the newest database backup goes to another machine (build step 1), so a failure of the server does not take the backups with it. The last `backups.offsite_copies_kept` weekly copies are kept (4 to start).
- **Deleted memories in backups.** `/forget` removes a memory from the live database at once. Copies of it age out of the daily backups after 30 days and out of the offsite copies after the offsite retention (4 weeks to start). Friends are told this in the `/help` memory note (2.6).
- Photo files live on the array share and need their own backup arrangement. A copy of the photo files outside the server can be added later (Parking Lot).

### Test and production copies

- Two bot applications: the test bot and the real Maika. Both run in the private server, in separate channels, with separate tokens and separate database files.
- Settings that differ come from `.env`: `ENVIRONMENT` (test or prod), `LISTEN_CHANNELS`, `LOG_CHANNEL_ID`, `DATABASE_PATH`, and the token. `LISTEN_CHANNELS` and `LOG_CHANNEL_ID` are starting values: they apply when `/config` has no value for `addressing.listen_channels` or the log channels, and a `/config` value overrides them (section 10). A `.env.example` file lists the variables without values.
- The test bot runs from VS Code during development. Production runs in Docker on Unraid.
- The real Maika account joins the friend server only at go-live (section 7).
- Slash commands are limited to chosen channels in Discord's Integrations settings (Server Settings, Integrations, the bot, Command Permissions), set per server by the owner. This is not part of the bot's code.

### Development workflow (VS Code)

VS Code alone is enough for the code. The editor needs no special setup beyond a few extensions.

- Outside the editor: create the application and bot user in the Discord Developer Portal, copy the token, enable the Message Content intent when conversation features start, generate the invite link.
- Python virtual environment in the project folder (`python -m venv .venv`), `pip install discord.py`, run from the integrated terminal.
- Token in a `.env` file or environment variable. Add `.env` to `.gitignore`. Discord resets tokens found in public repos.
- Useful extensions: Python (includes the debugger), SQLite Viewer, Docker, Remote - SSH.
- Loop: run locally with the test token, then build the Docker image and deploy to Unraid once a feature works.
- Code conventions: PEP 8 (79-character lines, enforced by `ruff` as set out in Repository layout and standards, below), type hints, docstrings, named constants, and explicit code over compact code. `ruff format` and `ruff check` run before work is finished. The conventions and the testing rule (Testing, below) are written into `CLAUDE.md` in the repo so Claude Code follows them.
- Because Claude Code writes most of the code, the replay test scripts (relationship and mood functions, trigger patterns), the decision log, and the settings change log carry extra weight as verification.

### Repository layout and standards

The repo is set up as a finished project from the first commit.

```
maika/
  src/maika/           bot code (commands, systems, the database module)
  tests/               unit, db, integration, replay (see Testing)
  migrations/          0001_foundation.sql and so on
  data/                canned responses, events.yaml, blocklist, synonyms (no secrets)
  docs/                design.md (this document) and a decisions log
  scripts/             backup, restore, and ingest helpers
  .github/workflows/   CI
  pyproject.toml       metadata, dependencies, ruff and pytest settings
  Dockerfile, .dockerignore
  .env.example, .gitignore
  CLAUDE.md, README.md, LICENSE, CHANGELOG.md
```

- **README.** What the bot is, its features, how to create the Discord application, configuration (`.env.example`), how to run it locally and in Docker, how to run the tests, and an architecture overview that links to `docs/design.md`.
- **PEP 8 enforcement.** `pyproject.toml` sets `ruff` to a 79-character line length with the pycodestyle (`E`, `W`), pyflakes (`F`), isort (`I`), pep8-naming (`N`), pydocstyle (`D`, PEP 257 convention), pyupgrade (`UP`), and bugbear (`B`) rule sets. Tests are exempt from the docstring rules. A `# noqa: E501` is allowed only for a URL that cannot be split. Comments and docstrings are limited to 72 characters, as PEP 8 recommends, through the `W505` rule with `max-doc-length = 72`. The reason for the limits is reading three editors and a browser side by side without horizontal scrolling. Long SQL and string literals are split across lines in the code. `ruff format` and `ruff check` run in a pre-commit hook and in CI.
- **CI.** A GitHub Actions workflow runs `ruff check`, `ruff format --check`, and `pytest` on every push and pull request. A status badge sits at the top of the README.
- **Dependencies.** Pinned versions in `pyproject.toml` with a lock file, and a `.python-version` file, so a fresh clone builds the same environment.
- **Git habits.** One branch per feature, a pull request for each (even when working alone), small commits with clear messages, a tag at the end of each build step, a short `CHANGELOG.md`, and GitHub Issues for the build steps and the Parking Lot.
- **Secrets and data.** `.env`, the database, and the photos are never committed. GitHub secret scanning is turned on. Discord resets a token found in a public repository.
- **License and visibility.** Decided before the first commit (see section 6).

---

### Testing

Testing is set up in build step 1, before the permission code, so every later step adds tests to a working base.

- **Tools.** `pytest` with tests in a `tests/` folder. `ruff check`, `ruff format`, and the full test suite run before any feature counts as finished. `CLAUDE.md` states this rule so Claude Code follows it.
- **Layout.** `tests/unit/` for pure functions (permission check, `decide_command`, addressing score, mood drift, tier computation, feed filters). `tests/db/` for database behavior against an in-memory database. `tests/integration/` for places where systems meet. `tests/replay/` for replay scripts with sample message files and the decisions they should produce.
- **Fixtures.**
  - A fresh in-memory database built from all the migration files, which also tests the migrations.
  - A fake clock: functions that depend on time take `now` as an argument (decay, recovery, cooldowns, daily gain limits), so tests control time and never wait.
  - Simple stand-ins for Discord users, channels, and messages, so tests of the thin Discord layer need no connection.
  - Fake model endpoints (down, busy, slow) for the fallback chain in Layer 3.
- **Step 1 tests.**
  - The permission check: every case in the order in 1.2 (owner, protected key, `chat` and `all` blocks, exact, wildcard, and `*` overrides, server row over global row, default level) and the wildcard overwrite rules.
  - Migrations: a fresh build, detection of pending files, rollback of a failing file, and the pre-migration backup.
  - Settings: type conversion, range checks, cross-checks, and rejection of a server scope on a global setting.
  - Cooldowns: two simultaneous claims produce one winner.
  - Audit: the change and its audit row are written in one transaction.
- **Later steps.** Each step adds the tests for its own pure functions and the replay cases named in its sections (3.4, 3.15, 3.17). The integration list in section 7 (permission checks against each lane, queue order with cancel, mood plus relationship plus cooldown sequences, restart recovery, memory visibility) grows as those parts exist.
- **Bugs.** A bug found later gets a failing test first, then the fix.
- **Regression.** After any change to a shared part (the database module, permission check, settings, registry, message pipeline), the full suite runs.
- **Manual checks.** The test bot in the private server covers what automated tests cannot: how a command looks, buttons, delays, and typing. Each step ends with a short manual checklist.
- **Priority.** Coverage is not a target. The code that needs thorough tests is the permission check, migrations, settings validation, the relationship and mood math, the addressing and command decisions, and anything that deletes data.

---

## 6. Open Questions

**Resolved**
- Server: i7-8700, 16 GB DDR4-2400 dual-channel, AVX2, Unraid 7.3.4.
- Second PC: RTX 3080 with 10 GB.
- Reminders: Discord only, with no tasks app sync.
- Owner tier display name: `owner`, changeable through `/config`.
- Cat-to-user mapping: removed with the history import. The folder ingest uses a `cats` table.
- 2011 bot's command set: moderation, image-board, and YouTube commands overlap with Discord's built-in features, so nothing carries over. Permission keys are listed in 1.2.
- Data scope: all data is global by Discord user ID (1.1).
- Memory search: FTS5 plus tags first. Embeddings only if testing shows misses (3.7).
- Go-live: Maika stays offline in the friend server until step 12 (section 7).
- 1070: install deferred, decided from call log data (4.4).
- Levels: 0 to 3 with level 3 named Admin, the owner above the ladder (2.3). Level 0 is never stored.
- Blocks: two kinds in their own table (9.4). Protected keys replace the exempt list (1.2).
- Timestamps: UTC Unix seconds. Settings and state values: text converted by type.
- Server-specific permission rows win over global rows.
- Log channels: eight streams, `#events` as its own channel, `#decisions` kept for testing (3.18).
- Database backups: daily at 4 a.m. plus pre-migration copies (section 5).
- Relationships: affinity (how Maika feels about a person) and familiarity (visits, fading after 30 quiet days toward a floor that never lowers a person more than one tier) decide the tier together, with `disliked` from affinity alone; no `notes` field; one event log shared with mood; affinity rises through neutral talk up to a cap of 1.5 and through small positive events under daily limits, and the baseline ratchets up to the minimum of the highest tier reached (3.4, 9.14, 9.15).
- Memories: level 1 can add memories, including about another person; `/forget` is a permanent delete with a trial confirmation; `/forget all` covers every memory about the person and leaves relationship numbers alone; managers use `/memories user:@x` with Delete buttons; deletions write an audit row without text and are not posted; no trash table, recovery from the daily backup (2.5, 9.16).
- Mood: two decimal values with a baseline for each, half-life drift toward the current baseline, rerolls at wake time and about every 6 hours, five named moods, relationship-scaled negative events with per-person cooldowns, sleepy for the first 5 replies after waking, `/pet` in any channel, anyone sees the named mood and only the owner sees the numbers, the person who caused a drop is named, `mood_events` rows deleted after 30 days (3.3, 9.14).
- Reminders: a timezone per person in `user_settings`, repeating reminders as RFC 5545 rules, delivered rows kept, blocks do not stop reminders already set (2.2, 9.10).
- Feeds: five tables with a `start_at` bookmark per subscription, a burst of new items posts in full, nobody is added to ping lists automatically, `all`-blocked users are skipped when pinging and `chat`-blocked users are pinged (2.1, 9.12).
- Cats and photos: a photo can show several cats (link table), removal deletes the row and file with an audit entry, no aliases, level 1 can create cats through a confirm prompt (2.4, 9.13).
- One-time events: definitions in `events.yaml`, one row per event and person in `one_time_events` (9.17), atomic claim with a `max_total` cap, return after absence for up to 3 people with slot variants, second-time variants as separate keys, calendar events with a per-server announce channel, a `during_sleep` field, and a 6-hour catch-up window; `/event list|reset|fire|disable|reload` (3.15).
- Nicknames: up to 3 per person, self-service plus `nickname.manage` (level 3) for others, learned names follow the three-zone rule (3.17), validation and a blocklist, `/forget all` deletes them (3.6, 9.18).
- Persona notes: `fact:`/`leaning:` prefix, `topic = value, because ...`, `#tags`, one active note per topic, deleting a note restores what it replaced, `/persona list|edit|reroll` owner-only (3.8, 9.19).
- Traits: a fixed allowed list, three sources (keyword, model, owner) with the owner winning, decayed signal counters, a "what I've noticed" view the person can delete from (3.5, 9.20).
- Repetition control: `recent_uses` table, stable line IDs, repeat counter in memory only (3.10, 9.21).
- `/help`: seven categories (Cats, Reminders and time, Feeds, About me, Fun, Manage, Owner), a first screen with a dropdown, one entry per top-level command, `/help command:` with autocomplete, ephemeral replies, plain wording for now (2.6).
- Addressing: listen channels per server, an editable name list, silence in the uncertain band, Discord replies only when others have posted in between and never pinging the author (3.2, 1.4).
- Conversational commands: three zones (ignore, ask for confirmation, act directly) from the addressing score and a fit result, confirmation as a channel message with buttons for the sender only, permission changes stay slash-only, nickname limit 40 characters and 5 words (3.17, 3.6).
- Split replies: rare, only for long model replies and second-intent follow-ups, a 2,000-character safety net, finish-first on a new message, remaining parts cancelled on "never mind" (3.11).
- `/relationship`: tier name plus a fixed description in her voice, public replies (setting), anyone can look up anyone, the lowest tier displayed as Disliked and the key renamed to `disliked` everywhere, permission levels renamed Everyone, Member, Trusted, Admin (3.4, 2.3).
- Topic guardrails: a starting list, a short keyword layer, the post-check, the repeat ladder for repeated pushing, an owner exemption setting (off by default), and the crisis handler left open (3.16).
- DMs: conversation off at launch (`dm.chat_enabled`), a relationship-shaped one-line reply, slash commands still work, no unprompted DMs apart from reminders, privacy rules ready if DM chat is enabled later (3.1).
- Per-keyword feed pings: personal ping words per subscription with an on/off switch that keeps them, removing the last word turns pings off, `feed_notify` gains `enabled` and `keywords` (2.1, 9.12).
- Settings: 225 definitions in 15 categories (section 10), named `area.name` with units in the name, a short description for autocomplete, Basic and Advanced levels, per-server scope for a few settings only, cross-checks between settings, tables of numbers stored as individual keys, `.env` values as fallbacks for listen and log channels, and the data files (canned lines, `events.yaml`, nickname blocklist, synonyms) outside the settings table.
- Testing: `pytest` set up in step 1, with a fresh in-memory database fixture, a fake clock, pure decision functions, replay scripts, and tests for the permission check, migrations, settings, and cooldowns before later steps build on them (section 5, Testing).
- Command-lane wording: failures and refusals (denied, not found, already exists, missing argument) use fixed pools in a data file, with no mood or tier, picked by the no-repeat rule; successes stay plain except for the social commands (3.1, 3.9).
- Missing arguments in conversation: a matched command with an empty required part asks for it in character, from per-command lines with a generic fallback, with no mood or affinity change (3.17).
- `/say`: owner-only; posts in the channel where it ran (optionally another channel of the same server, a reply target, and mentions off by default); ephemeral "Sent."; not counted as her own reply; audited (3.18).
- Cat photos shown in chat: `/addcat` posts the stored photo, and an "Add as cat photo" message menu adds a photo already shared in chat (2.4).
- Prefix commands: not used. Slash commands, the message menu, and conversational commands cover the cases (Parking Lot).
- Reply visibility: a command that changes what the channel sees replies publicly, and personal settings reply ephemerally (1.5).
- Photo removal: `/removephoto photo:<ID>` with the ID from the caption, a private confirm prompt that shows the photo, and a public line naming the removed photo, who removed it, and whose it was; no removal menu and no photo browser for now (2.4).
- Social-command pools: `/pet`, `/mood`, `/nickname`, `/relationship`, `/remember`, and the `/addcat` success reply use mood- and tier-keyed pools with a fallback chain and no model call (3.9).
- Listen list: no saved value falls back to `.env`, and a saved empty list turns listening off (3.2, 3.18, 10.1).
- Memory order: manual memories, `/memories`, and the conversational "remember that" command are built at step 13 (3.7, 7).
- Reminder DM failure: a notice in the original channel with no reminder text, status `failed`, and the text readable in `/remind list` for 7 days (2.2, 9.10).
- Event application: the cooldown claim, daily-limit check, and mood and relationship writes run in one `BEGIN IMMEDIATE` transaction (9.14).
- Self-correction and crashes: a crash between the parts of a correction or a split reply is accepted, and persistent recovery is in the Parking Lot (3.14).
- Sleeping messages: at wake she reads the listen channels from a watermark and replies to messages addressed to her, with caps, the sleepy flavor, and no mood or affinity events; the live queue limits do not apply (3.12).

**Facts to check**
- Whether the 1070 fits the case and has PCIe power connectors, and the electricity rate (relevant only if the 1070 gets installed).
- Whether Wake-on-LAN is enabled on the 3080 PC (BIOS and Windows adapter settings).
- How the 9070 XT PC will run Ollama (ROCm or Vulkan), and how the status script detects a game.
- Anime News Network's feed URLs, and whether a Steam name-to-ID lookup works without a key.
- Whether the Steam news `tags` parameter exists.
- The SQLite version in the Docker image (the trigram tokenizer needs 3.34 or newer), and how often tag and synonym searches miss in testing (9.16).
- Discord's attachment size limit for bot uploads.

**Decide before the step that needs it**
- Tier names, thresholds, descriptions, ceiling and floor (step 7). Kind-message patterns and gain sizes are tuned at the same step.
- Tone of the return-after-absence line, whether to reuse old Maika messages as callbacks, and how to find the old account's last message date (step 12).
- Review the starting avoided-topic list in 3.16 against real use (step 13).
- Repository visibility: public (decided). The license is chosen before the first commit. Read this document for anything that should stay private before the repository goes public, because a copy sits in `docs/design.md` (Repository layout and standards, section 5).
- The wording for each failure pool and the first set of social-command lines (step 8). The success messages of other commands stay plain.
- Memory expiry by type (step 13). Final trait list, thresholds, and combination handling (step 13). Persona note format (step 8).
- Buttons or reactions for feed pings (step 3, tested on the test bot).
- Default endpoint order and the idle timeout for the 3080 PC (step 14).
- Conversational-command reactions: which actions get personality reactions, and how strong by tier (3.17).
- Disliked tier: how blunt its lines are, against the earlier no-aggressive-lines rule (3.4).
- Crisis handler: triggers, wording, and whether it ships at all (step 13, 3.16).
- `/relationship` tier descriptions: placeholders rewritten in the owner's words (step 7).
- Defaults marked TBD in section 10 (about 40 values): chosen during build and testing, along with the cutoffs and name-list spellings tuned from `#decisions` (step 6).
- Name-mention interjections: probability by mood, cooldown length, minimum context size (3.2).
- Self-correction response format and validation rules (3.14).
- Command-usage log lines for protected keys: a person repeating `/forget` could flood `#bot-log` (logging design, step 1).
- Filter edits and older items (step 3): the query in 9.12 posts any stored item first seen after `start_at` that has no post row. After a filter edit, items that arrived earlier and match the new keywords would post at the next cycle. Options: accept that, or stamp the subscription's `start_at` with the edit time.
- Photo IDs in captions and the exact `/retag` and `/removephoto` options (step 4).
- Which Steam and Lodestone fields go into `feed_items` and `extra` (step 3, confirmed with real calls).
- Whether a hosted endpoint is ever added (4.6).
- Milestone thresholds and which calendar dates to include (step 12).

**Deep dives.** All planned design topics are settled, including the settings definitions (section 10) and all 27 tables (section 9). Buttons versus reactions is tested on the test bot at step 3. Mood, relationships, memory, traits, and persona notes are settled at the design level; their numbers and formats are revisited at implementation, and memory search, expiry, and the review queue are finished at steps 13 and 14.

**Tuning during testing.** Cooldown lengths, typing cap and speed, sleep and idle values, ping rates, interjection probability, split rules.

---

## 7. Build Order

1. **Foundation:** skeleton, settings definitions and `/config` (including `/config reset` and `/config channels`; the mechanism and the foundation settings for logging and backups are built here, and every other setting is defined with the feature that uses it, with section 10 as the specification), command registry with permission keys, protected keys, overrides, and blocks, `/perm` and `/block` commands, audit log, log streams and channels, cooldown table, bot state, numbered migrations with pre-migration backups, the daily database backup task and the weekly offsite copy of the newest backup, Docker, test and production setup (tables in section 9). **Testing setup comes first** (Testing in section 5): `pytest`, the fixtures, and the first tests for the permission check, migrations, settings, and cooldowns are written alongside the code they cover.
2. **Reminders,** `user_settings`, `/timezone`, and `/birthday` (tables 9.10 and 9.11).
3. **Feeds:** framework, subscriptions, filters and pings (tables 9.12), then FFXIV, Steam, news keywords, games.
4. **Cat photos,** including the "Add as cat photo" message menu, tested in the private server (tables 9.13).
5. **`/help`, `/say`.**
6. **Message handler:** bot guard, addressing, name detection, batching, reply queue, the one-line DM notice (3.1), test harness (replay scripts, dry-run mode, decision log).
7. **Mood and relationship state:** tiers, recovery, owner tier, `mood_events` (9.14), `relationships` (9.15), `/pet`, tested with replay scripts.
8. **Canned responses and the persona notes channel:** `persona_notes` (9.19), `recent_uses` (9.21); the fixed command-failure pools and the social-command pools (3.9) replace the plain messages used until then.
9. **Presence:** status, idle, delays, typing, sleep with the wake-time catch-up (3.12), cancel on "never mind".
10. **Repeat ladder, nicknames (9.18), Layer 2 conversational commands (3.17: regex patterns, fit result, decision function, confirmation buttons; "call me X", "remind me"; missing-argument questions), split replies.**
11. **Typos and self-correction.**
12. **One-time events** (9.17), including return after absence.
13. **Traits (keyword; 9.20), manual memories, `/memories`, and the conversational "remember that" command (table 9.16, 3.17), topic avoidance.**
14. **Layer 3:** `generate()` with endpoint list and fallback chain (status script, Wake-on-LAN, kill switch), persona prompt assembly, retrieval, automatic memory extraction with the review queue, model-assisted traits, flexible conversational commands, name-mention interjections, LLM-written self-corrections.

**Dependencies behind the order.** Cooldowns are in the foundation because canned lines, nicknames, and memory callbacks use them. Canned responses are keyed by mood and tier, so mood and relationship state come first. The command registry feeds `/help` and the permission overrides. Migrations let a table gain a column without wiping data.

**Go-live gate.** Friends will react the moment Maika appears online, and the return-after-absence event fires once. The real Maika stays offline in the friend server until step 12. Cat commands are tested in the private server meanwhile.

**Minimum go-live set.** Steps 1 through 9 plus the return event from step 12. Repeat ladder, typos, self-correction, traits, and memories are later additions.

**Migrations.** Numbered scripts from the first table onward; no schema change is made by hand.

Working rules: code follows PEP 8 and the conventions in section 5; one feature in progress at a time; keep the Parking Lot below for new ideas; commit from day one; record design decisions in the repo README; test each behavior system with replay scripts before friends see it; add integration tests for the cases where systems meet (permission checks against each lane, queue order with cancel, mood plus relationship plus cooldown sequences, restart recovery, and memory visibility); run the full test suite after any change to a shared part.

---

## 8. Parking Lot

Ideas not yet scheduled:

- Pokemon lookups via PokeAPI, if a new game revives interest.
- Word game in Discord (7-letter rounds with scoring). Low interest for now.
- Morning briefing: weather (Open-Meteo), deadlines, FFXIV reset times, new game news.
- Assignment and deadline tracker.
- Status monitoring for home server services.
- Jido integration: a command that sends a word to the Jido backend to create an Anki card from a phone (hand-written, no AI, per Jido's no-AI-in-code approach).
- Quote archive and "screenshot of the week" using the same table-plus-file pattern as cat photos.
- Unprompted post on first boot (currently decided against; the bot waits for a friend to react first).
- Thread-per-conversation mode for busy channels.
- Deviation noticing lines for traits (a joker gone quiet).
- Mood and affinity interaction details (bad mood amplifying negative events, friend teasing hurting less).
- Tasks app sync for reminders (dropped; reminders stay in Discord).
- Sonarr's Discord connection for anime episode alerts.
- Steam name search (name to app ID) when adding a source.
- AniList watching-list auto-follow.
- Web configuration dashboard if `/config` becomes unwieldy.
- Hosted hybrid endpoint (4.6).
- Mood-driven presence text: an activity line next to her status (Playing, Watching, or Listening to) that follows her mood or sleep state. Bots cannot show a user-style custom status, so this is the nearest equivalent. Presence updates are rate-limited, so it changes at state transitions only (3.11).
- Mood-driven profile: an avatar per named mood or sleep state, swapped through `client.user.edit(avatar=...)`, and the About Me text (the application description) edited through the current-application API endpoint, which discord.py may not expose directly. Discord does not publish the avatar rate limit (one community report says about 10 minutes), so changes stay rare and a refused change is ignored. The avatar character is not chosen yet.
- Embedding search for memories and persona notes (3.7).
- Cat photo additions through a reaction.
- Group and multi-person replies (one answer for several people asking at once).
- Per-channel restrictions on commands (for example keeping long outputs in one channel). Ephemeral replies cover the current cases.
- A reaction on a `#decisions` line that marks the decision wrong and saves it as a labeled test case.
- More mood names (relaxed: positive happiness with low energy; annoyed: mild unhappiness), and intensity (mild or strong flavor from the distance to the baseline) as a way to add variety without multiplying canned line sets.
- A copy of the photo files outside the server (another machine or a cloud sync of the photo folder).
- Near-duplicate photo detection (perceptual hashing) for resized or re-saved copies of a stored photo.
- Prefix commands (`!`) through discord.py hybrid commands, which serve slash and prefix from one definition. Left out because they bypass the Integrations channel restrictions, cannot reply ephemerally, and add a third front end to the registry and tests.
- `/say` to a channel in another server (channel autocomplete across servers). Not needed while `/say` runs in the channel it posts to.
- A "Remove cat photo" message menu that reads the photo ID from the bot's caption, as a shortcut for `/removephoto`.
- A `/photos` browser (private, one photo per page, filters by cat and uploader, Remove and Change cats buttons) for finding an old photo by looking at it. Build it if removing old photos turns out to be common.
- Persistent recovery for self-corrections and split replies, so a crash between the parts finishes the correction or the message on restart. The crash case is accepted for now (3.14).
- Cat aliases (nicknames for a cat) through a column on `cats` or a separate table.
- Music playback: the bot joins a voice channel and plays what it is asked to play. Notes for the later design discussion:
  - discord.py voice needs PyNaCl and FFmpeg in the Docker image, plus the Connect and Speak permissions in the channel.
  - Audio source is the main decision. Streaming from YouTube conflicts with YouTube's terms of service; Groovy and Rythm, two large Discord music bots, shut down in 2021 after Google demanded it. A local music library on the Unraid array avoids that, with search over file metadata.
  - Commands would follow the registry pattern (1.2) with their own keys, for example `music.play`, `music.skip`, `music.queue`, and a request could also come through the conversational lane ("play X").
  - Needs a queue per server, a leave-when-alone rule, and a rule for who may control playback.
- Settings mechanism follow-ups (build step 1):
  - Connect `backups.migration_copies` to `MIGRATION_BACKUPS_KEPT` in `migrate.py` when settings are wired into the backup code.
  - A JSON setting's default is a mutable dict or list; copy it on read when the first JSON setting is defined.
  - STR_LIST accepts empty strings and repeated items; decide the rules when `addressing.names` is wired up.
  - Weight lists (comma-separated `value:percent` text, section 10 introduction) have no setting type yet; add one or keep them as TEXT with their own checks when the first such setting arrives.
  - When `/config set` stores a value equal to the default, the reply should say so and mention `/config reset`, because an override equal to the default does not follow later default changes.
- Command layer follow-ups (permissions):
  - Validate that the target of `/perm` and `/block` is a real, non-bot member and that IDs are positive before calling the permission store.
  - `/block` and `/unblock` need a scope option (this server or everywhere); the reply lists the block rows that remain.
  - Confirm before a wildcard `/perm` command that would clear existing rows, listing the rows and offering Confirm and Cancel.
  - `/perm undo`: restore the rows a wildcard command cleared, using the audit log entries it wrote.
  - Show stored text (audit notes, keys and patterns containing `*`) in Discord with the characters escaped or in code formatting, so an asterisk is not read as Markdown italics.

---

## 9. Database Tables

Table designs settled (build steps 1 to 4, plus mood and relationships in step 7, persona notes and recent uses in step 8, nicknames in step 10, one-time events in step 12, and traits and memories in step 13). Later steps add their own tables through numbered migrations (9.9). The SQL below was run against SQLite to confirm it is valid.

### 9.1 Conventions

- Discord IDs are stored as `INTEGER` (SQLite integers are 64-bit, which fits Discord's IDs).
- Times are UTC Unix seconds (a whole number). Cooldown and recovery math is subtraction, and the bot converts to local time only when it displays a time.
- `guild_id = 0` means every server. A SQLite `UNIQUE` rule treats two `NULL` values as different, so `0` is used for "everywhere". When a server-specific row and a global row match the same key, the server-specific row wins.
- Values in `settings` and `bot_state` are stored as text and converted by type when read. Lists are stored as JSON text. The setting types are `bool`, `int`, `float`, `text`, `time` (HH:MM), `list[int]`, `list[str]` and `json` (stored as JSON text and parsed on read).
- Every structure change is a numbered migration file. Nobody edits the database structure by hand.
- Decimal math in SQL multiplies by `1.0` before dividing, because SQLite divides two integers as integers (this affects every decay formula).
- Code follows PEP 8 (section 5).

### 9.2 `users`

Stores the permission level of each person who has level 1 to 3. A person with no row is level 0, and the owner is never stored (the config file decides).

```sql
CREATE TABLE users (
    discord_id  INTEGER PRIMARY KEY,
    level       INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
    updated_at  INTEGER NOT NULL
);
```

- Level 3 is Admin. The owner sits above the ladder and passes every check.
- `/perm level user:@x level:n` (owner-only) sets a level. Setting level 0 deletes the row. The command rejects bot accounts and writes an `audit_log` row (kind `level`).
- `updated_at` stays even though the audit log holds the history, so the last change is visible in the table itself. The row and its audit entry are written in one transaction.
- The lookup reads one row by primary key on each command, with no cache.

```python
DEFAULT_LEVEL = 0


def get_level(user_id: int) -> int:
    """Return the user's level, or DEFAULT_LEVEL if they have no row."""
    row = db.execute(
        "SELECT level FROM users WHERE discord_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return DEFAULT_LEVEL
    return row["level"]
```

### 9.3 `permission_overrides`

One row per person, key, and server. `allow` or `deny` replaces the level for that key.

```sql
CREATE TABLE permission_overrides (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    key       TEXT NOT NULL,
    effect    TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    guild_id  INTEGER NOT NULL DEFAULT 0,
    set_by    INTEGER NOT NULL,
    set_at    INTEGER NOT NULL,
    UNIQUE (user_id, key, guild_id)
);
```

Example rows:

| user | key | effect |
|---|---|---|
| Friend A | `*` | deny |
| Friend A | `cat.view` | allow |
| Friend B | `feed.*` | allow |
| Friend B | `feed.remove` | deny |
| Friend C | `chat.talk` | deny |

Friend A can use only `cat.view` among the overridable keys. Friend B can use every feed command except `feed.remove`. Friend C gets no conversation, and commands still work.

- The `UNIQUE` rule means a second grant or deny on the same key overwrites the first row.
- A wildcard command runs in one transaction: it deletes the person's exact rows that the wildcard covers, writes the wildcard row, writes one audit row per change, and returns the list of cleared rows for the bot's reply.
- A command on `*` also removes the person's `area.*` rows in the same server scope, because those rows sit beneath `*` in the check order.
- A wildcard command that would clear existing rows can show them first and ask for confirmation (the command layer reads them with `find_covered_overrides`). For a reversible way to shut a person off, use `/block`, which leaves override rows untouched.
- Broad command first, exceptions after: `/perm deny feed.*` then `/perm grant feed.remove` leaves `feed.*` deny and `feed.remove` allow. The reverse order leaves only `feed.*` deny.
- There is no foreign key to `users`, because a level-0 person has override rows and no `users` row.
- `/perm show @x` displays the expanded result for every key, with the row or level that decided it ("Decided by" column), computed with the same lookup the real check uses.

### 9.4 `blocks`

Two kinds of block, kept apart from the override rows.

```sql
CREATE TABLE blocks (
    user_id   INTEGER NOT NULL,
    guild_id  INTEGER NOT NULL DEFAULT 0,
    kind      TEXT NOT NULL CHECK (kind IN ('chat', 'all')),
    set_by    INTEGER NOT NULL,
    set_at    INTEGER NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);
```

- `chat`: the bot does not converse with the person (`chat.talk` refused). Commands work.
- `all`: conversation and every command are refused, apart from protected keys (1.2).
- One block per person per server. Choosing another kind overwrites the row.
- A person can have a global row and a server row at once. Both apply, and the stricter kind wins (`all` over `chat`), so a block set in one server never loosens a global block. To block someone in one server only, set the block in that server and leave no global row.
- Removing a global block removes only the global row, and removing a block in one server removes only that server's row. The `/unblock` reply lists the block rows that remain.
- A block never touches override rows, so `/unblock` restores the person exactly as they were.
- A blocked person's messages, and those of a person with a `chat.talk` deny override, are dropped in the intake guards, before classification: no reply, no reaction, no mood, relationship, trait, or memory writes, and no place in sessions or batches. Stored data stays.
- A slash command that is refused returns an ephemeral line from the fixed `denied` pool (3.9), or "You don't have permission for that." until that pool is built. Discord requires a response to every command.
- Commands: `/block user:@x kind:chat|all` and `/unblock user:@x`, both under the fixed `perm.*` keys.

### 9.5 `audit_log`

A permanent history of owner changes. The bot only adds rows.

```sql
CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY,
    logged_at  INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    actor_id   INTEGER NOT NULL,
    target_id  INTEGER,
    key        TEXT,
    guild_id   INTEGER NOT NULL DEFAULT 0,
    old_value  TEXT,
    new_value  TEXT,
    note       TEXT
);

CREATE INDEX audit_log_target ON audit_log (target_id, logged_at);
```

- `kind` is `level`, `override`, `block`, `setting`, `photo` (a removed photo, 2.4), `memory` (a deleted memory, without its text, 9.16), `relationship` (an owner change, 3.4), or `nickname` (a change to someone else's list, 3.6), or `say` (a message posted with `/say`, 3.18). More kinds will follow (event resets). The column has no `CHECK` rule because SQLite cannot change one without rebuilding the table, so the code validates kinds from a constant list.
- A NULL `old_value` or `new_value` means "no row".
- A wildcard command writes one row for the wildcard and one row for each exact row it clears (note: "cleared by `/perm deny *`"). `/perm history @x` lists everything that happened to one person.
- The change and its audit row are written in one transaction.
- Retention: keep everything.
- Level, override, and block entries are copied to `#permissions`, settings entries to `#bot-log` (3.18).

### 9.6 `settings`

Only overrides live in the table. Defaults live in the code.

```sql
CREATE TABLE settings (
    key         TEXT NOT NULL,
    guild_id    INTEGER NOT NULL DEFAULT 0,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    updated_by  INTEGER NOT NULL,
    PRIMARY KEY (key, guild_id)
);
```

Each setting is declared once in code:

```python
SettingDefinition(
    key="presence.idle_minutes",
    category="presence",
    level=Level.BASIC,
    server_scope=False,
    value_type=int,
    default=10,
    minimum=1,
    maximum=120,
    description="Minutes without activity before the bot goes idle.",
)
```

- Lookup order: server row, global row, code default. The value is read from the database each time it is needed.
- `/config set` converts the text to the declared type, checks the range, writes the row and an audit entry (kind `setting`) in one transaction, and posts to `#bot-log`.
- `/config reset <key>` deletes the row, and the default applies again.
- `/config list` and `/help` build from the definitions. The full list of definitions is section 10.

### 9.7 `cooldowns`

A row means "this behavior is blocked for this scope until this time."

```sql
CREATE TABLE cooldowns (
    scope       TEXT NOT NULL,
    scope_id    INTEGER NOT NULL DEFAULT 0,
    key         TEXT NOT NULL,
    expires_at  INTEGER,
    PRIMARY KEY (scope, scope_id, key)
);
```

- `scope` is `user`, `channel`, `server`, or `global` (`scope_id` 0). An empty `expires_at` never expires.
- Example rows: `user | Alex | nickname_use`, `user | Alex | memory_callback:14`, `channel | #general | split_reply`.
- A behavior is on cooldown when a row exists and `expires_at` is empty or in the future. Durations come from settings.
- One statement checks and starts a cooldown together, so two simultaneous triggers cannot both pass. If it changes one row, the bot got the slot; if it changes zero, the cooldown was active:

```sql
INSERT INTO cooldowns (scope, scope_id, key, expires_at)
VALUES (:scope, :scope_id, :key, :new_expiry)
ON CONFLICT (scope, scope_id, key) DO UPDATE
SET expires_at = excluded.expires_at
WHERE cooldowns.expires_at IS NOT NULL
  AND cooldowns.expires_at <= :now;
```

- A task deletes expired rows hourly.
- All cooldowns are time-based, including the typo cooldown. Rules that count uses (a canned line cannot repeat until several others have been used) use the `recent_uses` table (9.21).

### 9.8 `bot_state`

Small values the bot writes about itself.

```sql
CREATE TABLE bot_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
```

- Keys so far: `heartbeat` (updated each minute; the startup comparison measures downtime), `first_boot`, `last_seen_old_account`. The mood state is `mood.energy`, `mood.happiness`, `mood.baseline_energy`, `mood.baseline_happiness`, `mood.updated_at`, and `mood.reroll_at`, and the post-wake sleepy counter is `sleep.groggy_remaining` and `sleep.groggy_until` (9.14). The sleep state keys are `sleep.state` (`awake` or `asleep`), `sleep.wake_at` (planned wake time while asleep), `sleep.override_until` (set by `/wake`; she stays awake until the next scheduled sleep window), `sleep.early_woken` (1 after an early wake, cleared at the planned wake time), and `sleep.catchup_from` (the time after which the wake-time catch-up reads messages, 3.12). A new key needs no migration.
- To clear a key, delete its row with `delete_state`. An empty value is never used to mean "cleared"; a missing row means the key is not set (for example `sleep.early_woken` after the planned wake time).
- Kept apart from `settings` because settings are values the owner tunes, and each change creates an audit entry and a log post. A heartbeat every minute would add 1,440 audit rows per day and put values in `/config list` that nobody should edit.

### 9.9 `schema_migrations` and migrations

```sql
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  INTEGER NOT NULL
);
```

- Migrations are numbered SQL files in a `migrations/` folder (`0001_foundation.sql` creates the tables above, then `0002_reminders.sql` for 9.10 and 9.11, `0003_feeds.sql` for 9.12, `0004_cats.sql` for 9.13, and so on). `schema_migrations` is the checklist of files already applied to a database.
- At startup, before connecting to Discord: open the database (foreign keys on, WAL mode), create `schema_migrations` if missing, find files not yet applied, back up the database if any are pending, apply each file in one transaction and record it. A failing file rolls back, the error goes to the Docker log, and the bot stops.
- A migration that has run on any database is never edited. A change goes in a new file.
- The test and production databases keep separate version records, and a new file runs on the test database first.
- No undo files. Rollback means restoring the pre-migration backup (section 5).
- Adding a column is one `ALTER TABLE` line. Changing a column's rules requires the rebuild pattern: create the new table, copy the rows, drop the old table, rename.
- A test builds a fresh database from all the files and checks the result.

### 9.10 `reminders`

One row per reminder. Build step 2.

```sql
CREATE TABLE reminders (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    message        TEXT NOT NULL,
    due_at         INTEGER NOT NULL,
    delivery       TEXT NOT NULL CHECK (delivery IN ('dm', 'channel')),
    channel_id     INTEGER,
    created_at     INTEGER NOT NULL,
    repeat_rule    TEXT,
    timezone       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'delivered', 'failed', 'cancelled')),
    delivered_at   INTEGER,
    delivered_via  TEXT CHECK (delivered_via IN ('dm', 'channel'))
);

CREATE INDEX reminders_due ON reminders (status, due_at);
```

Example rows (times shortened to labels for reading; the table stores Unix seconds):

| id | user | message | due_at | delivery | channel_id | repeat_rule | timezone | status | delivered_at | delivered_via |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Owner | submit CS assignment | 1793311200 | dm | #feeds | empty | empty | pending | empty | empty |
| 2 | Friend A | take out the trash | 1793714400 | channel | #cats | `FREQ=WEEKLY;BYDAY=TU` | America/New_York | pending | 1793300000 | channel |
| 3 | Owner | pay rent | 1793433600 | dm | #feeds | `FREQ=MONTHLY;BYMONTHDAY=-1` | America/Los_Angeles | pending | empty | empty |
| 4 | Friend B | call grandma | 1793290000 | dm | #cats | empty | empty | delivered | 1793290002 | dm |
| 5 | Friend B | check the oven | 1793291000 | dm | #cats | empty | empty | failed | empty | empty |

- `due_at` is the next time the reminder fires, in UTC seconds. The loop reads rows with `status = 'pending'` and `due_at <= now`; the index `reminders_due` serves that query.
- `channel_id` is the channel where the reminder was set. A `channel` delivery posts there, and a `dm` delivery uses it for the failure notice, which carries no reminder text. It is empty when the reminder was set in a DM.
- `repeat_rule` is empty for a one-time reminder. For a repeating one, the next due time is computed with python-dateutil from the current `due_at` (as the start) in the row's `timezone`. The row then moves to the next due time and stays `pending`, and `delivered_at` and `delivered_via` record the last delivery (row 2).
- `timezone` is an IANA name copied from the person's `user_settings` when the reminder is created. It is empty for one-time reminders, which need no calendar math.
- `status`: `pending` (waiting), `delivered` (a one-time reminder that was sent), `failed` (the DM failed; a failure notice without the text was posted in `channel_id` when possible), `cancelled` (removed with `/remind cancel`). Rows are kept after delivery.
- `/remind list` reads the person's `pending` rows and their `failed` rows from the last 7 days. `/remind cancel` sets `cancelled`.

### 9.11 `user_settings`

Per-person preferences. The same pattern as `settings` (9.6) with a person in place of a server.

```sql
CREATE TABLE user_settings (
    user_id     INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
);
```

| user_id | key | value | updated_at |
|---|---|---|---|
| Owner | timezone | America/Los_Angeles | 1793300000 |
| Friend A | timezone | America/New_York | 1793300500 |

- Only values a person has set are stored. Each key is declared once in code with its type, default, and description, like `SettingDefinition` (9.6).
- The first keys are `timezone` and `birthday` (month and day, entered with `/birthday set`, used by calendar events in 3.15). Later keys need no migration.
- Changes to a person's own settings stay out of the audit log, which covers owner changes (9.5).
- Commands: `/timezone set` and `/birthday set` (`prefs.set`) and `/timezone show` (`prefs.view`), all level 0.

### 9.12 Feed tables

Five tables. Build step 3. Poll details and commands are in 2.1.

```sql
CREATE TABLE sources (
    id                    INTEGER PRIMARY KEY,
    type                  TEXT NOT NULL,
    param                 TEXT NOT NULL,
    name                  TEXT NOT NULL,
    extra                 TEXT,
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_by            INTEGER NOT NULL,
    created_at            INTEGER NOT NULL,
    last_checked_at       INTEGER,
    last_success_at       INTEGER,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (type, param)
);

CREATE TABLE feed_items (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    item_id       TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    summary       TEXT,
    published_at  INTEGER,
    event_at      INTEGER,
    first_seen_at INTEGER NOT NULL,
    extra         TEXT,
    UNIQUE (source_id, item_id)
);

CREATE TABLE subscriptions (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    filters     TEXT,
    start_at    INTEGER NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    UNIQUE (source_id, channel_id)
);

CREATE TABLE feed_notify (
    subscription_id  INTEGER NOT NULL
                     REFERENCES subscriptions (id) ON DELETE CASCADE,
    user_id          INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    keywords         TEXT NOT NULL DEFAULT '[]',
    created_at       INTEGER NOT NULL,
    PRIMARY KEY (subscription_id, user_id)
);

CREATE TABLE feed_posts (
    id               INTEGER PRIMARY KEY,
    subscription_id  INTEGER NOT NULL
                     REFERENCES subscriptions (id) ON DELETE CASCADE,
    item_id          INTEGER NOT NULL REFERENCES feed_items (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    message_id       INTEGER NOT NULL,
    posted_at        INTEGER NOT NULL,
    UNIQUE (subscription_id, item_id, kind)
);

CREATE INDEX feed_posts_message ON feed_posts (message_id);
```

What each table holds:

| Table | One row is | Written by |
|---|---|---|
| `sources` | one thing watched (a Steam game, a Lodestone category, an RSS feed) | `/feed subscribe` when the source is new; the poll updates its health columns |
| `feed_items` | one item the poll found | the poll |
| `subscriptions` | one channel following one source | `/feed subscribe` |
| `feed_notify` | one person's ping entry for one subscription (on or off, plus ping words) | `/feed notify`, `/feed pingwords`, the ping controls |
| `feed_posts` | one message the bot sent for one item in one subscription | the posting step |

Example rows (Discord IDs and times shortened; the table stores integers):

`sources`

| id | type | param | name | extra | enabled | created_by | created_at | last_checked_at | last_success_at | consecutive_failures |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | ffxiv | maintenance | FFXIV Maintenance | empty | 1 | Owner | 1793300000 | 1793386300 | 1793386300 | 0 |
| 2 | steam | 570 | Dota 2 | `{"maxlength": 300}` | 1 | Friend A | 1793300050 | 1793386300 | 1793386300 | 0 |
| 3 | anime | `https://example.com/anime-news.rss` (placeholder) | Anime News | empty | 1 | Owner | 1793300090 | 1793386300 | 1793380000 | 2 |

`feed_items`

| id | source_id | item_id | title | published_at | event_at | first_seen_at |
|---|---|---|---|---|---|---|
| 1 | 2 | 8001 | Patch 7.38 Notes | 1793299000 | empty | 1793300060 |
| 2 | 2 | 8002 | Holiday Event Begins | 1793298000 | empty | 1793300060 |
| 3 | 2 | 8003 | Hotfix 7.38b | 1793380000 | empty | 1793380100 |
| 4 | 1 | m-2026-1103 | Scheduled Maintenance (All Worlds) | 1793383000 | 1793700000 | 1793383100 |

`subscriptions`

| id | source_id | guild | channel | filters | start_at | enabled |
|---|---|---|---|---|---|---|
| 1 | 2 (Dota 2) | private server | #feeds | `{"keywords": ["patch", "hotfix"]}` | 1793300100 | 1 |
| 2 | 1 (FFXIV) | private server | #feeds | empty | 1793300000 | 1 |
| 3 | 2 (Dota 2) | friend server | #games | empty | 1793350000 | 1 |

`feed_notify`

| subscription_id | user_id | enabled | keywords | created_at |
|---|---|---|---|---|
| 1 | Owner | 1 | `[]` | 1793300200 |
| 1 | Friend A | 1 | `["hotfix"]` | 1793300300 |
| 2 | Owner | 1 | `[]` | 1793300400 |
| 2 | Friend A | 0 | `["maintenance"]` | 1793300500 |

`feed_posts`

| id | subscription_id | item_id | kind | message_id | posted_at |
|---|---|---|---|---|---|
| 1 | 1 | 3 | item | 9000000000000000001 | 1793380200 |
| 2 | 2 | 4 | item | 9000000000000000002 | 1793383200 |
| 3 | 3 | 3 | item | 9000000000000000003 | 1793380200 |
| 4 | 2 | 4 | reminder | 9000000000000000004 | 1793696400 |

How the example plays out:

- Items 1 and 2 were found at 1793300060, before subscription 1 (start_at 1793300100) and subscription 3 (start_at 1793350000) existed, so neither subscription posts them. Item 3 arrived later, so both post it, and subscription 1 also checks it against its keyword list ("hotfix" matches).
- Subscription 1 pings Owner (no ping words, so every item) and Friend A (item 3 matches "hotfix") when item 3 posts. Item 4's reminder pings Owner only, because Friend A's entry on subscription 2 is off and keeps its word.
- Subscription 3 has no `feed_notify` rows, so its post is silent.
- Item 4 has an `event_at`, so a second post of kind `reminder` goes out one hour before it (row 4). The unique rule on subscription, item, and kind stops either post from repeating.
- When someone reacts or presses a button on a bot message, the bot looks up the message in `feed_posts` (index `feed_posts_message`) to find the subscription, then adds or removes that person's `feed_notify` row.
- Source 3 shows the poll health columns: two failed polls in a row. A success resets the count. A threshold in settings decides when a warning goes to `#bot-log`.

The query that finds the items a subscription still has to post (the keyword filter runs in code afterward):

```sql
SELECT i.*
FROM subscriptions s
JOIN feed_items i ON i.source_id = s.source_id
WHERE s.id = :subscription_id
  AND s.enabled = 1
  AND i.first_seen_at > s.start_at
  AND NOT EXISTS (
      SELECT 1 FROM feed_posts p
      WHERE p.subscription_id = s.id
        AND p.item_id = i.id
        AND p.kind = 'item'
  )
ORDER BY i.first_seen_at;
```

- `extra` columns hold JSON text for type-specific details, so a new source type needs no migration. Which fields Steam and Lodestone items carry is confirmed with real calls during step 3.
- Deleting a source deletes its items and subscriptions (and their notify and post rows) through `ON DELETE CASCADE`.
- **Filters and re-enabling.** The keyword filter runs after the query, and an item it rejects gets no `feed_posts` row. Changing a subscription's filters or turning a disabled subscription back on therefore sets its `start_at` to the current time, so items skipped earlier never post later as a backlog.
- Turning pings off sets `enabled = 0` and keeps the ping words. A `feed_notify` row is deleted only when the subscription goes away.
- The ping lookup selects the entries to check, and the ping words are matched in code with the same function as the channel filter:

```sql
SELECT user_id, keywords FROM feed_notify
WHERE subscription_id = :subscription_id AND enabled = 1;
```

- A person with an `all` block is skipped after the lookup (1.2). Adding a ping word sets `enabled = 1`, and removing the last word sets `enabled = 0`.

### 9.13 `cats`, `photos`, and `photo_cats`

Three tables. Build step 4. Behavior and commands are in 2.4.

```sql
CREATE TABLE cats (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_by  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE photos (
    id             INTEGER PRIMARY KEY,
    sha256         TEXT NOT NULL UNIQUE,
    ext            TEXT NOT NULL,
    original_name  TEXT,
    size_bytes     INTEGER NOT NULL,
    uploader_id    INTEGER NOT NULL,
    added_at       INTEGER NOT NULL
);

CREATE TABLE photo_cats (
    photo_id  INTEGER NOT NULL REFERENCES photos (id) ON DELETE CASCADE,
    cat_id    INTEGER NOT NULL REFERENCES cats (id) ON DELETE RESTRICT,
    PRIMARY KEY (photo_id, cat_id)
);

CREATE INDEX photo_cats_cat ON photo_cats (cat_id);
```

`cats`

| id | name | created_by | created_at |
|---|---|---|---|
| 1 | Buttons | Owner | 1793300000 |
| 2 | Mochi | Owner | 1793300005 |
| 3 | Pickles | Friend A | 1793386400 |

`photos`

| id | sha256 | ext | original_name | size_bytes | uploader_id | added_at |
|---|---|---|---|---|---|---|
| 1 | 5feceb66ffc8… | jpg | IMG_2041.jpg | 2481920 | Owner | 1793300100 |
| 2 | 6b86b273ff34… | jpg | IMG_2042.jpg | 1930244 | Owner | 1793300110 |
| 3 | d4735e3a265e… | png | buttons_and_mochi.png | 3120377 | Friend A | 1793386500 |
| 4 | 4e07408562be… | jpeg | 20260921_nap.jpeg | 845112 | Friend B | 1793472900 |

`photo_cats`

| photo_id | cat_id |
|---|---|
| 1 | 1 |
| 2 | 1 |
| 3 | 1 |
| 3 | 2 |
| 4 | 3 |

- The hash column holds 64 characters; the examples show 12. The file on disk is `<sha256>.<ext>`.
- `name` is unique and ignores capitalization. `original_name` is for display only.
- Photo 3 shows two cats: it has two `photo_cats` rows. `/cat Buttons` returns photos 1, 2, and 3. `/cat Mochi` returns photo 3.
- Per-cat counts come from `photo_cats` (Buttons 3, Mochi 1, Pickles 1). The total for milestones comes from `photos` (4). The two do not match because photo 3 counts for two cats.
- Removing a photo deletes its `photos` row, and `ON DELETE CASCADE` deletes its links. A cat with links cannot be deleted (`ON DELETE RESTRICT`), so `/removecat` works only on a cat with no photos.
- Adding a file that already exists inserts the missing link instead of a second photo: `INSERT OR IGNORE INTO photo_cats VALUES (:photo_id, :cat_id)`.
- Removal order: delete the rows and write the audit entry in one transaction, then delete the file. A startup check lists files that have no row.
- The cat list query for a random photo: `SELECT p.* FROM photos p JOIN photo_cats pc ON pc.photo_id = p.id WHERE pc.cat_id = :cat_id ORDER BY RANDOM() LIMIT 1`.
- Aliases are not stored. A later migration can add them.

### 9.14 `mood_events` and mood state

Build step 7. The current mood lives in `bot_state` (9.8). The table is the explanation log, and it is shared with the relationship system through `d_affinity`.

```sql
CREATE TABLE mood_events (
    id               INTEGER PRIMARY KEY,
    created_at       INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    user_id          INTEGER,
    guild_id         INTEGER,
    channel_id       INTEGER,
    message_id       INTEGER,
    d_energy         REAL NOT NULL DEFAULT 0,
    d_happiness      REAL NOT NULL DEFAULT 0,
    d_affinity       REAL NOT NULL DEFAULT 0,
    scale            REAL NOT NULL DEFAULT 1.0,
    energy_after     REAL NOT NULL,
    happiness_after  REAL NOT NULL,
    note             TEXT
);

CREATE INDEX mood_events_created ON mood_events (created_at);
CREATE INDEX mood_events_user ON mood_events (user_id, created_at);
```

- `d_energy`, `d_happiness`, and `d_affinity` hold the **raw** change. The applied change is the raw change times `scale` (negative events take the person's tier scale; every other event stores 1.0). `energy_after` and `happiness_after` hold the mood state right after the event. `kind_message` and `chat_neutral` carry only `d_affinity`. A gain cut by a daily limit (3.4) is written as the amount actually granted, so the sums in the limit query match what was applied, and a gain cut to zero writes no row.
- A row is written whenever any raw change is nonzero, even when the applied effect is zero. A close friend's banter therefore leaves a row, and the "overdone teasing" rule (3.4) counts those rows. Rerolls and `/mood set` always get a row.
- `reason` has no `CHECK` rule. The code validates it against a constant list (as with `audit_log` kinds, 9.5).
- System events (outage, reroll, early wake) leave the user and message columns empty. A message in a DM has `channel_id` and `message_id` filled and `guild_id` empty; the link then uses `@me`. `note` holds free text, such as the old and new baselines for a reroll.
- The person's current affinity lives in `relationships` (9.15). Log rows older than 30 days are deleted, and the affinity value stays.

State keys in `bot_state`: `mood.energy`, `mood.happiness`, `mood.baseline_energy`, `mood.baseline_happiness`, `mood.updated_at`, `mood.reroll_at`, `sleep.groggy_remaining`, `sleep.groggy_until`. They are written together in one transaction.

**Applying an event.** Steps 1 to 4 run inside one `BEGIN IMMEDIATE` transaction. SQLite takes the write lock at the start, so a second event handled at the same moment waits, then reads the updated state. It cannot pass the cooldown or the daily limit a second time, and it cannot overwrite the mood state from a stale read. Concurrent events are unlikely in a small server, so this is a precaution.
1. If the reason has a cooldown, claim it with the statement in 9.7 (key `mood_event:<reason>`, scope `user`). Zero rows changed means the cooldown was active, and the event is dropped.
2. Read the current mood state with the drift formula. For a positive affinity gain, also run the daily-limit query below and cut the gain to what the limits allow.
3. Compute the applied change (raw × scale), add it, and clamp to ±5.
4. Write the mood state keys, update the person's `relationships` row (9.15) with the affinity change, and write the `mood_events` row.

Example (baselines 0.5 energy and +1.0 happiness; times in seconds from the start; the affinity values are the placeholders from 3.4):

| id | Seconds | Reason | Who | d_energy | d_happiness | scale | applied happiness | d_affinity | happiness_after | Mood name |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0 | `rude_message` | stranger | 0 | -1.0 | 1.0 | -1.0 | -1.0 | 0.0 | neutral |
| 2 | 300 | `rude_message` | stranger | 0 | -1.0 | 1.0 | -1.0 | -1.0 | -0.98 | neutral |
| 3 | 330 | `bad_bot` | stranger | 0 | -1.0 | 1.0 | -1.0 | -0.5 | -1.98 | neutral |
| | 340 | `bad_bot` | stranger | blocked by the cooldown | | | | | | |
| 4 | 400 | `rude_message` | friend | 0 | -1.0 | 0.5 | -0.5 | -1.0 | -2.46 | grumpy |
| 5 | 600 | `good_bot` | owner | 0 | +1.0 | 1.0 | +1.0 | 0 | -1.42 | neutral |
| 6 | 620 | `chat_activity` | friend | +0.2 | 0 | 1.0 | 0 | 0 | -1.42 | neutral |

Three hours after the last event the state is energy 0.6 and happiness -0.21. The owner has no `relationships` row, so `d_affinity` is 0 for the owner.

The "why are you grumpy" query:

```sql
SELECT reason, user_id, guild_id, channel_id, message_id,
       d_happiness * scale AS applied
FROM mood_events
WHERE created_at >= :since AND d_happiness * scale < 0
ORDER BY d_happiness * scale ASC, created_at DESC
LIMIT 1;
```

In the example it returns row 3 (`bad_bot`; the three drops of -1.0 tie and the latest wins). The message link is `https://discord.com/channels/<guild>/<channel>/<message>`.

The daily gain limits (3.4): affinity already granted to one person in the last 24 hours.

```sql
SELECT COALESCE(SUM(CASE WHEN reason = 'chat_neutral' THEN d_affinity END), 0) AS neutral_24h,
       COALESCE(SUM(d_affinity), 0) AS positive_24h
FROM mood_events
WHERE user_id = :user_id
  AND d_affinity > 0
  AND created_at >= :since;
```

The gain granted is the smaller of the raw gain, `relationship.neutral_daily_cap - neutral_24h` (neutral gains only), and `relationship.positive_daily_cap - positive_24h`. A neutral gain also stops when stored affinity is below 0 or has reached `relationship.neutral_cap`.

The "overdone teasing" count (3.4):

```sql
SELECT COUNT(*)
FROM mood_events
WHERE user_id = :user_id
  AND reason IN ('rude_message', 'bad_bot', 'spam')
  AND created_at >= :since;
```

The daily cleanup: `DELETE FROM mood_events WHERE created_at < :now - :retention_seconds`.

### 9.15 `relationships`

Build step 7. One row per person, created at the first event or visit. No row means the defaults (affinity 0, familiarity 0). The owner has no row.

```sql
CREATE TABLE relationships (
    user_id           INTEGER PRIMARY KEY,
    affinity          REAL NOT NULL DEFAULT 0,
    baseline          REAL NOT NULL DEFAULT 0,
    familiarity       REAL NOT NULL DEFAULT 0,
    peak_familiarity  REAL NOT NULL DEFAULT 0,
    last_negative_at  INTEGER,
    last_counted_at   INTEGER,
    updated_at        INTEGER NOT NULL
);
```

Example rows (tiers use the placeholder minimums in 3.4; "familiarity now" is what the bot computes when it reads the row):

| user | affinity | baseline | familiarity (stored) | peak | last counted visit | familiarity now | tier |
|---|---|---|---|---|---|---|---|
| Friend A | 7.2 | 6 | 34 | 36 | 2 days ago | 34.0 | close friend |
| Friend B | 2.0 | 1 | 5 | 5 | 10 days ago | 5.0 | acquaintance |
| Friend C | -3.4 | 0 | 12 | 12 | 20 days ago | 12.0 | `disliked` (affinity at or below -3) |
| Friend D | 4.5 | 3 | 40 | 40 | 365 days ago | 10.6 | friend |

- `familiarity` is a decimal because of decay. The stored number stays as written until the person returns.
- **Reading a row.** Affinity: if it is below `baseline`, add the recovery that has accrued (rate × hours since `max(updated_at, last_negative_at + cooldown)`), up to the baseline. Familiarity: apply the decay from 3.4, whose floor depends on the tier of the peak. Neither read writes to the database.
- **Writing a row.** Apply both of those first, then the new change, then set `updated_at`. A negative affinity change (applied value below zero) sets `last_negative_at`. A visit (at least 6 hours after `last_counted_at`) adds 1 to familiarity, raises `peak_familiarity` if needed, and sets `last_counted_at`. Last, if the resulting tier is above stranger and its affinity minimum is higher than `baseline`, `baseline` is raised to that minimum (the ratchet in 3.4).
- Affinity stays between -5 and +10 and familiarity between 0 and 100 (settings).
- The tier is computed with the rule in 3.4 whenever it is needed and is not stored.
- Blocked people get no writes (9.4). Owner changes through `/relationship set` are written to `audit_log` as kind `relationship`.
- `/forget all` leaves this table unchanged.

### 9.16 `memories`

Build step 13. The search index is a second structure over the same rows.

```sql
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '',
    type          TEXT NOT NULL
                  CHECK (type IN ('fact', 'preference', 'event', 'running_joke')),
    importance    INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    source        TEXT NOT NULL CHECK (source IN ('manual', 'auto', 'observed')),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'pending', 'rejected')),
    created_by    INTEGER,
    guild_id      INTEGER,
    channel_id    INTEGER,
    message_id    INTEGER,
    visibility    TEXT NOT NULL DEFAULT 'global' CHECK (visibility IN ('global', 'dm')),
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER,
    expires_at    INTEGER
);

CREATE INDEX memories_user ON memories (user_id, status);
CREATE INDEX memories_expiry ON memories (expires_at);

CREATE VIRTUAL TABLE memories_fts USING fts5(
    text, tags, content='memories', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, text, tags)
    VALUES ('delete', old.id, old.text, old.tags);
END;

CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, text, tags)
    VALUES ('delete', old.id, old.text, old.tags);
    INSERT INTO memories_fts (rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
```

Example rows:

| id | user | text | tags | type | source | importance | created_by |
|---|---|---|---|---|---|---|---|
| 1 | Friend A | Has two cats named Mochi and Buttons | cat pets | fact | manual | 4 | Friend A |
| 2 | Friend A | Studying 日本語 and wants to visit 東京 next spring | japanese travel | event | manual | 3 | Friend B |
| 3 | Friend B | Plays Dota 2 most evenings | dota games | preference | manual | 2 | Friend B |

- `user_id` is who the memory is about. `created_by` is who made it (row 2 was written by another friend). The person it is about can see and delete both.
- `source`: `manual` (`/remember`), `auto` (Layer 3 extraction), `observed` (typo callbacks, mood events, one-time events). `status` supports the Layer 3 review queue; manual memories are `active`.
- `tags` is optional text typed into `/remember`, or written by the model in Layer 3. The search covers `text` and `tags`.
- `visibility` is `global` (usable in any server channel) or `dm` (created from a DM, never mentioned in a server channel, 3.1). It is also the hook for a larger group (1.1).
- **Search.** The trigram tokenizer matches words of 3 or more characters, including Japanese (日本語 is found). Two-character words such as 東京 do not match through the index; the code falls back to `LIKE '%東京%'`, which scans the table and is fast at hundreds of rows. A synonym list edited by the owner expands search words (kitten also searches cat). Trigram needs SQLite 3.34 or newer, so the version in the Docker image is checked at build.
- **Expiry.** A daily task deletes expired memories that are not pinned: `DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < :now AND pinned = 0`.

Queries:

```sql
-- /memories: one page of a person's memories
SELECT id, text, type, importance FROM memories
WHERE user_id = :user_id ORDER BY created_at DESC LIMIT 5 OFFSET :offset;

-- /forget <id> by the person it is about (zero rows deleted: send the generic reply)
DELETE FROM memories WHERE id = :id AND user_id = :user_id;

-- /forget <id> by a manager
DELETE FROM memories WHERE id = :id;

-- /forget all
DELETE FROM memories WHERE user_id = :user_id;

-- search for a reply (:include_dm is 1 in a DM and 0 in a server channel)
SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid
WHERE memories_fts MATCH :query AND m.user_id = :user_id AND m.status = 'active'
  AND (m.visibility = 'global' OR :include_dm = 1)
ORDER BY bm25(memories_fts) LIMIT 5;
```

- Every deletion writes an `audit_log` row in the same transaction: kind `memory`, the actor, the person the memory was about (`target_id`), the key (`memory.forget` or `memory.manage`), the memory ID in `old_value`, and the type and source in `note`. For `/forget all` the count goes in `old_value`. The memory text is not written.
- Rejected review-queue rows stay until a deletion, so Layer 3 does not suggest the same memory again. `/forget all` removes them.

### 9.17 `one_time_events`

Build step 12. Event definitions live in `events.yaml` (3.15); this table records which events fired and for whom.

```sql
CREATE TABLE one_time_events (
    event_key   TEXT NOT NULL,
    user_id     INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL CHECK (state IN ('pending', 'fired', 'disabled')),
    claimed_at  INTEGER NOT NULL,
    fired_at    INTEGER,
    guild_id    INTEGER,
    channel_id  INTEGER,
    message_id  INTEGER,
    variant     TEXT,
    PRIMARY KEY (event_key, user_id)
);
```

- `user_id` is the person who triggered the event, or 0 when no person is involved (milestones, calendar events). An unfired event has no row.
- Claim, in one transaction:

```sql
DELETE FROM one_time_events WHERE state = 'pending' AND claimed_at < :stale;

INSERT INTO one_time_events (event_key, user_id, state, claimed_at)
SELECT :key, :user, 'pending', :now
WHERE NOT EXISTS (SELECT 1 FROM one_time_events WHERE event_key = :key AND state = 'disabled')
  AND (SELECT COUNT(*) FROM one_time_events
       WHERE event_key = :key AND state IN ('pending', 'fired')) < :max_total
ON CONFLICT (event_key, user_id) DO NOTHING;
```

- Zero rows inserted means the claim was refused. After a successful send, `UPDATE one_time_events SET state = 'fired', fired_at = :now, message_id = :mid, variant = :variant WHERE event_key = :key AND user_id = :user AND state = 'pending'`. After a failed send the pending row is deleted. At startup all leftover `pending` rows are deleted.
- `/event reset <key>` deletes the key's rows. `/event disable <key>` writes a `disabled` row (user 0).

### 9.18 `nicknames`

Build step 10.

```sql
CREATE TABLE nicknames (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    nickname    TEXT NOT NULL COLLATE NOCASE,
    source      TEXT NOT NULL CHECK (source IN ('self', 'learned', 'owner')),
    set_by      INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    UNIQUE (user_id, nickname)
);

CREATE INDEX nicknames_user ON nicknames (user_id);
```

- `source`: `self` (`/nickname add`), `learned` (accepted "call me X"), `owner` (set by the owner or an Admin). `set_by` is who made the change.
- The cap of 3 per person is checked in code from a setting. `COLLATE NOCASE` makes "Mochi" and "mochi" the same nickname for one person.

### 9.19 `persona_notes`

Build step 8. The notes channel (3.8) is the editable source of truth; this table and its index are rebuilt from it.

```sql
CREATE TABLE persona_notes (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('fact', 'leaning')),
    origin         TEXT NOT NULL CHECK (origin IN ('owner', 'invented')),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
    topic          TEXT,
    value          TEXT,
    text           TEXT NOT NULL,
    tags           TEXT NOT NULL DEFAULT '',
    guild_id       INTEGER,
    channel_id     INTEGER,
    message_id     INTEGER UNIQUE,
    superseded_by  INTEGER REFERENCES persona_notes (id) ON DELETE SET NULL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE UNIQUE INDEX persona_one_active_per_topic ON persona_notes (topic)
    WHERE topic IS NOT NULL AND status = 'active';

CREATE VIRTUAL TABLE persona_fts USING fts5(
    text, tags, content='persona_notes', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER persona_ai AFTER INSERT ON persona_notes BEGIN
    INSERT INTO persona_fts (rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;

CREATE TRIGGER persona_ad AFTER DELETE ON persona_notes BEGIN
    INSERT INTO persona_fts (persona_fts, rowid, text, tags)
    VALUES ('delete', old.id, old.text, old.tags);
END;

CREATE TRIGGER persona_au AFTER UPDATE ON persona_notes BEGIN
    INSERT INTO persona_fts (persona_fts, rowid, text, tags)
    VALUES ('delete', old.id, old.text, old.tags);
    INSERT INTO persona_fts (rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
```

- `origin` is `owner` for a channel message and `invented` for a lazy opinion (4.2, `message_id` empty). `topic` and `value` are filled when the note has the `topic = value` form.
- The partial unique index allows one active note per topic; any number of superseded rows can share the topic.
- **Adding a note** (one transaction): mark the active row for that topic `superseded`, insert the new row, and set the old row's `superseded_by` to the new ID.
- **Deleting a note** (the channel message was deleted), in one transaction: read the row's `status` and `superseded_by`, and find the row it replaced (`superseded_by` equal to its ID); then delete the row. If the deleted row was `active`, set the row it replaced back to `active`. If the deleted row was already `superseded` (an intermediate note), leave the older row superseded and point its `superseded_by` at the deleted row's successor. The order matters: reactivating before the delete, or reactivating when a newer active note exists, would violate the unique index.
- **Editing** updates the row; the triggers keep the index in step. An edit that changes the topic of an active note is handled as a delete from the old topic (its predecessor becomes active again) followed by an add under the new topic (any active note there becomes superseded). An edit to a `superseded` note changes only its text and tags.

### 9.20 `user_traits` and `trait_signals`

Build step 13.

```sql
CREATE TABLE user_traits (
    user_id     INTEGER NOT NULL,
    trait       TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('keyword', 'model', 'owner')),
    confidence  REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, trait, source)
);

CREATE INDEX user_traits_user ON user_traits (user_id);

CREATE TABLE trait_signals (
    user_id     INTEGER NOT NULL,
    signal      TEXT NOT NULL,
    value       REAL NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, signal)
);
```

- Effective traits for a reply: the owner row if one exists, otherwise the model row, otherwise the keyword row; only traits at or above a minimum confidence count.

```sql
WITH eff AS (
    SELECT trait,
           COALESCE(MAX(CASE WHEN source = 'owner'   THEN confidence END),
                    MAX(CASE WHEN source = 'model'   THEN confidence END),
                    MAX(CASE WHEN source = 'keyword' THEN confidence END)) AS confidence
    FROM user_traits WHERE user_id = :user_id GROUP BY trait
)
SELECT trait, confidence FROM eff
WHERE confidence >= :min_confidence ORDER BY confidence DESC LIMIT 3;
```

- A signal counter decays with a 14-day half-life when it is bumped. Standard SQLite has no `pow()`, so the bot either registers one as a Python function or does the math in Python. The `* 1.0` keeps the division in floating point (9.1).

```sql
INSERT INTO trait_signals (user_id, signal, value, updated_at) VALUES (:user_id, :signal, :amount, :now)
ON CONFLICT (user_id, signal) DO UPDATE SET
    value = trait_signals.value * pow(0.5, (:now - trait_signals.updated_at) * 1.0 / :half_life) + :amount,
    updated_at = :now;
```

### 9.21 `recent_uses`

Build step 8. Supports the rule that a canned line cannot repeat until several others in its pool have been used (3.10).

```sql
CREATE TABLE recent_uses (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL DEFAULT 0,
    pool      TEXT NOT NULL,
    line_id   TEXT NOT NULL,
    used_at   INTEGER NOT NULL
);

CREATE INDEX recent_uses_lookup ON recent_uses (user_id, pool, id);
```

- `user_id` 0 means the pool is shared by everyone. `line_id` is the stable ID of a line in the response file (renaming a line's text does not break the history).
- Pick: read the newest `min(3, pool_size - 1)` `line_id` values for the (user, pool) pair, choose a line outside them, insert a row, then delete all but the newest 10 rows for that pair.

---

## 10. Settings Definitions

Every setting is declared once in code (9.6) with the fields below and shown by `/config`. This section is the draft list at design time; defaults marked TBD are chosen during build and testing, and many values are tuned once each part exists.

- **Naming:** `area.name`, with the unit in the name (`presence.idle_minutes`, `mood.half_life_hours`). Areas mirror the permission key areas where they overlap.
- **Scope:** G is one value everywhere. S may differ per server (listen channels, the announce channel, the blocked-topic list). `/config set` rejects a server scope on a G setting.
- **Level:** Basic settings appear in `/config list` by default. Advanced settings appear with the `all` option.
- **Descriptions:** each key has a one-line description (100 characters or fewer) shown in autocomplete. `/config get <key>` shows the full description, range, default, and current value.
- **Storage:** values are text converted by type (9.1). Lists are JSON text. Weight lists are comma-separated `value:percent` text.
- **Not settings:** the token, database path, and test-or-production flag live in `.env`. Canned lines, `events.yaml`, the nickname blocklist, and the synonym list live in data files.
- **Cross-checks:** listed at the end of the section.

### 10.1 addressing and queue (20)

Who she answers, and when (3.2).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `addressing.listen_channels` | list[int] | .env value | - | S | Basic | Channels she reads. No saved value uses the .env list. A saved empty list turns listening off for that server. |
| `addressing.chat_channels` | list[int] | empty | - | S | Basic | Channels where every message counts as addressed to her. |
| `addressing.names` | list[str] | "Maika", "マイカ" | - | G | Basic | Spellings that count as her name. |
| `addressing.low_cutoff` | float | TBD | 0 to 1 | G | Basic | Score below this: ignore the message. |
| `addressing.high_cutoff` | float | TBD | 0 to 1 | G | Basic | Score at or above this: answer the message. |
| `addressing.session_seconds` | int | 90 | 10 to 600 | G | Advanced | Seconds after her last message that a session stays open. |
| `addressing.max_followups` | int | 5 | 1 to 20 | G | Advanced | Follow-ups allowed without a fresh mention or reply. |
| `addressing.asker_window_seconds` | int | 180 | 10 to 900 | G | Advanced | Seconds the person she answered stays a follow-up candidate. |
| `addressing.typing_extends_window` | bool | true | - | G | Advanced | Typing by that person extends the follow-up window. |
| `addressing.adjacent_window_seconds` | int | 25 | 5 to 120 | G | Advanced | Seconds after her reply when a follow-up scores highest. |
| `addressing.batch_grace_seconds` | float | TBD | 0 to 10 | G | Advanced | Wait after typing stops before batching messages. |
| `addressing.batch_max_wait_seconds` | int | TBD | 1 to 60 | G | Advanced | Longest total wait while batching. |
| `reply.reference_gap_messages` | int | 1 | 1 to 20 | G | Basic | Others' messages in between before she uses a Discord reply. |
| `chat.user_cooldown_seconds` | int | TBD | 0 to 300 | G | Advanced | Minimum gap between replies to one person. |
| `queue.max_depth` | int | TBD | 1 to 50 | G | Advanced | Most messages waiting in one channel's reply queue. |
| `queue.max_age_seconds` | int | 60 | 5 to 600 | G | Advanced | Queued messages older than this are dropped. Applies to the live queue only (sleep catch-up: 3.12). |
| `interject.enabled` | bool | false | - | G | Basic | Name-mention interjections (needs Layer 3). |
| `interject.min_context_messages` | int | 3 | 0 to 20 | G | Advanced | Earlier messages required before she may interject. |
| `interject.probability` | float | TBD | 0 to 1 | G | Basic | Base chance of interjecting, before mood scaling. |
| `interject.channel_cooldown_minutes` | int | TBD | 1 to 720 | G | Advanced | Minimum gap between unprompted replies in a channel. |

### 10.2 commands (9)

Conversational commands (3.17).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `commands.conversational_enabled` | bool | true | - | G | Basic | Turns conversational commands on or off. |
| `commands.act_without_asking` | bool | true | - | G | Basic | Off: every conversational command asks first. |
| `commands.ask_cutoff` | float | TBD | 0 to 1 | G | Advanced | Below this score: ignore. Between the cutoffs: ask. |
| `commands.act_cutoff` | float | TBD | 0 to 1 | G | Advanced | At or above this score with a valid fit: act directly. |
| `commands.confirm_timeout_seconds` | int | TBD | 10 to 600 | G | Advanced | Seconds before confirmation buttons stop working. |
| `nickname.max_count` | int | 3 | 1 to 10 | G | Basic | Nicknames allowed per person. |
| `nickname.max_chars` | int | 40 | 1 to 80 | G | Basic | Longest nickname in characters. |
| `nickname.max_words` | int | 5 | 1 to 10 | G | Basic | Longest nickname in words. |
| `nickname.use_cooldown_minutes` | int | TBD | 0 to 1440 | G | Advanced | Minimum gap between replies that use a nickname. |

### 10.3 mood (31)

Mood values, drift, and events (3.3). Zero-effect rows are not settings.

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `mood.limit` | float | 5.0 | 1 to 10 | G | Advanced | Energy and happiness each range from minus to plus this. |
| `mood.half_life_hours` | float | 3 | 0.5 to 24 | G | Basic | Hours for a mood to return halfway to baseline. |
| `mood.reroll_hours` | float | 6 | 1 to 24 | G | Basic | Hours between baseline rerolls while awake. |
| `mood.baseline_weights_happiness` | text | -2:5,-1:10,0:20,1:30,2:25,3:10 | - | G | Basic | Reroll weights, value:percent pairs. |
| `mood.baseline_weights_energy` | text | TBD | - | G | Advanced | Energy reroll weights, value:percent pairs. |
| `mood.sleepy_energy_max` | float | -3 | -5 to 0 | G | Advanced | Energy at or below this: sleepy. |
| `mood.grumpy_happiness_max` | float | -2 | -5 to 0 | G | Advanced | Happiness at or below this: grumpy. |
| `mood.excited_happiness_min` | float | 2 | 0 to 5 | G | Advanced | Happiness needed for excited. |
| `mood.excited_energy_min` | float | 2 | 0 to 5 | G | Advanced | Energy needed for excited. |
| `mood.cheerful_happiness_min` | float | 2 | 0 to 5 | G | Advanced | Happiness at or above this: cheerful. |
| `mood.groggy_replies` | int | 5 | 0 to 20 | G | Basic | Replies after waking that carry the sleepy flavor. |
| `mood.groggy_minutes` | int | 60 | 1 to 240 | G | Advanced | Minutes after waking before sleepy flavor expires. |
| `mood.event_retention_days` | int | 30 | 1 to 365 | G | Advanced | Days mood_events rows are kept. |
| `mood.event_cooldown_seconds` | int | 90 | 10 to 600 | G | Advanced | Per-person gap between events of the same reason. |
| `mood.why_window_hours` | int | 6 | 1 to 48 | G | Advanced | Hours searched for the largest drop when asked why. |
| `mood.energy_per_reply` | float | 0.2 | 0 to 1 | G | Advanced | Energy gained per reply she sends. |
| `mood.energy_hourly_cap` | float | 1.0 | 0 to 5 | G | Advanced | Most energy chat activity adds per hour. |
| `mood.winddown_max` | float | 2.0 | 0 to 5 | G | Advanced | Energy baseline drop before the sleep window. |
| `mood.winddown_hours` | float | 2 | 0 to 6 | G | Advanced | Hours before sleep that the wind-down covers. |
| `mood.outage_per_hour` | float | -0.5 | -2 to 0 | G | Advanced | Happiness change per hour of downtime. |
| `mood.outage_cap` | float | -3 | -5 to 0 | G | Advanced | Largest total happiness drop from downtime. |
| `mood.event.rude_message.happiness` | float | -1.0 | -5 to 5 | G | Basic | Happiness change for a rough message. |
| `mood.event.spam.happiness` | float | -0.7 | -5 to 5 | G | Basic | Happiness change for a burst of messages. |
| `mood.event.bad_bot.happiness` | float | -1.0 | -5 to 5 | G | Basic | Happiness change for "bad bot". |
| `mood.event.good_bot.happiness` | float | +1.0 | -5 to 5 | G | Basic | Happiness change for "good bot". |
| `mood.event.thanks.happiness` | float | +0.5 | -5 to 5 | G | Basic | Happiness change for thanks. |
| `mood.event.pet.happiness` | float | +0.7 | -5 to 5 | G | Basic | Happiness change for /pet. |
| `mood.event.pet.energy` | float | +0.2 | -5 to 5 | G | Basic | Energy change for /pet. |
| `mood.event.early_wake.happiness` | float | -0.5 | -5 to 5 | G | Basic | Happiness change when woken early. |
| `mood.event.early_wake.energy` | float | -1.5 | -5 to 5 | G | Basic | Energy change when woken early. |
| `pet.cooldown_minutes` | int | TBD | 0 to 1440 | G | Advanced | Per-person gap between /pet uses that count. |

### 10.4 relationship (42)

Affinity, familiarity, and tiers (3.4).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `relationship.affinity_min` | float | -5 | -20 to 0 | G | Advanced | Lowest affinity. |
| `relationship.affinity_max` | float | 10 | 1 to 50 | G | Advanced | Highest affinity. |
| `relationship.familiarity_max` | float | 100 | 10 to 1000 | G | Advanced | Highest familiarity. |
| `relationship.recovery_per_hour` | float | TBD | 0 to 5 | G | Basic | Affinity regained per hour below baseline. |
| `relationship.recovery_delay_minutes` | int | 60 | 0 to 1440 | G | Advanced | Wait after a negative event before recovery starts. |
| `relationship.visit_gap_hours` | float | 6 | 0.5 to 48 | G | Advanced | Hours between visits that each count toward familiarity. |
| `relationship.decay_grace_days` | int | 30 | 0 to 365 | G | Advanced | Quiet days before familiarity starts fading. |
| `relationship.decay_half_life_days` | int | 60 | 1 to 730 | G | Advanced | Days to halve the gap to the floor. |
| `relationship.decay_floor_percent` | int | 25 | 0 to 100 | G | Advanced | Floor as a percent of peak familiarity (never under the tier below the peak's). |
| `relationship.acquaintance_min_affinity` | float | 1 | -5 to 10 | G | Basic | Affinity needed for acquaintance. |
| `relationship.acquaintance_min_familiarity` | float | 3 | 0 to 100 | G | Basic | Familiarity needed for acquaintance. |
| `relationship.friend_min_affinity` | float | 3 | -5 to 10 | G | Basic | Affinity needed for friend. |
| `relationship.friend_min_familiarity` | float | 10 | 0 to 100 | G | Basic | Familiarity needed for friend. |
| `relationship.close_friend_min_affinity` | float | 6 | -5 to 10 | G | Basic | Affinity needed for close friend. |
| `relationship.close_friend_min_familiarity` | float | 30 | 0 to 100 | G | Basic | Familiarity needed for close friend. |
| `relationship.disliked_max_affinity` | float | -3 | -5 to 0 | G | Advanced | Affinity at or below this: disliked. |
| `relationship.name.stranger` | text | Stranger | 1-24 chars | G | Basic | Display name of the stranger tier. |
| `relationship.name.acquaintance` | text | Acquaintance | 1-24 chars | G | Basic | Display name of the acquaintance tier. |
| `relationship.name.friend` | text | Friend | 1-24 chars | G | Basic | Display name of the friend tier. |
| `relationship.name.close_friend` | text | Close friend | 1-24 chars | G | Basic | Display name of the close friend tier. |
| `relationship.name.disliked` | text | Disliked | 1-24 chars | G | Basic | Display name of the lowest tier. |
| `relationship.name.owner` | text | owner | 1-24 chars | G | Basic | Display name of the owner tier. |
| `relationship.negative_scale.owner` | float | 0 | 0 to 1 | G | Advanced | Share of a negative event the owner takes. |
| `relationship.negative_scale.close_friend` | float | 0.25 | 0 to 1 | G | Advanced | Share a close friend takes. |
| `relationship.negative_scale.friend` | float | 0.5 | 0 to 1 | G | Advanced | Share a friend takes. |
| `relationship.negative_scale.acquaintance` | float | 0.75 | 0 to 1 | G | Advanced | Share an acquaintance takes. |
| `relationship.negative_scale.stranger` | float | 1.0 | 0 to 1 | G | Advanced | Share a stranger takes. |
| `relationship.negative_scale.disliked` | float | 1.0 | 0 to 1 | G | Advanced | Share the disliked tier takes. |
| `relationship.overdone_count` | int | 3 | 1 to 20 | G | Advanced | Rough events in the window before banter stops counting. |
| `relationship.overdone_window_minutes` | int | 10 | 1 to 120 | G | Advanced | Window for counting rough events. |
| `relationship.ephemeral` | bool | false | - | G | Basic | Make /relationship replies visible to the sender only. |
| `relationship.neutral_cap` | float | 1.5 | 0 to 10 | G | Basic | Highest affinity that ordinary chat alone can reach. |
| `relationship.neutral_daily_cap` | float | 0.25 | 0 to 5 | G | Basic | Most affinity one person can gain from ordinary chat in 24 hours. |
| `relationship.positive_daily_cap` | float | 0.5 | 0 to 5 | G | Basic | Most affinity one person can gain from all sources in 24 hours. |
| `relationship.gain.chat_neutral` | float | 0.05 | 0 to 1 | G | Advanced | Affinity gained per neutral exchange. |
| `relationship.gain.kind_message` | float | 0.05 | 0 to 1 | G | Advanced | Affinity gained per kind message. |
| `relationship.gain.thanks` | float | 0.10 | 0 to 1 | G | Advanced | Affinity gained for thanks. |
| `relationship.gain.pet` | float | 0.10 | 0 to 1 | G | Advanced | Affinity gained for /pet. |
| `relationship.gain.good_bot` | float | 0.15 | 0 to 1 | G | Advanced | Affinity gained for "good bot". |
| `relationship.loss.rude_message` | float | -1.0 | -5 to 0 | G | Advanced | Affinity change for a rough message, before the tier share. |
| `relationship.loss.spam` | float | -0.3 | -5 to 0 | G | Advanced | Affinity change for a burst of messages, before the tier share. |
| `relationship.loss.bad_bot` | float | -0.5 | -5 to 0 | G | Advanced | Affinity change for "bad bot", before the tier share. |

### 10.5 memory (18)

Memories, traits, and persona (3.5, 3.7).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `memory.confirm_forget` | bool | true | - | G | Basic | Ask for confirmation before /forget deletes. |
| `memory.page_size` | int | 5 | 3 to 10 | G | Advanced | Memories per /memories page. |
| `memory.button_timeout_minutes` | int | TBD | 1 to 60 | G | Advanced | Minutes before /memories buttons stop working. |
| `memory.retrieval_count` | int | 4 | 1 to 10 | G | Basic | Memories injected into a reply. |
| `memory.expiry_days.running_joke` | int | TBD | 1 to 3650 | G | Advanced | Days before a running joke expires. |
| `memory.expiry_days.event` | int | TBD | 1 to 3650 | G | Advanced | Days before an event memory expires. |
| `memory.expiry_days.fact` | int | TBD | 1 to 3650 | G | Advanced | Days before a fact expires. |
| `memory.expiry_days.preference` | int | TBD | 1 to 3650 | G | Advanced | Days before a preference expires. |
| `memory.callback_cooldown_days` | int | TBD | 0 to 365 | G | Advanced | Days before she raises a memory unprompted again. Direct mentions bypass it and restart it. |
| `memory.auto_enabled` | bool | false | - | G | Basic | Automatic memory extraction (Layer 3). |
| `memory.auto_min_importance` | int | TBD | 1 to 5 | G | Advanced | Lowest importance an automatic memory may have. |
| `memory.auto_daily_cap` | int | TBD | 0 to 50 | G | Advanced | Automatic memories per person per day. |
| `memory.review_required` | bool | true | - | G | Advanced | Automatic memories wait in the review queue. |
| `traits.signal_half_life_days` | int | 14 | 1 to 365 | G | Advanced | Half-life of trait signal counters. |
| `traits.min_confidence` | float | TBD | 0 to 1 | G | Advanced | Lowest confidence at which a trait counts. |
| `traits.max_in_prompt` | int | 3 | 1 to 10 | G | Advanced | Traits used per reply. |
| `traits.batch_enabled` | bool | false | - | G | Advanced | Nightly model-assisted trait batch. |
| `traits.batch_sample_size` | int | 50 | 10 to 500 | G | Advanced | Recent messages sampled per person. |

### 10.6 repeat (10)

Repetition control (3.10).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `repeat.window_minutes` | int | 10 | 1 to 120 | G | Advanced | Minutes of quiet that reset the repeat counter. |
| `repeat.enabled` | bool | true | - | G | Basic | Turns the repeat ladder on or off. |
| `repeat.meta_at` | int | 4 | 2 to 20 | G | Basic | Repeat number that gets the meta line. |
| `repeat.emoji_at` | int | 5 | 2 to 20 | G | Basic | Repeat number that gets an emoji only. |
| `repeat.silence_at` | int | 6 | 2 to 30 | G | Basic | Repeat number from which she stays silent. |
| `repeat.group_greetings` | bool | false | - | G | Advanced | Answer a group greeting once. |
| `responses.avoid_recent` | int | 3 | 0 to 10 | G | Advanced | Recent lines per pool a pick must avoid. |
| `responses.history_keep` | int | 10 | 3 to 100 | G | Advanced | Pick history rows kept per person and pool. |
| `llm.recent_replies_in_prompt` | int | 3 | 0 to 8 | G | Advanced | Recent replies shown to the model to avoid repeats. |
| `llm.similarity_threshold` | float | TBD | 0 to 1 | G | Advanced | Word overlap above which a reply is regenerated. |

### 10.7 presence (9)

Status and typing delays (3.11).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `presence.idle_minutes` | int | 10 | 1 to 120 | G | Basic | Minutes without activity before she goes idle. |
| `presence.delays_enabled` | bool | true | - | G | Basic | Turns reading and typing delays on or off. |
| `presence.read_delay_min_seconds` | float | 3 | 0 to 60 | G | Advanced | Shortest pause before typing after idle. |
| `presence.read_delay_max_seconds` | float | 15 | 0 to 120 | G | Advanced | Longest pause before typing after idle. |
| `presence.chat_delay_min_seconds` | float | 0.5 | 0 to 10 | G | Advanced | Shortest pause inside a conversation. |
| `presence.chat_delay_max_seconds` | float | 2 | 0 to 20 | G | Advanced | Longest pause inside a conversation. |
| `presence.online_wait_seconds` | float | 1.5 | 0 to 10 | G | Advanced | Pause after switching from idle to online. |
| `presence.typing_words_per_minute` | int | 100 | 20 to 400 | G | Basic | Typing speed used for the typing indicator. |
| `presence.typing_cap_seconds` | float | 5 | 1 to 30 | G | Basic | Longest typing indicator for one message. |

### 10.8 sleep (13)

Sleep schedule (3.12).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `sleep.enabled` | bool | true | - | G | Basic | Turns the sleep schedule on or off. |
| `sleep.window_start` | time | 00:00 | - | G | Basic | Earliest time sleep can begin. |
| `sleep.window_end` | time | 08:00 | - | G | Basic | End of the window in which sleep can begin. |
| `sleep.timezone` | text | owner's | IANA name | G | Basic | Timezone for the sleep schedule. |
| `sleep.quiet_minutes` | int | 20 | 1 to 180 | G | Advanced | Quiet minutes before she goes to sleep. |
| `sleep.duration_hours` | float | 8 | 1 to 14 | G | Basic | Hours asleep. |
| `sleep.latest_wake` | time | 10:00 | - | G | Basic | Latest time she wakes. |
| `sleep.early_wake_mentions` | int | 2 | 1 to 10 | G | Advanced | Mentions within the window that wake her early. |
| `sleep.early_wake_window_seconds` | int | 60 | 10 to 600 | G | Advanced | Window for counting those mentions. |
| `sleep.pushback_window_seconds` | int | TBD | 10 to 600 | G | Advanced | Second sleep request inside this window goes through. |
| `sleep.catchup_enabled` | bool | true | - | G | Basic | Turns the wake-time catch-up on or off. |
| `sleep.catchup_max_replies` | int | 3 | 1 to 10 | G | Advanced | Most replies she sends to messages from the time she slept. |
| `sleep.catchup_max_age_hours` | int | 12 | 1 to 24 | G | Advanced | Messages older than this are ignored at wake. |

### 10.9 quirks (14)

Typos, self-corrections, and split replies (3.13, 3.14, 3.11).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `typo.enabled` | bool | true | - | G | Basic | Typo events on or off. |
| `typo.probability` | float | 0.015 | 0 to 0.1 | G | Basic | Chance of a typo per eligible reply. |
| `typo.cooldown_minutes` | int | 60 | 1 to 1440 | G | Advanced | Minimum gap between typos. |
| `typo.fix_delay_min_seconds` | float | 2 | 0 to 30 | G | Advanced | Shortest wait before a typo is fixed. |
| `typo.fix_delay_max_seconds` | float | 5 | 0 to 60 | G | Advanced | Longest wait before a typo is fixed. |
| `selfcorrect.enabled` | bool | true | - | G | Basic | Self-corrections on or off. |
| `selfcorrect.probability` | float | 0.005 | 0 to 0.05 | G | Basic | Chance of a self-correction per eligible reply. |
| `selfcorrect.cooldown_minutes` | int | TBD | 1 to 1440 | G | Advanced | Minimum gap between self-corrections. |
| `split.enabled` | bool | true | - | G | Basic | Split replies on or off. |
| `split.length_threshold_chars` | int | TBD | 20 to 2000 | G | Advanced | Reply length above which a split is possible. |
| `split.probability` | float | TBD | 0 to 1 | G | Basic | Base chance of splitting an eligible reply. |
| `split.cooldown_minutes` | int | 45 | 1 to 1440 | G | Basic | Per-channel gap between splits. |
| `split.max_parts` | int | 3 | 2 to 5 | G | Advanced | Most parts in one split reply. |
| `split.interrupt_mode` | text | finish | finish or stop | G | Advanced | What happens when a new message arrives mid-split. |

### 10.10 events (10)

One-time events and calendar (3.15).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `events.timezone` | text | owner's | IANA name | G | Basic | Timezone for calendar events. |
| `events.announce_channel` | int | none | - | S | Basic | Channel for calendar announcements. |
| `events.anniversary_fallback_hour` | int | 21 | 0 to 23 | G | Advanced | Hour the anniversary posts if not yet attached to a reply. |
| `events.catchup_hours` | int | 6 | 0 to 48 | G | Advanced | Hours after the due time a missed event may still post. |
| `events.stale_pending_minutes` | int | TBD | 1 to 60 | G | Advanced | Age at which a pending claim is discarded. |
| `events.dry_run` | bool | false | - | G | Basic | Log events to #events without claiming or posting. |
| `events.photo_milestones` | text | 100,500,1000 | - | G | Advanced | Cat photo counts that trigger a milestone. |
| `events.message_milestone` | int | 1000 | 100 to 100000 | G | Advanced | Answered-message count that triggers a milestone. |
| `events.welcome_back_days` | int | 60 | 7 to 730 | G | Advanced | Days away before a welcome-back line is possible. |
| `events.welcome_back_cooldown_days` | int | 30 | 1 to 365 | G | Advanced | Minimum days between welcome-back lines. |

### 10.11 guardrails (5)

Topics to avoid (3.16).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `guardrails.enabled` | bool | true | - | G | Basic | Topic guardrails on or off. |
| `guardrails.topics` | list[str] | starting list | - | S | Basic | Blocked topics and categories. |
| `guardrails.owner_exempt` | bool | false | - | G | Basic | Owner bypasses the guardrails for testing. |
| `guardrails.regenerate_attempts` | int | 1 | 0 to 3 | G | Advanced | Retries before a canned fallback. |
| `guardrails.crisis_enabled` | bool | false | - | G | Basic | Crisis check-in handler (decided at step 13). |

### 10.12 feeds (13)

Feeds, reminders, and cat photos (2.1, 2.2, 2.4).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `feeds.poll_minutes.ffxiv` | int | TBD | 1 to 1440 | G | Basic | Minutes between FFXIV Lodestone polls. |
| `feeds.poll_minutes.steam` | int | TBD | 1 to 1440 | G | Basic | Minutes between Steam news polls. |
| `feeds.poll_minutes.anime` | int | TBD | 1 to 1440 | G | Basic | Minutes between anime news polls. |
| `feeds.poll_minutes.games` | int | TBD | 1 to 1440 | G | Basic | Minutes between IGDB polls. |
| `feeds.item_retention_days` | int | TBD | 1 to 365 | G | Advanced | Days feed items are kept. |
| `feeds.max_sources_per_person` | int | TBD | 0 to 50 | G | Advanced | Sources one person may create. |
| `feeds.max_pingwords` | int | 10 | 1 to 50 | G | Basic | Ping words per person per subscription. |
| `feeds.failure_warn_after` | int | 3 | 1 to 20 | G | Advanced | Failed polls in a row before a warning. |
| `feeds.maintenance_reminder_minutes` | int | 60 | 5 to 1440 | G | Advanced | Minutes before maintenance that the reminder posts. |
| `reminders.check_seconds` | int | 60 | 10 to 300 | G | Advanced | Seconds between checks for due reminders. |
| `reminders.min_repeat_minutes` | int | TBD | 1 to 1440 | G | Advanced | Shortest allowed repeat interval for a reminder. |
| `reminders.max_pending_per_person` | int | TBD | 1 to 500 | G | Advanced | Pending reminders one person may hold. |
| `cats.max_upload_mb` | int | TBD | 1 to 100 | G | Advanced | Largest photo accepted (Discord limit applies). |

### 10.13 llm (9)

Model access (4.5, 4.6).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `llm.enabled` | bool | false | - | G | Basic | Layer 3 replies on or off. |
| `llm.endpoints` | json | TBD | - | G | Basic | Ordered endpoint list with names, URLs, and budgets. |
| `llm.max_tokens` | int | TBD | 16 to 1024 | G | Advanced | Token cap on one reply. |
| `llm.max_wait_seconds` | float | 20 | 5 to 120 | G | Advanced | Longest total wait for a model reply before a canned one. |
| `llm.max_sentences` | int | 3 | 1 to 10 | G | Advanced | Sentence limit written into the prompt. |
| `llm.status_timeout_seconds` | float | 1 | 0.2 to 10 | G | Advanced | Timeout for endpoint status checks. |
| `llm.idle_timeout_minutes` | int | TBD | 5 to 120 | G | Advanced | Idle time before the 3080 PC sleeps (15 to 30). |
| `llm.max_concurrent` | int | TBD | 1 to 8 | G | Advanced | Model calls running at once. |
| `llm.hosted_endpoint_enabled` | bool | false | - | G | Advanced | Allow a hosted endpoint (tabled). |

### 10.14 dm (2)

Direct messages (3.1).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `dm.chat_enabled` | bool | false | - | G | Basic | Conversation in DMs. |
| `dm.notice_cooldown_hours` | int | 24 | 1 to 168 | G | Advanced | Hours between her only-chats-in-servers notices. |

### 10.15 logging (20)

Log channels and backups (3.18, section 5).

| Key | Type | Default | Range | Scope | Level | Description |
|---|---|---|---|---|---|---|
| `log.channel.bot_log` | int | none | - | G | Basic | Channel for #bot-log. Empty uses LOG_CHANNEL_ID from .env. |
| `log.channel.errors` | int | none | - | G | Basic | Channel for #errors. Empty falls back to #bot-log. |
| `log.channel.permissions` | int | none | - | G | Basic | Channel for #permissions. Empty falls back to #bot-log. |
| `log.channel.feeds` | int | none | - | G | Basic | Channel for #feeds. Empty falls back to #bot-log. |
| `log.channel.decisions` | int | none | - | G | Basic | Channel for #decisions. Empty falls back to #bot-log. |
| `log.channel.persona_log` | int | none | - | G | Basic | Channel for #persona-log. Empty falls back to #bot-log. |
| `log.channel.events` | int | none | - | G | Basic | Channel for #events. Empty falls back to #bot-log. |
| `log.channel.memory_review` | int | none | - | G | Basic | Channel for #memory-review. Empty falls back to #bot-log. |
| `log.level.bot_log` | text | info | debug, info, warning, error | G | Advanced | Detail level for #bot-log. |
| `log.level.errors` | text | info | debug, info, warning, error | G | Advanced | Detail level for #errors. |
| `log.level.permissions` | text | info | debug, info, warning, error | G | Advanced | Detail level for #permissions. |
| `log.level.feeds` | text | info | debug, info, warning, error | G | Advanced | Detail level for #feeds. |
| `log.level.decisions` | text | info | debug, info, warning, error | G | Advanced | Detail level for #decisions. |
| `log.level.persona_log` | text | info | debug, info, warning, error | G | Advanced | Detail level for #persona-log. |
| `log.level.events` | text | info | debug, info, warning, error | G | Advanced | Detail level for #events. |
| `log.level.memory_review` | text | info | debug, info, warning, error | G | Advanced | Detail level for #memory-review. |
| `backups.time` | time | 04:00 | - | G | Basic | Daily database backup time. |
| `backups.days_kept` | int | 30 | 1 to 365 | G | Basic | Days of daily backups kept. |
| `backups.migration_copies` | int | 10 | 1 to 50 | G | Advanced | Pre-migration copies kept. |
| `backups.offsite_copies_kept` | int | 4 | 1 to 52 | G | Advanced | Weekly offsite copies kept. |

### 10.16 Cross-checks

- `relationship.*_min_affinity` and `*_min_familiarity` must rise from acquaintance to friend to close friend; `disliked_max_affinity` must be below `acquaintance_min_affinity`.
- `addressing.low_cutoff` must be below `addressing.high_cutoff`; `commands.ask_cutoff` must be below `commands.act_cutoff`; `commands.act_cutoff` must be at least `addressing.high_cutoff`.
- `presence.*_min_*` must not exceed the matching `*_max_*`; the same holds for `typo.fix_delay_*`.
- `sleep.window_start` and `window_end` must differ; `sleep.latest_wake` must fall after `window_end`.
- `mood.*` threshold settings must sit inside `mood.limit`; `mood.excited_happiness_min` must be at least `mood.cheerful_happiness_min`.
- `relationship.negative_scale.*` values must not decrease from close friend to stranger.
- `relationship.neutral_cap` must be at least `relationship.acquaintance_min_affinity` and below `relationship.friend_min_affinity`; `relationship.neutral_daily_cap` must not exceed `relationship.positive_daily_cap`.
- `addressing.asker_window_seconds` must be at least `addressing.session_seconds`; `responses.history_keep` must be at least `responses.avoid_recent`.
- `split.max_parts` must be at least 2; `repeat.meta_at < emoji_at < silence_at`.
