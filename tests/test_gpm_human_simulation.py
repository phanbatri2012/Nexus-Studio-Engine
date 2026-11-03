import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from nexus_engine.services.gpm_youtube_automation import (
    human_type_text,
    simulate_post_comment_retention,
    simulate_pre_comment_dwell,
    post_comment_reply_via_gpm,
)


class TestGpmHumanSimulationHelpers(unittest.IsolatedAsyncioTestCase):
    async def test_human_type_text_empty(self):
        element = AsyncMock()
        await human_type_text(None, element, "")
        element.type.assert_not_called()

    async def test_human_type_text_types_characters_with_delays(self):
        page = MagicMock()
        element = AsyncMock()
        element.is_visible = AsyncMock(return_value=True)

        await human_type_text(
            page,
            element,
            "Xin chao",
            min_delay_ms=10,
            max_delay_ms=20,
            review_pause_range=(0.01, 0.02),
        )

        self.assertEqual(element.type.call_count, len("Xin chao"))
        typed_chars = [call.args[0] for call in element.type.call_args_list]
        self.assertEqual("".join(typed_chars), "Xin chao")

    async def test_simulate_pre_comment_dwell_bounded(self):
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        progress_msgs = []

        elapsed = await simulate_pre_comment_dwell(
            page,
            min_seconds=0.05,
            max_seconds=0.1,
            progress_callback=lambda msg: progress_msgs.append(msg),
        )

        self.assertGreaterEqual(elapsed, 0.04)
        self.assertTrue(len(progress_msgs) >= 1)
        self.assertIn("Đang xem video", progress_msgs[0])

    async def test_simulate_pre_comment_dwell_exits_if_page_closed(self):
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=True)

        elapsed = await simulate_pre_comment_dwell(
            page,
            min_seconds=5.0,
            max_seconds=10.0,
        )

        self.assertEqual(elapsed, 0.0)

    async def test_simulate_post_comment_retention_bounded(self):
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        progress_msgs = []

        elapsed = await simulate_post_comment_retention(
            page,
            min_seconds=0.05,
            max_seconds=0.1,
            progress_callback=lambda msg: progress_msgs.append(msg),
        )

        self.assertGreaterEqual(elapsed, 0.04)
        self.assertTrue(len(progress_msgs) >= 1)
        self.assertIn("Đã gửi bình luận thành công", progress_msgs[0])

    async def test_simulate_post_comment_retention_exits_if_page_closed(self):
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=True)

        elapsed = await simulate_post_comment_retention(
            page,
            min_seconds=5.0,
            max_seconds=10.0,
        )

        self.assertEqual(elapsed, 0.0)

    @patch("nexus_engine.services.gpm_youtube_automation.channel_browser_session")
    @patch("nexus_engine.services.gpm_youtube_automation.cleanup_owned_page", new_callable=AsyncMock)
    async def test_post_comment_reply_via_gpm_invokes_dwell_and_human_typing(
        self, mock_cleanup, mock_session
    ):
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        context = AsyncMock()
        context.new_page = AsyncMock(return_value=page)

        class FakeSession:
            async def __aenter__(self):
                return context, None, {}
            async def __aexit__(self, *args):
                pass

        mock_session.return_value = FakeSession()

        # Mock query selectors for reply
        thread = AsyncMock()
        reply_btn = AsyncMock()
        thread.query_selector = AsyncMock(return_value=reply_btn)
        page.wait_for_selector = AsyncMock()
        submit_btn = AsyncMock()

        async def fake_wait_for_selector(sel, timeout=None):
            if "submit" in sel:
                return submit_btn
            return AsyncMock()

        page.wait_for_selector.side_effect = fake_wait_for_selector

        with patch(
            "nexus_engine.services.gpm_youtube_automation._wait_for_target_comment_thread",
            new_callable=AsyncMock,
            return_value=thread,
        ), patch(
            "nexus_engine.services.gpm_youtube_automation.simulate_pre_comment_dwell",
            new_callable=AsyncMock,
            return_value=12.5,
        ) as mock_pre_dwell, patch(
            "nexus_engine.services.gpm_youtube_automation.simulate_post_comment_retention",
            new_callable=AsyncMock,
            return_value=65.0,
        ) as mock_post_dwell, patch(
            "nexus_engine.services.gpm_youtube_automation.human_type_text",
            new_callable=AsyncMock,
        ) as mock_type:
            progress_reports = []
            res = await post_comment_reply_via_gpm(
                profile_id="test_profile",
                video_url="https://www.youtube.com/watch?v=vid123",
                comment_text="Cảm ơn bạn đã xem video!",
                comment_id="comment123",
                auto_heart=False,
                pre_comment_dwell=True,
                post_comment_dwell=True,
                min_pre_dwell=10.0,
                max_pre_dwell=60.0,
                min_post_dwell=60.0,
                max_post_dwell=120.0,
                progress_callback=lambda msg: progress_reports.append(msg),
            )

            self.assertTrue(res["success"])
            self.assertEqual(res["pre_dwell_seconds"], 12.5)
            self.assertEqual(res["post_dwell_seconds"], 65.0)
            mock_pre_dwell.assert_awaited_once()
            mock_type.assert_awaited_once()
            mock_post_dwell.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
