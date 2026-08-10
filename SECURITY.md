# Security policy

Toponymia is a small project maintained by one person. This file says how to
report a security problem and what to expect back — deliberately without
promises it can't keep.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Use GitHub's **[private vulnerability reporting](https://github.com/drewherron/toponymia/security/advisories/new)**
(the Security tab → Report a vulnerability). It's the preferred route: it
creates a private thread, and it works even if email is having a bad day.

If you'd rather use email, write to <support@toponymia.org>.

Useful things to include, roughly in order of how much they help:

- What an attacker can actually do — read another user's data, edit as someone
  else, take over an account.
- The steps to reproduce it, ideally against your own account.
- The URL or the file and line.

## What to expect

One maintainer, no bounty, no SLA. Realistically: an acknowledgement within a
few days, and a fix prioritised by how bad it is. If you don't hear back within
a week, assume the message went astray and ping again.

Please give a reasonable window to fix things before disclosing publicly. If
you tell me you intend to publish on a date, I'll work to it rather than argue
about it.

## Scope

**In scope:** the code in this repository and the deployed site at
[www.toponymia.org](https://www.toponymia.org) — authentication and account
takeover, cross-site scripting, injection, authorisation flaws (editing,
moderation, or reading something your account shouldn't), and anything that
exposes another user's data.

**Out of scope**, because they're known and deliberate rather than
undiscovered:

- Volumetric denial of service. The site is one small box; you don't need to
  demonstrate that flooding it works.
- Rate limits being reachable at all. They're per-client fairness, not a
  security boundary, and the numbers are documented choices.
- Missing hardening headers with no demonstrated impact, and findings that are
  a scanner's output pasted verbatim.
- Anything requiring physical access, a compromised user device, or social
  engineering of the maintainer.

## Testing, if you're poking at the live site

Use your own accounts and your own content. Please don't run automated scanners
against the deployed site, don't touch other people's articles or discussions,
and don't create load you wouldn't want pointed at you. The wiki is public and
editable, so it is easy to do real damage by accident while looking for a
theoretical problem.

## Supported versions

The deployed site, and the current `master`. There are no released versions and
no backports.
