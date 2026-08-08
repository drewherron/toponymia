"""Fill an empty database with enough wiki content to exercise the UI.

Deliberately *deep* rather than wide: a handful of real places, each
resolved through the normal Overpass path so the anchors, geometry, and
highlight tokens are the same ones a real click produces — then loaded
with far more history and discussion than any of them has earned. The
point is to see what the article pane, the history list, and the talk page
do when they run out of room:

  reykjavik    140 revisions (history paginates at 100), 14 talk threads,
               one of them 80 posts deep, and a current revision that sits
               at every content ceiling at once — 20 names, 5 competing
               etymologies each with 12 elements and 30 references, 50
               derivations, 50 see-also links, and every prose section at
               the 10k-character limit.
  river-thames a merely large article: 24 revisions, 4 threads.
  ben-nevis    a stub with no article at all, but two talk threads — the
               "discussed before anyone wrote it" state.

The prose is lorem ipsum. Nothing here is a claim about any of these
places, and none of it should ever run against production.
"""

import copy
import random
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import resolve as resolve_mod
from core.articles import save_edit
from core.models import (
    Article,
    ModAction,
    Place,
    Report,
    Revision,
    TalkPost,
    TalkThread,
)
from core.moderation import log_action
from core.serializers import ContentSerializer

# Fixed seed: two runs of this command produce the same corpus, so a bug
# that only shows up on revision 97 is still there tomorrow.
SEED = 20260801

# The window the fake history spans. End well short of today so nothing
# reads as "edited in the future" if the clock drifts.
START = datetime(2024, 8, 1, 9, 0, tzinfo=UTC)
END = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)

# Ceilings from core.serializers, restated so the maxed-out revision can
# aim at them. Kept as literals rather than imported: if a ceiling moves,
# this should keep generating the old volume until someone re-tunes it
# deliberately, not silently start emitting 10x the content.
MAX_MARKDOWN = 10_000
MAX_NAMES = 20
MAX_ETYMOLOGIES = 5
MAX_REFERENCES = 30
MAX_ELEMENTS = 12
MAX_DERIVATIONS = 50
MAX_SEE_ALSO = 50

# Weighted so most seeded etymologies say nothing about confidence (the
# realistic case, and the one the article pane must not clutter) while
# every label still appears somewhere in the corpus — 'folk' especially,
# since it is the one with its own rendering.
CONFIDENCE_VALUES = [
    '', '', '', 'attested', 'probable', 'proposed', 'disputed', 'folk',
    'unknown',
]
ELEMENT_ROLES = ['generic', 'specific', 'affix', 'connective', '']

# name, feature_class, lng, lat, zoom — the arguments a map click sends.
# feature_class must be what web/src/map/features.ts kindOf() reports for
# the layer, or the basemap label will not light up amber.
TARGETS = {
    'reykjavik': ('Reykjavík', 'city', -21.9426, 64.1466, 12),
    'river-thames': ('River Thames', 'waterway', -0.0877, 51.5079, 13),
    'ben-nevis': ('Ben Nevis', 'peak', -5.0036, 56.7969, 13),
}

LOREM = (
    'lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod '
    'tempor incididunt ut labore et dolore magna aliqua enim ad minim veniam '
    'quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo '
    'consequat duis aute irure in reprehenderit voluptate velit esse cillum '
    'eu fugiat nulla pariatur excepteur sint occaecat cupidatat non proident '
    'sunt culpa qui officia deserunt mollit anim id est laborum at vero eos '
    'accusamus iusto odio dignissimos ducimus blanditiis praesentium '
    'voluptatum deleniti atque corrupti quos dolores quas molestias'
).split()

