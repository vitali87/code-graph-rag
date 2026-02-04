# (H) Adapted from https://github.com/microsoft/unixcoder
# (H) Copyright (c) Microsoft Corporation.
# (H) Licensed under the MIT license.

import torch
from torch import nn
from transformers import RobertaConfig, RobertaModel, RobertaTokenizer

from . import constants as cs


class UniXcoder(nn.Module):
    def __init__(self, model_name: str) -> None:
        super().__init__()
        self.tokenizer: RobertaTokenizer = RobertaTokenizer.from_pretrained(model_name)
        self.config: RobertaConfig = RobertaConfig.from_pretrained(model_name)
        self.config.is_decoder = True
        self.model: RobertaModel = RobertaModel.from_pretrained(
            model_name, config=self.config
        )

        self.register_buffer(
            cs.UNIXCODER_BUFFER_BIAS,
            torch.tril(
                torch.ones(
                    (cs.UNIXCODER_MAX_CONTEXT, cs.UNIXCODER_MAX_CONTEXT),
                    dtype=torch.uint8,
                )
            ).view(1, cs.UNIXCODER_MAX_CONTEXT, cs.UNIXCODER_MAX_CONTEXT),
        )
        self.lm_head: nn.Linear = nn.Linear(
            self.config.hidden_size, self.config.vocab_size, bias=False
        )
        self.lm_head.weight = self.model.embeddings.word_embeddings.weight
        self.lsm: nn.LogSoftmax = nn.LogSoftmax(dim=-1)

        self.tokenizer.add_tokens([cs.UNIXCODER_MASK_TOKEN], special_tokens=True)

    def tokenize(
        self,
        inputs: list[str],
        mode: cs.UniXcoderMode = cs.UniXcoderMode.ENCODER_ONLY,
        max_length: int = 512,
        padding: bool = False,
    ) -> list[list[int]]:
        assert max_length < cs.UNIXCODER_MAX_CONTEXT

        tokenizer = self.tokenizer

        tokens_ids = []
        for x in inputs:
            tokens = tokenizer.tokenize(x)
            match mode:
                case cs.UniXcoderMode.ENCODER_ONLY:
                    tokens = tokens[: max_length - 4]
                    tokens = (
                        [tokenizer.cls_token, mode, tokenizer.sep_token]
                        + tokens
                        + [tokenizer.sep_token]
                    )
                case cs.UniXcoderMode.DECODER_ONLY:
                    tokens = tokens[-(max_length - 3) :]
                    tokens = [tokenizer.cls_token, mode, tokenizer.sep_token] + tokens
                case cs.UniXcoderMode.ENCODER_DECODER:
                    tokens = tokens[: max_length - 5]
                    tokens = (
                        [tokenizer.cls_token, mode, tokenizer.sep_token]
                        + tokens
                        + [tokenizer.sep_token]
                    )

            converted = tokenizer.convert_tokens_to_ids(tokens)
            tokens_id: list[int] = (
                converted if isinstance(converted, list) else [converted]
            )
            if padding:
                pad_id = self.config.pad_token_id
                assert pad_id is not None
                tokens_id += [pad_id] * (max_length - len(tokens_id))
            tokens_ids.append(tokens_id)
        return tokens_ids

    def decode(self, source_ids: torch.Tensor) -> list[list[str]]:
        predictions = []
        for x in source_ids:
            prediction = []
            for y in x:
                t = y.cpu().numpy()
                t = list(t)
                if 0 in t:
                    t = t[: t.index(0)]
                text = self.tokenizer.decode(t, clean_up_tokenization_spaces=False)
                prediction.append(text)
            predictions.append(prediction)
        return predictions

    def forward(self, source_ids: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        pad_id = self.config.pad_token_id
        assert pad_id is not None
        mask = source_ids.ne(pad_id)
        token_embeddings = self.model(
            source_ids, attention_mask=mask.unsqueeze(1) * mask.unsqueeze(2)
        )[0]
        sentence_embeddings = (token_embeddings * mask.unsqueeze(-1)).sum(1) / mask.sum(
            -1
        ).unsqueeze(-1)
        return token_embeddings, sentence_embeddings

    def generate(
        self,
        source_ids: torch.Tensor,
        decoder_only: bool = True,
        eos_id: int | None = None,
        beam_size: int = 5,
        max_length: int = 64,
    ) -> torch.Tensor:
        # (H) self.bias is registered as buffer (Tensor) but typed as Module by ty
        bias: torch.Tensor = getattr(self, cs.UNIXCODER_BUFFER_BIAS)
        pad_id = self.config.pad_token_id
        assert pad_id is not None

        if decoder_only:
            mask = bias[:, : source_ids.size(-1), : source_ids.size(-1)]
        else:
            mask = source_ids.ne(pad_id)
            mask = mask.unsqueeze(1) * mask.unsqueeze(2)

        if eos_id is None:
            eos_id = self.config.eos_token_id
        assert eos_id is not None

        device = source_ids.device

        preds = []
        zero = torch.LongTensor(1).fill_(0).to(device)
        source_len = list(source_ids.ne(1).sum(-1).cpu().numpy())
        length = source_ids.size(-1)
        encoder_output = self.model(source_ids, attention_mask=mask)
        for i in range(source_ids.shape[0]):
            context = [
                [x[i : i + 1, :, : source_len[i]].repeat(beam_size, 1, 1, 1) for x in y]
                for y in encoder_output.past_key_values
            ]
            beam = Beam(beam_size, eos_id, device)
            input_ids = beam.getCurrentState().clone()
            context_ids = source_ids[i : i + 1, : source_len[i]].repeat(beam_size, 1)
            out = encoder_output.last_hidden_state[i : i + 1, : source_len[i]].repeat(
                beam_size, 1, 1
            )
            for _ in range(max_length):
                if beam.done():
                    break
                if _ == 0:
                    hidden_states = out[:, -1, :]
                    out = self.lsm(self.lm_head(hidden_states)).data
                    beam.advance(out)
                    input_ids.data.copy_(
                        input_ids.data.index_select(0, beam.getCurrentOrigin())
                    )
                    input_ids = beam.getCurrentState().clone()
                else:
                    length = context_ids.size(-1) + input_ids.size(-1)
                    out = self.model(
                        input_ids,
                        attention_mask=bias[:, context_ids.size(-1) : length, :length],
                        past_key_values=context,
                    ).last_hidden_state
                    hidden_states = out[:, -1, :]
                    out = self.lsm(self.lm_head(hidden_states)).data
                    beam.advance(out)
                    input_ids.data.copy_(
                        input_ids.data.index_select(0, beam.getCurrentOrigin())
                    )
                    input_ids = torch.cat(
                        (input_ids, beam.getCurrentState().clone()), -1
                    )
            hyp = beam.getHyp(beam.getFinal())
            pred = beam.buildTargetTokens(hyp)[:beam_size]
            pred = [
                torch.cat(
                    [x.view(-1) for x in p] + [zero] * (max_length - len(p))
                ).view(1, -1)
                for p in pred
            ]
            preds.append(torch.cat(pred, 0).unsqueeze(0))

        preds = torch.cat(preds, 0)

        return preds


class Beam:
    def __init__(self, size: int, eos: int, device: torch.device) -> None:
        self.size = size
        self.device = device
        self.scores: torch.Tensor = torch.FloatTensor(size).zero_().to(device)
        self.prevKs: list[torch.Tensor] = []
        self.nextYs: list[torch.Tensor] = [torch.LongTensor(size).fill_(0).to(device)]
        self._eos = eos
        self.eosTop = False
        self.finished: list[tuple[torch.Tensor, int, int]] = []

    def getCurrentState(self) -> torch.Tensor:
        batch = self.nextYs[-1].view(-1, 1)
        return batch

    def getCurrentOrigin(self) -> torch.Tensor:
        return self.prevKs[-1]

    def advance(self, wordLk: torch.Tensor) -> None:
        numWords = wordLk.size(1)

        if len(self.prevKs) > 0:
            beamLk = wordLk + self.scores.unsqueeze(1).expand_as(wordLk)

            for i in range(self.nextYs[-1].size(0)):
                if self.nextYs[-1][i] == self._eos:
                    beamLk[i] = -1e20
        else:
            beamLk = wordLk[0]
        flatBeamLk = beamLk.view(-1)
        bestScores, bestScoresId = flatBeamLk.topk(self.size, 0, True, True)

        self.scores = bestScores

        prevK = torch.div(bestScoresId, numWords, rounding_mode="floor")
        self.prevKs.append(prevK)
        self.nextYs.append(bestScoresId - prevK * numWords)

        for i in range(self.nextYs[-1].size(0)):
            if self.nextYs[-1][i] == self._eos:
                s = self.scores[i]
                self.finished.append((s, len(self.nextYs) - 1, i))

        if self.nextYs[-1][0] == self._eos:
            self.eosTop = True

    def done(self) -> bool:
        return self.eosTop and len(self.finished) >= self.size

    def getFinal(self) -> list[tuple[torch.Tensor, int, int]]:
        if len(self.finished) == 0:
            self.finished.append((self.scores[0], len(self.nextYs) - 1, 0))
        self.finished.sort(key=lambda a: -a[0])
        if len(self.finished) != self.size:
            unfinished = [
                (self.scores[i], len(self.nextYs) - 1, i)
                for i in range(self.nextYs[-1].size(0))
                if self.nextYs[-1][i] != self._eos
            ]
            unfinished.sort(key=lambda a: -a[0])
            self.finished += unfinished[: self.size - len(self.finished)]
        return self.finished[: self.size]

    def getHyp(
        self, beam_res: list[tuple[torch.Tensor, int, int]]
    ) -> list[list[torch.Tensor]]:
        hyps: list[list[torch.Tensor]] = []
        for _, timestep, k in beam_res:
            hyp: list[torch.Tensor] = []
            for j in range(len(self.prevKs[:timestep]) - 1, -1, -1):
                hyp.append(self.nextYs[j + 1][k])
                k = self.prevKs[j][k]
            hyps.append(hyp[::-1])
        return hyps

    def buildTargetTokens(
        self, preds: list[list[torch.Tensor]]
    ) -> list[list[torch.Tensor]]:
        sentence: list[list[torch.Tensor]] = []
        for pred in preds:
            tokens: list[torch.Tensor] = []
            for tok in pred:
                if tok == self._eos:
                    break
                tokens.append(tok)
            sentence.append(tokens)
        return sentence
