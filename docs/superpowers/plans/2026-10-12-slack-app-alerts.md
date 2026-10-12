# Slack app alerts Implementation Plan

**Goal:** Bugsnag-style Slack alert messages for both Slack backends, plus a new `slack_app` backend (bot token, per-project channel, Mark as fixed / Snooze 1hr / Ignore buttons).

**Architecture:** One shared message builder in `alerts/service_backends/slack.py` used by the webhook backend and the new `alerts/service_backends/slack_app.py`. Bot token and signing secret are installation-wide settings. A public, signature-verified view applies button clicks through the existing `apply_issue_action(..., user=None)` and updates the Slack message through a snappea task that POSTs to Slack's `response_url`.

**Tech Stack:** Django function-based views, snappea `shared_task`, `requests`, Django `TestCase`.

**Spec:** `docs/superpowers/specs/2026-10-12-slack-app-alerts-design.md`

## Global Constraints

- Python 3.12, ruff clean (`ruff check .`), 120 columns; comments minimal (AGENTS.md).
- Never pass the bot token or signing secret through snappea task args (they are stored in the queue DB); tasks read `get_settings()` themselves.
- Task args must be JSON-serializable (snappea eager mode dumps them in tests): pass `str(issue.id)`, never a UUID.
- The click endpoint verifies the Slack signature BEFORE any DB write transaction (it is public; write transactions take the global write lock).
- No model changes (no migration); `makemigrations --check` must stay clean.
- Slack mrkdwn escaping: only `&`, `<`, `>` (`_escape`); do NOT use `_safe_markdown` for new text (its backslashes show up literally in Slack).
- Run tests with: `python manage.py test alerts -v2` (settings `bugsink.settings.development`).
- Commit messages: one main line + at most 1-2 sentences. No co-author or AI attribution lines.

**User decisions (already made):** Slack app (not webhooks) with one bot token for all projects; credentials installation-wide (settings/env), not per project; per-project config is the channel only; existing webhook Slack backend also gets the Bugsnag look (without buttons); push to Docker Hub from main is already done (commit c1138c6).

---

### Task 1: Settings for the Slack app

**Goal:** `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` exist as settings (default empty), settable via env in Docker.

**Files:**
- Modify: `bugsink/app_settings.py:105-108` (DEFAULTS, after `ALERTS_WEBHOOK_DENY_NON_GLOBAL`)
- Modify: `bugsink/conf_templates/docker.py.template` (after the `ALERTS_WEBHOOK_DENY_NON_GLOBAL` entry, ~line 204)
- Modify: `bugsink/conf_templates/local.py.template` (after the commented `ALERTS_WEBHOOK_DENY_NON_GLOBAL`, ~line 93)
- Modify: `bugsink/conf_templates/singleserver.py.template` (same place, ~line 131)
- Create: `alerts/test_slack_app.py`

**Acceptance Criteria:**
- [ ] `get_settings().SLACK_BOT_TOKEN == ""` and `get_settings().SLACK_SIGNING_SECRET == ""` by default
- [ ] `override_bugsink_settings(SLACK_BOT_TOKEN="x")` works (it asserts the key exists in DEFAULTS)
- [ ] docker template reads both from env vars

**Verify:** `python manage.py test alerts.test_slack_app.TestSettings -v2` → OK

**Steps:**

- [ ] **Step 1: Write the failing test** — create `alerts/test_slack_app.py`:

```python
from django.test import TestCase as DjangoTestCase

from bugsink.app_settings import get_settings, override_settings as override_bugsink_settings


class TestSettings(DjangoTestCase):

    def test_defaults_are_empty(self):
        self.assertEqual(get_settings().SLACK_BOT_TOKEN, "")
        self.assertEqual(get_settings().SLACK_SIGNING_SECRET, "")

    def test_can_be_overridden(self):
        with override_bugsink_settings(SLACK_BOT_TOKEN="xoxb-test"):
            self.assertEqual(get_settings().SLACK_BOT_TOKEN, "xoxb-test")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python manage.py test alerts.test_slack_app.TestSettings -v2`
Expected: FAIL (`AttributeError`/KeyError for `SLACK_BOT_TOKEN`)

- [ ] **Step 3: Implement**

`bugsink/app_settings.py`, directly after `"ALERTS_WEBHOOK_DENY_NON_GLOBAL": True,`:

