import hashlib
import hmac
import json
import time
import requests

from django import forms
from django.template.defaultfilters import date, pluralize
from django.utils import timezone

from snappea.decorators import shared_task
from bugsink.app_settings import get_settings
from bugsink.transaction import immediate_atomic
from theme.templatetags.user import best_displayname

from issues.models import Issue, TurningPoint, TurningPointKind
from .slack import (
    SLACK_GREEN, _escape, _store_failure_info, _store_success_info, build_alert_message, latest_environment)

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
UPDATE_MESSAGE_URL = "https://slack.com/api/chat.update"
RESPONSE_URL_PREFIX = "https://hooks.slack.com/"
MAX_SIGNATURE_AGE = 5 * 60


def verify_slack_signature(secret, timestamp, body, signature):
    # https://api.slack.com/authentication/verifying-requests-from-slack
    if not secret or not timestamp or not signature:
        return False

    try:
        age = int(time.time()) - int(timestamp)
    except ValueError:
        return False
    if abs(age) > MAX_SIGNATURE_AGE:
        return False

    digest = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}".encode(), signature.encode())


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


def _call_slack(url, service_config_id, data):
    # Returns Slack's answer; failures are stored on the service config (and never raised)
    try:
        response = requests.post(
            url, json=data, timeout=5, headers={"Authorization": f"Bearer {get_settings().SLACK_BOT_TOKEN}"})
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            raise SlackAPIError(result.get("error", "unknown error"))

        _store_success_info(service_config_id)
        return result
    except requests.RequestException as e:
        _store_failure_info(service_config_id, e, getattr(e, "response", None))
    except Exception as e:
        _store_failure_info(service_config_id, e)


def _state(issue):
    if issue.is_resolved:
        if issue.is_resolved_by_next_release:
            return "Resolved in next release"
        if not issue.is_resolved_unconditionally and issue.get_fixed_at():
            return f"Resolved in {issue.get_fixed_at()[-1]}"
        return "Resolved"

    if issue.is_muted:
        if issue.unmute_after:
            # calling the date filter directly skips the template's conversion to local time
            return f"Muted until {date(timezone.localtime(issue.unmute_after), 'j M G:i T')}"
        vbcs = issue.get_unmute_on_volume_based_conditions()
        if vbcs:
            vbc = vbcs[0]
            return f"Muted until {vbc.volume} events per {vbc.nr_of_periods} {vbc.period}{pluralize(vbc.nr_of_periods)}"
        return "Muted"

    return None


def build_state_message(issue):
    """The alert as it reads after a state change: resolved/muted and by whom, or with its buttons back once open."""
    last_change = issue.turningpoint_set.filter(kind__in=[
        TurningPointKind.RESOLVED, TurningPointKind.MUTED, TurningPointKind.UNMUTED, TurningPointKind.REOPENED,
    ]).select_related("user").first()
    by = f" by {best_displayname(last_change.user)}" if last_change and last_change.user else ""
    environment = latest_environment(issue)

    state = _state(issue)
    if state is None:
        reopened = last_change and last_change.kind == TurningPointKind.REOPENED
        return build_alert_message(issue, environment, ("Reopened" if reopened else "Unmuted") + by, buttons=True)
    return build_alert_message(issue, environment, state + by, color=SLACK_GREEN)


@shared_task
def slack_app_backend_send_test_message(channel, project_name, display_name, service_config_id):
    text = f"Test message by Bugsink to test the Slack app setup (project: {_escape(project_name)}, " \
           f"backend: {_escape(display_name)})."
    _call_slack(POST_MESSAGE_URL, service_config_id, {"channel": channel, "text": text})


@shared_task
def slack_app_backend_send_alert(
        channel, issue_id, state_description, alert_article, alert_reason, service_config_id, unmute_reason=None):

    issue = Issue.objects.get(id=issue_id)
    data = build_alert_message(issue, latest_environment(issue), state_description, unmute_reason, buttons=True)
    result = _call_slack(POST_MESSAGE_URL, service_config_id, {"channel": channel, **data})
    if result:
        with immediate_atomic(only_if_needed=True):
            issue.slack_messages.create(service_config_id=service_config_id, channel=result["channel"], ts=result["ts"])


@shared_task
def slack_app_update_messages(issue_ids):
    for issue in Issue.objects.filter(id__in=issue_ids, is_deleted=False).prefetch_related("slack_messages"):
        messages = issue.slack_messages.all()
        if not messages:
            continue

        data = build_state_message(issue)
        for message in messages:
            _call_slack(
                UPDATE_MESSAGE_URL, message.service_config_id, {"channel": message.channel, "ts": message.ts, **data})


@shared_task
def slack_app_post_comment(turning_point_id):
    turning_point = TurningPoint.objects.select_related("user").get(id=turning_point_id)
    author = best_displayname(turning_point.user) if turning_point.user else "Bugsink"
    text = f"*{_escape(author)}* commented:\n{_escape(turning_point.comment)}"

    threads = {}
    for message in turning_point.issue.slack_messages.order_by("id"):
        threads.setdefault(message.service_config_id, message)  # the first alert in each channel holds the thread

    for message in threads.values():
        _call_slack(POST_MESSAGE_URL, message.service_config_id,
                    {"channel": message.channel, "thread_ts": message.ts, "text": text})


@shared_task
def slack_app_respond(response_url, body):
    if not response_url.startswith(RESPONSE_URL_PREFIX):
        raise ValueError("Unexpected Slack response_url")
    requests.post(response_url, json=body, timeout=5).raise_for_status()


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
