# Copyright (c) Meta Platforms, Inc. and affiliates

import torch
import torch.nn.functional as F
from typing import Union, Dict, Tuple, List

import spmd.tensor.api as dtensor
from spmd.tensor.placement_types import DTensorSpec, OutputSpecType

ArgKwargsType = Union[Tuple[object, ...], Dict[str, object]]


def unwrap_local_tensor(e: "dtensor.DTensor") -> torch.Tensor:
    return e._local_tensor if isinstance(e, dtensor.DTensor) else e


def unwrap_schema(e: object) -> object:
    return e._spec if isinstance(e, dtensor.DTensor) else e


def wrap(res: object, spec: OutputSpecType) -> object:
    if isinstance(res, torch.Tensor):
        assert spec is not None and isinstance(
            spec, DTensorSpec
        ), f"output spec does not match with output! Expected DTensorSpec, got {spec}."
        res = dtensor.DTensor(
            res,
            spec.mesh,
            spec.placements,
            size=spec.shape,
            requires_grad=res.requires_grad,
        )
        return res
    elif isinstance(res, list):
        assert spec is not None and isinstance(
            spec, list
        ), f"output spec does not match with output! Expected list, got {spec}."
        return list(
            dtensor.DTensor(e, s.mesh, s.placements, size=s.shape)
            for e, s in zip(res, spec)
        )
    elif isinstance(res, tuple):
        assert spec is not None and isinstance(
            spec, tuple
        ), f"output spec does not match with output! Expected tuple, got {spec}"
        return tuple(
            dtensor.DTensor(e, s.mesh, s.placements, size=s.shape)
            for e, s in zip(res, spec)
        )
    else:
        # if the res contains only non tensor values, we simply return it without rewrapping
        return res


def needs_pad(rank: int, pad_idx: int) -> bool:
    return pad_idx != 0 and rank >= pad_idx