```python

    # Slack app (alerts kind "slack_app"): one app per installation. The signing secret verifies button clicks.
    "SLACK_BOT_TOKEN": "",
    "SLACK_SIGNING_SECRET": "",
```

`bugsink/conf_templates/docker.py.template`, directly after the `"ALERTS_WEBHOOK_DENY_NON_GLOBAL": ...` entry (before `# Feature flags`):

```python

    # Slack app (alerts kind "slack_app"): bot token (xoxb-...) and the app's signing secret.
    "SLACK_BOT_TOKEN": os.getenv("SLACK_BOT_TOKEN", ""),
    "SLACK_SIGNING_SECRET": os.getenv("SLACK_SIGNING_SECRET", ""),
```

`bugsink/conf_templates/local.py.template` and `singleserver.py.template`, after the commented `# "ALERTS_WEBHOOK_DENY_NON_GLOBAL": True,` line:

```python

    # Slack app (alerts kind "slack_app"): bot token (xoxb-...) and the app's signing secret.
    # "SLACK_BOT_TOKEN": "",
    # "SLACK_SIGNING_SECRET": "",
```

- [ ] **Step 4: Run to verify it passes**

Run: `python manage.py test alerts.test_slack_app.TestSettings -v2`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bugsink/app_settings.py bugsink/conf_templates alerts/test_slack_app.py
git commit -m "Add SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET settings"
```

---

### Task 2: Bugsnag-style message builder (webhook backend uses it)

**Goal:** `build_alert_message` produces the new layout; `slack_backend_send_alert` uses it.

**Files:**
- Modify: `alerts/service_backends/slack.py` (add helpers after `_safe_markdown`, rewrite top of `slack_backend_send_alert`)
- Modify: `alerts/test_slack_app.py` (append tests)

**Acceptance Criteria:**
- [ ] Message = top-level `text` + one attachment `{color, blocks}`
- [ ] Top-level `text` (rendered by Slack above the colored bar, as Bugsnag does) = header `Event in {env} from {project} in {transaction} (<url|details>)`; env and transaction omitted when empty; dynamic parts escaped
- [ ] First attachment block: `*{state}*\n{title}` (full title, no truncation, escaped)
- [ ] Location block only when the issue has last-frame data: `{filename} - {module}.{function}`
- [ ] `unmute_reason` appended as its own section when given
- [ ] Actions block with 3 buttons (`resolve`, `snooze`, `ignore`; value = issue id) only when `buttons=True`
- [ ] `&`, `<`, `>` escaped in dynamic text
- [ ] Webhook backend posts the new payload; existing Slack tests still pass

**Verify:** `python manage.py test alerts -v2` → OK

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `alerts/test_slack_app.py` (add the imports at the top of the file):

```python
from unittest.mock import Mock, patch
import json

from events.factories import create_event
from issues.factories import get_or_create_issue
from projects.models import Project

from .service_backends.slack import SLACK_RED, build_alert_message, slack_backend_send_alert
```

```python
def section_texts(message):
    return [b["text"]["text"] for b in message["attachments"][0]["blocks"] if b["type"] == "section"]


