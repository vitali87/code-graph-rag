from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

# Optional heavyweight dependencies: a base install ships without the
# `semantic` extra, so this module must SKIP rather than abort collection
# (issue #1410). Only the third-party extras are skip conditions; the
# first-party module is imported normally afterwards so a defect inside it
# still fails collection instead of hiding as a skip.
torch = pytest.importorskip("torch")
pytest.importorskip("transformers")
_unixcoder = importlib.import_module("codebase_rag.unixcoder")

nn = torch.nn
Beam = _unixcoder.Beam
UniXcoder = _unixcoder.UniXcoder


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
        state = beam.get_current_state()
        assert state.shape == (5, 1)

    def test_returns_last_nextYs(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        beam.nextYs.append(torch.tensor([1, 2, 3]))
        state = beam.get_current_state()
        assert torch.all(state.flatten() == torch.tensor([1, 2, 3]))


class TestBeamGetCurrentOrigin:
    def test_returns_last_prevKs(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        beam.prevKs.append(torch.tensor([0, 1, 2]))
        origin = beam.get_current_origin()
        assert torch.all(origin == torch.tensor([0, 1, 2]))


class TestBeamDone:
    def test_not_done_initially(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        assert beam.done() is False

    def test_done_when_eos_top_and_enough_finished(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.eos_top = True
        beam.finished = [
            (torch.tensor(0.5), 1, 0),
            (torch.tensor(0.4), 1, 1),
        ]
        assert beam.done() is True

    def test_not_done_when_not_eos_top(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.eos_top = False
        beam.finished = [
            (torch.tensor(0.5), 1, 0),
            (torch.tensor(0.4), 1, 1),
        ]
        assert beam.done() is False

    def test_not_done_when_not_enough_finished(self) -> None:
        beam = Beam(size=3, eos=2, device=torch.device("cpu"))
        beam.eos_top = True
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
        final = beam.get_final()
        assert len(final) == 2
        assert final[0][0] >= final[1][0]

    def test_adds_current_state_if_empty_finished(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        beam.nextYs.append(torch.tensor([1, 3]))
        final = beam.get_final()
        assert len(final) >= 1


class TestBeamBuildTargetTokens:
    def test_builds_tokens_until_eos(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        preds = [
            [torch.tensor(1), torch.tensor(3), torch.tensor(2), torch.tensor(4)],
            [torch.tensor(5), torch.tensor(6)],
        ]
        result = beam.build_target_tokens(preds)
        assert len(result) == 2
        assert len(result[0]) == 2
        assert len(result[1]) == 2

    def test_handles_no_eos(self) -> None:
        beam = Beam(size=2, eos=99, device=torch.device("cpu"))
        preds = [
            [torch.tensor(1), torch.tensor(2), torch.tensor(3)],
        ]
        result = beam.build_target_tokens(preds)
        assert len(result[0]) == 3


class TestBeamMultipleEos:
    def test_normalizes_single_int_to_membership_set(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        assert beam._eos == frozenset({2})

    def test_stops_on_any_eos_in_list(self) -> None:
        # transformers can declare several valid stop ids; Beam must terminate
        # on any of them, not just the first.
        beam = Beam(size=1, eos=[2, 99], device=torch.device("cpu"))
        preds = [[torch.tensor(5), torch.tensor(99), torch.tensor(6)]]
        result = beam.build_target_tokens(preds)
        assert len(result[0]) == 1

    def test_advance_records_completion_on_alternate_eos(self) -> None:
        # advance must record a finished hypothesis and set eos_top when the top
        # token is any configured EOS id (not just the first).
        beam = Beam(size=1, eos=[2, 99], device=torch.device("cpu"))
        word_probs = torch.full((1, 100), -1e9)
        word_probs[0, 99] = 0.0
        beam.advance(word_probs)
        assert len(beam.finished) == 1
        assert beam.eos_top is True


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
        hyps = beam.get_hyp(beam_res)
        assert len(hyps) == 1
        assert len(hyps[0]) == 2


class TestBeamSuppressesFinishedBeams:
    def test_a_beam_ended_by_eos_is_not_extended(self) -> None:
        beam = Beam(size=2, eos=2, device=torch.device("cpu"))
        # Step one: EOS (2) and token 1 are the two best, so beam 0 ends.
        beam.advance(torch.tensor([[0.0, 0.5, 0.9]]))
        assert [int(t) for t in beam.nextYs[-1]] == [2, 1]
        # Step two scores beam 0 highest, but a finished beam must not grow:
        # every survivor has to come from beam 1.
        beam.advance(torch.tensor([[5.0, 5.0, 5.0], [0.1, 0.2, 0.3]]))
        assert all(int(k) == 1 for k in beam.get_current_origin())


class _FakeEncoderOutput:
    def __init__(
        self,
        last_hidden_state: torch.Tensor,
        past_key_values: tuple[tuple[torch.Tensor, torch.Tensor], ...] = (),
    ) -> None:
        self.last_hidden_state = last_hidden_state
        self.past_key_values = past_key_values


class TestGenerate:
    _HIDDEN = 4
    _VOCAB = 5

    def _make(self) -> UniXcoder:
        instance = UniXcoder.__new__(UniXcoder)
        nn.Module.__init__(instance)
        instance.config = MagicMock()
        instance.config.pad_token_id = 1
        instance.config.eos_token_id = 2
        instance.register_buffer(
            "bias", torch.tril(torch.ones(64, 64, dtype=torch.uint8)).view(1, 64, 64)
        )
        torch.manual_seed(0)
        instance.lm_head = nn.Linear(self._HIDDEN, self._VOCAB, bias=False)
        instance.lsm = nn.LogSoftmax(dim=-1)

        def fake_model(
            ids: torch.Tensor,
            attention_mask: torch.Tensor,
            past_key_values: list[list[torch.Tensor]] | None = None,
        ) -> _FakeEncoderOutput:
            batch, seq = ids.shape
            hidden = torch.randn(batch, seq, self._HIDDEN)
            if past_key_values is None:
                kv = torch.zeros(batch, 1, seq, self._HIDDEN)
                return _FakeEncoderOutput(hidden, ((kv, kv),))
            return _FakeEncoderOutput(hidden)

        instance.model = MagicMock(side_effect=fake_model)
        return instance

    def test_decodes_a_padded_prediction_per_beam(self) -> None:
        instance = self._make()
        source_ids = torch.tensor([[0, 5, 6, 7], [0, 5, 1, 1]])

        preds = instance.generate(source_ids, beam_size=2, max_length=3)

        assert preds.shape == (2, 2, 3)


class TestDecode:
    def _make(self) -> UniXcoder:
        instance = UniXcoder.__new__(UniXcoder)
        nn.Module.__init__(instance)
        instance.tokenizer = MagicMock()
        instance.tokenizer.decode.side_effect = lambda ids, **_kw: " ".join(
            str(int(i)) for i in ids
        )
        return instance

    def test_each_beam_decodes_up_to_its_first_pad(self) -> None:
        instance = self._make()
        source_ids = torch.tensor([[[5, 6, 0, 7], [8, 0, 0, 0]]])

        assert instance.decode(source_ids) == [["5 6", "8"]]

    def test_a_batch_shaped_decode_is_rejected(self) -> None:
        # Negative: a tokenizer answering a list for one sequence is not a
        # prediction, and must not be passed off as one.
        instance = self._make()
        instance.tokenizer.decode.side_effect = lambda ids, **_kw: ["a", "b"]
        source_ids = torch.tensor([[[5, 6]]])

        with pytest.raises(AssertionError):
            instance.decode(source_ids)
