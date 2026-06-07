from __future__ import annotations

import json
from typing import Any, Iterator

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.llm_client import LocalLLMClient, LocalLLMError, MODEL_STATE
from app.ai.prompts import DEFAULT_OBJECTIVE, SYSTEM_PROMPT
from app.ai.schemas import AIReportScopeValidation, DailyInventoryReviewReport
from app.ai.tools import TOOL_DEFINITIONS, create_ai_report_tool, register_ai_log_tool, run_tool


class InventoryAIAgent:
    def __init__(
        self,
        db: Session,
        llm_client: LocalLLMClient | None = None,
        max_steps: int = 8,
        max_repeated_calls: int = 2,
    ) -> None:
        self.db = db
        self.llm_client = llm_client or LocalLLMClient()
        self.max_steps = max_steps
        self.max_repeated_calls = max_repeated_calls

    def run_daily_inventory_review(self, objective: str | None = None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": objective or DEFAULT_OBJECTIVE},
        ]
        steps: list[dict[str, Any]] = []
        repeated_calls: dict[str, int] = {}
        final_report_correction_requested = False
        ai_validation_correction_requested = False
        original_request = objective or DEFAULT_OBJECTIVE

        register_ai_log_tool(
            "Inicio da revisao diaria de estoque.",
            {"model": MODEL_STATE.selected_model, "objective": objective or DEFAULT_OBJECTIVE},
        )

        for step_number in range(1, self.max_steps + 1):
            try:
                response = self.llm_client.chat_with_tools(messages, tools=TOOL_DEFINITIONS)
                message = response.choices[0].message
            except LocalLLMError as exc:
                error = {"status": "error", "message": str(exc), "steps": steps}
                register_ai_log_tool("Falha ao chamar modelo local.", error)
                return error
            except (IndexError, AttributeError) as exc:
                error = {
                    "status": "error",
                    "message": f"Resposta invalida do modelo: {type(exc).__name__}: {exc}",
                    "steps": steps,
                }
                register_ai_log_tool("Resposta invalida do modelo local.", error)
                return error

            tool_calls = list(message.tool_calls or [])
            assistant_message = message.model_dump(exclude_none=True)
            messages.append(assistant_message)

            if tool_calls:
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = _parse_tool_args(tool_call.function.arguments)
                    call_key = json.dumps(
                        {"tool": tool_name, "args": tool_args},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    repeated_calls[call_key] = repeated_calls.get(call_key, 0) + 1

                    if repeated_calls[call_key] > self.max_repeated_calls:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Chamada de tool repetida muitas vezes. Escolha outra acao ou finalize.",
                        }
                    elif isinstance(tool_args, dict):
                        result = run_tool(self.db, tool_name, tool_args)
                    else:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Argumentos da tool nao sao um objeto JSON valido.",
                            "raw_arguments": tool_call.function.arguments,
                        }

                    steps.append(
                        {
                            "step": step_number,
                            "action_type": "tool_call",
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "tool_result": _summarize_for_trace(result),
                        }
                    )
                    register_ai_log_tool(
                        "Tool executada pela IA.",
                        {
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "result_summary": _summarize_for_trace(result),
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                continue

            final_content = message.content or ""
            validation = _validate_final_report(final_content)
            if not validation["ok"]:
                steps.append(
                    {
                        "step": step_number,
                        "action_type": "invalid_final_report",
                        "message": validation["error"],
                        "raw_message": final_content,
                    }
                )
                register_ai_log_tool(
                    "Relatorio final invalido retornado pela IA.",
                    {"error": validation["error"], "raw_message": final_content[:2000]},
                )
                if not final_report_correction_requested:
                    final_report_correction_requested = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "O JSON final anterior nao validou no schema DailyInventoryReviewReport. "
                                "Corrija uma unica vez. Responda apenas com JSON valido, sem markdown, "
                                "sem texto extra, mantendo todas as categorias obrigatorias, mesmo vazias. "
                                f"Erro de validacao: {validation['error']}"
                            ),
                        }
                    )
                    continue

                error = {
                    "status": "aborted",
                    "message": "Relatorio final invalido apos uma tentativa de correcao.",
                    "validation_error": validation["error"],
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por relatorio final invalido.", error)
                return error

            final_report = validation["report"]
            ai_validation = _validate_report_against_request_with_ai(
                self.llm_client,
                original_request=original_request,
                final_report=final_report,
            )
            steps.append(
                {
                    "step": step_number,
                    "action_type": "ai_report_validation",
                    "validation": ai_validation,
                }
            )
            register_ai_log_tool("Validacao por IA executada.", ai_validation)
            if not ai_validation.get("ok"):
                error = {
                    "status": "aborted",
                    "message": "Nao foi possivel validar o relatorio final por IA.",
                    "validation_error": ai_validation.get("error"),
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por falha na validacao por IA.", error)
                return error

            scope_validation = ai_validation["validation"]
            if not scope_validation["passed"]:
                if not ai_validation_correction_requested:
                    ai_validation_correction_requested = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "A validacao por IA reprovou o relatorio final em relacao ao pedido original. "
                                "Corrija uma unica vez. Responda apenas com JSON valido no schema "
                                "DailyInventoryReviewReport, sem markdown e sem texto extra. "
                                f"Motivo: {scope_validation['reason']} "
                                f"Violacoes: {json.dumps(scope_validation['violations'], ensure_ascii=False)} "
                                f"Instrucao de correcao: {scope_validation['correction_instruction']}"
                            ),
                        }
                    )
                    continue

                saved_report = create_ai_report_tool("[REPROVADO] Revisao diaria de estoque", final_report)
                error = {
                    "status": "aborted",
                    "message": "Relatorio final reprovado pela validacao por IA apos uma correcao.",
                    "scope_validation": scope_validation,
                    "report": saved_report,
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por reprovacao na validacao por IA.", error)
                return error

            saved_report = create_ai_report_tool("Revisao diaria de estoque", final_report)
            steps.append(
                {
                    "step": step_number,
                    "action_type": "final_message",
                    "message": final_content,
                }
            )
            register_ai_log_tool(
                "Relatorio final gerado pela IA.",
                {"report": saved_report, "summary": final_report.get("executive_summary")},
            )
            return {
                "status": "completed",
                "message": "Revisao concluida.",
                "model": MODEL_STATE.selected_model,
                "ai_validation": scope_validation,
                "final_report": final_report,
                "report": saved_report,
                "steps": steps,
            }

        register_ai_log_tool("Revisao abortada por limite de passos.", {"steps": steps})
        return {
            "status": "aborted",
            "message": f"Limite de {self.max_steps} passos atingido sem relatorio final.",
            "steps": steps,
        }

    def stream_daily_inventory_review(self, objective: str | None = None) -> Iterator[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": objective or DEFAULT_OBJECTIVE},
        ]
        steps: list[dict[str, Any]] = []
        repeated_calls: dict[str, int] = {}
        final_report_correction_requested = False
        ai_validation_correction_requested = False
        original_request = objective or DEFAULT_OBJECTIVE

        register_ai_log_tool(
            "Inicio da revisao diaria de estoque.",
            {"model": MODEL_STATE.selected_model, "objective": objective or DEFAULT_OBJECTIVE},
        )
        yield {
            "event": "run_started",
            "type": "system",
            "message": "Revisao diaria iniciada.",
            "model": MODEL_STATE.selected_model,
            "objective": objective or DEFAULT_OBJECTIVE,
        }

        for step_number in range(1, self.max_steps + 1):
            yield {
                "event": "model_call_start",
                "type": "model",
                "step": step_number,
                "message": "Chamando modelo local com tools disponiveis.",
            }
            try:
                response = self.llm_client.chat_with_tools(messages, tools=TOOL_DEFINITIONS)
                message = response.choices[0].message
            except LocalLLMError as exc:
                error = {"status": "error", "message": str(exc), "steps": steps}
                register_ai_log_tool("Falha ao chamar modelo local.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return
            except (IndexError, AttributeError) as exc:
                error = {
                    "status": "error",
                    "message": f"Resposta invalida do modelo: {type(exc).__name__}: {exc}",
                    "steps": steps,
                }
                register_ai_log_tool("Resposta invalida do modelo local.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            tool_calls = list(message.tool_calls or [])
            assistant_message = message.model_dump(exclude_none=True)
            messages.append(assistant_message)
            yield {
                "event": "model_message",
                "type": "response" if not tool_calls else "model",
                "step": step_number,
                "message": _visible_message_summary(message.content, tool_calls),
                "tool_call_count": len(tool_calls),
            }

            if tool_calls:
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = _parse_tool_args(tool_call.function.arguments)
                    yield {
                        "event": "tool_call",
                        "type": "tool",
                        "step": step_number,
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "message": f"Solicitando tool {tool_name}.",
                    }
                    call_key = json.dumps(
                        {"tool": tool_name, "args": tool_args},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    repeated_calls[call_key] = repeated_calls.get(call_key, 0) + 1

                    if repeated_calls[call_key] > self.max_repeated_calls:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Chamada de tool repetida muitas vezes. Escolha outra acao ou finalize.",
                        }
                    elif isinstance(tool_args, dict):
                        result = run_tool(self.db, tool_name, tool_args)
                    else:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Argumentos da tool nao sao um objeto JSON valido.",
                            "raw_arguments": tool_call.function.arguments,
                        }

                    step = {
                        "step": step_number,
                        "action_type": "tool_call",
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "tool_result": _summarize_for_trace(result),
                    }
                    steps.append(step)
                    register_ai_log_tool(
                        "Tool executada pela IA.",
                        {
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "result_summary": _summarize_for_trace(result),
                        },
                    )
                    yield {
                        "event": "tool_result",
                        "type": "tool",
                        "step": step_number,
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_name,
                        "ok": not (isinstance(result, dict) and result.get("ok") is False),
                        "result": _summarize_for_trace(result),
                    }
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                continue

            final_content = message.content or ""
            validation = _validate_final_report(final_content)
            if not validation["ok"]:
                invalid_step = {
                    "step": step_number,
                    "action_type": "invalid_final_report",
                    "message": validation["error"],
                    "raw_message": final_content,
                }
                steps.append(invalid_step)
                register_ai_log_tool(
                    "Relatorio final invalido retornado pela IA.",
                    {"error": validation["error"], "raw_message": final_content[:2000]},
                )
                yield {
                    "event": "validation_error",
                    "type": "validation",
                    "step": step_number,
                    "message": validation["error"],
                    "raw_message": final_content,
                }
                if not final_report_correction_requested:
                    final_report_correction_requested = True
                    correction_message = (
                        "O JSON final anterior nao validou no schema DailyInventoryReviewReport. "
                        "Corrija uma unica vez. Responda apenas com JSON valido, sem markdown, "
                        "sem texto extra, mantendo todas as categorias obrigatorias, mesmo vazias. "
                        f"Erro de validacao: {validation['error']}"
                    )
                    messages.append({"role": "user", "content": correction_message})
                    yield {
                        "event": "correction_requested",
                        "type": "validation",
                        "step": step_number,
                        "message": "Solicitada uma correcao do relatorio final.",
                    }
                    continue

                error = {
                    "status": "aborted",
                    "message": "Relatorio final invalido apos uma tentativa de correcao.",
                    "validation_error": validation["error"],
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por relatorio final invalido.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            final_report = validation["report"]
            yield {
                "event": "ai_validation_start",
                "type": "validation",
                "step": step_number,
                "message": "Validando relatorio final contra o pedido original.",
            }
            ai_validation = _validate_report_against_request_with_ai(
                self.llm_client,
                original_request=original_request,
                final_report=final_report,
            )
            steps.append(
                {
                    "step": step_number,
                    "action_type": "ai_report_validation",
                    "validation": ai_validation,
                }
            )
            register_ai_log_tool("Validacao por IA executada.", ai_validation)
            yield {
                "event": "ai_validation_result",
                "type": "validation",
                "step": step_number,
                "validation": ai_validation,
                "message": _validation_message(ai_validation),
            }
            if not ai_validation.get("ok"):
                error = {
                    "status": "aborted",
                    "message": "Nao foi possivel validar o relatorio final por IA.",
                    "validation_error": ai_validation.get("error"),
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por falha na validacao por IA.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            scope_validation = ai_validation["validation"]
            if not scope_validation["passed"]:
                if not ai_validation_correction_requested:
                    ai_validation_correction_requested = True
                    correction_message = (
                        "A validacao por IA reprovou o relatorio final em relacao ao pedido original. "
                        "Corrija uma unica vez. Responda apenas com JSON valido no schema "
                        "DailyInventoryReviewReport, sem markdown e sem texto extra. "
                        f"Motivo: {scope_validation['reason']} "
                        f"Violacoes: {json.dumps(scope_validation['violations'], ensure_ascii=False)} "
                        f"Instrucao de correcao: {scope_validation['correction_instruction']}"
                    )
                    messages.append({"role": "user", "content": correction_message})
                    yield {
                        "event": "ai_scope_correction_requested",
                        "type": "validation",
                        "step": step_number,
                        "message": scope_validation["correction_instruction"],
                        "validation": scope_validation,
                    }
                    continue

                saved_report = create_ai_report_tool("[REPROVADO] Revisao diaria de estoque", final_report)
                error = {
                    "status": "aborted",
                    "message": "Relatorio final reprovado pela validacao por IA apos uma correcao.",
                    "scope_validation": scope_validation,
                    "report": saved_report,
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por reprovacao na validacao por IA.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            saved_report = create_ai_report_tool("Revisao diaria de estoque", final_report)
            steps.append(
                {
                    "step": step_number,
                    "action_type": "final_message",
                    "message": final_content,
                }
            )
            register_ai_log_tool(
                "Relatorio final gerado pela IA.",
                {"report": saved_report, "summary": final_report.get("executive_summary")},
            )
            result = {
                "status": "completed",
                "message": "Revisao concluida.",
                "model": MODEL_STATE.selected_model,
                "ai_validation": scope_validation,
                "final_report": final_report,
                "report": saved_report,
                "steps": steps,
            }
            yield {
                "event": "final_report",
                "type": "response",
                "step": step_number,
                "message": "Relatorio final validado.",
                "final_report": final_report,
                "report": saved_report,
            }
            yield {"event": "run_completed", "type": "system", "step": step_number, "result": result}
            return

        result = {
            "status": "aborted",
            "message": f"Limite de {self.max_steps} passos atingido sem relatorio final.",
            "steps": steps,
        }
        register_ai_log_tool("Revisao abortada por limite de passos.", {"steps": steps})
        yield {"event": "run_error", "type": "error", "result": result}


def _parse_tool_args(raw_arguments: str | None) -> dict[str, Any] | str:
    if not raw_arguments:
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return raw_arguments
    return parsed if isinstance(parsed, dict) else raw_arguments


def _validate_final_report(content: str) -> dict[str, Any]:
    if not content.strip():
        return {"ok": False, "error": "Resposta final vazia."}

    try:
        parsed = json.loads(_extract_json(content))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"JSON invalido: {exc}"}

    if isinstance(parsed, dict) and isinstance(parsed.get("final_report"), dict):
        parsed = parsed["final_report"]

    if not isinstance(parsed, dict):
        return {"ok": False, "error": "Relatorio final precisa ser um objeto JSON."}

    try:
        report = DailyInventoryReviewReport.model_validate(parsed)
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "report": report.model_dump(mode="json")}


def _validate_report_against_request_with_ai(
    llm_client: LocalLLMClient,
    *,
    original_request: str,
    final_report: dict[str, Any],
) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "Voce e um validador de QA. Compare o pedido original do usuario com o relatorio entregue. "
                "Avalie apenas se o relatorio respeita o escopo solicitado e se nao deixou de cobrir pontos "
                "explicitamente pedidos. Nao invente requisitos novos. Responda apenas JSON valido com os campos: "
                "passed boolean, reason string, violations array de strings, correction_instruction string."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "original_request": original_request,
                    "final_report": final_report,
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    try:
        raw_response = llm_client.chat(messages)
    except LocalLLMError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        parsed = json.loads(_extract_json(raw_response))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"Resposta invalida do validador de IA: {exc}",
            "raw_response": raw_response,
        }

    try:
        validation = AIReportScopeValidation.model_validate(parsed)
    except ValidationError as exc:
        return {
            "ok": False,
            "error": f"Schema invalido na validacao por IA: {exc}",
            "raw_response": raw_response,
        }

    return {"ok": True, "validation": validation.model_dump(mode="json")}


def _validation_message(ai_validation: dict[str, Any]) -> str:
    if not ai_validation.get("ok"):
        return f"Falha na validacao por IA: {ai_validation.get('error')}"
    validation = ai_validation["validation"]
    status = "aprovado" if validation["passed"] else "reprovado"
    return f"Relatorio {status} pela validacao por IA: {validation['reason']}"


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def _summarize_for_trace(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= 2000:
        return value
    return {"truncated": True, "preview": text[:2000]}


def _visible_message_summary(content: str | None, tool_calls: list[Any]) -> str:
    if tool_calls:
        names = [tool_call.function.name for tool_call in tool_calls]
        return "Modelo solicitou tools: " + ", ".join(names)
    if content:
        compact = " ".join(content.split())
        return compact[:240] + ("..." if len(compact) > 240 else "")
    return "Modelo respondeu sem texto visivel."
