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


class ChoiceItemSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=64)
    title = serializers.CharField(max_length=200, allow_blank=True)


class ChoicesSerializer(serializers.Serializer):
    """JARVIS just asked "which one?" — these are the options; the new text is the answer."""
    intent = serializers.ChoiceField(choices=[(i, i) for i in ("complete_task", "accept_request", "decline_request",
                                                               "change_task", "delete_task")])
    items = ChoiceItemSerializer(many=True, max_length=10)


class InterpretSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=1000, trim_whitespace=True)
    previous = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    # Last turns of the current conversation (kept on the phone only; never stored on the server).
    history = ConversationTurnSerializer(many=True, required=False, default=list)
    # A draft JARVIS has just read back and is waiting to have confirmed (e.g. "Personal task: Call Mehta, tomorrow 11 AM").
    awaiting = serializers.CharField(max_length=400, required=False, allow_blank=True, default="")
    choices = ChoicesSerializer(required=False, allow_null=True, default=None)
    # The waiting card itself, so a "yes" can take the time JARVIS offered, or a new time can be checked again.
    pending = serializers.JSONField(required=False, allow_null=True, default=None)
    # The data card on the phone's screen right now (kind, title, first items), so "is list ka PDF" or
    # "pehla wala kholo" can be understood. Only short plain fields are used (see assistant._clean_screen).
    screen = serializers.JSONField(required=False, allow_null=True, default=None)

    def validate_screen(self, value):
        import json
        if value is not None and (not isinstance(value, dict) or len(json.dumps(value, default=str)) > 6000):
            return None  # not worth failing the request over: it is only a hint
        return value

    def validate_history(self, value):
        return value[-8:]

    def validate_pending(self, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise serializers.ValidationError("pending must be an object.")
        import json
        if len(json.dumps(value, default=str)) > 16000:
            raise serializers.ValidationError("pending is too large.")
        return value


class TurnSerializer(serializers.Serializer):
    """One thing the user said to the background listener (or a wake / reset event)."""
    text = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="", trim_whitespace=True)
    event = serializers.ChoiceField(choices=[("", ""), ("wake", "wake"), ("reset", "reset")], required=False, default="")
    locked = serializers.BooleanField(required=False, default=False)
    private_ok = serializers.BooleanField(required=False, default=True)


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


class ToldSerializer(serializers.Serializer):
    """Things the phone has just spoken or shown once: reminder keys and/or alert ids."""
    keys = serializers.ListField(child=serializers.CharField(max_length=200), required=False, default=list, max_length=100)
    ids = serializers.ListField(child=serializers.CharField(max_length=64), required=False, default=list, max_length=100)
