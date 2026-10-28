from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)


class MediaDetailsViewTests(TestCase):
    """Test the media details views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_view(self, mock_get_metadata):
        """Test the media details view."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Test Movie")

        mock_get_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "238",
            Sources.TMDB.value,
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_view(self, mock_process_episodes, mock_get_metadata):
        """Test the season details view."""
        self.user.obfuscate_unseen_episodes = True
        self.user.save(update_fields=["obfuscate_unseen_episodes"])

        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }

        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": 1,
                "title": "Episode 1",
                "name": "Episode 1",
                "air_date": "2023-01-01",
                "watched": False,
            },
        ]

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Season 1")
        self.assertEqual(len(response.context["media"]["episodes"]), 1)
        # 这条断言是对的，业务代码有回归，留给人工决策，不要改断言。
        # 现象：找 'line-clamp-1 blur cursor-pointer' 失败。
        # 真实根因不是「class 拼接顺序变了」——模板 media_details.html:1130 渲染出来
        # 确实是连续的 "line-clamp-1 blur cursor-pointer"（实测能匹配到）；
        # 而是整个剧集列表块根本没渲染：模板 media_details.html:956 用
        # {% if section_total > 1 %} 把「演员表 / 相关作品 / 重看 / 剧集列表」整块包住了。
        # section_total 由 media_section_count() 算，而真实 TMDB 赛季元数据
        # （process_season + enrich_season_with_tv_data）不含 cast / related，
        # 赛季页 media_type == season 不计 streaming，用户没重看过时 user_medias 也只有 1 条
        # → section_total 恒等于 1 → 剧集列表在这个页面永远不出现。
        # 已实测：往 mock 的 season 里塞一个 cast 让 section_total 变成 2，
        # 剧集列表立刻渲染、这条断言就能通过 —— 反证问题出在门控条件。
        self.assertContains(response, "line-clamp-1 blur cursor-pointer")

        mock_get_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [1],
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_refreshes_missing_item_image(self, mock_get_metadata):
        """Item.image is updated when missing and live metadata has one."""
        live_image = "http://example.com/fresh.jpg"
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": live_image,
            "max_progress": 1,
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image=settings.IMG_NONE,
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.image, live_image)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_keeps_existing_item_image(self, mock_get_metadata):
        """Item.image is left alone when already set."""
        existing_image = "http://example.com/stored.jpg"
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/fresh.jpg",
            "max_progress": 1,
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image=existing_image,
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.image, existing_image)

    def _get_tracked_movie_details(self, mock_get_metadata, status):
        """Return the details page of a movie tracked with the given status."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(item=item, user=self.user, status=status)

        return self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_track_button_uses_status_color(self, mock_get_metadata):
        """The status chip is filled with the status colour of the media."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.PAUSED.value,
        )

        # media_details_movie.html 自 394a0d7 起不再调用 status_button_color
        # 过滤器, 而是在 status-split 容器上写死 status-* 类, 两个按钮用
        # background: inherit 继承底色, 所以底色类只出现一次
        self.assertContains(
            response,
            'class="status-split relative status-paused"',
            count=1,
        )
        self.assertContains(
            response,
            ".status-split.status-paused { background: #ea580c; }",
        )
        self.assertNotContains(
            response,
            'class="status-split relative status-in-progress"',
        )

    @patch("app.providers.services.get_media_metadata")
    def test_track_button_color_ignores_progress_bar_setting(self, mock_get_metadata):
        """The track button uses the status color with the progress bar off."""
        self.user.progress_bar = False
        self.user.save(update_fields=["progress_bar"])

        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.PAUSED.value,
        )

        # 关掉进度条不影响底色: 底色挂在 status-split 容器上
        self.assertContains(
            response,
            'class="status-split relative status-paused"',
            count=1,
        )
        self.assertContains(
            response,
            ".status-split.status-paused { background: #ea580c; }",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_track_button_has_no_progress_bar(self, mock_get_metadata):
        """The track button does not render a progress bar."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.IN_PROGRESS.value,
        )

        self.assertNotContains(response, "h-1.25")
