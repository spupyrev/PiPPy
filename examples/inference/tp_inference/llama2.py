# (c) Meta Platforms, Inc. and affiliates. Confidential and proprietary.

import json
import math
from manifold.clients.python import *  # noqa
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

import torch
import torch.distributed as dist

# For checkpoint reading directly from manifold
import torch.manifold.patch
import torch.nn.functional as F
from gen_ai.genie_projects.llm.metaformers.src.checkpoint_conversion.converter import (
    build_distributed_state_dict_from_consolidated,
)
from torch import nn
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.api import StateDictType
from torch.distributed.fsdp.wrap import ModuleWrapPolicy
from torchmultimodal import _PATH_MANAGER as PathManager
from torchmultimodal.fb.examples.llama.llama_2_tokenizer import Tokenizer

log = logging.getLogger(__name__)


@dataclass
class ModelArgs:
    dim: int = 4096
    n_layers: int = 32
    n_heads: int = 32
    n_kv_heads: Optional[int] = None
    vocab_size: int = -1  # defined later by tokenizer
    multiple_of: int = 256  # make SwiGLU hidden layer size multiple of large power of 2
    ffn_dim_multiplier: Optional[float] = None
    norm_eps: float = 1e-5

    max_batch_size: int = 32
    max_seq_len: int = 2048


# TODO: update this to use RMSNorm in MultiModal
class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0):
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)  # type: ignore
    freqs = torch.outer(t, freqs).float()  # type: ignore
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)  # complex64
    return freqs_cis


def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    ndim = x.ndim
    assert 0 <= 1 < ndim
    assert freqs_cis.shape == (x.shape[1], x.shape[-1])
    shape = [d if i == 1 or i == ndim - 1 else 1 for i, d in enumerate(x.shape)]
    return freqs_cis.view(*shape)


