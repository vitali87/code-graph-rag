from codebase_rag.constants import PermissionMode
from codebase_rag.models import SessionState


class TestSessionPermissionMode:
    def test_default_mode_is_normal(self) -> None:
        state = SessionState()
        assert state.permission_mode == PermissionMode.NORMAL
        assert state.is_yolo() is False

    def test_cycle_toggles_to_yolo(self) -> None:
        state = SessionState()
        assert state.cycle_permission_mode() == PermissionMode.YOLO
        assert state.is_yolo() is True

    def test_cycle_toggles_back_to_normal(self) -> None:
        state = SessionState()
        state.cycle_permission_mode()
        assert state.cycle_permission_mode() == PermissionMode.NORMAL
        assert state.is_yolo() is False
