from unittest import mock

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
from app.templatetags import app_tags
from users.forms import UserUpdateForm

# 本机没有外网，TMDB 拿不到元数据，Movie.save() 里 process_status() 会
# 直接 KeyError: 'max_progress'（见 app/models.py:1402），整个 setUp 就红了。
# 这里给一条假元数据，让建测试电影这件事不依赖网络。
FAKE_METADATA = {
    "max_progress": 1,
    "title": "Test Movie",
    "image": "http://example.com/image.jpg",
    "details": {},
}


class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.external_credentials = {
            "username": "test2",
            "password": "12345",
            "profile_private": True,
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.external_user = get_user_model().objects.create_user(
            **self.external_credentials
        )
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 2
        # 全程把 provider 打成假的：本机没有外网，真实调用会在 save() 里炸。
        patcher = mock.patch(
            "app.providers.services.get_media_metadata",
            return_value=FAKE_METADATA,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        for i in range(1, 6):
            item = Item.objects.create(
                media_id=movies_id[i - 1],
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Test Movie {i}",
                image="http://example.com/image.jpg",
            )
            status = (
                Status.COMPLETED.value
                if i <= num_completed
                else Status.IN_PROGRESS.value
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=status,
                progress=1 if i <= num_completed else 0,
                score=i,
            )

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def test_media_list_with_filters(self):
        """电影库的排序/版式照常生效，状态则永远锁在「全部」。

        电影 / 剧集 / 季的数据全部来自豆瓣，那边只有「已看过」一种标记，
        库里一条在看 / 已搁置 / 已放弃都没有；一旦让状态筛选生效（哪怕只是
        把 ?status=Dropped 存进偏好），用户裸点「电影」就会看到空白页。
        所以这三类库的状态永远全量，URL 里的 status 一律无视、也不写库。
        """
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        # 状态被锁死：既不是 URL 里的 Completed，也不是库里的旧值
        self.assertEqual(response.context["current_status"], "All")
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_layout"], "table")

        # 全量返回，不再被筛成 2 条
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.user.refresh_from_db()
        # 状态偏好一个字节都没被 URL 改写
        self.assertEqual(self.user.movie_status, "All")
        # 排序和版式照旧要记住
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_movie_status_choices_hidden(self):
        """电影 / 剧集 / 季的列表页不再渲染状态下拉（整个控件隐藏）。"""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["status_choices"]), [])

        # 「已放弃 / 在看 / 已搁置」这些词一个都不该出现在页面上
        content = response.content.decode()
        self.assertNotIn("已放弃", content)
        self.assertNotIn("已搁置", content)

    def test_dirty_status_url_does_not_empty_movie_list(self):
        """老书签 / 脏链接 ?status=Dropped 也不能把电影库筛空。"""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?status=Dropped",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_status"], "All")
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.user.refresh_from_db()
        # 脏值不许落库：否则下次裸进来又会被筛空
        self.assertNotEqual(self.user.movie_status, "Dropped")

    def test_dirty_status_url_does_not_redirect_with_status(self):
        """裸进列表页时的 302 补全不许把状态拼回地址栏。"""
        # 先记住一个版式，制造出「有 session 桶 → 会触发 302」的条件
        self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=table&status=Dropped",
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
        )
        url = response.headers.get("Location", "")
        if url:  # 只有真的跳了才检查参数
            self.assertNotIn("status=", url)

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=grid",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=table",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_table_items.html")

    def test_media_list_soft_navigation_returns_full_page(self):
        """Soft-navigation body swaps (after an edit modal) get the full page."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
            headers={"hx-request": "true", "x-soft-navigation": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

    def test_public_media_list_ignores_invalid_filters(self):
        """Test invalid public filters fall back to the target user's preferences."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])

        # 视图里有一条历史兜底：整个库一条有评分的数据都没有时，会把 sort 从
        # score 换成 progress（app/views.py:665-671）。这里给这个用户造一条有评分
        # 的电影，让下面的断言测到「非法参数回退到用户偏好」本身，而不是那条兜底。
        scored_item = Item.objects.create(
            media_id="999001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Scored Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=scored_item,
            user=self.external_user,
            status=Status.COMPLETED.value,
            progress=1,
            score=9,
        )

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
            + "?status=invalid&sort=bad_field&layout=invalid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["current_status"], self.external_user.movie_status
        )
        self.assertEqual(
            response.context["current_sort"], self.external_user.movie_sort
        )
        self.assertEqual(
            response.context["current_layout"], self.external_user.movie_layout
        )

    def test_anonymous_user_can_view_public_media_list(self):
        """Test anonymous users can view public media lists."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])
        self.client.logout()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)

    def test_profile_private_defaults_to_true(self):
        """Test new users have private profiles by default."""
        user = get_user_model().objects.create_user(
            username="private-default",
        )

        self.assertTrue(user.profile_private)

    def test_private_media_list(self):
        """Test the private media list view."""
        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 404)

        form = UserUpdateForm(
            data={"username": "test2", "profile_private": False},
            instance=self.external_user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        external_user = form.save()
        external_user.refresh_from_db()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)
