"""
螢幕跟隨關係與播放同步
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeGuard, cast

if TYPE_CHECKING:
    from config import ConfigStore


logger = logging.getLogger(__name__)

SYNC_INTERVAL_MS = 60_000
SYNC_TOLERANCE_MS = 500
RESTORE_DELAY_MS = 3_000


class FollowWallpaper(Protocol):

    @property
    def screen_id(self) -> str: ...

    @property
    def current_media(self) -> Path | None: ...

    @property
    def play_state(self) -> str: ...

    @property
    def is_exiting(self) -> bool: ...

    @property
    def is_recreating(self) -> bool: ...

    enable_auto_pause: bool

    def set_play_media(self, path: Path, /) -> None: ...

    def media_play(self) -> None: ...

    def media_pause(self) -> None: ...

    def media_stop(self) -> None: ...

    def is_image(self) -> bool: ...

    def get_playback_time(self) -> int | None: ...

    def get_playback_length(self) -> int: ...

    def seek_time(self, position: int) -> bool: ...

    def needs_auto_pause(self) -> bool: ...


class AutoPauseHandler(Protocol):

    def __call__(self, wallpaper: FollowWallpaper, *, from_timer: bool = False) -> bool: ...


def valid_screen_id(value: object) -> TypeGuard[str]:

    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


class FollowGraph:
    """
    以跟隨者識別查找直接來源
    """

    def __init__(self, sources: object = None) -> None:

        self._sources: dict[str, str] = self.normalize(sources)


    @staticmethod
    def normalize(raw: object) -> dict[str, str]:
        """
        略過無效識別, 並移除循環內的關係
        """

        if not isinstance(raw, dict):
            return {}

        entries = cast(dict[object, object], raw)
        sources: dict[str, str] = {
            follower: source for follower, source in entries.items()
            if valid_screen_id(follower) and valid_screen_id(source) and follower != source
        }
        visited: set[str] = set()
        cyclic: set[str] = set()

        for start in sources:
            path: list[str] = []
            positions: dict[str, int] = {}
            screen_id = start

            while screen_id in sources and screen_id not in visited:
                if screen_id in positions:
                    cyclic.update(path[positions[screen_id]:])
                    break

                positions[screen_id] = len(path)
                path.append(screen_id)
                screen_id = sources[screen_id]

            visited.update(path)

        return {follower: source for follower, source in sources.items() if follower not in cyclic}


    @property
    def sources(self) -> dict[str, str]:

        return self._sources.copy()


    def root(self, screen_id: str) -> str:

        while screen_id in self._sources:
            screen_id = self._sources[screen_id]

        return screen_id


    def subtree(self, screen_id: str) -> tuple[str, ...]:
        """
        依來源先於跟隨者的順序取得子群組, 包含指定螢幕
        """

        result = [screen_id]

        for source in result:
            result.extend(follower for follower, parent in self._sources.items() if parent == source)

        return tuple(result)


    def can_follow(self, follower: str, source: str) -> bool:

        return (
            valid_screen_id(follower) and valid_screen_id(source) and
            follower != source and source not in self.subtree(follower)
        )


    def follow(self, follower: str, source: str) -> None:

        if not self.can_follow(follower, source):
            raise ValueError("跟隨關係不能形成循環")

        self._sources[follower] = source


    def detach(self, screen_id: str) -> bool:

        return self._sources.pop(screen_id, None) is not None


    def remove_screen(self, screen_id: str) -> bool:
        """
        移除以此螢幕為任一端點的關係
        """

        removed = [
            follower for follower, source in self._sources.items()
            if follower == screen_id or source == screen_id
        ]

        for follower in removed:
            del self._sources[follower]

        return bool(removed)


    def retain_screens(self, screen_ids: Iterable[str]) -> None:

        available = set(screen_ids)
        self._sources = {
            follower: source for follower, source in self._sources.items()
            if follower in available and source in available
        }


class FollowController:
    """
    保存待同步螢幕並套用來源的媒體, 狀態與進度
    """

    def __init__(self, wallpapers: Mapping[str, FollowWallpaper], config_store: ConfigStore) -> None:

        self.wallpapers = wallpapers
        self.config_store = config_store
        self.graph = FollowGraph()
        self.pending_sync: set[str] = set()
        self._pending_selection: set[str] = set()
        self._syncing: set[str] = set()
        self.is_exiting = False


    def save(self) -> None:

        config = self.config_store.load()
        config["screenFollows"] = self.graph.sources
        self.config_store.save(config)


    def restore(self, sources: object) -> None:

        if self.is_exiting:
            return

        self.graph = FollowGraph(sources)
        self.graph.retain_screens(self.wallpapers)
        if self.graph.sources != sources:
            self.save()

        self._pending_selection.update(self.graph.sources)
        self.synchronize()


    def can_follow(self, follower: str, source: str) -> bool:

        target = self.wallpapers.get(follower)
        origin = self.wallpapers.get(source)
        return bool(
            not self.is_exiting and target is not None and origin is not None and
            not target.is_exiting and not origin.is_exiting and origin.current_media and
            self.graph.can_follow(follower, source)
        )


    def follow(self, follower: str, source: str) -> bool:

        if not self.can_follow(follower, source):
            return False

        self.graph.follow(follower, source)
        self._pending_selection.update(self.graph.subtree(follower))
        self.save()
        self.synchronize(source)
        self.check_auto_pause(self.wallpapers[source])
        return True


    def detach(self, screen_id: str) -> None:

        if self.is_exiting:
            return

        if self.graph.detach(screen_id):
            self.save()

        self.pending_sync.intersection_update(self.graph.sources)
        self._pending_selection.intersection_update(self.graph.sources)


    def remove_screen(self, screen_id: str) -> None:

        if self.is_exiting:
            return

        if self.graph.remove_screen(screen_id):
            self.save()

        self.pending_sync.intersection_update(self.graph.sources)
        self._pending_selection.intersection_update(self.graph.sources)


    def on_ready(self, screen_id: str, wallpaper: FollowWallpaper) -> None:
        """
        只處理登錄表中仍指向同一桌布物件的通知
        """

        if not self.is_exiting and self.wallpapers.get(screen_id) is wallpaper:
            self.synchronize(screen_id)


    def synchronize(self, screen_id: str | None = None) -> None:

        if self.is_exiting:
            return

        roots = (
            (self.graph.root(screen_id),) if screen_id is not None else
            tuple(dict.fromkeys(self.graph.root(follower) for follower in self.graph.sources))
        )

        for root in roots:
            if root in self._syncing:
                continue

            self._syncing.add(root)
            try:
                self._synchronize_group(root)
            finally:
                self._syncing.discard(root)


    def _synchronize_group(self, root: str) -> None:

        source = self.wallpapers.get(root)
        followers = self.graph.subtree(root)[1:]
        if source is None or source.is_exiting or not followers:
            return

        self.pending_sync.update(followers)
        if source.is_recreating or source.current_media is None:
            return

        for screen_id in followers:
            target = self.wallpapers.get(screen_id)
            if self.is_exiting or self.wallpapers.get(root) is not source:
                return

            if (
                target is None or target.is_exiting or target.is_recreating or
                self.graph.root(screen_id) != root
            ):
                continue

            try:
                source_media = source.current_media
                if source_media is None:
                    return

                changed_media = target.current_media != source_media
                if changed_media:
                    target.set_play_media(source_media)

                # 切換期間可能移除螢幕或解除跟隨, 套用前重新確認來源
                if (
                    self.is_exiting or self.wallpapers.get(screen_id) is not target or
                    self.wallpapers.get(root) is not source or source.is_exiting or
                    self.graph.root(screen_id) != root or source.current_media != source_media
                ):
                    continue

                target.enable_auto_pause = source.enable_auto_pause
                if target.play_state != source.play_state:
                    if source.play_state == "stop":
                        target.media_stop()
                    elif source.play_state == "pause":
                        if target.play_state == "stop":
                            target.media_play()
                        target.media_pause()
                    else:
                        target.media_play()

                if changed_media or screen_id in self._pending_selection:
                    self.config_store.remember_wallpaper(screen_id, source_media)
                    self._pending_selection.discard(screen_id)

                if source.is_image() or source.play_state == "stop":
                    self.pending_sync.discard(screen_id)
                    continue

                source_time = source.get_playback_time()
                target_time = target.get_playback_time()
                if source_time is None or target_time is None:
                    continue

                difference = abs(source_time - target_time)
                length = source.get_playback_length()
                if length > 0 and difference <= length:
                    difference = min(difference, length - difference)

                if difference > SYNC_TOLERANCE_MS and not target.seek_time(source_time):
                    continue

                self.pending_sync.discard(screen_id)

            except Exception:
                logger.exception("同步跟隨螢幕失敗 - %s -> %s", screen_id, root)


    def check_auto_pause(self, wallpaper: FollowWallpaper, *, from_timer: bool = False) -> bool:
        """
        檢查群組暫停條件, 未處理時回傳 False
        """

        if self.is_exiting or self.wallpapers.get(wallpaper.screen_id) is not wallpaper:
            return False

        root = self.graph.root(wallpaper.screen_id)
        members = [self.wallpapers[screen_id] for screen_id in self.graph.subtree(root) if screen_id in self.wallpapers]
        if len(members) < 2:
            return False

        # 跟隨者的計時通知不重複掃描同一群組
        if from_timer and wallpaper.screen_id != root:
            return True

        source = self.wallpapers.get(root)
        if (
            source is None or not source.enable_auto_pause or source.is_image() or
            any(member.is_exiting or member.is_recreating or not member.current_media for member in members)
        ):
            return True

        need_pause = all(member.needs_auto_pause() for member in members)
        changed = False

        for member in members:
            if need_pause and member.play_state == "play":
                member.media_pause()
                changed = True
            elif not need_pause and member.play_state == "pause":
                member.media_play()
                changed = True

        if changed:
            self.synchronize(root)

        return True


    def shutdown(self) -> None:

        self.is_exiting = True
        self.pending_sync.clear()
        self._pending_selection.clear()
