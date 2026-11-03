from rest_framework.permissions import BasePermission


class IsMD(BasePermission):
    message = "Only the MD can do this."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_md and request.user.is_active)


class IsPA(BasePermission):
    message = "Only the PA can do this."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_pa and request.user.is_active)


class IsMDOrPA(BasePermission):
    message = "Not allowed."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and (user.is_md or user.is_pa))
