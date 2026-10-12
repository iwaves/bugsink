# Slack app alerts (Bugsnag-style messages, per-project channel, action buttons)

## Goal

1. Slack alerts look like Bugsnag's: red bar, "Event in {env} from {project} in {transaction} (details)", full
   exception text, a Location line.
2. One Slack app (bot token) posts to a different channel per project: no webhook per channel.
3. Buttons on the message: Mark as fixed, Snooze 1hr, Ignore.

## Decisions

* New backend kind `slack_app`; the existing `slack` (webhook) kind stays, and gets the new message look (without
  buttons).
* Bot token and signing secret are installation-wide settings (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`), not stored
  per project. Slack's signature must be verified before we know which project a click belongs to.
* Per-project config of `slack_app`: `channel` only (`#name` or channel ID). A project can have several rows.
* Slack users are not Bugsink users: any channel member can click. Actions are recorded with `user=None` (as the REST
  API does).
* Out of scope: Slack OAuth install flow, mapping Slack to Bugsink users, updating messages on changes made elsewhere.

## Components

### Settings
`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` (default empty) in `bugsink/app_settings.py` DEFAULTS. Only
`docker.py.template` reads them from env (docker only knows env vars); `local` and `singleserver` carry commented
examples. All three carry an operator comment: the Slack app needs `chat:write`, the bot must be invited to each alert
channel, and the Interactivity Request URL is `{BASE_URL}/alerts/slack/interactivity/`. Both values are masked on the
superuser `/settings/` page.

### Message builder (`alerts/service_backends/slack.py`)
`build_alert_message(issue, environment, state_description, unmute_reason=None, buttons=False, color=RED)` returns the JSON
payload: top-level `text` (the header line below; Slack renders it above the colored bar, like Bugsnag) plus one
attachment `{color, blocks}` holding the rest:

* top-level `text` (mrkdwn): `Event in {environment} from {project} in {transaction} (<url|details>)`; empty parts omitted
* `state_description` ("New issue" / "Regression" / "Unmuted issue"), plus `unmute_reason` when given
* `issue.title()` as a mrkdwn section, truncated to 500 raw characters (before escaping: Slack rejects sections over
  3000 characters and escaping grows the text)
* `Location`: `last_frame_filename - last_frame_module.last_frame_function` from the issue's denormalized columns; omitted when
  empty (no line numbers: not a column)
* if `buttons`: an actions block with 3 buttons; `action_id` in `resolve` / `snooze` / `ignore`, `value` = issue id

`environment` = that of the issue's latest event (`latest_environment(issue)`, empty if no event; the only value that
needs an event query, and it loads only the `environment` column). Both webhook and app tasks use this; dynamic text
is escaped with `_escape` (only `&`, `<`, `>`, the characters Slack requires), not `_safe_markdown`.

### Sending (`alerts/service_backends/slack_app.py`)
* `SlackAppConfigForm`: `channel` (required); `clean_channel()` fails with a clear message if `SLACK_BOT_TOKEN` is
  unset.
* `slack_app_backend_send_alert` task: POST `https://slack.com/api/chat.postMessage` (plain `requests`, 5s timeout,
  fixed host so no `safe_post`), `Authorization: Bearer <token>`, payload + `channel`. Slack answers 200 on failure, so
  check `ok` in the JSON; failures go through the existing `_store_failure_info` / `_store_success_info`.
* test message: same task path with a simple "TEST" text.
* register in `alerts/models.py` (`kind` choices + backend class).

### Click endpoint (`alerts/views.py`, URL `alerts/slack/interactivity/` in `bugsink/urls.py`)
`login_exempt` + `csrf_exempt` + POST only.

1. Verify: `v0=` + HMAC-SHA256(signing secret, `v0:{timestamp}:{raw body}`) with `hmac.compare_digest`; reject
   timestamps older than 5 minutes; reject everything if the secret is unset. 4xx on failure.
2. Parse form field `payload` (JSON). A payload whose type is not `block_actions`, or with an unknown `action_id`,
   is a 400. Map `action_id` to `resolve` / `mute_for:hour,1,` / `mute`. Inside one transaction: load the issue, gate on
   `is_valid_issue_action`, then `apply_issue_action(IssueStateManager, issue, action, user=None)` and `issue.save()`.
3. Return 200 immediately; then a snappea task POSTs to `response_url` (accepted only if it starts with
   `https://hooks.slack.com/`) with `replace_original`: the same message rebuilt with `buttons=False`, a status as
   `state_description` ("Marked as fixed" / "Snoozed for 1 hour" / "Ignored") and a green bar.
4. Action not valid for the issue's current state (e.g. mute on a resolved issue, resolve twice): ephemeral reply via
   `response_url` ("This issue has already been resolved or muted."), no state change. `IncongruentStateException` is
   not used. Unknown/deleted issue: ephemeral "This issue no longer exists.".

## Testing

* Unit: signature verification (valid, bad signature, stale timestamp, secret unset); message builder (with and
  without event/environment/Location, with/without buttons).
* Integration: send alert via `slack_app` with mocked `requests.post` (assert channel, token header, buttons), then a
  signed click on "resolve" resolves the issue and posts the replacement to a mocked `response_url`.
* Chained buttons: send an alert through `slack_app` with mocked `requests.post`, take each button's `action_id` and
  `value` from the posted payload, and POST a signed click built from them; each must return 200 and change the issue
  (resolved / muted / muted). Guards that the ids built in `slack.py` match the click endpoint's keys.
* Existing Slack tests keep passing; webhook alert test asserts the new attachment shape.

## Deployment

Docker: stock `Dockerfile`; the two new settings arrive as env vars via `docker.py.template`.
Slack side (one-time): app with `chat:write`, Interactivity Request URL `{BASE_URL}/alerts/slack/interactivity/`, bot
invited to each channel.

## Risks

* Bugsink must be reachable from Slack over public HTTPS or buttons do nothing (messages still send).
* Anyone in a channel can resolve/mute issues of that project from Slack.
* Not testable end-to-end without a real Slack app; the first live check is manual.
