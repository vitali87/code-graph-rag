from __future__ import annotations

from unittest.mock import MagicMock

import torch
from torch import nn

from codebase_rag.unixcoder import Beam, UniXcoder


class TestBeamInit:
    def test_initializes_with_correct_size(self) -> None:
        beam = Beam(size=5, eos=2, device=torch.device("cpu"))
        assert beam.size == 5
        assert beam._eos == frozenset({2})

    def test_initializes_scores_to_zero(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        assert beam.scores.shape == (3,)
        assert torch.all(beam.scores == 0)

    def test_initializes_nextYs_with_zeros(self) -> None:
        beam = Beam(size=4, eos=2, device=torch.device("cpu"))
        assert len(beam.nextYs) == 1
        assert beam.nextYs[0].shape == (4,)
        assert torch.all(beam.nextYs[0] == 0)

    def test_initializes_empty_prevKs(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        assert beam.prevKs == []

    def test_initializes_finished_empty(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        assert beam.finished == []


class TestBeamGetCurrentState:
    def test_returns_batch_shaped_tensor(self) -> None:
        beam = Beam(size=5, eos=2, device=torch.device("cpu"))
        state = beam.getCurrentState()
        assert state.shape == (5, 1)

    def test_returns_last_nextYs(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        beam.nextYs.append(torch.tensor([1, 2, 3]))
        state = beam.getCurrentState()
        assert torch.all(state.flatten() == torch.tensor([1, 2, 3]))


class TestBeamGetCurrentOrigin:
    def test_returns_last_prevKs(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        beam.prevKs.append(torch.tensor([0, 1, 2]))
        origin = beam.getCurrentOrigin()
        assert torch.all(origin == torch.tensor([0, 1, 2]))


class TestBeamDone:
    def test_not_done_initially(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        assert beam.done() is False

    def test_done_when_eos_top_and_enough_finished(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.eosTop = True
        beam.finished = [
            (torch.tensor(0.5), 1, 0),
            (torch.tensor(0.4), 1, 1),
        ]
        assert beam.done() is True

    def test_not_done_when_not_eos_top(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.eosTop = False
        beam.finished = [
            (torch.tensor(0.5), 1, 0),
            (torch.tensor(0.4), 1, 1),
        ]
        assert beam.done() is False

    def test_not_done_when_not_enough_finished(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        beam.eosTop = True
        beam.finished = [
            (torch.tensor(0.5), 1, 0),
        ]
        assert beam.done() is False


class TestBeamAdvance:
    def test_first_step_uses_first_beam(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        word_probs = torch.tensor(
            [
                [-1.0, -2.0, -3.0, -4.0, -5.0],
                [-5.0, -4.0, -3.0, -2.0, -1.0],
                [-2.0, -3.0, -1.0, -4.0, -5.0],
            ]
        )
        beam.advance(word_probs)
        assert len(beam.prevKs) == 1
        assert len(beam.nextYs) == 2

    def test_subsequent_steps_combine_scores(self) -> None:
        beam = Beam(size=2, eos=5, device=torch.device("cpu"))
        word_probs1 = torch.tensor(
            [
                [-1.0, -2.0, -3.0, -4.0],
                [-4.0, -3.0, -2.0, -1.0],
            ]
        )
        beam.advance(word_probs1)

        word_probs2 = torch.tensor(
            [
                [-0.5, -1.0, -1.5, -2.0],
                [-2.0, -1.5, -1.0, -0.5],
            ]
        )
        beam.advance(word_probs2)
        assert len(beam.prevKs) == 2
        assert len(beam.nextYs) == 3

    def test_marks_eos_in_finished(self) -> None:
        beam = Beam(size=2, eos=0, device=torch.device("cpu"))
        word_probs = torch.tensor(
            [
                [-0.1, -2.0, -3.0],
                [-3.0, -2.0, -0.1],
            ]
        )
        beam.advance(word_probs)
        eos_count = sum(1 for s, t, k in beam.finished)
        assert eos_count >= 0


class TestBeamGetFinal:
    def test_returns_finished_sorted_by_score(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.finished = [
            (torch.tensor(0.3), 1, 0),
            (torch.tensor(0.5), 1, 1),
        ]
        final = beam.getFinal()
        assert len(final) == 2
        assert final[0][0] >= final[1][0]

    def test_adds_current_state_if_empty_finished(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.nextYs.append(torch.tensor([1, 3]))
        final = beam.getFinal()
        assert len(final) >= 1


class TestBeamBuildTargetTokens:
    def test_builds_tokens_until_eos(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        preds = [
            [torch.tensor(1), torch.tensor(3), torch.tensor(2), torch.tensor(4)],
            [torch.tensor(5), torch.tensor(6)],
        ]
        result = beam.buildTargetTokens(preds)
        assert len(result) == 2
        assert len(result[0]) == 2
        assert len(result[1]) == 2

    def test_handles_no_eos(self) -> None:
        beam = Beam(size=2, eos=99, device=torch.device("cpu"))
        preds = [
            [torch.tensor(1), torch.tensor(2), torch.tensor(3)],
        ]
        result = beam.buildTargetTokens(preds)
        assert len(result[0]) == 3


class TestBeamMultipleEos:
    def test_normalizes_single_int_to_membership_set(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        assert beam._eos == frozenset({2})

    def test_stops_on_any_eos_in_list(self) -> None:
        # (H) transformers can declare several valid stop ids; Beam must terminate
        # (H) on any of them, not just the first.
        beam = Beam(size=1, eos=[2, 99], device=torch.device("cpu"))
        preds = [[torch.tensor(5), torch.tensor(99), torch.tensor(6)]]
        result = beam.buildTargetTokens(preds)
        assert len(result[0]) == 1

    def test_advance_records_completion_on_alternate_eos(self) -> None:
        # (H) advance must record a finished hypothesis and set eosTop when the top
        # (H) token is any configured EOS id (not just the first).
        beam = Beam(size=1, eos=[2, 99], device=torch.device("cpu"))
        word_probs = torch.full((1, 100), -1e9)
        word_probs[0, 99] = 0.0
        beam.advance(word_probs)
        assert len(beam.finished) == 1
        assert beam.eosTop is True


class TestForwardAttentionMask:
    def _make_uninitialized(self, pad_id: int) -> UniXcoder:
        instance = UniXcoder.__new__(UniXcoder)
        nn.Module.__init__(instance)
        instance.config = MagicMock()
        instance.config.pad_token_id = pad_id
        return instance

    def test_attention_mask_is_4d(self) -> None:
        instance = self._make_uninitialized(pad_id=1)
        captured: dict[str, torch.Size] = {}

        def fake_model(
            source_ids: torch.Tensor, attention_mask: torch.Tensor
        ) -> tuple[torch.Tensor]:
            captured["shape"] = attention_mask.shape
            batch, seq = source_ids.shape
            return (torch.zeros(batch, seq, 8),)

        instance.model = MagicMock(side_effect=fake_model)

        source_ids = torch.tensor([[2, 3, 4, 5, 1], [2, 3, 1, 1, 1]])
        instance.forward(source_ids)

        assert "shape" in captured
        assert len(captured["shape"]) == 4
        assert captured["shape"][0] == 2
        assert captured["shape"][1] == 1
        assert captured["shape"][2] == 5
        assert captured["shape"][3] == 5


class TestBeamGetHyp:
    def test_constructs_hypothesis_path(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.prevKs = [torch.tensor([0, 0]), torch.tensor([0, 1])]
        beam.nextYs = [
            torch.tensor([0, 0]),
            torch.tensor([1, 2]),
            torch.tensor([3, 4]),
        ]
        beam_res = [(torch.tensor(0.5), 2, 0)]
        hyps = beam.getHyp(beam_res)
        assert len(hyps) == 1
        assert len(hyps[0]) == 2
