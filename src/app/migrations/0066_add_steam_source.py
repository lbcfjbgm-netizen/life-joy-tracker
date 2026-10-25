from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the Steam source, for games imported from Steam without IGDB."""

    dependencies = [
        ("app", "0065_episode_notes_historicalepisode_notes"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="item",
            name="app_item_source_valid",
        ),
        migrations.AlterField(
            model_name="item",
            name="source",
            field=models.CharField(
                choices=[
                    ("tmdb", "The Movie Database"),
                    ("mal", "MyAnimeList"),
                    ("mangaupdates", "MangaUpdates"),
                    ("igdb", "Internet Game Database"),
                    ("openlibrary", "Open Library"),
                    ("hardcover", "Hardcover"),
                    ("comicvine", "Comic Vine"),
                    ("bgg", "BoardGameGeek"),
                    ("steam", "Steam"),
                    ("manual", "Manual"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="item",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "source__in",
                        [
                            "tmdb",
                            "mal",
                            "mangaupdates",
                            "igdb",
                            "openlibrary",
                            "hardcover",
                            "comicvine",
                            "bgg",
                            "steam",
                            "manual",
                        ],
                    ),
                ),
                name="app_item_source_valid",
            ),
        ),
    ]
