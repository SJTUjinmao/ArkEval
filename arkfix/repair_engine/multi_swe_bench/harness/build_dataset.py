import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from tqdm import tqdm
from simple_parsing.helpers.serialization.serializable import FrozenSerializable
from simple_parsing.helpers.fields import field

from multi_swe_bench.harness.constant import (
    BUILD_IMAGE_LOG_FILE,
    BUILD_IMAGE_WORKDIR,
    INITIAL_LOG_FILE,
)
from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import Base, PullRequest
from multi_swe_bench.harness.registry import register_data, data_registry
from multi_swe_bench.utils.docker_util import build, exists, run
from multi_swe_bench.utils.fs_utils import copy_source_code
from multi_swe_bench.utils.git_util import clone_repository, get_all_commit_hashes
from multi_swe_bench.utils.logger import get_non_propagate_logger, setup_logger
from multi_swe_bench.utils.thread_util import Result, SPMCThreadPool



def str_to_bool(value):
    if isinstance(value, bool):
        return value

    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("Boolean value expected.")

    if value.lower() in {"true", "yes", "1"}:
        return True
    elif value.lower() in {"false", "no", "0"}:
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")

@dataclass(frozen=True)
class CliArgs(FrozenSerializable):
    workdir: Path
    pr_file: Path
    need_clone: bool
    repo_dir: Optional[Path]
    max_workers_build_image: int
    max_workers_run_instance: int
    global_env: Optional[list[str]]
    clear_env: bool
    log_level: str = "INFO"
    print_to_console: bool = False
    print_to_console_build_image: bool = False
    print_to_console_run_instance: bool = False
    regenerate_reports_for_pr_file: bool = False
    regenerate_reports_for_workdir: bool = False


def init_logger(
    workdir: Path, log_level: str, print_to_console: bool
) -> logging.Logger:
    logger = setup_logger(workdir, INITIAL_LOG_FILE, log_level, print_to_console)
    logger.info("Initialize logger successfully.")
    return logger


def load_pull_requests(logger: logging.Logger) -> dict[PullRequest]:
    logger.info("Loading dataset...")
    prs: dict[PullRequest] = {}
    for instance_id, data in data_registry.items():
        pr: PullRequest = PullRequest.from_dict(data)
        if instance_id in prs:
            err_msg = f"Pull request {pr.id} already exists, please check the pull requests file."
            logger.error(err_msg)
            raise ValueError(err_msg)

        prs[instance_id] = pr

    logger.info(f"Dataset loaded successfully. Total items: {len(prs)}")

    return prs


def create_instances(
    prs: dict[PullRequest], config: Config, logger: logging.Logger
) -> dict[str, Instance]:
    logger.info("Creating instances...")

    instances: dict[str, Instance] = {}
    for instance_id, pr in prs.items():
        instance = Instance.create(pr, config)
        instances[instance_id] = instance

    logger.info(f"Instances created successfully. Total instances: {len(instances)}")

    return instances


def check_commit_hashes(
    repo_dir: Path, instances: list[Instance], logger: logging.Logger
):
    logger.info("Checking commit hashes...")

    error_happened = False
    repo_commits: dict[str, set[str]] = {}
    skip_repos = set()

    for instance in instances:
        repo_name = instance.repo_name
        if repo_name in skip_repos:
            continue

        if repo_name not in repo_commits:
            repo_path = repo_dir / repo_name
            if not repo_path.exists():
                org_dir = repo_dir / instance.pr.org
                clone_repository(org_dir, instance.pr.org, instance.pr.repo)

            commits = get_all_commit_hashes(repo_path, logger)
            if len(commits) == 0:
                logger.error(f"No commit hashes found in {repo_name}.")
                skip_repos.add(repo_name)
                error_happened = True
                continue

            repo_commits[repo_name] = commits

        if instance.pr.base.sha not in repo_commits[repo_name]:
            logger.error(
                f"The commit hash of pr-{instance.pr.number}({instance.pr.base.sha}) not found in {repo_name}."
            )
            error_happened = True

    if error_happened:
        raise FileNotFoundError(
            "Some commit hashes not found in the source code, please check the logs."
        )

    logger.info("Commit hashes checked successfully.")


