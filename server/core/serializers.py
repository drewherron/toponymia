"""Validation of article content — the JSON snapshot stored per Revision. Kept
as plain Serializers: content is a document, not a model row."""

from rest_framework import serializers

from .languages import normalize_code
from .models import ModAction

# Size ceilings for a stored snapshot. Every revision is kept forever and the
# write throttle allows 40 edits/min, so unbounded fields are a storage-growth
# lever for any verified account — these are the backstop. Set well above
# anything a real article needs (the longest etymologies run a few thousand
# characters) and enforced only on *new* edits: revert copies an old snapshot
# straight through save_edit without revalidating, so pre-existing revisions
# stay revertable whatever their size.
MAX_MARKDOWN = 10_000       # one etymology (or a legacy body)
MAX_NAMES = 20              # names on one article
MAX_ETYMOLOGIES = 5         # competing hypotheses under one name
MAX_REFERENCES = 30         # references on one etymology
MAX_FROM_LANGUAGES = 10     # source languages on one etymology
MAX_ELEMENTS = 12           # etymon rows under one etymology
MAX_DERIVATIONS = 50
MAX_SEE_ALSO = 50

# Longest finite account ban, in days. See BanSerializer.expires_days.
MAX_BAN_DAYS = 3650


class LanguageCodeField(serializers.CharField):
    """An ISO 639-3 code; 639-1 two-letter input is normalized (fr -> fra)."""

    def __init__(self, **kwargs):
        super().__init__(max_length=16, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data).strip()
        if not value:
            return ''
        code = normalize_code(value)
        if code is None:
            raise serializers.ValidationError(
                f'unknown language code "{value}" — use ISO 639-3'
            )
        return code


class ElementSerializer(serializers.Serializer):
    """One etymon: the word a name is built from. `form` is the only
    required field — an editor who knows *aqua* 'water' but not whether it
    counts as generic or specific must not be blocked on the taxonomy.

    `script` and `transliteration` are accepted but not offered by the
    editor yet; unknown keys are dropped, so exposing them later (or adding
    a Wikidata `lexeme_id`) is a form change, not a schema migration.
    """

    ROLES = ['generic', 'specific', 'affix', 'connective']

    form = serializers.CharField(max_length=100)
    language = LanguageCodeField(allow_blank=True, default='')
    gloss = serializers.CharField(max_length=200, allow_blank=True, default='')
    role = serializers.ChoiceField(
        choices=ROLES, allow_blank=True, default=''
    )
    script = serializers.CharField(
        max_length=100, allow_blank=True, default=''
    )
    transliteration = serializers.CharField(
        max_length=200, allow_blank=True, default=''
    )


class EtymologySerializer(serializers.Serializer):
    """One *hypothesis* about where a name comes from. A name carries a
    list of these: contested names are the normal case in toponymy, and
    one blob per name forces editors to either edit-war over it or bury
    the alternatives in prose.

    `from_languages` and `references` live here rather than on the name
    because rival hypotheses genuinely cite different languages and
    different sources; hanging them off the name asserts one answer.

    `confidence` distinguishes '' (nobody said) from 'unknown' (an
    assertion that scholarship doesn't know). Collapsing the two would
    poison the field the first time anyone trusted it.
    """

    CONFIDENCE = [
        'attested', 'probable', 'proposed', 'disputed', 'folk', 'unknown',
    ]

    etymology_md = serializers.CharField(
        allow_blank=True, trim_whitespace=False, default='',
        max_length=MAX_MARKDOWN,
    )
    confidence = serializers.ChoiceField(
        choices=CONFIDENCE, allow_blank=True, default=''
    )
    from_languages = serializers.ListField(
        child=LanguageCodeField(), default=list,
        max_length=MAX_FROM_LANGUAGES,
    )
    elements = ElementSerializer(
        many=True, default=list, max_length=MAX_ELEMENTS
    )
    references = serializers.ListField(
        child=serializers.CharField(max_length=1000), default=list,
        max_length=MAX_REFERENCES,
    )


class NameSerializer(serializers.Serializer):
    """A name, plus every hypothesis about it. name / language /
    is_endonym are facts about the name itself; everything contestable
    hangs off `etymologies`, whose first entry is the primary one (order is
    editorial — no separate flag to keep consistent).

    `transliteration` is the name romanized, and belongs beside `name`
    rather than inside it. A name written in a script the reader can't
    read is a heading they can't say, but folding the romanization into
    `name` ("上海 (Shanghai)") would put display formatting into the value
    that feeds search, the page <title> and the meta description, where
    nothing can reliably take it back out again. Same reasoning, and the
    same field, as ElementSerializer one level down."""

    name = serializers.CharField(max_length=255)
    transliteration = serializers.CharField(
        max_length=200, allow_blank=True, default=''
    )
    language = LanguageCodeField(allow_blank=True, default='')
    is_endonym = serializers.BooleanField(default=False)
    etymologies = EtymologySerializer(
        many=True, default=list, max_length=MAX_ETYMOLOGIES
    )


class DerivationSerializer(serializers.Serializer):
    term = serializers.CharField(max_length=255)
    note = serializers.CharField(max_length=1000, allow_blank=True, default='')
    url = serializers.URLField(allow_blank=True, default='')


