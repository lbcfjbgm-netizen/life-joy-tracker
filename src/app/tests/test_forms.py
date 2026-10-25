from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from app.forms import (
    AnimeForm,
    EpisodeForm,
    GameForm,
    ManualItemForm,
    SeasonForm,
    TvForm,
)
from app.models import TV, Item, MediaTypes, Season, Sources, Status


class BasicMediaForm(TestCase):
    """Test the standard media form."""

    def setUp(self):
        """Create a user."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )

        Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test tv",
            image="http://example.com/image.jpg",
        )

    def test_valid_media_form(self):
        """Test the standard media form with valid data."""
        form_data = {
            "media_id": "1",
            "source": Sources.MAL.value,
            "media_type": MediaTypes.ANIME.value,
            "user": self.user.id,
            "score": 7.5,
            "progress": 25,
            "status": Status.PAUSED.value,
            "repeats": 0,
            "start_date": "2023-02-01",
            "end_date": "2023-06-30",
            "notes": "New notes",
        }
        form = AnimeForm(data=form_data)
        self.assertTrue(form.is_valid())

    def test_valid_tv_form(self):
        """Test the TV form with valid data."""
        form_data = {
            "media_id": "1",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "user": self.user.id,
            "score": 7.5,
            "status": Status.COMPLETED.value,
            "repeats": 0,
            "notes": "New notes",
        }
        form = TvForm(data=form_data)
        self.assertTrue(form.is_valid())

    def test_media_date_fields_are_optional(self):
        """Start and end dates should remain optional in tracking forms."""
        form = AnimeForm()

        self.assertFalse(form.fields["start_date"].required)
        self.assertFalse(form.fields["end_date"].required)

    def test_valid_season_form(self):
        """Test the season form with valid data."""
        form_data = {
            "media_id": "1",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.SEASON.value,
            "user": self.user.id,
            "score": 7.5,
            "status": Status.COMPLETED.value,
            "repeats": 0,
            "season_number": 1,
            "notes": "New notes",
        }
        form = SeasonForm(data=form_data)
        self.assertTrue(form.is_valid())

    def test_valid_episode_form(self):
        """Test the episode form with valid data."""
        form_data = {
            "end_date": "2023-06-01",
        }
        form = EpisodeForm(data=form_data)
        self.assertTrue(form.is_valid())

    def test_valid_episode_datetime_form(self):
        """Test the episode form with valid data."""
        form_data = {
            "end_date": "2023-06-01T12:00:00Z",
        }
        form = EpisodeForm(data=form_data)
        self.assertTrue(form.is_valid())


class BasicGameForm(TestCase):
    """Test the game form."""

    def setUp(self):
        """Create a user."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.item = Item.objects.create(
            media_id="1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/image.jpg",
        )

    def test_default_progress(self):
        """Test the game form using the default progress format."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "25:00",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 1500)

    def test_plain_number_progress(self):
        """Test the game form with a plain number for hours (e.g., '5')."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "5",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 300)

    def test_alternate_progress(self):
        """Test the game form using an alternate progress format."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "25h 00min",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 1500)

    def test_second_alternate_progress(self):
        """Test the game form using a second alternate progress format."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "30min",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 30)

    def test_third_alternate_progress(self):
        """Test the game form using a second alternate progress format."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "9h",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 540)

    def test_fourth_alternate_progress(self):
        """Test the game form using a second alternate progress format."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "9h30min",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 570)

    def test_float_progress(self):
        """Test the game form with float progress format (e.g., 1.5 hours)."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "1.5",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 90)

    def test_float_progress_half_hour(self):
        """Test the game form with 0.5 float progress (30 minutes)."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "0.5",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["progress"], 30)

    def test_invalid_negative_float_progress(self):
        """Test that negative float progress is rejected."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "-1.5",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertFalse(form.is_valid())

    def test_invalid_inf_progress(self):
        """Test that infinity progress is rejected."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "inf",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertFalse(form.is_valid())

    def test_invalid_nan_progress(self):
        """Test that NaN progress is rejected."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "nan",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertFalse(form.is_valid())

    def test_invalid_progress(self):
        """Test the game form using an invalid default progress format."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "25:00m",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertFalse(form.is_valid())

    def test_invalid_minutes(self):
        """Test the game form using an invalid default progress format."""
        form_data = {
            "media_id": "1",
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "user": self.user.id,
            "status": Status.COMPLETED.value,
            "progress": "25h61m",
            "repeats": 0,
        }
        form = GameForm(data=form_data)
        self.assertFalse(form.is_valid())


