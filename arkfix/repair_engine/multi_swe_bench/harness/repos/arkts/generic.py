"""Generic ArkTS Instance for HarmonyOS / OpenHarmony repositories.

Registered for all known ArkTS repos in the dataset. The agent container
is a lightweight Node.js image with the repo cloned at the base commit.
Compilation/testing is done separately via run_arkts_hvigor_native.py.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


class ArkTSImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return "mswe-arkts/oh-base:latest"

    def image_name(self) -> str:
        return f"mswe-arkts/{self.pr.org}__{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        if isinstance(base, Image):
            base = base.image_full_name()

        git_base = os.environ.get("MSWE_GIT_CLONE_BASE", "https://github.com").rstrip("/")
        if self.config.need_clone:
            clone_cmd = f"RUN git clone {git_base}/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            clone_cmd = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {base}

{self.global_env}


WORKDIR /home/
{clone_cmd}

{self.clear_env}
"""


class ArkTSImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return ArkTSImageBase(self.pr, self.config)

    def image_name(self) -> str:
        return f"mswe-arkts/{self.pr.org}__{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"
        prepare_sh = f"""#!/bin/bash
set -e
cd {repo_dir}
if [ -d ".git" ]; then
    git reset --hard
    git checkout {self.pr.base.sha}
fi
echo "hwsdk.dir=/home/oh_sdk" > local.properties
echo "sdk.dir=/home/oh_sdk" >> local.properties
echo "nodejs.dir=/usr/local/bin/node" >> local.properties
"""
        run_sh = f"""#!/bin/bash
set -e
cd {repo_dir}
echo "ArkTS repo ready at {repo_dir}"
git log -1 --oneline
"""
        return [
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {dep.image_full_name()}

{self.global_env}

{copy_commands}
RUN chmod +x /home/*.sh || true
RUN bash /home/prepare.sh

{self.clear_env}
"""


class _ArkTSInstance(Instance):
    """Generic ArkTS instance — subclassed per org/repo via register_arkts_repo()."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ArkTSImageDefault(self.pr, self._config)

    def run(self) -> str:
        return "bash /home/run.sh"

    def test_patch_run(self) -> str:
        return "bash /home/run.sh"

    def fix_patch_run(self) -> str:
        return "bash /home/run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ArkTS eval is done externally via hvigor; treat any non-error as pass.
        failed = bool(re.search(r"\bERROR\b|\bFAILED\b|\bfatal\b", test_log, re.IGNORECASE))
        return TestResult(
            passed_count=0 if failed else 1,
            failed_count=1 if failed else 0,
            skipped_count=0,
            passed_tests=set() if failed else {"arkts"},
            failed_tests={"arkts"} if failed else set(),
            skipped_tests=set(),
        )


def _register(org: str, repo: str):
    """Dynamically create and register a named subclass for (org, repo)."""
    cls = type(f"ArkTS_{org}__{repo}", (_ArkTSInstance,), {})
    Instance.register(org, repo)(cls)
    return cls


# ── Register all known ArkTS repos ──────────────────────────────────────────
_register("bytedance", "rdbStore")
_register("asasugar", "HPRichText")
_register("yongoe1024", "RdbPlus")
_register("HomoArk", "Homogram")
_register("openharmony-tpc-incubate", "photo-deal-demo")
_register("openharmony-tpc-incubate", "ohos_ijkplayer")
_register("openharmony-tpc-incubate", "md360player")

# Fallback: any unknown repo under these orgs auto-uses _ArkTSInstance
Instance.register_fallback("split")(_ArkTSInstance)
Instance.register_fallback("openharmony")(_ArkTSInstance)
Instance.register_fallback("openharmony-tpc")(_ArkTSInstance)
Instance.register_fallback("local_repo")(_ArkTSInstance)