# Plausible-looking name variants to hang etymology sections off. Only the
# first is the endonym; the rest are exonyms, which is the shape the
# article pane renders differently.
NAME_ROSTER = [
    ('Reykjavík', 'is', True, ['non']),
    ('Reykjavik', 'en', False, ['is']),
    ('Reykjavig', 'da', False, ['is']),
    ('Reykjavikur', 'non', False, []),
    ('Rekiavik', 'fr', False, ['is']),
    ('Reikiavik', 'es', False, ['is']),
    ('Reykjavíkur', 'is', False, ['non']),
    ('Rejkiavik', 'pl', False, ['is']),
    ('Reykjavík-borg', 'is', False, ['non']),
    ('Reykjaviikki', 'fi', False, ['is']),
    ('Reikjavik', 'de', False, ['is']),
    ('Rejkjavik', 'cs', False, ['is']),
    ('Reykjavíkurborg', 'is', False, ['non']),
    ('Reikjavikas', 'lt', False, ['is']),
    ('Reykiavik', 'it', False, ['is']),
    ('Reiquiavique', 'pt', False, ['is']),
    ('Rejkjavík', 'hu', False, ['is']),
    ('Reykjavikas', 'lv', False, ['is']),
    ('Rėkjavik', 'sl', False, ['is']),
    ('Reykjavikia', 'la', False, ['is']),
]

THAMES_ROSTER = [
    ('River Thames', 'en', True, ['ang', 'lat']),
    ('Tamesis', 'la', False, ['cym']),
    ('Tamise', 'fr', False, ['lat']),
    ('Themse', 'de', False, ['lat']),
    ('Afon Tafwys', 'cy', False, ['cym']),
    ('Isis', 'en', False, ['lat']),
]

EDIT_SUMMARIES = [
    'expand the {0} section',
    'copyedit',
    'add a reference for {0}',
    'tidy wording in {0}',
    'restructure the etymology',
    'fix a typo',
    'merge duplicate note',
    'add cross-reference',
    'trim repetition',
    'clarify the attestation date',
    'revert an accidental deletion',
    'formatting only',
    '',
]

THREAD_TITLES = [
    'Which spelling should lead the article?',
    'Source for the 9th-century attestation?',
    'Proposal: split the exonym sections',
    'The Danish form looks wrong to me',
    'Long thread: everything about the second element',
    'Can we standardise reference formatting?',
    'Is the Latin form actually attested?',
    'Requesting protection after last week',
    'Talk page housekeeping',
    'Duplicate entry under a different language code',
    'Question from a new editor',
    'Map label does not match the article title',
    'Archive of the 2025 naming discussion',
    'Off-topic but worth recording',
]


