"""Custom allauth account adapter — enforces the registration blocklists.

Two of them, with different lifecycles:

**Banned emails.** When an account is banned its email address(es) are recorded
in BannedEmail (see core.moderation.block_user_emails). This adapter refuses a
*signup* that reuses a still-active address, which — together with allauth's
unique-email rule — is what makes an account ban outlive the single login it
was placed on.

That check is scoped to the signup endpoint on purpose. `clean_email` is shared
with the add-email and password-reset flows; raising in the reset flow would
leak which addresses are blocked and break the enumeration resistance that flow
is built for (see the password-reset settings). So we only intervene when the
active request is the headless signup call.

**Retired usernames.** Closing an account takes its username out of
circulation (see core.accounts). `clean_username` needs no such scoping — it is
not shared with a flow that must stay enumeration-resistant — and it reuses
allauth's stock "username_taken" wording rather than announcing that a name is
reserved, which would turn signup into an oracle for which accounts have
closed.
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context
from django.conf import settings

from .accounts import username_is_reserved
from .moderation import active_email_ban


class AccountAdapter(DefaultAccountAdapter):
    error_messages = {
        **DefaultAccountAdapter.error_messages,
        'email_blocked': 'This email address can’t be used to register.',
    }

    def is_open_for_signup(self, request):
        """Closed while `PRELAUNCH` is set — see the setting for why.

        allauth's headless signup checks this *before* creating anything and
        answers 403, and reports it in the config endpoint, which is where
        `/api/me/` picks it up for the SPA. Login and password reset are
        untouched: an account that already exists still works.
        """
        return not settings.PRELAUNCH

    def clean_email(self, email):
        email = super().clean_email(email)
        request = context.request
        # Only the signup call: add-email/reset also route through here (see
        # module docstring). request is None outside a request (shell, tests
        # exercising the helper directly) — nothing to block there.
        if (
            request is not None
            and request.path.rstrip('/').endswith('/auth/signup')
            and active_email_ban(email) is not None
        ):
            raise self.validation_error('email_blocked')
        return email

    def clean_username(self, username, shallow=False):
        username = super().clean_username(username, shallow=shallow)
        # `shallow` means "no database lookups" — allauth uses it while
        # generating a candidate name, where a reservation is not yet relevant
        # and the eventual non-shallow call will catch it anyway.
        if not shallow and username_is_reserved(username):
            raise self.validation_error('username_taken')
        return username
