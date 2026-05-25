from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from ..core.progress import ProgressReporter


ParamType = Literal["str", "int", "bool", "path", "dir"]


@dataclass
class ParamSpec:
    name: str
    type: ParamType
    label: str
    help: str = ""
    required: bool = True
    default: Any = None


@dataclass
class TaskSpec:
    name: str
    title: str
    description: str
    run: Callable[[dict, ProgressReporter], Any]
    params: list[ParamSpec] = field(default_factory=list)
