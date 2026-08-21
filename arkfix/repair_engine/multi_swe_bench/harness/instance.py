from dataclasses import dataclass, replace
from typing import Tuple

from dataclasses_json import dataclass_json

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


class Instance:
    _registry = {}
    _fallback_registry = {}  # org -> class, for repos that don't need per-repo registration
    _name_aliases = {
        # Local repair repo folder names that should resolve to existing runnable handlers.
        "repair_repo/ImageKnife": "local_repo/ImageKnife",
        "repair_repo/applications_app_samples": "openharmony/applications_app_samples",
    }

    @property
    def pr(self) -> PullRequest:
        raise NotImplementedError

    @property
    def repo_name(self) -> str:
        return f"{self.pr.org}/{self.pr.repo}"

    @classmethod
    def register(cls, org: str, repo: str):
        def inner_wrapper(wrapped_class):
            name = f"{org}/{repo}"
            cls._registry[name] = wrapped_class
            return wrapped_class

        return inner_wrapper

    @classmethod
    def register_fallback(cls, org: str):
        """Register a fallback class for an entire org (used when exact org/repo is not found)."""
        def inner_wrapper(wrapped_class):
            cls._fallback_registry[org] = wrapped_class
            return wrapped_class
        return inner_wrapper

    @classmethod
    def register_alias(cls, source_org: str, source_repo: str, target_org: str, target_repo: str):
        """Register an alternate org/repo name that should resolve to an existing handler."""
        cls._name_aliases[f"{source_org}/{source_repo}"] = f"{target_org}/{target_repo}"

    @classmethod
    def create(cls, pr: PullRequest, config: Config, *args, **kwargs):
        name = f"{pr.org}/{pr.repo}"
        resolved_name = cls._name_aliases.get(name, name)
        resolved_pr = pr
        if resolved_name != name:
            resolved_org, resolved_repo = resolved_name.split("/", 1)
            resolved_pr = replace(pr, org=resolved_org, repo=resolved_repo)
        name = resolved_name
        if name in cls._registry:
            return cls._registry[name](resolved_pr, config, *args, **kwargs)
        # Fallback: try org-level match (e.g. all "split/*" ArkTS repos)
        resolved_org = name.split("/", 1)[0]
        if resolved_org in cls._fallback_registry:
            return cls._fallback_registry[resolved_org](resolved_pr, config, *args, **kwargs)
        raise ValueError(f"Instance '{name}' is not registered.")

    def dependency(self) -> "Image":
        raise NotImplementedError

    def name(self) -> str:
        return self.dependency().image_full_name()

    def run(self) -> str:
        raise NotImplementedError

    def test_patch_run(self) -> str:
        raise NotImplementedError

    def fix_patch_run(self) -> str:
        raise NotImplementedError

    def parse_log(self, test_log: str) -> TestResult:
        raise NotImplementedError

@dataclass
class Record:
    instance: Instance
    language: str
    data: dict