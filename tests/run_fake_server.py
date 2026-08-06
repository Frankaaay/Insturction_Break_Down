"""Local browser-QA server. It never calls an external LLM."""

import os
import sys
from pathlib import Path

os.environ["EXECUTION_DB_PATH"] = ""
os.environ["OPERATOR_TOKEN"] = ""
os.environ["GRASPARM_AGENT_TOKEN"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


def fake_decompose(instruction: str, provider: str, model: str | None = None) -> dict:
    return {
        "status": "ok",
        "steps": [
            {
                "action_id": "A_001",
                "action": "Pick",
                "logic": 0,
                "slots": {"obj_a": "瓶子"},
                "zh": "拿起瓶子",
                "en": "Pick up the bottle.",
            }
        ],
    }


server.decompose = fake_decompose


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(server.app, host="127.0.0.1", port=8765)
