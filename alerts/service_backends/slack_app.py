import hashlib
import hmac
import json
import time
import requests

from django import forms

from snappea.decorators import shared_task
from bugsink.app_settings import get_settings

from issues.models import Issue
from .slack import _escape, _store_failure_info, _store_success_info, build_alert_message, latest_environment

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
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
