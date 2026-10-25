from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)


class CreateEntryViewTests(TestCase):
    """Test the create entry view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_create_entry_get(self):
        """Test the GET method of create_entry view."""
        response = self.client.get(reverse("create_entry"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/create_entry.html")
        self.assertIn("media_types", response.context)

        self.assertEqual(response.context["media_types"], MediaTypes.values)

    def test_create_entry_get_hides_disabled_media_types(self):
        """Disabled media types are not offered by the create form."""
        self.user.comic_enabled = False
        self.user.boardgame_enabled = False
        self.user.save(update_fields=["comic_enabled", "boardgame_enabled"])

        response = self.client.get(reverse("create_entry"))
        media_types = response.context["media_types"]

        self.assertNotIn(MediaTypes.COMIC.value, media_types)
        self.assertNotIn(MediaTypes.BOARDGAME.value, media_types)
        self.assertIn(MediaTypes.GAME.value, media_types)
        # Episodes are not a standalone switch, they ride along with seasons.
        self.assertIn(MediaTypes.EPISODE.value, media_types)

    # ── 以下 4 条：断言是对的，业务代码有回归（同 test_forms.py 的 IMG_NONE 问题）──
    # POST /create 会走到 ManualItemForm，clean() 把 settings.IMG_NONE（640 字符的
    # data:image/svg+xml 内联占位图）塞进 image 字段，随后过不了 Item.image 的
    # URLField(max_length=200) 校验，于是表单判无效、Item 一条都建不出来。
    # 已用 test client 复现：response 里带 "Image: Enter a valid URL." 消息。
    # 该修的是业务代码，不是这里的断言，所以保持原样等决策。
    def test_create_entry_post_movie(self):
        """Test creating a movie entry."""
        form_data = {
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "status": Status.COMPLETED.value,
            "score": 8,
            "progress": 1,
            "start_date": "2023-01-01T00:00",
            "end_date": "2023-01-02T00:00",
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="Test Movie",
                media_type=MediaTypes.MOVIE.value,
            ).exists(),
        )

        movie = Movie.objects.get(item__title="Test Movie")
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.score, 8)
        self.assertEqual(movie.progress, 1)
        self.assertEqual(movie.user, self.user)

    # 同上（IMG_NONE 过不了 image 的 URL/长度校验），等业务代码修复。
    def test_create_entry_post_tv(self):
        """Test creating a TV show entry."""
        form_data = {
            "title": "Test TV Show",
            "media_type": MediaTypes.TV.value,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="Test TV Show",
                media_type=MediaTypes.TV.value,
            ).exists(),
        )

        tv = TV.objects.get(item__title="Test TV Show")
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(tv.score, 7)
        self.assertEqual(tv.user, self.user)

    # 同上（IMG_NONE 过不了 image 的 URL/长度校验），等业务代码修复。
    def test_create_entry_post_season(self):
        """Test creating a season entry with parent TV."""
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "parent_tv": parent_tv.id,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="TV Show",
                media_type=MediaTypes.SEASON.value,
                season_number=1,
            ).exists(),
        )

        season = Season.objects.get(item__title="TV Show")
        self.assertEqual(season.status, Status.IN_PROGRESS.value)
        self.assertEqual(season.score, 7)
        self.assertEqual(season.user, self.user)
        self.assertEqual(season.related_tv, parent_tv)

    # 同上（IMG_NONE 过不了 image 的 URL/长度校验），等业务代码修复。
    def test_create_entry_post_episode(self):
        """Test creating an episode entry with parent season."""
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Show",
            season_number=1,
        )
        parent_season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=parent_tv,
            status=Status.IN_PROGRESS.value,
        )

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.EPISODE.value,
            "season_number": 1,
            "episode_number": 1,
            "parent_season": parent_season.id,
            "end_date": "2023-01-02T00:00",
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="TV Show",
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=1,
            ).exists(),
        )

        episode = Episode.objects.get(item__title="TV Show")
        self.assertEqual(episode.related_season, parent_season)
        end_date_local = timezone.localtime(episode.end_date)
        self.assertEqual(end_date_local.strftime("%Y-%m-%d %H:%M"), "2023-01-02 00:00")

    def test_create_entry_post_duplicate_item(self):
        """Test creating a duplicate item."""
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Show",
            season_number=1,
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=parent_tv,
            status=Status.IN_PROGRESS.value,
        )

        initial_count = Item.objects.count()

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "parent_tv": parent_tv.id,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
            "repeats": 0,
        }

        with transaction.atomic():
            self.client.post(reverse("create_entry"), form_data)

        self.assertEqual(Item.objects.count(), initial_count)
