from __future__ import annotations

from dotenv import load_dotenv

from .cli import app


def main() -> None:
    load_dotenv()  # 从当前工作目录加载 .env（含 MODEL_SCOPE_ACCESS_TOKEN）
    app()


if __name__ == "__main__":
    main()
