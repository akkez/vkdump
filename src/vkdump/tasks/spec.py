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
    # Optional dynamic default for any field type. Called once per form
    # rebuild; if it returns a non-None value, it overrides `default`
    # (e.g. pull "last used dump source" from app_config). Lets a task
    # prefill cross-run remembered values without each one growing its
    # own settings plumbing.
    default_provider: Callable[[], Any] | None = None


@dataclass
class TaskSpec:
    name: str
    title: str
    description: str
    run: Callable[[dict, ProgressReporter], Any]
    params: list[ParamSpec] = field(default_factory=list)
    # Optional: maps a successful run's result dict to a file system
    # path (typically an HTML index) that the GUI's "Open output"
    # button should reveal in the user's default browser. Returns
    # None / missing when the run produced nothing openable.
    result_open_path: Callable[[Any], Any] | None = None
    # Whether a successful run can change DB rows that other panels'
    # `choices_provider` queries read. True by default; set False for
    # read-only tasks (stats) so finishing them doesn't trigger a
    # repaint cascade across every tab.
    mutates_data: bool = True
    # If True, the GUI pops the run's `result` (assumed JSON-serialisable)
    # into a message box on success. Used by probing tasks that just need
    # to show a one-shot response to the user.
    result_dialog: bool = False
    # If True, the GUI checks the active account has a VK API token before
    # starting; if not, opens the auth dialog first and only proceeds once
    # the user has authorized (or cancels the run).
    requires_vk_token: bool = False