class TestBuildAlertMessage(DjangoTestCase):

    def setUp(self):
        self.project = Project.objects.create(name="CAT Backend")
        self.issue, _ = get_or_create_issue(project=self.project)
        self.issue.calculated_type = "IllegalTransitionException"
        self.issue.calculated_value = 'Illegal transition "cancel"'
        self.url = get_settings().BASE_URL + self.issue.get_absolute_url()

    def test_full_message(self):
        self.issue.transaction = "GET /payment/finalize-transaction"
        self.issue.last_frame_filename = "src/Controller.php"
        self.issue.last_frame_module = "App\\Controller"
        self.issue.last_frame_function = "finalizeTransaction"

        message = build_alert_message(self.issue, "production", "New issue", buttons=True)

        self.assertEqual(
            message["text"],
            f"Event in production from CAT Backend in GET /payment/finalize-transaction (<{self.url}|details>)")
        self.assertEqual(section_texts(message), [
            '*New issue*\nIllegalTransitionException: Illegal transition "cancel"',
            "*Location*\nsrc/Controller.php - App\\Controller.finalizeTransaction",
        ])
        attachment = message["attachments"][0]
        self.assertEqual(attachment["color"], SLACK_RED)
        buttons = attachment["blocks"][-1]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], ["resolve", "snooze", "ignore"])
        self.assertEqual({b["value"] for b in buttons}, {str(self.issue.id)})

    def test_minimal_message_has_no_location_and_no_buttons(self):
        message = build_alert_message(self.issue, "", "Regression")

        self.assertEqual(message["text"], f"Event from CAT Backend (<{self.url}|details>)")
        self.assertEqual(len(message["attachments"][0]["blocks"]), 1)

    def test_escapes_special_characters(self):
        self.issue.transaction = "GET /a?b=1&c=<x>"

        message = build_alert_message(self.issue, "", "New issue")

        self.assertIn("in GET /a?b=1&amp;c=&lt;x&gt; (", message["text"])

    def test_escapes_title(self):
        self.issue.calculated_value = "<!channel> & co"

        message = build_alert_message(self.issue, "", "New issue")

        self.assertIn("&lt;!channel&gt; &amp; co", section_texts(message)[0])

    def test_unmute_reason_is_added(self):
        message = build_alert_message(self.issue, "", "Unmuted issue", unmute_reason="Seen 5 times today")

        self.assertEqual(section_texts(message)[1], "Seen 5 times today")


class TestWebhookBackendUsesBuilder(DjangoTestCase):

    @patch("alerts.service_backends.base.BaseWebhookBackend.safe_post")
    def test_posts_attachment_with_latest_environment(self, mock_post):
        project = Project.objects.create(name="CAT Backend")
        issue, _ = get_or_create_issue(project=project)
        create_event(issue=issue, environment="production")
        config = project.service_configs.create(
            display_name="x", kind="slack", config=json.dumps({"webhook_url": "https://hooks.slack.com/test"}))

        slack_backend_send_alert("https://hooks.slack.com/test", str(issue.id), "New issue", "a", "NEW", config.id)

        data = json.loads(mock_post.call_args.kwargs["data"])
        self.assertIn("Event in production from CAT Backend", data["text"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python manage.py test alerts.test_slack_app -v2`
Expected: FAIL/ERROR (`ImportError: cannot import name 'build_alert_message'`)

- [ ] **Step 3: Implement** — in `alerts/service_backends/slack.py`, directly after `_safe_markdown`:

```python
SLACK_RED = "#e01e5a"
SLACK_GREEN = "#2eb67d"

# action_id -> button label; the click endpoint (alerts/views.py) maps the same ids to issue actions
SLACK_BUTTONS = {"resolve": "Mark as fixed", "snooze": "Snooze 1hr", "ignore": "Ignore"}


def _escape(text):
    # the only characters Slack requires escaping in mrkdwn text
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mrkdwn(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def latest_environment(issue):
    event = issue.event_set.order_by("-digest_order").first()
    return event.environment if event else ""


def build_alert_message(issue, environment, state_description, unmute_reason=None, buttons=False, color=SLACK_RED):
    issue_url = get_settings().BASE_URL + issue.get_absolute_url()

    header = ["Event"]
    if environment:
        header.append(f"in {_escape(environment)}")
    header.append(f"from {_escape(issue.project.name)}")
    if issue.transaction:
        header.append(f"in {_escape(issue.transaction)}")
    header.append(f"(<{issue_url}|details>)")

    blocks = [_mrkdwn(f"*{_escape(state_description)}*\n{_escape(issue.title())}")]

    if unmute_reason:
        blocks.append(_mrkdwn(unmute_reason))

    if issue.last_frame_filename:
        function = ".".join(filter(None, [issue.last_frame_module, issue.last_frame_function]))
        location = issue.last_frame_filename + (f" - {function}" if function else "")
        blocks.append(_mrkdwn(f"*Location*\n{_escape(location)}"))

    if buttons:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action_id,
             "value": str(issue.id)}
            for action_id, label in SLACK_BUTTONS.items()
        ]})

    # the header is the top-level text: Slack shows it above the colored bar, like Bugsnag does
    return {"text": " ".join(header), "attachments": [{"color": color, "blocks": blocks}]}
```

Then replace the body of `slack_backend_send_alert` from `issue = Issue.objects.get(...)` up to (not including) the `try:` with:

```python
    issue = Issue.objects.get(id=issue_id)
    data = build_alert_message(issue, latest_environment(issue), state_description, unmute_reason)

