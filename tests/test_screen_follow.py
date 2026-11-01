"""
跟隨關係, 播放狀態與進度同步測試
"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from config import ConfigStore
from screen_follow import FollowController, FollowGraph


class Playback:
    """
    以欄位控制就緒狀態, 遮蔽條件與播放進度
    """

    def __init__(self, screen_id: str, media: Path) -> None:

        self.screen_id = screen_id
        self.current_media = media
        self.play_state = "play"
        self.enable_auto_pause = True
        self.is_exiting = False
        self.is_recreating = False
        self.ready = True
        self.blocked = False
        self.position = 2000
        self.length = 10000
        self.seekable = True
        self.seeks: list[int] = []


    def is_image(self) -> bool:

        return self.current_media.suffix == ".png"


    def set_play_media(self, media: Path) -> None:

        self.current_media = media
        self.ready = False
        self.position = 0


    def media_play(self) -> None:

        self.play_state = "play"


    def media_pause(self) -> None:

        if not self.is_image():
            self.play_state = "pause"


    def media_stop(self) -> None:

        self.play_state = "stop"


    def get_playback_time(self) -> int | None:

        return self.position if self.ready and self.play_state != "stop" else None


    def get_playback_length(self) -> int:

        return self.length


    def seek_time(self, position: int) -> bool:

        if not self.seekable:
            return False

        self.seeks.append(position)
        self.position = position
        return True


    def needs_auto_pause(self) -> bool:

        return self.blocked


class FollowGraphTests(unittest.TestCase):

    def test_chain_rejects_cycles_and_reparents_a_whole_subtree(self) -> None:

        graph = FollowGraph({"b": "a", "c": "b"})
        self.assertEqual(graph.subtree("a"), ("a", "b", "c"))
        self.assertEqual(graph.root("c"), "a")
        for follower, source in (("a", "a"), ("a", "b"), ("a", "c"), ("b", "c")):
            with self.subTest(follower=follower, source=source):
                self.assertFalse(graph.can_follow(follower, source))
                with self.assertRaises(ValueError):
                    graph.follow(follower, source)

        graph.follow("b", "d")
        self.assertEqual(graph.subtree("a"), ("a",))
        self.assertEqual(graph.subtree("d"), ("d", "b", "c"))


    def test_detaching_or_removing_source_preserves_lower_relationships(self) -> None:

        for operation in ("detach", "remove_screen"):
            with self.subTest(operation=operation):
                graph = FollowGraph({"b": "a", "c": "b", "d": "c"})
                getattr(graph, operation)("b")
                expected = {"c": "b", "d": "c"} if operation == "detach" else {"d": "c"}
                self.assertEqual(graph.sources, expected)


    def test_validation_removes_entire_cycles_and_invalid_ids_without_mutating_input(self) -> None:

        raw = {"a": "b", "b": "c", "c": "a", "child": "a", "d": "e", "e": "d",
               "self": "self", "ok": "offline", "": "a", "bad\0": "a", "blank": " ", "type": []}
        original = raw.copy()
        self.assertEqual(FollowGraph.normalize(raw), {"child": "a", "ok": "offline"})
        self.assertEqual(raw, original)
        for invalid in (None, False, 123, [], "screen"):
            self.assertEqual(FollowGraph.normalize(invalid), {})


    def test_long_chain_is_validated_without_recursion(self) -> None:

        graph = FollowGraph({str(i): str(i + 1) for i in range(1500)})
        self.assertEqual(graph.root("0"), "1500")
        self.assertFalse(graph.can_follow("1500", "0"))


class FollowControllerTests(unittest.TestCase):

    def setUp(self) -> None:

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.store = ConfigStore(directory / "config.json", directory, ROOT / "lang")
        self.store.save({"language": "zh-TW", "recentVideo": ["older.mp4"]})
        self.media = directory / "sample.mp4"
        self.screens = {key: Playback(key, self.media) for key in "abcd"}
        self.controller = FollowController(self.screens, self.store)
        self.enterContext(mock.patch("screen_follow.logger"))


    def test_join_copies_selection_and_manual_state_without_changing_history(self) -> None:

        source, follower = self.screens["a"], self.screens["b"]
        source.position = 3500
        source.play_state = "pause"
        source.enable_auto_pause = False
        self.assertTrue(self.controller.follow("b", "a"))
        self.assertEqual((follower.play_state, follower.enable_auto_pause, follower.position), ("pause", False, 3500))
        config = self.store.load()
        self.assertEqual(config["screenFollows"], {"b": "a"})
        self.assertEqual(config["screenWallpapers"], {"b": "sample.mp4"})
        self.assertEqual(config["recentVideo"], ["older.mp4"])


    def test_source_media_change_waits_for_load_and_uses_latest_position(self) -> None:

        self.controller.follow("b", "a")
        self.controller.follow("c", "b")
        source = self.screens["a"]
        source.current_media = self.media.with_name("new.mp4")
        self.controller.synchronize("a")
        source.position = 4500
        for key in "bc":
            target = self.screens[key]
            self.assertEqual(target.current_media, source.current_media)
            self.assertEqual(target.position, 0)
            self.assertEqual(target.seeks, [])
            target.ready = True
            self.controller.on_ready(key, target)
            self.assertEqual(target.position, 4500)
            self.assertEqual(target.seeks, [4500])

        self.assertEqual(self.store.load()["screenWallpapers"], {"b": "new.mp4", "c": "new.mp4"})


    def test_small_drift_and_loop_boundary_do_not_seek(self) -> None:

        source, follower = self.screens["a"], self.screens["b"]
        self.controller.follow("b", "a")
        for source_time, target_time in ((1000, 1500), (100, 9900)):
            source.position, follower.position = source_time, target_time
            self.controller.synchronize()
            self.assertEqual(follower.seeks, [])

        source.position, follower.position = 4000, 1500
        self.controller.synchronize()
        self.assertEqual(follower.seeks, [4000])


    def test_reattach_defers_sync_and_detach_discards_late_source_events(self) -> None:

        source, follower = self.screens["a"], self.screens["b"]
        self.controller.follow("b", "a")
        follower.is_recreating = True
        source.position = 7000
        self.controller.synchronize()
        self.assertEqual(follower.position, 2000)
        follower.is_recreating = False
        self.controller.on_ready("b", follower)
        self.assertEqual(follower.position, 7000)
        self.controller.detach("b")
        follower.position = 1200
        self.controller.on_ready("a", source)
        self.assertEqual(follower.position, 1200)
        self.assertEqual(self.store.load()["screenFollows"], {})


    def test_group_pauses_only_when_all_members_are_blocked_and_preserves_manual_pause(self) -> None:

        self.controller.follow("b", "a")
        self.controller.follow("c", "b")
        for key in "ab":
            self.screens[key].blocked = True
        self.controller.check_auto_pause(self.screens["b"])
        self.assertEqual([self.screens[key].play_state for key in "abc"], ["play"] * 3)
        self.screens["c"].blocked = True
        self.controller.check_auto_pause(self.screens["c"])
        self.assertEqual([self.screens[key].play_state for key in "abc"], ["pause"] * 3)
        self.screens["b"].blocked = False
        self.controller.check_auto_pause(self.screens["a"])
        self.assertEqual([self.screens[key].play_state for key in "abc"], ["play"] * 3)
        self.screens["a"].play_state = "pause"
        self.screens["a"].enable_auto_pause = False
        self.controller.synchronize("a")
        self.controller.check_auto_pause(self.screens["b"])
        self.assertEqual([self.screens[key].play_state for key in "abc"], ["pause"] * 3)
        self.assertEqual(self.screens["d"].play_state, "play")


    def test_joining_visible_follower_resumes_an_auto_paused_source(self) -> None:

        source = self.screens["a"]
        source.blocked = True
        source.play_state = "pause"
        self.controller.follow("b", "a")
        self.assertEqual(source.play_state, "play")
        self.assertEqual(self.screens["b"].play_state, "play")


    def test_group_timers_only_scan_once_but_member_notifications_still_apply_immediately(self) -> None:

        self.controller.follow("b", "a")
        source, follower = self.screens["a"], self.screens["b"]
        with (
            mock.patch.object(source, "needs_auto_pause", return_value=True) as first,
            mock.patch.object(follower, "needs_auto_pause", return_value=True) as second,
        ):
            self.controller.check_auto_pause(follower, from_timer=True)
            first.assert_not_called()
            second.assert_not_called()
            self.controller.check_auto_pause(source, from_timer=True)
            first.assert_called_once()
            second.assert_called_once()
            self.assertEqual(source.play_state, "pause")
            second.return_value = False
            self.controller.check_auto_pause(follower)
            self.assertEqual(source.play_state, "play")


    def test_relation_change_during_media_copy_does_not_overwrite_local_output(self) -> None:

        self.controller.follow("b", "a")
        source, follower = self.screens["a"], self.screens["b"]
        source.current_media = self.media.with_name("new.mp4")
        local = self.media.with_name("local.png")

        def select_locally(media: Path) -> None:
            self.controller.detach("b")
            follower.current_media = local
            follower.play_state = "stop"
            follower.enable_auto_pause = False

        with mock.patch.object(follower, "set_play_media", side_effect=select_locally):
            self.controller.synchronize()

        self.controller.on_ready("a", source)
        self.assertEqual((follower.current_media, follower.play_state, follower.enable_auto_pause), (local, "stop", False))
        self.assertEqual(self.store.load()["screenWallpapers"], {"b": "sample.mp4"})
        self.assertEqual(self.store.load()["screenFollows"], {})


    def test_removing_middle_screen_keeps_output_and_does_not_refollow_on_reconnection(self) -> None:

        self.controller.follow("b", "a")
        self.controller.follow("c", "b")
        self.controller.follow("d", "c")
        removed = self.screens.pop("b")
        self.controller.remove_screen("b")
        self.screens["c"].position = 7300
        self.controller.on_ready("b", removed)
        self.assertEqual(self.screens["c"].position, 7300)
        self.screens["b"] = Playback("b", self.media)
        self.controller.synchronize()
        self.assertEqual(self.controller.graph.sources, {"d": "c"})
        self.assertEqual(self.screens["d"].position, 7300)
        self.assertEqual(self.store.load()["screenFollows"], {"d": "c"})


    def test_restore_prunes_missing_endpoints_and_does_not_restore_manual_pause(self) -> None:

        self.controller.restore({"b": "a", "c": "missing", "offline": "a"})
        self.assertEqual(self.controller.graph.sources, {"b": "a"})
        self.assertEqual(self.store.load()["screenFollows"], {"b": "a"})
        self.assertEqual(self.screens["b"].play_state, "play")


    def test_images_and_stopped_videos_are_copied_without_reading_progress(self) -> None:

        source = self.screens["a"]
        for image in (True, False):
            with self.subTest(image=image):
                source.current_media = self.media.with_suffix(".png") if image else self.media
                source.play_state = "stop"
                source.enable_auto_pause = False
                with mock.patch.object(source, "get_playback_time", side_effect=AssertionError("unexpected seek")):
                    self.controller.follow("b", "a")
                self.assertEqual(self.screens["b"].current_media, source.current_media)
                self.assertEqual(self.screens["b"].play_state, "stop")


    def test_nonseekable_outputs_and_failed_members_do_not_block_other_followers(self) -> None:

        self.controller.follow("b", "a")
        self.controller.follow("c", "a")
        self.screens["a"].position = 6000
        self.screens["b"].seekable = False
        self.controller.synchronize()
        self.assertEqual(self.screens["b"].position, 2000)
        self.assertEqual(self.screens["b"].seeks, [])
        self.assertEqual(self.screens["c"].position, 6000)
        self.screens["b"].seekable = True
        self.controller.synchronize()
        self.assertEqual(self.screens["b"].position, 6000)
        self.assertEqual(self.screens["b"].seeks, [6000])

        self.screens["a"].position = 8000
        with mock.patch.object(self.screens["b"], "seek_time", side_effect=RuntimeError("seek failed")):
            self.controller.synchronize()
        self.assertEqual(self.screens["b"].position, 6000)
        self.assertEqual(self.screens["c"].position, 8000)


    def test_shutdown_ignores_queued_events_and_keeps_saved_relationships(self) -> None:

        self.controller.follow("b", "a")
        self.controller.shutdown()
        self.screens["a"].position = 8000
        self.controller.on_ready("a", self.screens["a"])
        self.controller.synchronize()
        self.assertEqual(self.screens["b"].position, 2000)
        self.assertEqual(self.store.load()["screenFollows"], {"b": "a"})


if __name__ == "__main__":
    unittest.main()
