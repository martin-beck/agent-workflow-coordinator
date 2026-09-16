# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Immutable lifecycle observations for bounded formal correspondence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    phase: str
    revision: int
    owner: str
    lock: str
    project_id: str
    session_digest: str
    fencing_token: str
    _token: object = field(repr=False, compare=False)


class LifecycleObserver(Protocol):
    def __call__(self, event: LifecycleEvent) -> None: ...
