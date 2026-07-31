# Privacy Policy

*Last updated 2026-08-01.*

Toponymia is a map-based wiki about place names, run by one person. It has no
advertising, no analytics, and no tracking. This policy describes what the site
collects, why, and what happens to it. It is meant to be read, not to be
survived — if anything here is unclear, ask.

## 1. What the site collects

**If you only read**, the site does not ask you for anything. No account is
needed, and no cookie is set until you log in.

**If you create an account**, the site stores:

- **Your username.** It is public — it appears on every edit you make and in
  the revision history, because that is how contributors are credited (see the
  [Terms of Use](/terms), section 2). You can remove it by closing your
  account; see section 4.
- **Your email address.** Required, and verified by a code before the account
  works. It is used to confirm the account, to reset a forgotten password, and
  for essential notices about the account. It is *not* shown publicly and is
  not used for marketing.
- **Your password**, stored only as a salted hash. Nobody, including the
  operator, can read it.

**If you contribute**, your articles, name and etymology fields, and talk
posts are stored and published under your username, along with the time of
each edit. Contributions are permanent: see section 4.

## 2. Server logs

The web server keeps ordinary access logs — your IP address, the time, the page
requested, and your browser's user-agent string. They exist to keep the site
running and to investigate abuse.

**Access logs are retained for 30 days and then deleted.**

IP addresses are not stored in the site's database. Rate limiting (for example,
on signups) counts requests per IP in memory only; those counters are transient
and are never written to disk.

## 3. Cookies and local storage

The site sets two cookies, both strictly necessary and neither used for
tracking:

- `sessionid` — keeps you logged in.
- `csrftoken` — protects against cross-site request forgery.

Your browser also stores two preferences locally: your light/dark theme choice
and your preferred map-label language. These never leave your device and are
not sent to the server.

There are no advertising cookies, no analytics cookies, and no third-party
tracking scripts of any kind.

## 4. Contributions are public and permanent

This is the part most worth understanding before you contribute.

Edits, etymology fields, and talk posts are published under your username and
kept in the article's revision history. That history is how the site credits
authors, and the license you grant when you contribute is irrevocable (Terms of
Use, section 2). So a contribution cannot be withdrawn from the site or from
anyone who has already reused it, even if you later close your account.

Closing your account does remove your **name** from that history: the username
is replaced with an anonymous `[deleted-…]` placeholder that identifies nobody,
and the placeholder can never be registered by anyone else. Your original
username is retired at the same time — nobody can register it afterwards, so
it cannot come to refer to someone else. That applies to you too: closing an
account is not a way to give up a username and take it back later. What
closing cannot do is remove the contributions themselves. Note also that it
does not rewrite prose — if someone addressed you by name in a talk post, that
text stays as written.

Moderators can also remove content from public view: a talk post's text, a
revision's text and edit summary, or a whole article. Two details cut in
opposite directions, and both are worth knowing:

- A removed **revision keeps your username** in the public history, alongside
  the time of the edit. That entry is what credits you for work that may still
  be part of the live article, so removing the content does not remove your
  name from the record.
- A removed **talk post does not keep your username**: publicly it shows no
  author at all.

In both cases the underlying record is retained and remains visible to
moderators, so that a removal can be reviewed or undone.

## 5. Other services your browser contacts

Two parts of the site load from other providers. Because your browser fetches
them directly, those providers can see your IP address:

- **OpenFreeMap** (`tiles.openfreemap.org`) serves the map tiles. It receives
  your IP address and which parts of the map you are viewing.
- **Photon**, run by Komoot (`photon.komoot.io`), powers place search. When you
  search, your search text and your IP address go to Photon. This happens
  whether or not you have an account.

For comparison, when the site looks up map features it queries the
OpenStreetMap Overpass API **from the server**, so Overpass sees the server,
not you.

Two services handle data on the site's behalf: the hosting provider, which
processes traffic and stores the logs described in section 2, and an email
provider, which delivers verification and password-reset messages and therefore
handles your email address.

The site does not sell your data, and does not share it with anyone beyond what
this section describes.

## 6. Children

You must be at least 13 years old to create an account (Terms of Use, section
1). The site deliberately does **not** collect birthdates — asking for one
would create more personal data than it protects.

## 7. Your choices

- **Change your email or password** at any time: click your username in the
  header to open your account panel. Changing your email sends a code to the
  new address, and the change only takes effect once you enter it.
- **Close your account** from the same panel, confirmed with your password.
  Your email address is deleted, your password is erased, your username is
  replaced with an anonymous placeholder, and the account is deactivated. The
  username you were using is retired, so that it cannot later be registered by
  someone else — we keep a record of the name alone for this, with nothing
  linking it back to your former account. If you never contributed anything,
  the account is removed outright and the name stays available.
  Your contributions stay in the revision history, for the reasons in section
  4 — closing an account cannot unpublish work that others have reused. This
  cannot be undone, and an account with an active suspension cannot be closed
  until the suspension is resolved.
- **Ask what is held about you**, or raise any privacy concern, by emailing
  <support@toponymia.org>.

## 8. Changes

This policy may change as the site grows. Changes are posted here with the date
they take effect, and the full revision history of this document is public in
the site's source repository.

## 9. Contact

Questions about this policy, or about data the site holds about you:
<support@toponymia.org>.

For copyright complaints specifically, see the designated-agent contact in the
[Terms of Use](/terms), section 5.
