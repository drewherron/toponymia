"""Custom allauth account adapter — enforces the registration blocklist.

When an account is banned its email address(es) are recorded in BannedEmail
(see core.moderation.block_user_emails). This adapter refuses a *signup* that
reuses a still-active address, which — together with allauth's unique-email
rule — is what makes an account ban outlive the single login it was placed on.

Scoped to the signup endpoint on purpose. `clean_email` is shared with the
add-email and password-reset flows; raising in the reset flow would leak which
addresses are blocked and break the enumeration resistance that flow is built
for (see the password-reset settings). So we only intervene when the active
request is the headless signup call.
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context

from .moderation import active_email_ban


class AccountAdapter(DefaultAccountAdapter):
    error_messages = {
        **DefaultAccountAdapter.error_messages,
        'email_blocked': 'This email address can’t be used to register.',
    }

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
