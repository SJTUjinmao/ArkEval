import logging
from pathlib import Path
from typing import Optional

import docker

_docker_client: docker.DockerClient | None = None


def _client() -> docker.DockerClient:
    """Lazily connect so importing run.py does not require a running Docker daemon."""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def exists(image_name: str) -> bool:
    try:
        _client().images.get(image_name)
        return True
    except docker.errors.ImageNotFound:
        return False


def build(
    workdir: Path, dockerfile_name: str, image_full_name: str, logger: logging.Logger
):
    workdir = str(workdir)
    logger.info(
        f"Start building image `{image_full_name}`, working directory is `{workdir}`"
    )
    try:
        build_logs = _client().api.build(
            path=workdir,
            dockerfile=dockerfile_name,
            tag=image_full_name,
            rm=True,
            forcerm=True,
            decode=True,
            encoding="utf-8",
        )

        for log in build_logs:
            if "stream" in log:
                logger.info(log["stream"].strip())
            elif "error" in log:
                error_message = log["error"].strip()
                logger.error(f"Docker build error: {error_message}")
                raise RuntimeError(f"Docker build failed: {error_message}")
            elif "status" in log:
                logger.info(log["status"].strip())
            elif "aux" in log:
                logger.info(log["aux"].get("ID", "").strip())

        logger.info(f"image({workdir}) build success: {image_full_name}")
    except docker.errors.BuildError as e:
        logger.error(f"build error: {e}")
        raise e
    except Exception as e:
        logger.error(f"Unknown build error occurred: {e}")
        raise e


def run(
    image_full_name: str,
    run_command: str,
    output_path: Optional[Path] = None,
    global_env: Optional[list[str]] = None,
) -> str:
    container = _client().containers.run(
        image=image_full_name,
        command=run_command,
        remove=False,
        detach=True,
        stdout=True,
        stderr=True,
        environment=global_env,
    )

    output = ""
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            for line in container.logs(stream=True, follow=True):
                line_decoded = line.decode("utf-8")
                f.write(line_decoded)
                output += line_decoded
    else:
        container.wait()
        output = container.logs().decode("utf-8")

    container.remove()

    return output
