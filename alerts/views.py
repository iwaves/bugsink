import json

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from bugsink.app_settings import get_settings
from bugsink.decorators import atomic_for_request_method, login_exempt
from bugsink.transaction import immediate_atomic
from issues.models import Issue, IssueStateManager, apply_issue_action, is_valid_issue_action

from .forms import AlertSettingsForm
from .models import AlertSettings
from .service_backends.slack import SLACK_GREEN, build_alert_message, latest_environment
from .service_backends.slack_app import slack_app_respond, verify_slack_signature

DEBUG_CONTEXTS = {
    "issue_alert": {
        "site_title": get_settings().SITE_TITLE,
        "base_url": get_settings().BASE_URL + "/",
        "issue_title": "AttributeError: 'NoneType' object has no attribute 'data'",
        "project_name": "My first project",
        "alert_article": "a",
        "alert_reason": "NEW",
        "issue_url": get_settings().BASE_URL + "/issues/issue/00000000-0000-0000-0000-000000000000/",
        "settings_url": get_settings().BASE_URL + "/accounts/preferences/",
    },
}

# action_id -> (issue action, status shown on the message after the click)
SLACK_ISSUE_ACTIONS = {
    "resolve": ("resolve", "Marked as fixed"),
    "snooze": ("mute_for:hour,1,", "Snoozed for 1 hour"),
    "ignore": ("mute", "Ignored"),
}


def debug_email(request, template_name):
    return render(request, 'mails/' + template_name + ".html", DEBUG_CONTEXTS[template_name])


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
    action = (payload.get("actions") or [{}])[0]
    if payload.get("type") != "block_actions" or action.get("action_id") not in SLACK_ISSUE_ACTIONS:
        return HttpResponseBadRequest()
    issue_action, status = SLACK_ISSUE_ACTIONS[action["action_id"]]

    try:
        with immediate_atomic(only_if_needed=True):
            issue = Issue.objects.get(id=action["value"], is_deleted=False)
            is_valid = is_valid_issue_action(issue_action, issue)
            if is_valid:
                apply_issue_action(IssueStateManager, issue, issue_action, user=None)
                issue.save()
    except Issue.DoesNotExist:
        reply = _ephemeral("This issue no longer exists.")
    else:
        if is_valid:
            reply = {"replace_original": True, **build_alert_message(
                issue, latest_environment(issue), status, color=SLACK_GREEN)}
        else:
            reply = _ephemeral("This issue has already been resolved or muted.")

    slack_app_respond.delay(payload["response_url"], reply)
    return HttpResponse()


@atomic_for_request_method
@user_passes_test(lambda u: u.is_superuser)
def alert_settings(request):
    if request.method == "POST":
        form = AlertSettingsForm(request.POST, instance=AlertSettings.get())

        if form.is_valid():
            form.save()
            messages.success(request, _("Alert settings saved."))
            return redirect("alert_settings")

    else:
        form = AlertSettingsForm(instance=AlertSettings.get())

    return render(request, "alerts/alert_settings.html", {"form": form})
