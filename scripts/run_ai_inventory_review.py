from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.ai.agent import InventoryAIAgent  # noqa: E402
from app.ai.llm_client import LocalLLMClient, LocalLLMError  # noqa: E402
from app.database import SessionLocal, create_db_and_tables  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Executa revisao diaria de estoque com LM Studio.")
    parser.add_argument("--model", help="Modelo do LM Studio a usar nesta execucao.")
    parser.add_argument("--objective", help="Objetivo adicional para a revisao.")
    parser.add_argument("--list-models", action="store_true", help="Lista modelos antes de executar.")
    args = parser.parse_args()

    create_db_and_tables()
    try:
        client = LocalLLMClient()
    except LocalLLMError as exc:
        print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    if args.model:
        client.set_model(args.model)

    if args.list_models:
        try:
            print(json.dumps(client.list_models(), ensure_ascii=False, indent=2, default=str))
        except LocalLLMError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False, indent=2))

    db = SessionLocal()
    try:
        agent = InventoryAIAgent(db=db, llm_client=client)
        result = agent.run_daily_inventory_review(objective=args.objective)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") == "completed" else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
