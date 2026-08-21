"""OpenHarmony applications_app_samples (Gitee/GitHub mirror) — ArkTS sample monorepo.

Minimal docker image: Node + git clone + checkout base commit. No pnpm/ohpm at
image build (agent uses hvigor via mounted CLI per config). Test scripts are no-ops
so image build succeeds; evaluation uses defect_arkts.yaml compile commands inside container.
"""
from __future__ import annotations

import os
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AppSamplesImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "node:18-bullseye"

    def image_name(self) -> str:
        return f"{self.pr.org}/{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            git_base = os.environ.get("MSWE_GIT_CLONE_BASE", "https://github.com").rstrip("/")
            code = f"RUN git clone {git_base}/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV CI=1
ENV NO_COLOR=1

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
{code}

{self.clear_env}

"""


class AppSamplesImagePR(Image):
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
        return AppSamplesImageBase(self.pr, self.config)

    def image_name(self) -> str:
        return f"{self.pr.org}/{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        repo = pr.repo
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git fetch --unshallow 2>/dev/null || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Placeholder: agent runs hvigor via config (assembleHar). Exit 0 for image build.
exit 0
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
exit 0
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
exit 0
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        copy_commands = ""
        for f in self.files():
            copy_commands += f"COPY {f.name} /home/\n"
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN chmod +x /home/*.sh || true
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("openharmony", "applications_app_samples")
class ApplicationsAppSamples(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AppSamplesImagePR(self.pr, self._config)

    def run(self) -> str:
        return "bash /home/run.sh"

    def test_patch_run(self) -> str:
        return "bash /home/test-run.sh"

    def fix_patch_run(self) -> str:
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return TestResult(
            passed_count=1,
            failed_count=0,
            skipped_count=0,
            passed_tests={"placeholder"},
            failed_tests=set(),
            skipped_tests=set(),
        )
