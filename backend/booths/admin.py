from django.contrib import admin

from .models import Booth, Cluster, Constituency, Voter, VoterChange


@admin.register(Constituency)
class ConstituencyAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "state", "poll_date")


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("name", "constituency")
    list_filter = ("constituency",)
    search_fields = ("name",)


@admin.register(Booth)
class BoothAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "cluster", "constituency")
    list_filter = ("constituency", "cluster")
    search_fields = ("name", "number")
    list_editable = ("cluster",)


@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = ("serial_no", "name", "epic", "booth", "house_no", "age", "gender", "status", "needs_assistance")
    list_filter = ("status", "needs_assistance", "booth__cluster", "gender")
    search_fields = ("name", "epic", "house_no")
    list_select_related = ("booth",)
    raw_id_fields = ("booth",)
    readonly_fields = ("status_ts", "details_ts", "updated_at", "updated_by")


@admin.register(VoterChange)
class VoterChangeAdmin(admin.ModelAdmin):
    list_display = ("received_at", "user", "voter", "client_ts", "applied", "device_id")
    readonly_fields = [f.name for f in VoterChange._meta.fields]
    raw_id_fields = ("voter",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
