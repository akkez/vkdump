from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from ..core.progress import ProgressReporter


ParamType = Literal["str", "int", "bool", "path", "dir", "path_any", "choice"]


@dataclass
class ParamSpec:
    name: str
    type: ParamType
    label: str
    help: str = ""
    required: bool = True
    default: Any = None
    # For type="choice": static list of (value, label) options.
    choices: list[tuple[str, str]] = field(default_factory=list)
    # For type="choice" with options known only at form-build time
    # (e.g. populated from the DB). Called once per form rebuild.
    choices_provider: Callable[[], list[tuple[str, str]]] | None = None


@dataclass
class TaskSpec:
    name: str
    title: str
    description: str
    run: Callable[[dict, ProgressReporter], Any]
    params: list[ParamSpec] = field(default_factory=list)
