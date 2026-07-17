"""Validation of article content — the JSON snapshot stored per Revision
(DESIGN.md §4). Kept as plain Serializers: content is a document, not a
model row."""

from rest_framework import serializers


class NameSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    language = serializers.CharField(
        max_length=16, allow_blank=True, default=''
    )
    from_languages = serializers.ListField(
        child=serializers.CharField(max_length=16), default=list
    )
    is_endonym = serializers.BooleanField(default=False)
    etymology_md = serializers.CharField(
        allow_blank=True, trim_whitespace=False, default=''
    )
    references = serializers.ListField(
        child=serializers.CharField(max_length=1000), default=list
    )


class DerivationSerializer(serializers.Serializer):
    term = serializers.CharField(max_length=255)
    note = serializers.CharField(max_length=1000, allow_blank=True, default='')
    url = serializers.URLField(allow_blank=True, default='')


class ContentSerializer(serializers.Serializer):
    body_md = serializers.CharField(allow_blank=True, trim_whitespace=False)
    names = NameSerializer(many=True, default=list)
    derivations = DerivationSerializer(many=True, default=list)
    see_also = serializers.ListField(
        child=serializers.CharField(max_length=255), default=list
    )

    def validate(self, data):
        if not data['body_md'].strip() and not data['names']:
            raise serializers.ValidationError(
                'article needs a body or at least one name'
            )
        return data


class ArticleEditSerializer(serializers.Serializer):
    content = ContentSerializer()
    comment = serializers.CharField(max_length=255, allow_blank=True, default='')


class RevertSerializer(serializers.Serializer):
    revision_id = serializers.IntegerField()
    comment = serializers.CharField(max_length=255, allow_blank=True, default='')


class TalkPostSerializer(serializers.Serializer):
    body_md = serializers.CharField(max_length=10000, trim_whitespace=False)

    def validate_body_md(self, value):
        if not value.strip():
            raise serializers.ValidationError('post cannot be empty')
        return value


class TalkThreadSerializer(TalkPostSerializer):
    title = serializers.CharField(max_length=255)


class ReportSerializer(serializers.Serializer):
    """A flag on a revision or a talk post (DESIGN.md §6)."""

    target_type = serializers.ChoiceField(choices=['revision', 'talk_post'])
    target_id = serializers.IntegerField()
    reason = serializers.CharField(
        max_length=500, allow_blank=True, default=''
    )


class ReportActionSerializer(serializers.Serializer):
    """A moderator's decision on a report. `delete` soft-deletes the
    target and marks the report resolved; the others only set status."""

    action = serializers.ChoiceField(choices=['resolve', 'dismiss', 'delete'])


class ProtectionSerializer(serializers.Serializer):
    """A moderator setting an article's protection level (DESIGN.md §6)."""

    protection_level = serializers.ChoiceField(
        choices=['none', 'registered', 'admin']
    )