```

(delete the old `issue_url/title/link/sections/fields/data` code; keep the `try/except` block as is.)

- [ ] **Step 4: Run to verify they pass**

Run: `python manage.py test alerts -v2`
Expected: all PASS (existing Slack tests included)

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add alerts && git commit -m "Slack alerts: Bugsnag-style message layout" -m "Shared builder with environment, transaction and location; used by the webhook backend."
```

---

### Task 3: `slack_app` backend (form, tasks, registration)

**Goal:** A project can add a "Slack app" alert config with a channel; alerts and test messages go through `chat.postMessage` with buttons.

**Files:**
- Create: `alerts/service_backends/slack_app.py`
- Modify: `alerts/models.py` (import, kind choices, backend class)
- Modify: `alerts/test_slack_app.py` (append tests)

**Acceptance Criteria:**
- [ ] kind `slack_app` listed as "Slack app" and resolvable via `get_alert_service_backend_class`
- [ ] Form: required `channel`; invalid with a clear message when `SLACK_BOT_TOKEN` is empty
- [ ] Alert task POSTs `https://slack.com/api/chat.postMessage` with `Authorization: Bearer <token>`, `channel`, buttons, 5s timeout
- [ ] Slack's `{"ok": false, "error": ...}` is stored as failure (`SlackAPIError`, message = error code); success clears failure status
- [ ] Token is read inside the task, never passed as a task arg

**Verify:** `python manage.py test alerts -v2` → OK

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `alerts/test_slack_app.py` (imports at top: `from alerts.models import MessagingServiceConfig`, `from django.utils import timezone`, `from .service_backends.slack_app import SlackAppConfigForm`):

```python
TOKEN = "xoxb-test"


class TestSlackAppConfigForm(DjangoTestCase):

    def test_requires_bot_token(self):
        form = SlackAppConfigForm(data={"channel": "#errors"})

        self.assertFalse(form.is_valid())
        self.assertIn("SLACK_BOT_TOKEN", form.errors["channel"][0])

    def test_valid_with_token(self):
        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            form = SlackAppConfigForm(data={"channel": "#errors"})

            self.assertTrue(form.is_valid())
            self.assertEqual(form.get_config(), {"channel": "#errors"})


class TestSlackAppSending(DjangoTestCase):

    def setUp(self):
        self.project = Project.objects.create(name="CAT Backend")
        self.config = MessagingServiceConfig.objects.create(
            project=self.project, display_name="#errors", kind="slack_app", config=json.dumps({"channel": "#errors"}))
        self.issue, _ = get_or_create_issue(project=self.project)

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_alert_is_posted_to_channel_with_bot_token(self, mock_post):
        mock_post.return_value = Mock(json=lambda: {"ok": True})
        self.config.last_failure_timestamp = timezone.now()
        self.config.save()

        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            self.config.get_backend().send_alert(str(self.issue.id), "New issue", "a", "NEW")

        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://slack.com/api/chat.postMessage")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer xoxb-test")
        self.assertEqual(kwargs["json"]["channel"], "#errors")
        self.assertEqual(kwargs["json"]["attachments"][0]["blocks"][-1]["type"], "actions")
        self.config.refresh_from_db()
        self.assertIsNone(self.config.last_failure_timestamp)

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_slack_error_is_stored_as_failure(self, mock_post):
        mock_post.return_value = Mock(json=lambda: {"ok": False, "error": "channel_not_found"})

        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            self.config.get_backend().send_alert(str(self.issue.id), "New issue", "a", "NEW")

        self.config.refresh_from_db()
        self.assertEqual(self.config.last_failure_error_type, "SlackAPIError")
        self.assertEqual(self.config.last_failure_error_message, "channel_not_found")

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_test_message_is_posted_to_channel(self, mock_post):
        mock_post.return_value = Mock(json=lambda: {"ok": True})

        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            self.config.get_backend().send_test_message()

        self.assertEqual(mock_post.call_args.kwargs["json"]["channel"], "#errors")
        self.assertIn("Test message by Bugsink", mock_post.call_args.kwargs["json"]["text"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python manage.py test alerts.test_slack_app -v2`
Expected: ERROR (`ModuleNotFoundError: alerts.service_backends.slack_app`)