def apply_rotary_emb(
    xq: torch.Tensor,
    xk: torch.Tensor,
    freqs_cis: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    To be replaced by torchMM's RotaryEmbedding
    """
    xq_ = torch.view_as_complex(xq.float().reshape(*xq.shape[:-1], -1, 2))
    xk_ = torch.view_as_complex(xk.float().reshape(*xk.shape[:-1], -1, 2))
    freqs_cis = reshape_for_broadcast(freqs_cis, xq_)
    xq_out = torch.view_as_real(xq_ * freqs_cis).flatten(3)
    xk_out = torch.view_as_real(xk_ * freqs_cis).flatten(3)
    return xq_out.type_as(xq), xk_out.type_as(xk)


def repeat_kv(x: torch.Tensor, n_rep: int) -> torch.Tensor:
    """torch.repeat_interleave(x, dim=2, repeats=n_rep)"""
    bs, slen, n_kv_heads, head_dim = x.shape
    if n_rep == 1:
        return x
    return (
        x[:, :, :, None, :]
        .expand(bs, slen, n_kv_heads, n_rep, head_dim)
        .reshape(bs, slen, n_kv_heads * n_rep, head_dim)
    )


# TODO: below are taken from facebookresearch/llama, update to use torchMM components
# once they are compatible
class Attention(nn.Module):
    def __init__(
        self,
        n_heads: int,
        n_kv_heads: int,
        dim: int,
        max_batch_size: int,
        max_seq_len: int,
    ):
        super().__init__()
        self.n_kv_heads = n_heads if n_kv_heads is None else n_kv_heads
        self.n_local_heads = n_heads
        self.n_local_kv_heads = self.n_kv_heads
        self.n_rep = self.n_local_heads // self.n_local_kv_heads
        self.head_dim = dim // n_heads

        self.wq = nn.Linear(
            dim,
            n_heads * self.head_dim,
            bias=False,
        )
        self.wk = nn.Linear(
            dim,
            self.n_kv_heads * self.head_dim,
            bias=False,
        )
        self.wv = nn.Linear(
            dim,
            self.n_kv_heads * self.head_dim,
            bias=False,
        )
        self.wo = nn.Linear(n_heads * self.head_dim, dim, bias=False)
        self.max_batch_size = max_batch_size
        self.max_seq_len = max_seq_len
        self._init_cache_k()
        self._init_cache_v()

    def _init_cache_k(self):
        self.cache_k = torch.zeros(
            (
                self.max_batch_size,
                self.max_seq_len,
                self.n_local_kv_heads,
                self.head_dim,
            )
        )

    def _init_cache_v(self):
        self.cache_v = torch.zeros(
            (
                self.max_batch_size,
                self.max_seq_len,
                self.n_local_kv_heads,
                self.head_dim,
            )
        )

    def forward(
        self,
        x: torch.Tensor,
        start_pos: int,
        freqs_cis: torch.Tensor,
        mask: Optional[torch.Tensor],
    ):
        bsz, seqlen, _ = x.shape
        xq, xk, xv = self.wq(x), self.wk(x), self.wv(x)

        xq = xq.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        xk = xk.view(bsz, seqlen, self.n_local_kv_heads, self.head_dim)
        xv = xv.view(bsz, seqlen, self.n_local_kv_heads, self.head_dim)

        xq, xk = apply_rotary_emb(xq, xk, freqs_cis=freqs_cis)

        self.cache_k = self.cache_k.to(xq)
        self.cache_v = self.cache_v.to(xq)

        self.cache_k[:bsz, start_pos : start_pos + seqlen] = xk
        self.cache_v[:bsz, start_pos : start_pos + seqlen] = xv

        keys = self.cache_k[:bsz, : start_pos + seqlen]
        values = self.cache_v[:bsz, : start_pos + seqlen]

        # repeat k/v heads if n_kv_heads < n_heads
        keys = repeat_kv(keys, self.n_rep)  # (bs, seqlen, n_local_heads, head_dim)
        values = repeat_kv(values, self.n_rep)  # (bs, seqlen, n_local_heads, head_dim)

        xq = xq.transpose(1, 2)  # (bs, n_local_heads, seqlen, head_dim)
        keys = keys.transpose(1, 2)
        values = values.transpose(1, 2)
        scores = torch.matmul(xq, keys.transpose(2, 3)) / math.sqrt(self.head_dim)
        if mask is not None:
            scores = scores + mask  # (bs, n_local_heads, seqlen, cache_len + seqlen)
        scores = F.softmax(scores.float(), dim=-1).type_as(xq)
        output = torch.matmul(scores, values)  # (bs, n_local_heads, seqlen, head_dim)
        output = output.transpose(1, 2).contiguous().view(bsz, seqlen, -1)
        return self.wo(output)


class FeedForward(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int,
        multiple_of: int,
        ffn_dim_multiplier: Optional[float],
    ):
        super().__init__()
        hidden_dim = int(2 * hidden_dim / 3)
        # custom dim factor multiplier
        if ffn_dim_multiplier is not None:
            hidden_dim = int(ffn_dim_multiplier * hidden_dim)
        hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.w1 = nn.Linear(dim, hidden_dim, bias=False)
        self.w2 = nn.Linear(hidden_dim, dim, bias=False)
        self.w3 = nn.Linear(dim, hidden_dim, bias=False)

    def forward(self, x):
        return self.w2(F.silu(self.w1(x)) * self.w3(x))


class TransformerBlock(nn.Module):
    def __init__(
        self,
        layer_id: int,
        n_heads: int,
        n_kv_heads: int,
        dim: int,
        multiple_of: int,
        ffn_dim_multiplier: Optional[float],
        max_batch_size: int,
        max_seq_len: int,
        norm_eps: float,
    ):
        super().__init__()
        self.n_heads = n_heads
        self.dim = dim
        self.head_dim = dim // n_heads
        self.attention = Attention(
            n_heads, n_kv_heads, dim, max_batch_size, max_seq_len
        )
        self.feed_forward = FeedForward(
            dim=dim,
            hidden_dim=4 * dim,
            multiple_of=multiple_of,
            ffn_dim_multiplier=ffn_dim_multiplier,
        )
        self.layer_id = layer_id
        self.attention_norm = RMSNorm(dim, eps=norm_eps)
        self.ffn_norm = RMSNorm(dim, eps=norm_eps)

    def forward(
        self,
        x: torch.Tensor,
        start_pos: int,
        freqs_cis: torch.Tensor,
        mask: Optional[torch.Tensor],
    ):
        h = x + self.attention(self.attention_norm(x), start_pos, freqs_cis, mask)
        out = h + self.feed_forward(self.ffn_norm(h))
        return out


class Transformer(nn.Module):
    """
    LLama2 implementation, free of any coupling to parallelism implementations, heavily drawn from
    https://github.com/facebookresearch/llama.
    """

    def __init__(
        self,
        vocab_size: int,
        n_layers: int,
        dim: int,
        n_heads: int,
        n_kv_heads: int,
        multiple_of: int,
        ffn_dim_multiplier: Optional[float],
        max_batch_size: int,
        max_seq_len: int,
        norm_eps: float,
    ):
        super().__init__()
        self.vocab_size = vocab_size
        self.n_layers = n_layers
        self.dim = dim
        self.n_heads = n_heads
        self.max_seq_len = max_seq_len
        self.tok_embeddings = nn.Embedding(vocab_size, dim)
        self.max_batch_size = max_batch_size
        self.max_seq_len = max_seq_len

        self.layers = torch.nn.ModuleList()
        for layer_id in range(n_layers):
            self.layers.append(
                TransformerBlock(
                    layer_id,
                    n_heads,
                    n_kv_heads,
                    dim,
                    multiple_of,
                    ffn_dim_multiplier,
                    max_batch_size,
                    max_seq_len,
                    norm_eps,
                )
            )

        self.norm = RMSNorm(dim, eps=norm_eps)
        self.output = nn.Linear(dim, vocab_size, bias=False)

        self.freqs_cis = precompute_freqs_cis(
            self.dim // self.n_heads, self.max_seq_len * 2
        )

    def forward(self, tokens: torch.Tensor, start_pos: int):
        _bsz, seqlen = tokens.shape
        print(
            f"RV: before embedding lookup, input {tokens}, start:{start_pos}",
            flush=True,
        )
        h = self.tok_embeddings(tokens)
        self.freqs_cis = self.freqs_cis.to(h.device)
        freqs_cis = self.freqs_cis[start_pos : start_pos + seqlen]

        mask = None
        if seqlen > 1:
            mask = torch.full(
                (1, 1, seqlen, seqlen), float("-inf"), device=tokens.device
            )
            mask = torch.triu(mask, diagonal=start_pos + 1).type_as(h)

        for layer in self.layers:
            h = layer(h, start_pos, freqs_cis, mask)
        h = self.norm(h)
        output = self.output(h).float()
        return output


### --- Utilities for model creation / loading ---- ####


def _build_model_args(ckpt_dir: str, max_seq_len, max_batch_size) -> ModelArgs:
    """
    Reads params.json from checkpoint and builds ModelArgs to initialize
    model with.
    """
    with PathManager.open(os.path.join(ckpt_dir, "params.json"), "r") as f:
        params = json.loads(f.read())

    # Some checkpoints have other details besides "model", fix this up and use a
    # clearly specified format.
    model_params = params.get("model", params)
    model_args: ModelArgs = ModelArgs(
        max_seq_len=max_seq_len,
        max_batch_size=max_batch_size,
        dim=model_params["dim"],
        n_layers=model_params["n_layers"],
        n_heads=model_params["n_heads"],
        n_kv_heads=model_params.get("n_kv_heads", model_params["n_heads"]),
        multiple_of=model_params["multiple_of"],
        ffn_dim_multiplier=model_params.get("ffn_dim_multiplier", None),
        norm_eps=model_params["norm_eps"],
    )
    return model_args


def _create_tokenizer(bucket: str, tokenizer_path: str) -> Tokenizer:
    local_tokenizer_path = "/tmp/tokenizer_path"
    with ManifoldClient.get_client(bucket) as client:
        client.sync_get(tokenizer_path, local_tokenizer_path)
    log.debug(f"successfully saved tokenizer to {local_tokenizer_path}")
    tokenizer = Tokenizer(model_path=local_tokenizer_path)
    return tokenizer


def _init_local_model(model_args: ModelArgs) -> Transformer:
    with torch.device("meta"):
        model = Transformer(
            model_args.vocab_size,
            model_args.n_layers,
            model_args.dim,
            model_args.n_heads,
            model_args.n_kv_heads,  # pyre-ignore[6]
            model_args.multiple_of,
            model_args.ffn_dim_multiplier,
            model_args.max_batch_size,
            model_args.max_seq_len,
            model_args.norm_eps,
        )

    model.freqs_cis = precompute_freqs_cis(
        model.dim // model.n_heads, model.max_seq_len * 2
    )
    for tformer_block in model.layers:
        tformer_block.attention._init_cache_k()
        tformer_block.attention._init_cache_v()

    return model


def get_consolidated_ckpt_path(
    ckpt_dir: Union[str, Path], mp_rank: int = 0, mp_size: int = 1
) -> Union[str, Path]:
    """
    From https://fburl.com/code/7wfw9goy
    """
    if mp_size == 1:
        assert mp_rank == 0
        filename = "consolidated.00.pth"
    else:
        filename = f"consolidated.{mp_rank:02d}.pth"
    if isinstance(ckpt_dir, Path):
        return ckpt_dir / filename
    else:
        return os.path.join(ckpt_dir, filename)


def _load_checkpoint(model, model_parallel_size: int, ckpt_dir: str) -> None:
    mp_group, _ = dist.new_subgroups(group_size=model_parallel_size)
    local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if local_rank == -1:
        raise RuntimeError("Expected local_rank to be set, but it is not!")
    mp_rank = local_rank % model_parallel_size
    state_dict_pth = get_consolidated_ckpt_path(
        ckpt_dir=ckpt_dir, mp_rank=mp_rank, mp_size=model_parallel_size
    )
    state_dict = torch.load(state_dict_pth)
    dist_state_dict = build_distributed_state_dict_from_consolidated(
        model, state_dict, model_parallel_world_size=model_parallel_size
    )
    log.debug("build distributed_state_dict")
    missing_keys, unexpected_keys = model.load_state_dict(dist_state_dict, strict=False)
    assert not missing_keys
    assert len(unexpected_keys) == 1 and "freqs" in unexpected_keys[0]


class Llama:
    @staticmethod
    def build(
        ckpt_dir: str,
        tokenizer_path: str,
        max_seq_len: int,
        max_batch_size: int,
        model_parallel_size: int = 8,
    ) -> "Llama":
        """
        Heavily motivated from https://github.com/facebookresearch/llama/blob/main/llama/generation.py#L51,
        and adapted for native parallelism APIs.
        """
        start = time.time()
        torch.set_default_tensor_type(torch.cuda.HalfTensor)
        local_rank = int(os.environ.get("LOCAL_RANK", -1))
        if local_rank == -1:
            raise RuntimeError("Expected local_rank to be set, but it is not!")

        torch.cuda.set_device(local_rank)
        model_args = _build_model_args(ckpt_dir, max_seq_len, max_batch_size)
        tokenizer = _create_tokenizer(
            bucket="pytorch_multimodal", tokenizer_path=tokenizer_path
        )
        model_args.vocab_size = tokenizer.n_words

        model = _init_local_model(model_args)
        model = FSDP(
            model,
            param_init_fn=lambda module: module.to_empty(
                device=torch.device("cuda"), recurse=False
            ),
            device_id=torch.cuda.current_device(),
            auto_wrap_policy=ModuleWrapPolicy({TransformerBlock}),
        )
        FSDP.set_state_dict_type(model, StateDictType.SHARDED_STATE_DICT)
        dist.barrier()
        log.debug(f"Rank {dist.get_rank()}: created FSDP model {model}")
        # Convert state_dict from fairscale + load in to FSDP
        _load_checkpoint(
            model=model,
            model_parallel_size=model_parallel_size,
            ckpt_dir=ckpt_dir,
        )
        param_numel = sum(p.numel() for p in model.parameters())
        log.debug(
            f"Loaded {param_numel * dist.get_world_size()} params (across all workers) in {time.time() - start:.2f} seconds"
        )
        return Llama(model, tokenizer)

    def __init__(self, model: Union[FSDP, Transformer], tokenizer: Tokenizer):
        self.model = model
        self.tokenizer = tokenizer