class ManualItemFormTest(TestCase):
    """Test the manual item form functionality."""

    def setUp(self):
        """Create a user and necessary parent items."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        # Create a manual TV show
        self.tv_item = Item.objects.create(
            media_id="manual_tv_1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Test Manual TV",
            image="http://example.com/tv.jpg",
        )
        self.tv = TV.objects.create(
            item=self.tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        # Create a manual Season
        self.season_item = Item.objects.create(
            media_id="manual_tv_1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Test Manual TV",
            season_number=1,
            image="http://example.com/season.jpg",
        )
        self.season = Season.objects.create(
            item=self.season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

    def test_init_with_user(self):
        """Test form initialization with user parameter."""
        form = ManualItemForm(user=self.user)
        self.assertEqual(form.fields["parent_tv"].queryset.count(), 1)
        self.assertEqual(form.fields["parent_season"].queryset.count(), 1)

    def test_init_without_user(self):
        """Test form initialization without user parameter."""
        form = ManualItemForm()
        self.assertEqual(form.fields["parent_tv"].queryset.count(), 0)
        self.assertEqual(form.fields["parent_season"].queryset.count(), 0)

    def test_valid_standalone_media(self):
        """Test creating a standalone media item (movie, anime, etc.)."""
        form_data = {
            "media_type": MediaTypes.MOVIE.value,
            "title": "Test Manual Movie",
            "image": "http://example.com/movie.jpg",
        }
        form = ManualItemForm(data=form_data, user=self.user)
        self.assertTrue(form.is_valid())

        # Save and verify
        item = form.save()
        self.assertEqual(item.source, Sources.MANUAL.value)
        self.assertTrue(item.media_id)
        self.assertIsNone(item.season_number)
        self.assertIsNone(item.episode_number)

    # ── 以下 4 条：断言是对的，业务代码有回归，留给人工决策，不要改断言 ──
    # 现象：ManualItemForm.is_valid() 恒为 False，errors = {
    #   'image': ['Enter a valid URL.', 'Ensure this value has at most 200 characters (it has 640).']}
    # 根因：settings.IMG_NONE 从「一个约 140 字符的 themoviedb SVG 网址」改成了
    #   「640 字符的内联 data:image/svg+xml... 」占位图（settings.py:383，目的是省掉
    #   每页一次第三方 TLS 握手）；而 ManualItemForm.clean() 在用户没填图片时把
    #   settings.IMG_NONE 塞进 cleaned_data["image"]，这个值随后要过 Item.image 的
    #   URLField(max_length=200) 校验 → 既不是合法 URL、又超长，于是整张表单被判无效。
    # 影响：真实站点「创建条目」页所有手动创建都失败（已用 test client 复现：
    #   POST /create 返回 "Image: Enter a valid URL."，Item 一条都建不出来）。
    # 该修的是业务代码（forms.py 跳过 IMG_NONE 的字段校验，或模型放开 image 校验），
    # 不是测试，所以这几条保持原样，等决策。
    def test_valid_season_creation(self):
        """Test creating a season for an existing TV show."""
        form_data = {
            "media_type": MediaTypes.SEASON.value,
            "parent_tv": self.tv.id,
            "season_number": 2,
        }
        form = ManualItemForm(data=form_data, user=self.user)
        self.assertTrue(form.is_valid())

        # Save and verify
        item = form.save()
        self.assertEqual(item.source, Sources.MANUAL.value)
        self.assertEqual(item.media_id, self.tv_item.media_id)
        self.assertEqual(item.title, self.tv_item.title)
        self.assertEqual(item.season_number, 2)
        self.assertIsNone(item.episode_number)

    # 同上：episode 创建逻辑没变，卡在 is_valid() 的 IMG_NONE 校验。
    def test_valid_episode_creation(self):
        """Test creating an episode for an existing season."""
        form_data = {
            "media_type": MediaTypes.EPISODE.value,
            "parent_season": self.season.id,
            "episode_number": 5,
        }
        form = ManualItemForm(data=form_data, user=self.user)
        self.assertTrue(form.is_valid())

        # Save and verify
        item = form.save()
        self.assertEqual(item.source, Sources.MANUAL.value)
        self.assertEqual(item.media_id, self.season_item.media_id)
        self.assertEqual(item.title, self.season_item.title)
        self.assertEqual(item.season_number, self.season_item.season_number)
        self.assertEqual(item.episode_number, 5)

    def test_missing_title_for_standalone(self):
        """Test that title is required for standalone."""
        form_data = {
            "media_type": MediaTypes.MOVIE.value,
        }
        form = ManualItemForm(data=form_data, user=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_missing_parent_for_season(self):
        """Test that parent TV is required for seasons."""
        form_data = {
            "media_type": MediaTypes.SEASON.value,
            "season_number": 3,
        }
        form = ManualItemForm(data=form_data, user=self.user)
        self.assertFalse(form.is_valid())

    def test_missing_parent_for_episode(self):
        """Test that parent season is required for episodes."""
        form_data = {
            "media_type": MediaTypes.EPISODE.value,
            "episode_number": 2,
        }
        form = ManualItemForm(data=form_data, user=self.user)
        self.assertFalse(form.is_valid())

    # 同上（IMG_NONE 超长/非法 URL 导致 is_valid() 恒 False），等业务代码修复。
    def test_default_image(self):
        """Test that default image is used when none provided."""
        form_data = {
            "media_type": MediaTypes.BOOK.value,
            "title": "Test Manual Book",
        }
        form = ManualItemForm(data=form_data, user=self.user)
        self.assertTrue(form.is_valid())

        # Save and verify
        item = form.save()
        self.assertEqual(item.image, settings.IMG_NONE)

    # 同上：unique manual id 生成逻辑本身没变，是 is_valid() 先被 IMG_NONE 卡死，
    # 走不到 save()。等业务代码修复后这条应自动转绿。
    def test_manual_id_generation(self):
        """Test that unique manual IDs are generated."""
        # Create first item
        form1 = ManualItemForm(
            data={"media_type": MediaTypes.ANIME.value, "title": "Test Anime 1"},
            user=self.user,
        )
        self.assertTrue(form1.is_valid())
        item1 = form1.save()

        # Create second item
        form2 = ManualItemForm(
            data={"media_type": MediaTypes.ANIME.value, "title": "Test Anime 2"},
            user=self.user,
        )
        self.assertTrue(form2.is_valid())
        item2 = form2.save()

        # IDs should be different
        self.assertNotEqual(item1.media_id, item2.media_id)
        self.assertTrue(item1.media_id)
        self.assertTrue(item2.media_id)
