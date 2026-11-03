"""
播放意圖與自動暫停狀態
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


PlaybackCommand = Literal["play", "pause", "stop"]
AutoPauseReason = Literal["screen_off", "battery", "fullscreen"]


@dataclass(frozen=True)
class PlaybackState:

    requested_state: PlaybackCommand = "stop"
    # 群組全部需要暫停時, 保存各螢幕的原因
    auto_pause_reasons: tuple[tuple[str, AutoPauseReason], ...] = ()

    @property
    def effective_state(self) -> PlaybackCommand:

        if self.requested_state == "play" and self.auto_pause_reasons:
            return "pause"

        return self.requested_state


class PlaybackTarget(Protocol):

    @property
    def screen_id(self) -> str: ...

    @property
    def current_media(self) -> Path | None: ...

    @property
    def play_state(self) -> str: ...

    @property
    def playback_state(self) -> PlaybackState: ...

    @property
    def is_exiting(self) -> bool: ...

    @property
    def is_recreating(self) -> bool: ...

    def is_image(self) -> bool: ...

    def get_auto_pause_reason(self) -> AutoPauseReason | None: ...

    def apply_playback_state(self, state: PlaybackState | None = None) -> None: ...


def update_auto_pause(members: Sequence[PlaybackTarget]) -> bool:
    """
    單螢幕與跟隨群組共用暫停判斷, 回傳輸出狀態是否改變
    """

    if not members:
        return False

    source = members[0]
    if (
        source.playback_state.requested_state != "play" or source.is_image() or
        any(member.is_exiting or member.is_recreating or not member.current_media for member in members)
    ):
        return False

    reasons: list[tuple[str, AutoPauseReason]] = []
    for member in members:
        reason = member.get_auto_pause_reason()
        if reason is None:
            # 有成員不需暫停時, 略過其餘偵測並清除整組的自動暫停
            reasons.clear()
            break
        reasons.append((member.screen_id, reason))

    state = PlaybackState(source.playback_state.requested_state, tuple(reasons))
    changed = False
    for member in members:
        previous = member.play_state
        member.apply_playback_state(state)
        changed |= member.play_state != previous

    return changed