- [ ] **Step 3: Implement** — create `alerts/service_backends/slack_app.py`:

```python
import json
import requests

from django import forms

from snappea.decorators import shared_task
from bugsink.app_settings import get_settings

from issues.models import Issue
from .slack import _escape, _store_failure_info, _store_success_info, build_alert_message, latest_environment

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


class SlackAPIError(Exception):
    pass


class SlackAppConfigForm(forms.Form):
    channel = forms.CharField(
        required=True, help_text='Channel name or ID, e.g. "#cat-backend-errors"; the bot must be invited to it')

    def __init__(self, *args, **kwargs):
        config = kwargs.pop("config", None)

        super().__init__(*args, **kwargs)
        if config:
            self.fields["channel"].initial = config.get("channel", "")

    def get_config(self):
        return {"channel": self.cleaned_data.get("channel")}

    def clean_channel(self):
        if not get_settings().SLACK_BOT_TOKEN:
            raise forms.ValidationError("SLACK_BOT_TOKEN is not configured on this Bugsink installation.")
        return self.cleaned_data["channel"]


def _post_message(service_config_id, data):
    try:
        response = requests.post(
            POST_MESSAGE_URL, json=data, timeout=5,
            headers={"Authorization": f"Bearer {get_settings().SLACK_BOT_TOKEN}"})
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            raise SlackAPIError(result.get("error", "unknown error"))

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        _store_failure_info(service_config_id, e, getattr(e, "response", None))
    except Exception as e:
        _store_failure_info(service_config_id, e)


@shared_task
def slack_app_backend_send_test_message(channel, project_name, display_name, service_config_id):
    text = f"Test message by Bugsink to test the Slack app setup (project: {_escape(project_name)}, " \
           f"backend: {_escape(display_name)})."
    _post_message(service_config_id, {"channel": channel, "text": text})


@shared_task
def slack_app_backend_send_alert(
        channel, issue_id, state_description, alert_article, alert_reason, service_config_id, unmute_reason=None):

    issue = Issue.objects.get(id=issue_id)
    data = build_alert_message(issue, latest_environment(issue), state_description, unmute_reason, buttons=True)
    _post_message(service_config_id, {"channel": channel, **data})


class SlackAppBackend:
    def __init__(self, service_config):
        self.service_config = service_config

    @classmethod
    def get_form_class(cls):
        return SlackAppConfigForm

    def send_test_message(self):
        config = json.loads(self.service_config.config)
        slack_app_backend_send_test_message.delay(
            config["channel"],
            self.service_config.project.name,
            self.service_config.display_name,
            self.service_config.id,
        )

    def send_alert(self, issue_id, state_description, alert_article, alert_reason, **kwargs):
        config = json.loads(self.service_config.config)
        slack_app_backend_send_alert.delay(
            config["channel"],
            issue_id,
            state_description,
            alert_article,
            alert_reason,
            self.service_config.id,
            **kwargs,
        )
```

`alerts/models.py`: add `from .service_backends.slack_app import SlackAppBackend` under the Slack import; add `("slack_app", "Slack app"),` after `("slack", "Slack"),`; add before `raise ValueError`:

```python
    if kind == "slack_app":
        return SlackAppBackend
```

- [ ] **Step 4: Run to verify they pass**

Run: `python manage.py test alerts -v2`
Expected: all PASS

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add alerts && git commit -m "Add Slack app alert backend" -m "Posts via chat.postMessage with a bot token to a per-project channel."
```

---

### Task 4: Slack signature verification

**Goal:** `verify_slack_signature` implements Slack's v0 signing check.

**Files:**
- Modify: `alerts/service_backends/slack_app.py` (add function + imports)
- Modify: `alerts/test_slack_app.py` (append tests)

**Acceptance Criteria:**
- [ ] valid signature within 5 minutes → True
- [ ] wrong secret, tampered body, timestamp older than 5 min (or in the far future), unset secret, missing/garbled headers → False
- [ ] constant-time comparison, safe for non-ASCII header values

**Verify:** `python manage.py test alerts.test_slack_app.TestVerifySlackSignature -v2` → OK

**Steps:**

- [ ] **Step 1: Write the failing tests** — append (imports at top: `import hashlib, hmac, time`, `from .service_backends.slack_app import verify_slack_signature`):

```python
SECRET = "signing-secret"


