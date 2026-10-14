"""Packs agent/gman_agent into one file for hosts: python3 agent/build.py [output] (default agent/dist/gman-agent.pyz).

The result is a zipapp of the stdlib-only package: python3 gman-agent.pyz --runtime docker --source-id server-01 ...
"""
import sys
import tempfile
import zipapp
from pathlib import Path
from shutil import copytree, ignore_patterns

AGENT = Path(__file__).resolve().parent


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        copytree(AGENT / "gman_agent", Path(staging) / "gman_agent", ignore=ignore_patterns("__pycache__", "*.pyc"))
        # The archive root runs the package; modules stay importable as gman_agent.*.
        (Path(staging) / "__main__.py").write_text("from gman_agent.cli import run\n\nrun()\n")
        zipapp.create_archive(staging, output, interpreter="/usr/bin/env python3", compressed=True)
    return output


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else AGENT / "dist/gman-agent.pyz"))
