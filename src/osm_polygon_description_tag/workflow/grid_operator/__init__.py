"""Public Grid operator API with cohesive implementation modules."""

import sys
from types import ModuleType

from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    REQUIRED_CORES,
)
from osm_polygon_description_tag.workflow.grid_scheduler import JobState

from . import bundle as _bundle_module
from . import models as _models_module
from . import reconciliation as _reconciliation_module
from . import results as _results_module
from . import script as _script_module
from . import stage as _stage_module
from . import state as _state_module
from . import submission as _submission_module
from . import verify as _verify_module
from .bundle import (
    initialize_shard_checkpoint,
    prepare_job,
    quarantine_orphan_artifacts,
    read_bundle,
    read_intent,
)
from .models import (
    BUNDLE_FILENAME,
    GRID_SUBMISSION_LOCK_FILENAME,
    INTENT_FILENAME,
    JOB_CONFIG_FILENAME,
    JOB_SCRIPT_FILENAME,
    QUARANTINE_DIRNAME,
    STAGE_MANIFEST_FILENAME,
    GridOperatorError,
    JobBundle,
    JobNameResolver,
    JobPaths,
    PreparedJob,
    StagedFile,
    SubmissionIntent,
    bundle_for_shard,
    jobs_root,
)
from .reconciliation import (
    Reconciliation,
    collect_results,
    gather_policy_evidence,
    gather_policy_outputs,
    reconcile_job,
    resolve_job_name,
)
from .results import acknowledge_collected_results, adopt_retrieved_intent
from .script import job_paths, render_job_script
from .stage import prepare_portable_job
from .submission import SubmissionPlan, plan_submission, submission_lock, submit_job
from .verify import (
    build_bundle_transfer_argv,
    build_result_retrieval_argv,
    import_retrieved_results,
    verify_prepared_bundle,
)

_COMPATIBILITY_MODULES = (
    _models_module,
    _script_module,
    _bundle_module,
    _stage_module,
    _verify_module,
    _submission_module,
    _reconciliation_module,
    _results_module,
    _state_module,
)


class _GridOperatorModule(ModuleType):
    """Propagate legacy monkeypatches to the module that owns each helper."""

    def __setattr__(self, name: str, value: object) -> None:
        names = ("fsync_directory", name) if name == "_fsync_directory" else (name,)
        for candidate in names:
            for module in _COMPATIBILITY_MODULES:
                if candidate in vars(module):
                    setattr(module, candidate, value)
        super().__setattr__(name, value)


def __getattr__(name: str) -> object:
    """Resolve legacy module attributes from their focused implementation module.

    The pre-refactor operator was one module, so tests and downstream tooling
    could reach implementation details through ``grid_operator``. Keep those
    imports working while making the package's documented ``__all__`` the only
    supported public surface.
    """
    for module in _COMPATIBILITY_MODULES:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


sys.modules[__name__].__class__ = _GridOperatorModule

__all__ = [
    "BUNDLE_FILENAME",
    "GRID_SUBMISSION_LOCK_FILENAME",
    "INTENT_FILENAME",
    "JOB_CONFIG_FILENAME",
    "JOB_SCRIPT_FILENAME",
    "MAX_PROCESSING_SECONDS",
    "MAX_WALLTIME_SECONDS",
    "QUARANTINE_DIRNAME",
    "REQUIRED_CORES",
    "STAGE_MANIFEST_FILENAME",
    "GridOperatorError",
    "JobBundle",
    "JobNameResolver",
    "JobPaths",
    "JobState",
    "PreparedJob",
    "Reconciliation",
    "StagedFile",
    "SubmissionIntent",
    "SubmissionPlan",
    "acknowledge_collected_results",
    "adopt_retrieved_intent",
    "build_bundle_transfer_argv",
    "build_result_retrieval_argv",
    "bundle_for_shard",
    "collect_results",
    "gather_policy_evidence",
    "gather_policy_outputs",
    "import_retrieved_results",
    "initialize_shard_checkpoint",
    "job_paths",
    "jobs_root",
    "plan_submission",
    "prepare_job",
    "prepare_portable_job",
    "quarantine_orphan_artifacts",
    "read_bundle",
    "read_intent",
    "reconcile_job",
    "render_job_script",
    "resolve_job_name",
    "submission_lock",
    "submit_job",
    "verify_prepared_bundle",
]
