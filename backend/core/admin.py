"""Admin for setup and support.

PrivateTask is intentionally NOT registered: a developer or support person with
admin access must not be able to browse the MD's personal tasks.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    Alert, AuditLog, IntegrationCommand, SharedTask, StaffMember, TaskCategory, TaskPanelLink, TaskRequest, User,
)


@admin.register(User)
class JarvisUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("JARVIS", {"fields": ("role", "display_name")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("JARVIS", {"fields": ("role", "display_name")}),)
    list_display = ("username", "display_name", "role", "is_active", "last_login")


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "department", "designation", "external_id", "active", "source")
    list_filter = ("active", "department")


@admin.register(SharedTask)
class SharedTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "assignee_type", "staff", "priority", "status", "sync_state", "remote_legacy_id", "due_at")
    list_filter = ("assignee_type", "status", "sync_state")
    readonly_fields = ("remote_task_uuid", "remote_legacy_id", "remote_status", "remote_version", "sync_revision",
                       "version")


@admin.register(TaskCategory)
class TaskCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "department", "active")


@admin.register(TaskPanelLink)
class TaskPanelLinkAdmin(admin.ModelAdmin):
    """The grant itself is never displayed; replace it with the taskpanel_link command."""

    list_display = ("user", "actor_key", "actor_name", "scopes", "expires_at", "active")
    exclude = ("grant",)
    readonly_fields = ("user", "actor_key", "linked_at")


@admin.register(IntegrationCommand)
class IntegrationCommandAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "actor", "state", "attempts", "error_code", "created_at")
    list_filter = ("state", "type")
    readonly_fields = [f.name for f in IntegrationCommand._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(TaskRequest)
class TaskRequestAdmin(admin.ModelAdmin):
    list_display = ("kind", "state", "requested_by", "created_at", "decided_at")
    list_filter = ("state", "kind")
    readonly_fields = ("payload", "payload_hash", "base_version")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("title", "recipient", "kind", "priority", "created_at", "acknowledged_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target_type", "target_id")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