class ContentSerializer(serializers.Serializer):
    # Vestigial: no longer editable in the UI (everything belongs to a
    # name's etymology), but kept in the snapshot schema so pre-removal
    # revisions render and revert unchanged.
    body_md = serializers.CharField(
        allow_blank=True, trim_whitespace=False, default='',
        max_length=MAX_MARKDOWN,
    )
    names = NameSerializer(many=True, default=list, max_length=MAX_NAMES)
    derivations = DerivationSerializer(
        many=True, default=list, max_length=MAX_DERIVATIONS
    )
    see_also = serializers.ListField(
        child=serializers.CharField(max_length=255), default=list,
        max_length=MAX_SEE_ALSO,
    )

    def validate(self, data):
        if not data['names']:
            raise serializers.ValidationError(
                'article needs at least one name'
            )
        return data


class ArticleEditSerializer(serializers.Serializer):
    content = ContentSerializer()
    comment = serializers.CharField(max_length=255, allow_blank=True, default='')


class RevertSerializer(serializers.Serializer):
    revision_id = serializers.IntegerField()
    comment = serializers.CharField(max_length=255, allow_blank=True, default='')


class ArticleDeleteSerializer(serializers.Serializer):
    """An admin's whole-article deletion. The reason is
    optional but goes straight into the audit log, so it is the only record
    of *why* — write it as if the author will read it."""

    reason = serializers.CharField(
        max_length=500, allow_blank=True, default=''
    )


class TalkPostSerializer(serializers.Serializer):
    body_md = serializers.CharField(max_length=10000, trim_whitespace=False)

    def validate_body_md(self, value):
        if not value.strip():
            raise serializers.ValidationError('post cannot be empty')
        return value


class TalkThreadSerializer(TalkPostSerializer):
    title = serializers.CharField(max_length=255)


class ReportSerializer(serializers.Serializer):
    """A flag on a revision or a talk post."""

    target_type = serializers.ChoiceField(choices=['revision', 'talk_post'])
    target_id = serializers.IntegerField()
    category = serializers.ChoiceField(
        choices=[
            'spam', 'vandalism', 'harassment', 'personal_info',
            'copyright', 'other',
        ],
        default='other',
    )
    reason = serializers.CharField(
        max_length=500, allow_blank=True, default=''
    )


class ReportActionSerializer(serializers.Serializer):
    """A moderator's decision on a report. `delete` soft-deletes a reported
    talk post; `suppress` soft-hides a reported revision; both then resolve.
    `resolve`/`dismiss` only set status."""

    action = serializers.ChoiceField(
        choices=['resolve', 'dismiss', 'delete', 'suppress']
    )
    # Optional moderator note, recorded in the audit log.
    reason = serializers.CharField(
        max_length=500, allow_blank=True, default=''
    )


class BanSerializer(serializers.Serializer):
    """A moderator's account ban.

    Nulls are tolerated where the hand-rolled parsing this replaced tolerated
    them (it coerced with `or`), so an older client sending an explicit null
    still means "permanent" / "no reason" rather than a 400.
    """

    reason = serializers.CharField(
        max_length=500, allow_blank=True, allow_null=True, default=''
    )
    # 0 or null = permanent, which is the `expires=None` path. The ceiling
    # exists because timezone.now() + timedelta(days=...) raises OverflowError
    # well before it reaches datetime.max — an unbounded value here was a 500
    # from the request body. Ten years is indistinguishable from permanent for
    # any real sanction, and permanent has its own spelling anyway.
    expires_days = serializers.IntegerField(
        min_value=0, max_value=MAX_BAN_DAYS, allow_null=True, default=0
    )
    remove_content = serializers.BooleanField(default=False)


class RoleSerializer(serializers.Serializer):
    """A superuser promoting a user to moderator or demoting one back."""

    role = serializers.ChoiceField(choices=['user', 'moderator'])
    reason = serializers.CharField(
        max_length=500, allow_blank=True, allow_null=True, default=''
    )


class ActionGroupField(serializers.ListField):
    """`?action=` as one kind or a comma-separated group of them.

    The useful questions are about *kinds* of action — "what has been
    removed", "what has been put back" — and each of those spans several
    values (a removal is a post, a thread, an article or a revision). Doing
    that grouping on the client would mean one request per member, or
    filtering a page after the fact and reporting a `total` that doesn't
    match the rows. A single value still works: it splits to a list of one.
    """

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = [part for part in data.split(',') if part]
        return super().to_internal_value(data)


class AuditFilterSerializer(serializers.Serializer):
    """Query-string filters for the global audit feed. All optional — the
    unfiltered feed is the default view.

    `actor` and `target` are user ids that go straight into a queryset
    filter, where a non-numeric value raises ValueError rather than
    returning nothing: a 500 from a hand-typed URL. Validating them here
    turns that into a 400.
    """

    actor = serializers.IntegerField(required=False)
    target = serializers.IntegerField(required=False)
    action = ActionGroupField(
        child=serializers.ChoiceField(choices=ModAction.Action.choices),
        required=False,
        allow_empty=False,
    )
    # Same treatment as the ids above: validated here so a hand-typed page
    # number is a 400, not a slice on a string. Clamped to the feed's length
    # by the view, which is the only thing that knows it.
    offset = serializers.IntegerField(required=False, min_value=0)


class ProtectionSerializer(serializers.Serializer):
    """A moderator setting an article's protection level."""

    protection_level = serializers.ChoiceField(
        choices=['none', 'registered', 'admin']
    )
