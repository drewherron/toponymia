"""Mail the site sends on its own behalf.

Everything else that leaves the server is allauth's (verification and
password-reset codes). This module is for notifications the project itself
raises — currently just "a report was filed".

Two constraints shape all of it:

* **No task queue.** Nothing here runs out of band, so a send happens inline on
  the request that triggered it. That makes failure isolation mandatory rather
  than tidy: a dead SMTP host must never turn a successful report into a 500,
  and must never lose the report row that was already committed.
* **Recipients are moderators, not a settings list.** Promoting someone should
  not need a redeploy, so the address list is a live query.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.db.models import Q
from django.utils import timezone

from .models import Report

logger = logging.getLogger(__name__)

# Most report mail we'll send in REPORT_MAIL_WINDOW. Reporting is idempotent
# per (reporter, target, open) and needs a verified account, but `report:
# 15/min` is a rate, not a cap — one determined account filing against distinct
# targets could otherwise turn the moderators' inboxes into the denial of
# service. Past the ceiling the queue is still the source of truth; only the
# notification stops.
REPORT_MAIL_CEILING = 20
REPORT_MAIL_WINDOW = timedelta(hours=1)


def moderator_emails(exclude=None):
    """Addresses to notify: every active moderator with an address on file.

    `is_staff or is_superuser` mirrors `moderation.is_moderator` — a superuser
    created without the staff flag still moderates, so it must still be told.
    """
    User = get_user_model()
    recipients = (
        User.objects.filter(Q(is_staff=True) | Q(is_superuser=True))
        .filter(is_active=True)
        .exclude(email='')
    )
    if exclude is not None:
        recipients = recipients.exclude(pk=exclude.pk)
    return sorted(set(recipients.values_list('email', flat=True)))


def _send(subject, body, recipients, blind=True):
    """Send, or log and carry on. Never raises.

    Moderator mail goes out BCC: a notification should not tell each moderator
    what the others' addresses are. Mail to a single person uses To instead —
    a one-to-one message with an empty To: header reads as a blast, to the
    reader and to their spam filter alike.
    """
    try:
        EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[] if blind else list(recipients),
            bcc=list(recipients) if blind else [],
        ).send(fail_silently=False)
    except Exception:
        # Deliberately broad: every exception an SMTP backend can raise is less
        # important than the request that triggered it, which has already done
        # the thing the user asked for.
        logger.exception('notification email failed: %s', subject)


def notify_new_report(report, url=None):
    """Tell the moderators a report was filed. Call only for newly *created*
    reports — re-filing an open one is a no-op by design, and should be silent.

    The rate ceiling counts reports filed in the window rather than mail sent,
    which needs no new state to be correct: mail goes out only on creation, so
    the two track each other until the ceiling stops the mail. The report that
    lands exactly on the ceiling sends a notice that further mail is paused, so
    the silence afterwards is explained rather than looking like a broken
    mailer.

    The reported *content* is deliberately not included. A moderator should
    read harassment in the queue, where there's context and a button, not have
    it delivered to their inbox. The reporter's own note is included — it's
    what makes a report triageable, and it's mod-only mail.
    """
    recipients = moderator_emails(exclude=report.reporter)
    if not recipients:
        return

    since = timezone.now() - REPORT_MAIL_WINDOW
    recent = Report.objects.filter(created__gte=since).count()
    if recent > REPORT_MAIL_CEILING:
        return

    if recent == REPORT_MAIL_CEILING:
        _send(
            '[Toponymia] Report notifications paused',
            (
                f'{recent} reports have been filed in the past hour, which is '
                'the notification ceiling.\n\n'
                'Further reports will not be emailed until the rate drops. '
                'They are still being recorded — open the moderation queue to '
                'see them.\n'
            ),
            recipients,
        )
        return

    kind = 'talk post' if report.talk_post_id is not None else 'revision'
    lines = [
        f'A {kind} was reported by {report.reporter.username}.',
        '',
        f'Category: {report.get_category_display()}',
    ]
    if report.reason:
        lines += ['', f'Their note: {report.reason}']
    if url:
        lines += ['', url]
    lines += ['', 'Open the moderation queue to act on it.']

    _send(
        f'[Toponymia] {report.get_category_display()} report on a {kind}',
        '\n'.join(lines) + '\n',
        recipients,
    )


# What a closed report says to the person who filed it, keyed by the action
# the moderator took rather than by the report's status: `delete`, `suppress`
# and `resolve` all land on RESOLVED, so the status alone cannot tell a
# take-down from a close.
#
# Three rules shape the wording. The moderator's note is never quoted — it is
# an audit record written for the dashboard, not a reply to the reporter. The
# content is never quoted either, for the same reason the report notification
# omits it. And the dismissal does not invite a response: there is no appeals
# inbox behind it, so a message that sounds like the opening of a conversation
# would be a promise the site can't keep.
_OUTCOME_BODIES = {
    'delete': (
        'Thanks for reporting this. We reviewed it and removed the content.'
    ),
    'suppress': (
        'Thanks for reporting this. We reviewed it and removed the content '
        'from public view.'
    ),
    'dismiss': (
        'Thanks for reporting this. We reviewed it and decided it does not '
        'break the site rules, so the content stays up.\n\n'
        'We will not be taking further action on this report.'
    ),
    # `resolve` can't promise a removal — it covers everything from "already
    # handled by a revert" to "closed after a look". But vagueness alone
    # would land badly on the one person who is still looking at the content
    # they reported: a valid report followed by "dealt with" and no visible
    # change reads as nothing having happened. So it says the report was
    # acted on *and* that the content may still be there, rather than leaving
    # them to reconcile the two themselves.
    'resolve': (
        'Thanks for reporting this. We reviewed it and took action.\n\n'
        'You may still see the content you reported — not every outcome '
        'removes it. An edit can be corrected or reverted instead, and some '
        'reports are handled in ways that leave the page looking much the '
        'same.'
    ),
}


def notify_report_outcome(report, action, actor=None, url=None):
    """Tell the reporter what a moderator decided.

    Called once per queue decision, so the rate is a human clicking buttons —
    no ceiling here, unlike `notify_new_report`, where the rate is the public.

    Silent when there's nobody to tell: no address on file, a deactivated
    account, or a moderator acting on a report they filed themselves (they
    just watched the outcome happen).
    """
    reporter = report.reporter
    if actor is not None and actor.pk == reporter.pk:
        return
    if not reporter.email or not reporter.is_active:
        return
    body = _OUTCOME_BODIES.get(action)
    if body is None:
        return

    kind = 'talk post' if report.talk_post_id is not None else 'revision'
    lines = [f'You reported a {kind} on Toponymia.', '', body]
    if url:
        lines += ['', url]

    _send(
        '[Toponymia] Update on your report',
        '\n'.join(lines) + '\n',
        [reporter.email],
        blind=False,
    )
