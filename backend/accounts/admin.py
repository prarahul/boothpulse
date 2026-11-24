from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import AuditLog, OTPCode, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "first_name", "last_name", "phone", "role", "cluster", "booth", "last_sync_at", "is_active")
    list_filter = ("role", "is_active", "constituency", "cluster")
    search_fields = ("username", "phone", "first_name", "last_name")
    autocomplete_fields = ("booth", "cluster")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Campaign role", {"fields": ("phone", "role", "constituency", "cluster", "booth")}),
        ("Device", {"fields": ("last_sync_at", "last_device_id")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Campaign role", {"fields": ("phone", "role", "constituency", "cluster", "booth")}),
    )
    readonly_fields = ("last_sync_at", "last_device_id")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "user", "action", "target", "device_id", "ip")
    list_filter = ("action",)
    search_fields = ("target", "user__username", "user__phone")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = ("phone", "created_at", "expires_at", "attempts", "used")
    exclude = ("code_hash",)
    readonly_fields = ("phone", "created_at", "expires_at", "attempts", "used")
