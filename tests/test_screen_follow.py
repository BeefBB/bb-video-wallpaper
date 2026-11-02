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
from playback import AutoPauseReason, PlaybackState
from screen_follow import FollowController, FollowGraph


class Playback:
    """
    以欄位控制就緒狀態, 遮蔽條件與播放進度
    """

    def __init__(self, screen_id: str, media: Path) -> None:
        """
        初始化指定螢幕的媒體與播放狀態, 設定就緒與進度同步所需欄位
        """

        self.screen_id = screen_id
        self.current_media = media
        self.play_state = "play"
        self.playback_state = PlaybackState("play")
        self.is_exiting = False
        self.is_recreating = False
        self.ready = True
        self.blocked = False
        self.position = 2000
        self.length = 10000
        self.seekable = True
        self.seeks: list[int] = []


    def is_image(self) -> bool:
        """
        依副檔名判斷目前媒體是否為 PNG 圖片
        """

        return self.current_media.suffix == ".png"


    def set_play_media(self, media: Path) -> None:
        """
        更換媒體並清除就緒狀態, 將播放進度歸零
        """

        self.current_media = media
        self.ready = False
        self.position = 0


    def apply_playback_state(self, state: PlaybackState | None = None) -> None:
        """
        保存指定播放意圖, 並套用包含自動暫停條件的有效狀態
        """

        if state is not None:
            self.playback_state = state
        self.play_state = self.playback_state.effective_state


    def get_playback_time(self) -> int | None:
        """
        只在媒體已就緒且未停止時回傳播放進度
        """

        return self.position if self.ready and self.play_state != "stop" else None


    def get_playback_length(self) -> int:
        """
        回傳目前保存的媒體長度
        """

        return self.length


    def seek_time(self, position: int) -> bool:
        """
        可調整播放進度時記錄並套用指定位置, 否則回報失敗
        """

        if not self.seekable:
            return False

        self.seeks.append(position)
        self.position = position
        return True


    def get_auto_pause_reason(self) -> AutoPauseReason | None:
        """
        被遮蔽時回傳全螢幕暫停原因, 否則回傳無暫停原因
        """

        return "fullscreen" if self.blocked else None


class FollowGraphTests(unittest.TestCase):

    def test_chain_rejects_cycles_and_reparents_a_whole_subtree(self) -> None:
        """
        驗證連鎖跟隨拒絕形成循環, 改接來源時整個子樹一同移動
        """

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
        """
        驗證解除跟隨或移除中間螢幕時, 不相關的下層關係仍保留
        """

        for operation in ("detach", "remove_screen"):
            with self.subTest(operation=operation):
                graph = FollowGraph({"b": "a", "c": "b", "d": "c"})
                getattr(graph, operation)("b")
                expected = {"c": "b", "d": "c"} if operation == "detach" else {"d": "c"}
                self.assertEqual(graph.sources, expected)


    def test_validation_removes_entire_cycles_and_invalid_ids_without_mutating_input(self) -> None:
        """
        驗證正規化移除完整循環與無效螢幕識別, 不修改傳入資料
        """

        raw = {"a": "b", "b": "c", "c": "a", "child": "a", "d": "e", "e": "d",
               "self": "self", "ok": "offline", "": "a", "bad\0": "a", "blank": " ", "type": []}
        original = raw.copy()
        self.assertEqual(FollowGraph.normalize(raw), {"child": "a", "ok": "offline"})
        self.assertEqual(raw, original)
        for invalid in (None, False, 123, [], "screen"):
            self.assertEqual(FollowGraph.normalize(invalid), {})


class FollowControllerTests(unittest.TestCase):

    def setUp(self) -> None:
        """
        建立暫存設定與四個播放替身, 初始化跟隨控制器並替換日誌
        """

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.store = ConfigStore(directory / "config.json", directory, ROOT / "lang")
        self.store.save({"language": "zh-TW", "recentVideo": ["older.mp4"]})
        self.media = directory / "sample.mp4"
        self.screens = {key: Playback(key, self.media) for key in "abcd"}
        self.controller = FollowController(self.screens, self.store)
        self.enterContext(mock.patch("screen_follow.logger"))


    def test_small_drift_and_loop_boundary_do_not_seek(self) -> None:
        """
        驗證小幅進度差與循環邊界不觸發校正, 超過容許差距才調整位置
        """

        source, follower = self.screens["a"], self.screens["b"]
        self.controller.follow("b", "a")
        for source_time, target_time in ((1000, 1500), (100, 9900)):
            source.position, follower.position = source_time, target_time
            self.controller.synchronize()
            self.assertEqual(follower.seeks, [])

        source.position, follower.position = 4000, 1500
        self.controller.synchronize()
        self.assertEqual(follower.seeks, [4000])


    def test_group_pauses_only_when_all_members_are_blocked_and_preserves_manual_pause(self) -> None:
        """
        驗證群組只在全部成員受阻時自動暫停, 並保留來源的手動暫停
        """

        # 可見的跟隨者加入後, 來源從自動暫停恢復播放
        source = self.screens["a"]
        source.blocked = True
        source.apply_playback_state(PlaybackState("play", (("a", "fullscreen"),)))
        self.controller.follow("b", "a")
        self.assertEqual(source.play_state, "play")
        self.assertEqual(self.screens["b"].play_state, "play")
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
        self.screens["a"].apply_playback_state(PlaybackState("pause"))
        self.controller.synchronize("a")
        self.controller.check_auto_pause(self.screens["b"])
        self.assertEqual([self.screens[key].play_state for key in "abc"], ["pause"] * 3)
        self.assertEqual(self.screens["d"].play_state, "play")


    def test_relation_change_during_media_copy_does_not_overwrite_local_output(self) -> None:
        """
        驗證同步媒體途中解除跟隨並改選本機圖片後, 延遲訊號不覆寫本機輸出
        """

        self.controller.follow("b", "a")
        source, follower = self.screens["a"], self.screens["b"]
        source.current_media = self.media.with_name("new.mp4")
        local = self.media.with_name("local.png")

        def select_locally(media: Path) -> None:
            """
            解除跟隨並改用本機圖片, 將播放意圖設為停止
            """

            self.controller.detach("b")
            follower.current_media = local
            follower.apply_playback_state(PlaybackState("stop"))

        with mock.patch.object(follower, "set_play_media", side_effect=select_locally):
            self.controller.synchronize()

        self.controller.on_ready("a", source)
        self.assertEqual((follower.current_media, follower.play_state, follower.playback_state.requested_state), (local, "stop", "stop"))
        self.assertEqual(self.store.load()["screenWallpapers"], {"b": "sample.mp4"})
        self.assertEqual(self.store.load()["screenFollows"], {})


    def test_removing_middle_screen_keeps_output_and_does_not_refollow_on_reconnection(self) -> None:
        """
        驗證移除中間螢幕後保留其餘輸出與下層關係, 重新連線不自動恢復舊跟隨
        """

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


    def test_nonseekable_outputs_and_failed_members_do_not_block_other_followers(self) -> None:
        """
        驗證無法調整進度或校正失敗的成員不阻止其他跟隨者同步
        """

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
        """
        驗證控制器關閉後忽略延遲就緒訊號, 並保留已保存的跟隨關係
        """

        self.controller.follow("b", "a")
        self.controller.shutdown()
        self.screens["a"].position = 8000
        self.controller.on_ready("a", self.screens["a"])
        self.controller.synchronize()
        self.assertEqual(self.screens["b"].position, 2000)
        self.assertEqual(self.store.load()["screenFollows"], {"b": "a"})


if __name__ == "__main__":
    unittest.main()
