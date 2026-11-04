from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Kind, Priority, Status

TIME_FORMATS = ["%H:%M", "%H:%M:%S"]


class LoginSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        user = self.user
        data["user"] = {"username": user.username, "role": user.role, "name": user.spoken_name}
        return data


class TaskInputSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    kind = serializers.ChoiceField(choices=Kind.choices, default=Kind.TASK)
    priority = serializers.ChoiceField(choices=Priority.choices, default=Priority.NORMAL)
    due_date = serializers.DateField(required=False, allow_null=True, default=None)
    due_time = serializers.TimeField(required=False, allow_null=True, default=None, input_formats=TIME_FORMATS)
    duration_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=5, max_value=1440,
                                                default=None)
    remind_before_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=1440,
                                                     default=None)
    announcement = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs.get("due_time") and not attrs.get("due_date"):
            raise serializers.ValidationError({"due_date": "A date is required when a time is given."})
        return attrs


class DelegationInputSerializer(TaskInputSerializer):
    staff_id = serializers.IntegerField()
    category_key = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    # The full spoken instruction; TaskPanel keeps it as the task description (guide §3 step 3).
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True, default="")


class PaTaskInputSerializer(TaskInputSerializer):
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True, default="")
    meeting_with_pa = serializers.BooleanField(required=False, default=False)


class TeamDelegationSerializer(TaskInputSerializer):
    staff_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1, max_length=60)
    category_key = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True, default="")


class PaTaskStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[(s, s) for s in (Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED,
                                                                Status.COMPLETED)])
    expected_version = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class RequestInputSerializer(TaskInputSerializer):
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class TaskPatchSerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=200, required=False)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    priority = serializers.ChoiceField(choices=Priority.choices, required=False)
    kind = serializers.ChoiceField(choices=Kind.choices, required=False)
    status = serializers.ChoiceField(
        choices=[(s, s) for s in (Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED, Status.CANCELLED)], required=False
    )
    due_date = serializers.DateField(required=False, allow_null=True)
    due_time = serializers.TimeField(required=False, allow_null=True, input_formats=TIME_FORMATS)
    duration_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=5, max_value=1440)
    remind_before_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=1440)
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")

    def changes(self) -> dict:
        data = dict(self.validated_data)
        data.pop("expected_version", None)
        data.pop("reason", None)
        return data


class VersionSerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)


class ConversationTurnSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=[("user", "user"), ("jarvis", "jarvis")])
    text = serializers.CharField(max_length=2000, allow_blank=True, trim_whitespace=True)


class InterpretSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=1000, trim_whitespace=True)
    previous = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    # Last turns of the current conversation (kept on the phone only; never stored on the server).
    history = ConversationTurnSerializer(many=True, required=False, default=list)
    # A draft JARVIS has just read back and is waiting to have confirmed (e.g. "Personal task: Call Mehta, tomorrow 11 AM").
    awaiting = serializers.CharField(max_length=400, required=False, allow_blank=True, default="")

    def validate_history(self, value):
        return value[-8:]


class AskSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=1000, trim_whitespace=True)
    lang = serializers.ChoiceField(choices=[("en", "en"), ("hi", "hi")], required=False, default="en")
    history = ConversationTurnSerializer(many=True, required=False, default=list)

    def validate_history(self, value):
        return value[-8:]


class MemoryInputSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=300, trim_whitespace=True)


class DecisionSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class AckSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=200)
