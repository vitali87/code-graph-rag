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


class TestEffectivePermissionMode:
    """--no-confirm and YOLO used to be two flags read separately: the config
    table called --no-confirm "YOLO Mode" while the status bar said Normal
    mode asks, and neither was true."""

    def test_no_confirm_is_auto_approve_not_yolo(self) -> None:
        state = SessionState(confirm_edits=False)
        assert state.effective_permission_mode() == PermissionMode.AUTO_APPROVE
        assert state.prompts_for_approval() is False
        assert state.is_yolo() is False

    def test_yolo_wins_over_no_confirm(self) -> None:
        state = SessionState(confirm_edits=False)
        state.cycle_permission_mode()
        assert state.effective_permission_mode() == PermissionMode.YOLO

    def test_the_default_prompts(self) -> None:
        state = SessionState()
        assert state.effective_permission_mode() == PermissionMode.NORMAL
        assert state.prompts_for_approval() is True

    def test_each_mode_has_its_own_label(self) -> None:
        from codebase_rag import constants as cs
        from codebase_rag import main
        from codebase_rag.main import _permission_mode_label

        session = main.app_context.session
        saved = (session.confirm_edits, session.permission_mode)
        try:
            session.confirm_edits, session.permission_mode = (
                False,
                PermissionMode.NORMAL,
            )
            assert _permission_mode_label() == cs.PERMISSION_MODE_AUTO_APPROVE_LABEL
            session.confirm_edits = True
            assert _permission_mode_label() == cs.PERMISSION_MODE_NORMAL_LABEL
        finally:
            session.confirm_edits, session.permission_mode = saved