class Command(BaseCommand):
    help = 'Seed the database with deep demo content (lorem ipsum).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset', action='store_true',
            help='Delete the demo places first, then re-seed from scratch.',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Allow the command to run with DEBUG off. Do not.',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['force']:
            raise CommandError(
                'refusing to seed lorem ipsum with DEBUG off (--force to '
                'override)'
            )

        self.rng = random.Random(SEED)
        users = self._users()

        if options['reset']:
            self._reset()

        places = {}
        for key, (name, kind, lng, lat, zoom) in TARGETS.items():
            place, created = resolve_mod.resolve(name, kind, lng, lat, zoom)
            places[key] = place
            self.stdout.write(
                f'{place.slug}: {"resolved" if created else "reused"} '
                f'{place.anchor_level} {place.wikidata_qid or ""}'
            )

        self._seed_reykjavik(places['reykjavik'], users)
        self._seed_thames(places['river-thames'], users)
        self._seed_stub(places['ben-nevis'], users)

        self.stdout.write(self.style.SUCCESS(
            f'seeded: {Revision.objects.count()} revisions, '
            f'{TalkThread.objects.count()} threads, '
            f'{TalkPost.objects.count()} posts, '
            f'{Report.objects.count()} reports'
        ))

    # -- users ------------------------------------------------------------

    def _users(self):
        """The accounts the seeded history is attributed to.

        Everything is written as an existing user rather than by creating
        new ones: the point is to exercise content volume, and inventing
        accounts on a database that already has real ones only muddies the
        moderation views.
        """
        from django.contrib.auth import get_user_model

        model = get_user_model()
        users = {u.username: u for u in model.objects.all()}
        if not users:
            raise CommandError('no users exist — create one first')

        mods = [u for u in users.values() if u.is_staff]
        editors = [u for u in users.values() if not u.is_staff] or mods
        return {
            'all': list(users.values()),
            'editors': editors,
            'mods': mods,
            'mod': mods[0] if mods else editors[0],
        }

    def _reset(self):
        places = Place.objects.filter(slug__in=TARGETS)
        # Article.current_revision is PROTECT, so the pointer has to be
        # dropped before the cascade can take the revisions with it.
        Article.objects.filter(place__in=places).update(current_revision=None)
        deleted, _ = places.delete()
        self.stdout.write(f'reset: removed {deleted} rows')

    # -- text generators --------------------------------------------------

    def _words(self, n):
        return ' '.join(self.rng.choice(LOREM) for _ in range(n))

    def _sentence(self, words=None):
        text = self._words(words or self.rng.randint(8, 20))
        return text[0].upper() + text[1:] + '.'

    def _paragraph(self, sentences=None):
        return ' '.join(
            self._sentence() for _ in range(sentences or self.rng.randint(3, 7))
        )

    def _markdown(self, target):
        """Markdown of roughly `target` characters, using enough of the
        syntax (headings, lists, quotes, links, code) that a rendering bug
        has somewhere to show up."""
        parts = []
        length = 0
        while length < target:
            roll = self.rng.random()
            if roll < 0.55:
                block = self._paragraph()
            elif roll < 0.70:
                block = '\n'.join(
                    f'- {self._sentence(self.rng.randint(5, 14))}'
                    for _ in range(self.rng.randint(2, 6))
                )
            elif roll < 0.80:
                block = f'### {self._words(self.rng.randint(2, 5)).title()}'
            elif roll < 0.88:
                block = f'> {self._sentence()}'
            elif roll < 0.94:
                block = (
                    f'{self._sentence()} See [{self._words(2)}]'
                    f'(/place/river-thames) and *{self._words(2)}*, '
                    f'**{self._words(2)}**.'
                )
            else:
                block = f'`{self._words(3)}` — {self._sentence()}'
            parts.append(block)
            length += len(block) + 2
        text = '\n\n'.join(parts)
        return text[:target].rstrip()

    def _reference(self):
        style = self.rng.random()
        if style < 0.4:
            return (
                f'{self._words(2).title()}, *{self._words(4).title()}* '
                f'({self.rng.randint(1854, 2024)}), '
                f'p. {self.rng.randint(3, 900)}.'
            )
        if style < 0.7:
            return f'https://example.org/{self._words(2).replace(" ", "-")}'
        return self._sentence(self.rng.randint(6, 14))

    def _element(self, from_languages):
        return {
            'form': self._words(1),
            'language': (
                self.rng.choice(from_languages) if from_languages else ''
            ),
            'gloss': (
                self._words(self.rng.randint(1, 2))
                if self.rng.random() < 0.85 else ''
            ),
            'role': self.rng.choice(ELEMENT_ROLES),
            'script': '',
            'transliteration': '',
        }

    def _etymology(self, etymology_chars, from_languages):
        return {
            'etymology_md': self._markdown(etymology_chars),
            'confidence': self.rng.choice(CONFIDENCE_VALUES),
            'from_languages': list(from_languages),
            'elements': [
                self._element(from_languages)
                for _ in range(self.rng.randint(0, 3))
            ],
            'references': [
                self._reference() for _ in range(self.rng.randint(0, 4))
            ],
        }

    def _name_entry(self, roster_row, etymology_chars):
        name, language, is_endonym, from_languages = roster_row
        return {
            'name': name,
            'language': language,
            'is_endonym': is_endonym,
            'etymologies': [
                self._etymology(etymology_chars, from_languages)
            ],
        }

    # -- article history --------------------------------------------------

    def _history(self, place, users, roster, count, timeline):
        """Write `count` revisions, each a small mutation of the last.

        Mutating a running content dict rather than generating each
        revision independently is what makes the diff view worth looking
        at: consecutive revisions differ by one paragraph or one field,
        the way real edits do, instead of by everything at once.
        """
        content = {
            'body_md': '',
            'names': [self._name_entry(roster[0], 600)],
            'derivations': [],
            'see_also': [],
        }
        revisions = []
        for i in range(count):
            if i:
                self._mutate(content, roster)
            author = self.rng.choice(users['editors'] + users['mods'])
            subject = content['names'][0]['name']
            comment = self.rng.choice(EDIT_SUMMARIES).format(subject)[:255]
            revision = save_edit(
                place, author, self._validated(copy.deepcopy(content)), comment
            )
            _backdate(revision, timeline[i])
            revisions.append(revision)
        return revisions

    def _primary(self, name_entry):
        """The name's leading hypothesis — what a mutation edits unless it
        is specifically about alternatives. Every seeded name is born with
        one and no branch below removes the last, so this never indexes
        into an empty list."""
        return name_entry['etymologies'][0]

    def _mutate(self, content, roster):
        names = content['names']
        choice = self.rng.random()
        if choice < 0.36:
            # The common edit: rewrite one section.
            self._primary(self.rng.choice(names))['etymology_md'] = (
                self._markdown(self.rng.randint(300, 2500))
            )
        elif choice < 0.42:
            # The contested-name edit. Rarer than a rewrite, but it has to
            # appear in the corpus: a second hypothesis is what the article
            # pane and the diff view render differently, and a demo without
            # one never shows that path.
            target = self.rng.choice(names)
            etymologies = target['etymologies']
            if len(etymologies) < MAX_ETYMOLOGIES:
                etymologies.append(
                    self._etymology(
                        self.rng.randint(200, 900),
                        self.rng.choice(roster)[3],
                    )
                )
            elif len(etymologies) > 1:
                etymologies.pop()
        elif choice < 0.55 and len(names) < min(MAX_NAMES, len(roster)):
            # By first unused row, not by position: a removal earlier in
            # the history shortens the list, and indexing by length would
            # re-add a name that is already there — a duplicate section
            # that the PlaceName dedup hides but the article pane shows.
            present = {entry['name'] for entry in names}
            unused = [row for row in roster if row[0] not in present]
            if unused:
                content['names'].append(
                    self._name_entry(unused[0], self.rng.randint(300, 1400))
                )
        elif choice < 0.62 and len(names) > 2:
            names.pop(self.rng.randrange(1, len(names)))
        elif choice < 0.70:
            # A word-level tweak — the diff case that has to stay readable.
            target = self._primary(self.rng.choice(names))
            words = target['etymology_md'].split(' ')
            if len(words) > 20:
                words[self.rng.randrange(len(words))] = self.rng.choice(LOREM)
                target['etymology_md'] = ' '.join(words)
        elif choice < 0.76:
            # Reclassifying a hypothesis — a one-field edit, which is the
            # diff view's smallest interesting row.
            target = self._primary(self.rng.choice(names))
            target['confidence'] = self.rng.choice(CONFIDENCE_VALUES)
        elif choice < 0.80:
            target = self._primary(self.rng.choice(names))
            elements = target['elements']
            if elements and self.rng.random() < 0.3:
                elements.pop()
            elif len(elements) < MAX_ELEMENTS:
                elements.append(self._element(target['from_languages']))
        elif choice < 0.84:
            target = self._primary(self.rng.choice(names))
            if target['references'] and self.rng.random() < 0.3:
                target['references'].pop()
            elif len(target['references']) < MAX_REFERENCES:
                target['references'].append(self._reference())
        elif choice < 0.93:
            if len(content['derivations']) < MAX_DERIVATIONS:
                content['derivations'].append({
                    'term': self._words(self.rng.randint(1, 3)).title(),
                    'note': self._sentence(),
                    'url': (
                        f'https://example.org/{self._words(1)}'
                        if self.rng.random() < 0.6 else ''
                    ),
                })
        elif len(content['see_also']) < MAX_SEE_ALSO:
            content['see_also'].append(self._words(self.rng.randint(1, 4)).title())

    def _maxed_content(self, roster):
        """Every ceiling at once — the layout's worst case.

        20 names, each with 5 competing etymologies, each of those
        carrying a 10,000-character prose section, 12 elements and 30
        references — plus 50 derivations and 50 see-also entries. That is
        the largest snapshot the validators will accept (roughly a
        megabyte), so if the article pane survives it, it survives
        anything.
        """
        return {
            'body_md': self._markdown(MAX_MARKDOWN),
            'names': [
                {
                    **self._name_entry(row, MAX_MARKDOWN),
                    'etymologies': [
                        {
                            **self._etymology(MAX_MARKDOWN, row[3]),
                            'elements': [
                                self._element(row[3])
                                for _ in range(MAX_ELEMENTS)
                            ],
                            'references': [
                                self._reference()
                                for _ in range(MAX_REFERENCES)
                            ],
                        }
                        for _ in range(MAX_ETYMOLOGIES)
                    ],
                }
                for row in roster[:MAX_NAMES]
            ],
            'derivations': [
                {
                    'term': self._words(self.rng.randint(2, 6)).title(),
                    'note': self._paragraph(2)[:1000],
                    'url': f'https://example.org/{self._words(1)}',
                }
                for _ in range(MAX_DERIVATIONS)
            ],
            'see_also': [
                self._words(self.rng.randint(2, 8)).title()[:255]
                for _ in range(MAX_SEE_ALSO)
            ],
        }

    def _validated(self, content):
        """Run the snapshot through the real serializer.

        Seeded content that the API itself would reject is a trap: the
        article renders, then the first person to press Save gets a 400
        from a field they never touched. Language codes are normalized
        here too (`is` -> `isl`), exactly as a real edit would be.
        """
        serializer = ContentSerializer(data=content)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    # -- talk -------------------------------------------------------------

    def _thread(self, place, users, title, post_count, timeline, long_post=-1):
        thread = TalkThread.objects.create(place=place, title=title[:255])
        _backdate(thread, timeline[0])
        posts = []
        for i in range(post_count):
            body = (
                self._markdown(9500) if i == long_post
                else self._paragraph(self.rng.randint(1, 5))
            )
            post = TalkPost.objects.create(
                thread=thread,
                author=self.rng.choice(users['all']),
                body_md=body,
            )
            _backdate(post, timeline[i])
            posts.append(post)
        return thread, posts

    # -- the three places -------------------------------------------------

    def _seed_reykjavik(self, place, users):
        if place.talk_threads.exists() or hasattr(place, 'article'):
            self.stdout.write('reykjavik already seeded, skipping')
            return

        with transaction.atomic():
            revisions = self._history(
                place, users, NAME_ROSTER, 139, _timeline(139, START, END)
            )
            # The last word is the maxed-out snapshot, so the article the
            # pane loads by default is the worst case rather than a
            # median one.
            biggest = save_edit(
                place,
                users['editors'][0],
                self._validated(self._maxed_content(NAME_ROSTER)),
                'expand every section to the limit',
            )
            _backdate(biggest, END)
            revisions.append(biggest)

            article = Article.objects.get(place=place)
            _backdate(article, START)

            # Two suppressed revisions mid-history: public history skips
            # them, the mod view does not.
            for revision in self.rng.sample(revisions[10:120], 2):
                Revision.objects.filter(pk=revision.pk).update(
                    suppressed=revision.created, suppressed_by=users['mod']
                )
                log_action(
                    users['mod'], ModAction.Action.SUPPRESS_REVISION,
                    target_user=revision.author,
                    reason='seeded demo suppression',
                    article=article, revision=revision,
                )

            threads = []
            for i, title in enumerate(THREAD_TITLES):
                count = 80 if i == 4 else self.rng.randint(2, 15)
                start = START + timedelta(days=30 * i)
                span = min(END, start + timedelta(days=self.rng.randint(3, 90)))
                thread, posts = self._thread(
                    place, users, title, count,
                    _timeline(count, start, span),
                    # One 9,500-character post, in the deep thread.
                    long_post=17 if i == 4 else -1,
                )
                threads.append((thread, posts))

            # A thread whose title is at the 255-character ceiling.
            self._thread(
                place, users, self._sentence(60)[:255], 3,
                _timeline(3, END - timedelta(days=20), END),
            )

            # Soft-deleted post (tombstone in an otherwise live thread),
            # an edited post, and a whole deleted thread.
            _, deep_posts = threads[4]
            for post in (deep_posts[6], deep_posts[41]):
                TalkPost.objects.filter(pk=post.pk).update(
                    deleted=post.created + timedelta(hours=2),
                    deleted_by=users['mod'],
                )
                log_action(
                    users['mod'], ModAction.Action.DELETE_POST,
                    target_user=post.author, reason='seeded demo removal',
                    talk_post=post,
                )
            TalkPost.objects.filter(pk=deep_posts[9].pk).update(
                edited=deep_posts[9].created + timedelta(minutes=40)
            )
            dead_thread = threads[13][0]
            TalkThread.objects.filter(pk=dead_thread.pk).update(
                deleted=END, deleted_by=users['mod']
            )
            log_action(
                users['mod'], ModAction.Action.DELETE_THREAD,
                reason='seeded demo removal',
            )

            # Reports: three open (the mod queue has something to show)
            # and two already handled.
            self._reports(users, revisions, threads, article)

    def _reports(self, users, revisions, threads, article):
        reporters = users['editors']
        categories = ['spam', 'vandalism', 'harassment', 'copyright', 'other']
        open_targets = [
            ('revision', revisions[60]),
            ('revision', revisions[95]),
            ('talk_post', threads[4][1][22]),
        ]
        for i, (kind, target) in enumerate(open_targets):
            report = Report.objects.create(
                reporter=reporters[i % len(reporters)],
                category=categories[i % len(categories)],
                reason=self._sentence(self.rng.randint(6, 20))[:500],
                status=Report.Status.OPEN,
                **{kind: target},
            )
            _backdate(report, END - timedelta(days=9 - i * 3))

        handled = [
            ('revision', revisions[30], Report.Status.RESOLVED),
            ('talk_post', threads[1][1][0], Report.Status.DISMISSED),
        ]
        for i, (kind, target, status) in enumerate(handled):
            report = Report.objects.create(
                reporter=reporters[(i + 1) % len(reporters)],
                category=categories[(i + 3) % len(categories)],
                reason=self._sentence(10)[:500],
                status=status,
                handled_by=users['mod'],
                handled_at=END - timedelta(days=40 - i),
                **{kind: target},
            )
            _backdate(report, END - timedelta(days=42 - i))
            log_action(
                users['mod'],
                ModAction.Action.RESOLVE_REPORT
                if status == Report.Status.RESOLVED
                else ModAction.Action.DISMISS_REPORT,
                reason='seeded demo decision',
                article=article, report=report,
            )

    def _seed_thames(self, place, users):
        if place.talk_threads.exists() or hasattr(place, 'article'):
            self.stdout.write('river-thames already seeded, skipping')
            return
        with transaction.atomic():
            start = START + timedelta(days=120)
            self._history(
                place, users, THAMES_ROSTER, 24, _timeline(24, start, END)
            )
            _backdate(Article.objects.get(place=place), start)
            for i, title in enumerate(THREAD_TITLES[:4]):
                count = self.rng.randint(3, 10)
                begin = start + timedelta(days=60 * i)
                self._thread(
                    place, users, title, count,
                    _timeline(count, begin, begin + timedelta(days=45)),
                )

    def _seed_stub(self, place, users):
        """A place with talk but no article — a discussion that predates
        anyone writing the thing."""
        if place.talk_threads.exists():
            self.stdout.write('ben-nevis already seeded, skipping')
            return
        with transaction.atomic():
            for i, title in enumerate(THREAD_TITLES[10:12]):
                count = self.rng.randint(3, 8)
                begin = START + timedelta(days=200 + 40 * i)
                self._thread(
                    place, users, title, count,
                    _timeline(count, begin, begin + timedelta(days=30)),
                )


def _timeline(count, start, end):
    """`count` ascending timestamps between start and end."""
    if count == 1:
        return [start]
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]


def _backdate(instance, when):
    """Set an auto_now_add `created` after the fact.

    auto_now_add overwrites whatever the caller passes on save(), so the
    only way to place a row in the past is an UPDATE that bypasses the
    field's pre_save hook.
    """
    type(instance).objects.filter(pk=instance.pk).update(created=when)
    instance.created = when
