from __future__ import annotations

import os
from dataclasses import dataclass

from app.ai.llm_client import LocalLLMClient, LocalLLMError
from app.ai.runtime_guard import current_ai_runtime


QUALITY_MODEL = os.getenv("AI_QUALITY_MODEL", "google/gemma-4-e4b")
WORKER_MODEL = os.getenv("AI_WORKER_MODEL", "nvidia/nemotron-3-nano-4b")
BALANCED_MODEL = os.getenv("AI_BALANCED_MODEL", "mistralai/ministral-3-3b")
AI_AUTO_LOAD_MODELS = os.getenv("AI_AUTO_LOAD_MODELS", "false").strip().lower() in {"1", "true", "yes", "on"}
_LOAD_ATTEMPTS: dict[str, dict[str, object]] = {}


@dataclass(frozen=True)
class ModelRole:
    role: str
    model: str
    description: str


MODEL_ROLES: dict[str, ModelRole] = {
    "quality": ModelRole(
        role="quality",
        model=QUALITY_MODEL,
        description="OCR, resumos e geracao que exige maior qualidade.",
    ),
    "worker": ModelRole(
        role="worker",
        model=WORKER_MODEL,
        description="Tarefas gerais rapidas, chamadas estruturadas e rotinas operacionais.",
    ),
    "balanced": ModelRole(
        role="balanced",
        model=BALANCED_MODEL,
        description="Modelo intermediario para testes, OCR leve e tarefas rapidas com alguma visao.",
    ),
}


TASK_MODEL_ROLE: dict[str, str] = {
    "ocr": "quality",
    "meeting_summary": "quality",
    "quality_generation": "quality",
    "daily_inventory_review": "worker",
    "structured_report": "worker",
    "agent_request": "worker",
    "scope_validation": "worker",
    "general": "worker",
    "balanced": "balanced",
}


def model_for_task(task: str, fallback_role: str = "worker") -> str:
    role_name = TASK_MODEL_ROLE.get(task, fallback_role)
    role = MODEL_ROLES.get(role_name) or MODEL_ROLES[fallback_role]
    return role.model


def role_for_task(task: str, fallback_role: str = "worker") -> ModelRole:
    role_name = TASK_MODEL_ROLE.get(task, fallback_role)
    return MODEL_ROLES.get(role_name) or MODEL_ROLES[fallback_role]


def model_policy_snapshot() -> dict[str, object]:
    return {
        "roles": {
            key: {
                "role": value.role,
                "model": value.model,
                "description": value.description,
            }
            for key, value in MODEL_ROLES.items()
        },
        "tasks": dict(TASK_MODEL_ROLE),
        "env_overrides": {
            "AI_QUALITY_MODEL": QUALITY_MODEL,
            "AI_WORKER_MODEL": WORKER_MODEL,
            "AI_BALANCED_MODEL": BALANCED_MODEL,
            "AI_AUTO_LOAD_MODELS": AI_AUTO_LOAD_MODELS,
        },
        "load_attempts": dict(_LOAD_ATTEMPTS),
    }


def client_for_task(task: str, fallback_role: str = "worker") -> LocalLLMClient:
    model = active_model_for_task(task, fallback_role=fallback_role)
    client = LocalLLMClient(model=model)
    ensure_expected_model_available(client, model)
    return client


def active_model_for_task(task: str, fallback_role: str = "worker") -> str:
    return model_for_task(task, fallback_role=fallback_role)


def available_model_ids(client: LocalLLMClient | None = None) -> list[str]:
    try:
        result = (client or LocalLLMClient()).list_models()
    except LocalLLMError:
        return []
    ids: list[str] = []
    for model in result.get("models") or []:
        model_id = str(model.get("id") or "").strip()
        if model_id:
            ids.append(model_id)
    return ids


def ensure_expected_model_available(client: LocalLLMClient, model_name: str) -> None:
    ids = available_model_ids(client)
    if not ids:
        raise LocalLLMError("Nenhum modelo foi listado pelo LM Studio. Carregue um modelo antes de chamar a IA.")
    if model_name not in ids:
        raise LocalLLMError(
            "Modelo incorreto para esta tarefa. "
            f"Esperado: {model_name}. Disponiveis no LM Studio: {', '.join(ids)}. "
            "Carregue exatamente o modelo esperado no LM Studio ou ajuste a variavel de ambiente da politica."
        )


def ensure_model_loaded(client: LocalLLMClient, model_name: str) -> dict[str, object]:
    if not model_name:
        return {"ok": False, "model": model_name, "message": "Modelo vazio."}
    payload = {
        "ok": False,
        "model": model_name,
        "message": (
            "Carga nativa de modelo desabilitada. O app apenas envia o nome do modelo "
            "na chamada OpenAI-compatible e nunca tenta carregar outro modelo no LM Studio."
        ),
        "reason": "native_load_disabled",
    }
    _LOAD_ATTEMPTS[model_name] = payload
    return payload


def load_role(role_name: str) -> dict[str, object]:
    role = MODEL_ROLES.get(role_name)
    if not role:
        return {"ok": False, "role": role_name, "message": "Papel de modelo desconhecido."}
    client = LocalLLMClient(model=role.model)
    return {"role": role_name, **ensure_model_loaded(client, role.model)}


def load_all_policy_models() -> dict[str, object]:
    return {
        "ok": False,
        "single_model_enforced": True,
        "message": "Carga manual removida da aplicacao operacional. Mantenha exatamente um modelo carregado no LM Studio.",
        "active_runtime": current_ai_runtime(),
    }
