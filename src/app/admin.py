import contextlib

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered

from app.models import (
    Episode,
    Highlight,
    ImportItem,
    ImportJob,
    Item,
    Note,
    UserMessage,
)


# Custom ModelAdmin classes with search functionality
@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    """Custom admin for Item model with search and filter options."""

    search_fields = ["title", "media_id", "source"]
    list_display = [
        "title",
        "media_id",
        "season_number",
        "episode_number",
        "media_type",
        "source",
    ]
    list_filter = ["media_type", "source"]


@admin.register(Episode)
class EpisodeAdmin(admin.ModelAdmin):
    """Custom admin for Episode model with search and filter options."""

    search_fields = ["item__title", "related_season__item__title"]
    list_display = ["__str__", "end_date"]


@admin.register(UserMessage)
class UserMessageAdmin(admin.ModelAdmin):
    """Custom admin for persistent user messages."""

    search_fields = ["user__username", "message"]
    list_display = ["message", "level", "user", "created_at", "shown_at"]
    list_filter = ["level", "shown_at"]


class MediaAdmin(admin.ModelAdmin):
    """Custom admin for regular media model with search and filter options."""

    search_fields = ["item__title", "user__username", "notes"]
    list_display = ["__str__", "status", "score", "user"]
    list_filter = ["status"]


# Register models with custom admin classes


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    """Admin for the bulk import bookkeeping rows."""

    search_fields = ["source", "task_type", "error"]
    list_display = ["__str__", "source", "task_type", "status", "created_at", "finished_at"]
    list_filter = ["status", "source", "task_type"]
    readonly_fields = ["created_at"]


@admin.register(ImportItem)
class ImportItemAdmin(admin.ModelAdmin):
    """Admin for the per row import results."""

    search_fields = ["external_id", "external_title", "note"]
    list_display = ["__str__", "job", "status", "match_method", "created_at"]
    list_filter = ["status", "match_method"]
    raw_id_fields = ["job", "item"]


@admin.register(Highlight)
class HighlightAdmin(admin.ModelAdmin):
    """Admin for imported book highlights."""

    search_fields = ["item__title", "text", "chapter"]
    list_display = ["__str__", "item", "page", "chapter", "created_at"]
    list_filter = ["created_at"]
    raw_id_fields = ["item"]


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    """Admin for notes attached to an item."""

    search_fields = ["item__title", "content", "abstract", "chapter"]
    list_display = ["__str__", "item", "chapter", "created_at"]
    list_filter = ["created_at"]
    raw_id_fields = ["item"]


# Auto-register remaining models
app_models = apps.get_app_config("app").get_models()
SpecialModels = [
    "Item",
    "Episode",
    "BasicMedia",
    "UserMessage",
    "ImportJob",
    "ImportItem",
    "Highlight",
    "Note",
    "AICopy",
    # 游玩时长快照不是 Media 的子类，套 MediaAdmin 会报 admin.E108。
    "PlaytimeSnapshot",
]
for model in app_models:
    if (
        not model.__name__.startswith("Historical")
        and model.__name__ not in SpecialModels
    ):
        with contextlib.suppress(AlreadyRegistered):
            admin.site.register(model, MediaAdmin)