def sign(body, secret=SECRET, timestamp=None):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    digest = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return timestamp, "v0=" + digest


class TestVerifySlackSignature(DjangoTestCase):
    body = b"payload=%7B%7D"

    def test_valid(self):
        timestamp, signature = sign(self.body)
        self.assertTrue(verify_slack_signature(SECRET, timestamp, self.body, signature))

    def test_wrong_secret(self):
        timestamp, signature = sign(self.body, secret="other")
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body, signature))

    def test_tampered_body(self):
        timestamp, signature = sign(self.body)
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body + b"x", signature))

    def test_stale_timestamp(self):
        timestamp, signature = sign(self.body, timestamp=int(time.time()) - 301)
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body, signature))

    def test_unset_secret_never_verifies(self):
        timestamp, signature = sign(self.body, secret="")
        self.assertFalse(verify_slack_signature("", timestamp, self.body, signature))

    def test_missing_or_garbled_headers(self):
        self.assertFalse(verify_slack_signature(SECRET, None, self.body, None))
        self.assertFalse(verify_slack_signature(SECRET, "not-a-number", self.body, "v0=abc"))
        timestamp, _ = sign(self.body)
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body, "v0=é"))
```

- [ ] **Step 2: Run to verify they fail**

Run: `python manage.py test alerts.test_slack_app.TestVerifySlackSignature -v2`
Expected: ERROR (`ImportError: cannot import name 'verify_slack_signature'`)

- [ ] **Step 3: Implement** — in `alerts/service_backends/slack_app.py` add `import hashlib, hmac, time` at the top (alongside `json`), and after `POST_MESSAGE_URL`:

```python
MAX_SIGNATURE_AGE = 5 * 60


def verify_slack_signature(secret, timestamp, body, signature):
    # https://api.slack.com/authentication/verifying-requests-from-slack
    if not secret or not timestamp or not signature:
        return False

    try:
        age = time.time() - int(timestamp)
    except ValueError:
        return False
    if abs(age) > MAX_SIGNATURE_AGE:
        return False

    digest = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}".encode(), signature.encode())
```

- [ ] **Step 4: Run to verify they pass**

Run: `python manage.py test alerts.test_slack_app.TestVerifySlackSignature -v2`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add alerts && git commit -m "Add Slack request signature verification"
```

---

### Task 5: Click endpoint

**Goal:** Slack button clicks resolve/snooze/ignore the issue and update the Slack message.

**Files:**
- Modify: `alerts/service_backends/slack_app.py` (add `slack_app_respond` task)
- Modify: `alerts/views.py` (add `slack_interactivity` view + imports)
- Modify: `bugsink/urls.py` (import + path)
- Modify: `alerts/test_slack_app.py` (append tests)

**Acceptance Criteria:**
- [ ] `POST /alerts/slack/interactivity/` is reachable without login and without CSRF token
- [ ] Bad/missing signature → 403, no state change, no reply
- [ ] `resolve` → issue resolved; `snooze` → muted with `unmute_after` ≈ now + 1h; `ignore` → muted without `unmute_after`; all recorded via `apply_issue_action(..., user=None)`
- [ ] After a successful click, the original message is replaced (no buttons, green bar, status as state line) via `response_url`
- [ ] Unknown/deleted issue, or muting a resolved issue → ephemeral reply, no state change
- [ ] `response_url` not starting with `https://hooks.slack.com/` is refused by the respond task
- [ ] GET → 405

**Verify:** `python manage.py test alerts -v2` → OK

**Steps:**

- [ ] **Step 1: Write the failing tests** — append (imports at top: `from urllib.parse import urlencode`, `from uuid import uuid4`, `from django.test import Client`, `from .service_backends.slack_app import slack_app_respond`):

