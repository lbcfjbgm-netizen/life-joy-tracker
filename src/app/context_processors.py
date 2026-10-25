# https://docs.djangoproject.com/en/stable/ref/templates/api/#writing-your-own-context-processors

from django.conf import settings

from app.models import MediaTypes, Sources, Status, UserMessage


def export_vars(request):  # noqa: ARG001
    """Export variables to templates."""
    return {
        "REGISTRATION": settings.REGISTRATION,
        "REDIRECT_LOGIN_TO_SSO": settings.REDIRECT_LOGIN_TO_SSO,
        "IMG_NONE": settings.IMG_NONE,
        "TRACK_TIME": settings.TRACK_TIME,
    }


def media_enums(request):  # noqa: ARG001
    """Export media enums to templates."""
    return {
        "MediaTypes": MediaTypes,
        "Sources": Sources,
        "Status": Status,
    }


def sidebar_items(request):
    """Expose the user's sidebar feature toggles to templates.

    ``sidebar_items`` maps the User feature field names to booleans, so a
    template can use ``{% if sidebar_items.create_enabled %}``. Anonymous
    visitors get an empty mapping, which is falsy for every entry.
    """
    if not request.user.is_authenticated:
        return {"sidebar_items": {}}

    return {"sidebar_items": request.user.get_enabled_sidebar_items()}


def persistent_messages(request):
    """Return persistent user notifications that have not been shown yet."""
    if not request.user.is_authenticated:
        return {"persistent_messages": []}

    return {
        "persistent_messages": list(
            UserMessage.objects.filter(
                user=request.user,
                shown_at__isnull=True,
            ),
        ),
    }
