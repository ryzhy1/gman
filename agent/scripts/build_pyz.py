"""Сборка агента одним файлом для хостов: python3 agent/scripts/build_pyz.py [выход] (по умолчанию agent/dist/gman-agent.pyz).

Результат — zipapp пакетов агента (только stdlib): python3 gman-agent.pyz --runtime docker --source-id server-01 ...
"""
import sys
import tempfile
import zipapp
from pathlib import Path
from shutil import copy2, copytree, ignore_patterns

AGENT = Path(__file__).resolve().parent.parent
SOURCE = AGENT / "src"
PACKAGES = ("config", "runtime_access", "collection", "watching", "delivery")
ENTRYPOINT = "collect.py"
MAIN = "from collect import run\n\nrun()\n"
INTERPRETER = "/usr/bin/env python3"
DEFAULT_OUTPUT = AGENT / "dist/gman-agent.pyz"


def build(output: Path) -> Path:
    """Собирает zipapp: пакеты агента и точка входа; __main__ запускает CLI."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging)
        for package in PACKAGES:
            copytree(SOURCE / package, root / package, ignore=ignore_patterns("__pycache__", "*.pyc"))
        copy2(SOURCE / ENTRYPOINT, root / ENTRYPOINT)
        (root / "__main__.py").write_text(MAIN)
        zipapp.create_archive(root, output, interpreter=INTERPRETER, compressed=True)
    return output


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    print(build(target))
