from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the Douban and WeRead sources, for books imported from those services."""

    dependencies = [
        ("app", "0067_game_achievements_synced_at_game_achievements_total_and_more"),
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
                    ("douban", "Douban"),
                    ("weread", "WeRead"),
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
                            "douban",
                            "weread",
                            "manual",
                        ],
                    ),
                ),
                name="app_item_source_valid",
            ),
        ),
    ]
