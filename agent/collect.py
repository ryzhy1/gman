"""Inventory agent from the repository: python3 agent/collect.py --runtime docker --source-id server-01 ...

The code is in agent/gman_agent/ (see its cli module for all modes). Hosts run the single-file build of the same
package, gman-agent.pyz (agent/build.py), with the same arguments.
"""
from gman_agent.cli import run

if __name__ == "__main__":
    run()