```python
class TestSlackInteractivity(DjangoTestCase):
    url = "/alerts/slack/interactivity/"

    def setUp(self):
        self.project = Project.objects.create(name="CAT Backend")
        self.issue, _ = get_or_create_issue(project=self.project)

    def click(self, action_id, secret=SECRET, issue_id=None):
        payload = {
            "type": "block_actions",
            "response_url": "https://hooks.slack.com/actions/T1/2/3",
            "actions": [{"action_id": action_id, "value": str(issue_id or self.issue.id)}],
        }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        timestamp, signature = sign(body, secret=secret)

        with override_bugsink_settings(SLACK_SIGNING_SECRET=SECRET), patch("alerts.views.slack_app_respond") as respond:
            response = Client(enforce_csrf_checks=True).post(
                self.url, body, content_type="application/x-www-form-urlencoded",
                HTTP_X_SLACK_REQUEST_TIMESTAMP=timestamp, HTTP_X_SLACK_SIGNATURE=signature)
        return response, respond

    def test_resolve_resolves_and_replaces_message(self):
        response, respond = self.click("resolve")

        self.assertEqual(response.status_code, 200)
        self.issue.refresh_from_db()
        self.assertTrue(self.issue.is_resolved)
        response_url, body = respond.delay.call_args.args
        self.assertEqual(response_url, "https://hooks.slack.com/actions/T1/2/3")
        self.assertTrue(body["replace_original"])
        self.assertIn("*Marked as fixed*", section_texts(body)[0])
        self.assertEqual(body["attachments"][0]["color"], SLACK_GREEN)
        self.assertNotIn("actions", [b["type"] for b in body["attachments"][0]["blocks"]])

    def test_snooze_mutes_for_an_hour(self):
        self.click("snooze")

        self.issue.refresh_from_db()
        self.assertTrue(self.issue.is_muted)
        self.assertAlmostEqual(
            self.issue.unmute_after.timestamp(), (timezone.now() + timedelta(hours=1)).timestamp(), delta=60)

    def test_ignore_mutes_indefinitely(self):
        self.click("ignore")

        self.issue.refresh_from_db()
        self.assertTrue(self.issue.is_muted)
        self.assertIsNone(self.issue.unmute_after)

    def test_bad_signature_is_rejected(self):
        response, respond = self.click("resolve", secret="other")

        self.assertEqual(response.status_code, 403)
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.is_resolved)
        respond.delay.assert_not_called()

    def test_muting_a_resolved_issue_gets_an_ephemeral_reply(self):
        self.issue.is_resolved = True
        self.issue.save()

        response, respond = self.click("ignore")

        self.assertEqual(response.status_code, 200)
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.is_muted)
        self.assertEqual(respond.delay.call_args.args[1]["response_type"], "ephemeral")

    def test_unknown_issue_gets_an_ephemeral_reply(self):
        response, respond = self.click("resolve", issue_id=uuid4())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(respond.delay.call_args.args[1]["response_type"], "ephemeral")

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


class TestSlackAppRespond(DjangoTestCase):

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_posts_body_to_response_url(self, mock_post):
        slack_app_respond("https://hooks.slack.com/actions/T1/2/3", {"text": "x"})

        mock_post.assert_called_once_with("https://hooks.slack.com/actions/T1/2/3", json={"text": "x"}, timeout=5)

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_refuses_foreign_response_url(self, mock_post):
        with self.assertRaises(ValueError):
            slack_app_respond("https://evil.example.com/x", {"text": "x"})

        mock_post.assert_not_called()
```

Also add imports at top: `from datetime import timedelta` and `from .service_backends.slack import SLACK_GREEN`.

- [ ] **Step 2: Run to verify they fail**

Run: `python manage.py test alerts.test_slack_app -v2`
Expected: ERROR (`ImportError: cannot import name 'slack_app_respond'`)

- [ ] **Step 3: Implement**

`alerts/service_backends/slack_app.py`, after `SlackAppBackend`'s helpers (before the class):

```python
RESPONSE_URL_PREFIX = "https://hooks.slack.com/"


@shared_task
def slack_app_respond(response_url, body):
    if not response_url.startswith(RESPONSE_URL_PREFIX):
        raise ValueError("Unexpected Slack response_url")
    requests.post(response_url, json=body, timeout=5).raise_for_status()
```

`alerts/views.py` — add imports and the view (keep `debug_email` and `DEBUG_CONTEXTS` as they are):

```python
import json

from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from bugsink.app_settings import get_settings
from bugsink.decorators import login_exempt
from bugsink.transaction import immediate_atomic
from issues.models import Issue, IncongruentStateException, IssueStateManager, apply_issue_action

from .service_backends.slack import SLACK_GREEN, build_alert_message, latest_environment
from .service_backends.slack_app import slack_app_respond, verify_slack_signature

# action_id -> (issue action, status shown on the message after the click)
SLACK_ISSUE_ACTIONS = {
    "resolve": ("resolve", "Marked as fixed"),
    "snooze": ("mute_for:hour,1,", "Snoozed for 1 hour"),
    "ignore": ("mute", "Ignored"),
}
```

