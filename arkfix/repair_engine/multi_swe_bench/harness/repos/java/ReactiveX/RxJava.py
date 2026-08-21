import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RxJavaImageBase(Image):
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
        return "ubuntu:22.04"

    def image_name(self) -> str:
        return f"{self.pr.org}/{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "config_gradle.sh",
                """#!/bin/bash
set -e

echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc
source ~/.bashrc

PROXY_SETTINGS=""

GRADLE_PROPERTIES="$HOME/.gradle/gradle.properties"

if [ ! -d "$HOME/.gradle" ]; then
    mkdir -p "$HOME/.gradle"
fi

if [ ! -f "$GRADLE_PROPERTIES" ]; then
    touch "$GRADLE_PROPERTIES"
fi

if ! grep -q "systemProp.http.proxyHost" "$GRADLE_PROPERTIES"; then
    echo "$PROXY_SETTINGS" >> "$GRADLE_PROPERTIES"
    echo "Added proxy settings to $GRADLE_PROPERTIES"
fi

""",
            )
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-17-jdk
{code}

{copy_commands}

RUN bash /home/config_gradle.sh

{self.clear_env}

"""


class RxJavaImageBaseJDK11(Image):
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
        return "ubuntu:22.04"

    def image_name(self) -> str:
        return f"{self.pr.org}/{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return "base-jdk-11"

    def workdir(self) -> str:
        return "base-jdk-11"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "config_gradle.sh",
                """#!/bin/bash
set -e

echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc
source ~/.bashrc

PROXY_SETTINGS=""

GRADLE_PROPERTIES="$HOME/.gradle/gradle.properties"

if [ ! -d "$HOME/.gradle" ]; then
    mkdir -p "$HOME/.gradle"
fi

if [ ! -f "$GRADLE_PROPERTIES" ]; then
    touch "$GRADLE_PROPERTIES"
fi

if ! grep -q "systemProp.http.proxyHost" "$GRADLE_PROPERTIES"; then
    echo "$PROXY_SETTINGS" >> "$GRADLE_PROPERTIES"
    echo "Added proxy settings to $GRADLE_PROPERTIES"
fi

""",
            )
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-11-jdk
{code}

{copy_commands}

RUN bash /home/config_gradle.sh

{self.clear_env}

"""


class RxJavaImageBaseJDK8(Image):
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
        return "ubuntu:20.04"

    def image_name(self) -> str:
        return f"{self.pr.org}/{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return "base-jdk-8"

    def workdir(self) -> str:
        return "base-jdk-8"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "config_gradle.sh",
                """#!/bin/bash
set -e

echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc
source ~/.bashrc

PROXY_SETTINGS=""

GRADLE_PROPERTIES="$HOME/.gradle/gradle.properties"

if [ ! -d "$HOME/.gradle" ]; then
    mkdir -p "$HOME/.gradle"
fi

if [ ! -f "$GRADLE_PROPERTIES" ]; then
    touch "$GRADLE_PROPERTIES"
fi

if ! grep -q "systemProp.http.proxyHost" "$GRADLE_PROPERTIES"; then
    echo "$PROXY_SETTINGS" >> "$GRADLE_PROPERTIES"
    echo "Added proxy settings to $GRADLE_PROPERTIES"
fi

""",
            )
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-8-jdk
{code}

{copy_commands}

RUN bash /home/config_gradle.sh

{self.clear_env}

"""


class RxJavaImageDefault(Image):
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
        if 7205 < self.pr.number <= 7298:
            return RxJavaImageBaseJDK11(self.pr, self._config)
        elif self.pr.number <= 7205:
            return RxJavaImageBaseJDK8(self.pr, self._config)

        return RxJavaImageBase(self.pr, self._config)

    def image_name(self) -> str:
        return f"{self.pr.org}/{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        if self.pr.number <= 7205:
            return [
                File(
                    ".",
                    "fix.patch",
                    f"{self.pr.fix_patch}",
                ),
                File(
                    ".",
                    "test.patch",
                    f"{self.pr.test_patch}",
                ),
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

""".format(
                        pr=self.pr
                    ),
                ),
                File(
                    ".",
                    "prepare.sh",
                    """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
sed -i '/repositories {{/a \    maven \{{ url "https://oss.jfrog.org/artifactory/oss-release-local/" \}}' build.gradle
sed -i '/repositories {{/a \    maven \{{ url "https://groovy.jfrog.io/artifactory/libs-release/" \}}' build.gradle
./gradlew clean test --max-workers 2 --continue || true
""".format(
                        pr=self.pr
                    ),
                ),
                File(
                    ".",
                    "run.sh",
                    """#!/bin/bash
set -e

cd /home/{pr.repo}
./gradlew clean test --max-workers 2 --continue
""".format(
                        pr=self.pr
                    ),
                ),
                File(
                    ".",
                    "test-run.sh",
                    """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew clean test --max-workers 2 --continue

""".format(
                        pr=self.pr
                    ),
                ),
                File(
                    ".",
                    "fix-run.sh",
                    """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew clean test --max-workers 2 --continue

""".format(
                        pr=self.pr
                    ),
                ),
            ]
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(
                    pr=self.pr
                ),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
./gradlew clean test --max-workers 2 --continue || true
""".format(
                    pr=self.pr
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
./gradlew clean test --max-workers 2 --continue
""".format(
                    pr=self.pr
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew clean test --max-workers 2 --continue

""".format(
                    pr=self.pr
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew clean test --max-workers 2 --continue

""".format(
                    pr=self.pr
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}
"""


@Instance.register("ReactiveX", "RxJava")
class RxJava(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RxJavaImageDefault(self.pr, self._config)

    def run(self) -> str:
        return "bash /home/run.sh"

    def test_patch_run(self) -> str:
        return "bash /home/test-run.sh"

    def fix_patch_run(self) -> str:
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
