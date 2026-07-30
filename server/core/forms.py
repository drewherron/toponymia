"""Signup form additions — the Terms of Use agreement.

Wired in via ACCOUNT_SIGNUP_FORM_CLASS. allauth mixes this class into its own
BaseSignupForm, and allauth.headless's SignupInput derives from that, so the
`terms` field is validated by the real /_allauth/browser/v1/auth/signup
endpoint and not only by the React form. That matters: a checkbox enforced
solely in the client is bypassed by posting to the endpoint directly, which
would make every TermsAcceptance row a claim we couldn't stand behind.
"""

from django import forms

from .models import TermsAcceptance
from .terms import TERMS_VERSION


class TermsSignupForm(forms.Form):
    terms = forms.BooleanField(
        required=True,
        label='I have read and agree to the Terms of Use',
        error_messages={
            'required': 'You must agree to the Terms of Use to create an account.'
        },
    )

    def signup(self, request, user):
        """Called by allauth once the user row exists (see
        account.forms.BaseSignupForm.save). Reached only after validation, so
        `terms` was submitted true."""
        TermsAcceptance.objects.create(user=user, version=TERMS_VERSION)