```python
def _ephemeral(text):
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


@csrf_exempt
@require_POST
@login_exempt
def slack_interactivity(request):
    # Verified before (and outside of) any transaction: this endpoint is public and write transactions take the
    # global write lock.
    if not verify_slack_signature(
            get_settings().SLACK_SIGNING_SECRET, request.headers.get("X-Slack-Request-Timestamp"), request.body,
            request.headers.get("X-Slack-Signature")):
        return HttpResponseForbidden()

    payload = json.loads(request.POST["payload"])
    action = payload["actions"][0]
    issue_action, status = SLACK_ISSUE_ACTIONS[action["action_id"]]

    try:
        with immediate_atomic(only_if_needed=True):
            issue = Issue.objects.get(id=action["value"], is_deleted=False)
            apply_issue_action(IssueStateManager, issue, issue_action, user=None)
            issue.save()
    except Issue.DoesNotExist:
        reply = _ephemeral("This issue no longer exists.")
    except IncongruentStateException:
        reply = _ephemeral("This issue is already resolved.")
    else:
        message = build_alert_message(issue, latest_environment(issue), status, color=SLACK_GREEN)
        reply = {"replace_original": True, **message}

    slack_app_respond.delay(payload["response_url"], reply)
    return HttpResponse()
```

`bugsink/urls.py`: change `from alerts.views import debug_email as debug_alerts_email` to also import the view:

```python
from alerts.views import debug_email as debug_alerts_email, slack_interactivity
```

and add next to the other top-level paths (e.g. above `path('projects/', ...)`):

```python
    path("alerts/slack/interactivity/", slack_interactivity, name="slack_interactivity"),
```

- [ ] **Step 4: Run to verify they pass**

Run: `python manage.py test alerts -v2`
Expected: all PASS. If the 403/200 tests fail with a 302 to the login page, `login_exempt` is not reaching the outermost wrapper: check decorator order (`csrf_exempt`, `require_POST`, `login_exempt` from top to bottom).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add alerts bugsink/urls.py && git commit -m "Add Slack interactivity endpoint" -m "Verified clicks resolve, snooze or ignore the issue and update the Slack message."
```

---

### Task 6: Full verification

**Goal:** Everything CI checks passes locally; manual check of the config UI.

**Files:** none (verification only)

**Acceptance Criteria:**
- [ ] full test suite passes
- [ ] `ruff check .` clean; `python manage.py makemigrations --check` reports no changes
- [ ] adding a "Slack app" service in the project alerts UI works and shows the channel field

**Verify:** `python manage.py test -v1 && ruff check . && python manage.py makemigrations --check --dry-run` → OK

**Steps:**

- [ ] **Step 1: Run the whole suite**

Run: `python manage.py test -v1`
Expected: OK (the event-samples repo must be next to this repo, or set `SAMPLES_DIR`; tests needing it fail otherwise and are unrelated to this change)

- [ ] **Step 2: Lint, migrations, tailwind**

Run: `ruff check . && python manage.py makemigrations --check --dry-run`
Expected: no findings, "No changes detected". No Tailwind changes were made, so no CSS rebuild is needed.

- [ ] **Step 3: Manual UI check** (AGENTS.md)

Run: `SLACK_BOT_TOKEN=xoxb-fake python manage.py runserver`, log in (admin@example.org / admin), open a project's alerts setup, add a service of kind "Slack app", enter a channel, save. Expected: saved; with the env var unset, saving shows the "SLACK_BOT_TOKEN is not configured" error under the channel field.

- [ ] **Step 4: Commit any fixes**

```bash
git status && git add -A && git commit -m "Fix issues found in verification"   # only if something changed
```

## Manual live check (after merge/deploy)

Create a Slack app with the `chat:write` scope, set Interactivity Request URL to `{BASE_URL}/alerts/slack/interactivity/`, install it, invite the bot to a channel, set `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` on the container, add a "Slack app" service to a project with that channel, use "send test message", then trigger an error and click the three buttons.