def check_source_code(
    repo_dir: Path,
    image: Image,
    clone_if_missing: bool,
    logger: logging.Logger,
):
    source_code_path = repo_dir / image.pr.org / image.pr.repo
    if not source_code_path.exists():
        flat_path = repo_dir / image.pr.repo
        if not flat_path.exists():
            raise FileNotFoundError(
                f"Source code not found at {repo_dir}. Use --clone_if_missing to clone the repository or manually clone the repository."
            )
    
def build_image(
    image: Image,
    cli: CliArgs,
    logger: logging.Logger,
):
    if exists(image.image_full_name()):
        logger.info(f"Image {image.image_full_name()} already exists, skipping...")
        return

    if isinstance(image.dependency(), Image):
        build_image(image.dependency(), cli, logger)

    workdir = cli.workdir / image.pr.org / image.pr.repo / BUILD_IMAGE_WORKDIR
    image_dir = workdir / image.workdir()
    image_dir.mkdir(parents=True, exist_ok=True)

    if cli.repo_dir and image.need_copy_code:
        check_source_code(cli.repo_dir, image, True, logger)
        copy_source_code(cli.repo_dir, image, image_dir)

    dockerfile_path = image_dir / image.dockerfile_name()
    dockerfile_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dockerfile_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(image.dockerfile())

    for file in image.files():
        file_path = image_dir / file.dir / file.name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(file.content)

    logger.info(f"Building image {image.image_full_name()}...")
    build(
        image_dir,
        image.dockerfile_name(),
        image.image_full_name(),
        get_non_propagate_logger(
            image_dir,
            BUILD_IMAGE_LOG_FILE,
            cli.log_level,
            cli.print_to_console_build_image,
        ),
    )
    logger.info(f"Image {image.image_full_name()} built successfully.")


def build_images(instances: list[Instance], cli: CliArgs, logger: logging.Logger):
    logger.info("Start to build images...")

    images: dict[str, dict[str, Image]] = {}
    external_images: set[str] = set()
    for instance in instances:
        required_image = instance.dependency()

        while isinstance(required_image, Image):
            parent_image = required_image.dependency()

            if isinstance(parent_image, Image):
                parent_image_name = parent_image.image_full_name()
            else:
                parent_image_name = parent_image
                external_images.add(parent_image_name)

            if parent_image_name in images:
                images[parent_image_name][
                    required_image.image_full_name()
                ] = required_image
            else:
                images[parent_image_name] = {
                    required_image.image_full_name(): required_image
                }

            required_image = parent_image

    thread_pool = SPMCThreadPool(cli.max_workers_build_image, logger)
    thread_pool.start()

    next_images: dict[str, Image] = {}
    for external_name in external_images:
        for name, image in images[external_name].items():
            next_images[name] = image

    while next_images:
        for image in next_images.values():
            thread_pool.send(image.image_full_name(), build_image, image, cli, logger)

        err_happened = False
        results: dict[str, Result] = thread_pool.join()
        for name, result in results.items():
            if not result.success:
                err_happened = True
                logger.error(f"Failed to build image {name}: {result.error}")

        if err_happened:
            raise RuntimeError("Failed to build images, please check the logs.")

        new_next_images = {}
        for name, image in next_images.items():
            if name not in images:
                continue

            for new_name, new_image in images[name].items():
                new_next_images[new_name] = new_image
        next_images = new_next_images

    thread_pool.stop()
    logger.info("Images built successfully.")

def convert_to_dict(env: Optional[list[str]]) -> Optional[dict[str, str]]:
    if env is None:
        return None

    if len(env) == 0:
        return None

    result = {}
    for item in env:
        key_value = item.split("=")
        if len(key_value) == 2:
            key, value = key_value
            result[key] = value

    return result

def prepare_datas(file_path: str, cli: CliArgs, prebuild: bool) -> dict[str, Instance]:
    
    
    logger = init_logger(cli.workdir, cli.log_level, cli.print_to_console)

    register_data(file_path)

    prs = load_pull_requests(logger)

    instances = create_instances(
        prs,
        Config(
            need_clone=cli.need_clone,
            global_env=convert_to_dict(cli.global_env),
            clear_env=cli.clear_env,
        ),
        logger,
    )
    if prebuild:
        build_images(list(instances.values()), cli, logger)
    return instances
