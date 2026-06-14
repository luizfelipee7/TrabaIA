from __future__ import annotations

import json
import unicodedata
from typing import Any, Iterator

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.llm_client import LocalLLMClient, LocalLLMError
from app.ai.schemas import OperationalAnswer
from app.ai.tools import OPERATIONAL_TOOL_DEFINITIONS, run_operational_tool


SYSTEM_PROMPT = """
Voce e uma IA operacional de entrega, nao um chat.

Regras:
- Entregue o servico pedido pelo usuario de forma objetiva.
- Para qualquer pergunta sobre estoque, produtos, SKU, fornecedores, alertas, contas, documentos ou banco de dados, use uma tool.
- Nunca diga que consultou o banco se nenhuma tool foi usada.
- Nao invente itens, quantidades, fornecedores ou alertas.
- Nao exponha chain-of-thought. Mostre apenas a entrega final e eventos observaveis via tools.
- A resposta final deve ser JSON no schema OperationalAnswer.
- Nao coloque linhas de tabela na resposta final; o sistema renderiza a tabela diretamente do resultado da tool.

Ferramentas operacionais disponiveis:
- fuzzy_search_inventory_tool(query, limit): PRINCIPAL tool de busca por nome. Use para qualquer pedido envolvendo nome de produto, letra inicial, palavra-chave ou trecho. Exemplos de uso:
  * "itens que comecem com A" -> query="a", limit=200
  * "quantos algodoes tem" -> query="algodao", limit=200
  * "seringa descartavel" -> query="seringa descartavel", limit=50
  A tool tenta multiplas estrategias internamente e SEMPRE retorna o melhor resultado disponivel.
- search_inventory_items_tool: use apenas para filtros especificos (baixo estoque, validade, categoria exata, SKU).
- get_inventory_item_tool: detalhe de produto por ID ou SKU.
- list_suppliers_tool: fornecedores e contatos incompletos.
- list_stock_alerts_tool: alertas persistidos.
- list_saved_documents_tool: contas, documentos, boletos, notas e anexos salvos; use para perguntas sobre pagamento/vencimento/comprovante aproximado.
- run_stock_check_tool: checagem deterministica de estoque.
""".strip()


class OperationalAIAgent:
    def __init__(self, db: Session, llm_client: LocalLLMClient) -> None:
        self.db = db
        self.llm_client = llm_client

    def run(self, prompt: str) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        final_result: dict[str, Any] | None = None
        for event in self.stream(prompt):
            trace_event = dict(event)
            if isinstance(trace_event.get("result"), dict):
                trace_event["result"] = {
                    "status": trace_event["result"].get("status"),
                    "title": trace_event["result"].get("title"),
                    "summary": trace_event["result"].get("summary"),
                }
            trace.append(trace_event)
            if event.get("event") in {"run_completed", "run_error", "run_busy"}:
                final_result = event.get("result")
        if final_result is None:
            final_result = _error_result("Execucao encerrada sem entrega final.", trace=trace)
        final_result.setdefault("trace", trace)
        return final_result

    def stream(self, prompt: str) -> Iterator[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        llm_calls = 0
        tool_results: list[dict[str, Any]] = []
        seen_tool_calls: set[str] = set()

        yield {
            "event": "request_received",
            "type": "system",
            "message": "Pedido recebido pelo agente operacional.",
            "prompt": prompt,
            "model": self.llm_client.model,
        }

        step = 0
        tool_required_retry_used = False
        while True:
            step += 1
            response = self._call_model_for_tools(messages)
            llm_calls += 1
            if response.get("error"):
                result = _error_result(response["error"], llm_calls=llm_calls)
                yield {"event": "run_error", "type": "error", "message": result["summary"], "result": result}
                return

            message = response["message"]
            tool_calls = list(message.tool_calls or [])
            yield {
                "event": "model_message",
                "type": "model",
                "step": step,
                "message": _visible_model_message(message.content, tool_calls),
                "tool_call_count": len(tool_calls),
                "usage": response.get("usage"),
            }

            if not tool_calls:
                if message.content:
                    messages.append({"role": "assistant", "content": message.content})
                if _requires_data_tool(prompt) and not tool_results and not tool_required_retry_used:
                    tool_required_retry_used = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "O pedido exige dados operacionais. Use uma ou mais tools antes de responder. "
                                "Para pedidos como 'itens que comecam com A', use fuzzy_search_inventory_tool com query='a'."
                            ),
                        }
                    )
                    yield {
                        "event": "tool_required",
                        "type": "system",
                        "step": step,
                        "message": "Pedido exige banco; solicitando nova decisao de tool ao modelo.",
                    }
                    continue
                break

            messages.append(message.model_dump(exclude_none=True))
            repeated_only = True
            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                tool_args = _parse_tool_args(tool_call.function.arguments)
                call_key = json.dumps({"tool": tool_name, "args": tool_args}, ensure_ascii=False, sort_keys=True)
                if call_key in seen_tool_calls:
                    result = {
                        "ok": False,
                        "tool_name": tool_name,
                        "error": "Chamada de tool repetida sem progresso.",
                    }
                elif isinstance(tool_args, dict):
                    seen_tool_calls.add(call_key)
                    repeated_only = False
                    result = run_operational_tool(self.db, tool_name, tool_args)
                else:
                    repeated_only = False
                    result = {
                        "ok": False,
                        "tool_name": tool_name,
                        "error": "Argumentos da tool nao sao JSON valido.",
                        "raw_arguments": tool_call.function.arguments,
                    }

                tool_results.append(result)
                yield {
                    "event": "tool_call",
                    "type": "tool",
                    "step": step,
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "message": f"Executando {tool_name}.",
                }
                yield {
                    "event": "tool_result",
                    "type": "tool",
                    "step": step,
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_name,
                    "ok": result.get("ok") is not False,
                    "result": _compact_for_trace(result),
                    "message": _tool_result_message(result),
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(_compact_for_model(result), ensure_ascii=False, default=str),
                    }
                )

            if repeated_only:
                yield {
                    "event": "no_progress_guard",
                    "type": "warning",
                    "step": step,
                    "message": "O modelo repetiu apenas chamadas ja executadas; seguindo para a entrega com os dados disponiveis.",
                }
                break

        if _requires_data_tool(prompt) and not tool_results:
            result = _error_result(
                "A IA nao usou uma ferramenta obrigatoria para consultar dados operacionais.",
                llm_calls=llm_calls,
            )
            yield {"event": "run_error", "type": "error", "message": result["summary"], "result": result}
            return

        yield {
            "event": "finalization_start",
            "type": "response",
            "message": "Validando entrega final curta e renderizando dados reais das tools.",
        }

        final = self._request_final_answer(prompt, messages)
        llm_calls += final.pop("_llm_calls", 0)
        if final.get("status") == "error":
            final["metadata"] = {
                **final.get("metadata", {}),
                "llm_calls": llm_calls,
                "tool_call_count": len(tool_results),
                "model": self.llm_client.model,
            }
            yield {"event": "run_error", "type": "error", "message": final.get("summary"), "result": final}
            return

        final = _attach_tool_visualization(final, tool_results)
        final["metadata"] = {
            **final.get("metadata", {}),
            "llm_calls": llm_calls,
            "tool_call_count": len(tool_results),
            "model": self.llm_client.model,
        }
        yield {
            "event": "run_completed",
            "type": "response",
            "message": "Entrega operacional concluida.",
            "result": final,
        }

    def _call_model_for_tools(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            response = self.llm_client.chat_completion(
                messages,
                tools=OPERATIONAL_TOOL_DEFINITIONS,
                tool_choice="auto",
            )
            return {"message": response.choices[0].message, "usage": _usage_from_response(response)}
        except LocalLLMError as exc:
            return {"error": str(exc)}
        except (IndexError, AttributeError) as exc:
            return {"error": f"Resposta invalida do modelo: {type(exc).__name__}: {exc}"}

    def _request_final_answer(self, prompt: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        final_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    "Agora gere a entrega final curta. Responda apenas JSON valido no schema OperationalAnswer. "
                    "Nao inclua linhas da tabela nem copie todos os registros; o sistema renderiza os dados da tool. "
                    "Use visualization_type='table' quando a tool retornou linhas tabulares, 'list' para lista curta, "
                    "ou 'answer' para resposta textual. "
                    f"Pedido original: {prompt}"
                ),
            },
        ]
        structured_error: str | None = None
        for attempt in range(2):
            try:
                response = self.llm_client.chat_completion(
                    final_messages,
                    response_format=_operational_answer_response_format(),
                )
                content = response.choices[0].message.content or ""
                if not content.strip():
                    structured_error = "Structured output retornou conteudo vazio (modelo nao suporta response_format)."
                    return self._request_final_answer_without_structured_output(
                        final_messages,
                        correction_error=structured_error,
                    )
                parsed = json.loads(content)
                answer = OperationalAnswer.model_validate(parsed)
                return _normalize_final_answer(answer, llm_calls=1)
            except LocalLLMError as exc:
                structured_error = str(exc)
                return self._request_final_answer_without_structured_output(
                    final_messages,
                    correction_error=structured_error,
                )
            except (json.JSONDecodeError, ValidationError, IndexError, AttributeError) as exc:
                structured_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    final_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "A resposta anterior nao validou no schema OperationalAnswer. "
                                "Corrija uma unica vez. Responda apenas JSON, sem markdown, sem linhas de tabela. "
                                f"Erro: {structured_error}"
                            ),
                        }
                    )
                    continue
                return _error_result(
                    f"Nao foi possivel validar a entrega final da IA: {structured_error}",
                    metadata={"structured_output_error": structured_error},
                    llm_calls=1,
                )

        return _error_result(
            f"Nao foi possivel validar a entrega final da IA: {structured_error or 'erro desconhecido'}",
            metadata={"structured_output_error": structured_error},
            llm_calls=1,
        )

    def _request_final_answer_without_structured_output(
        self,
        final_messages: list[dict[str, Any]],
        *,
        correction_error: str,
    ) -> dict[str, Any]:
        messages = [
            *final_messages,
            {
                "role": "user",
                "content": (
                    "O endpoint nao aceitou structured output. "
                    "Responda apenas JSON puro com TODOS estes campos obrigatorios: "
                    '"status" (completed|no_data|needs_clarification|error), '
                    '"title" (string curta), '
                    '"summary" (resumo em uma frase), '
                    '"answer" (resposta textual ao usuario), '
                    '"visualization_type" (answer|table|list), '
                    '"items" (array de strings, pode ser []), '
                    '"sources" (array de strings, pode ser []). '
                    "Sem markdown, sem texto fora do JSON."
                ),
            },
        ]
        last_error = correction_error
        last_parsed: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                raw = self.llm_client.chat(messages)
                last_parsed = json.loads(_extract_json(raw))
                answer = OperationalAnswer.model_validate(last_parsed)
                normalized = _normalize_final_answer(answer, llm_calls=1)
                normalized["metadata"] = {
                    **normalized.get("metadata", {}),
                    "structured_output_fallback": True,
                    "structured_output_error": correction_error,
                }
                return normalized
            except (LocalLLMError, json.JSONDecodeError, ValidationError, IndexError, AttributeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "A resposta anterior ainda nao validou. Corrija uma unica vez. "
                                "Inclua TODOS os campos: status, title, summary, answer, visualization_type, items, sources. "
                                f"Erro: {last_error}"
                            ),
                        }
                    )
                    continue
        # Se o modelo retornou pelo menos um JSON parcial, recuperar com defaults
        if isinstance(last_parsed, dict):
            return _recover_partial_answer(
                last_parsed,
                metadata={
                    "structured_output_fallback": True,
                    "structured_output_error": correction_error,
                    "fallback_error": last_error,
                    "recovered_from_partial": True,
                },
            )
        return _error_result(
            f"Nao foi possivel validar a entrega final da IA: {last_error}",
            metadata={"structured_output_error": correction_error, "fallback_error": last_error},
            llm_calls=1,
        )


def _operational_answer_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "OperationalAnswer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["completed", "no_data", "needs_clarification", "error"]},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "answer": {"type": "string"},
                    "visualization_type": {"type": "string", "enum": ["answer", "table", "list"]},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["status", "title", "summary", "answer", "visualization_type", "items", "sources"],
                "additionalProperties": False,
            },
        },
    }


def _normalize_final_answer(answer: OperationalAnswer, *, llm_calls: int) -> dict[str, Any]:
    return {
        "status": answer.status,
        "mode": "operational_agent",
        "source": "lm_studio_tools",
        "title": answer.title,
        "summary": answer.summary,
        "answer": answer.answer,
        "visualization": {
            "type": answer.visualization_type,
            "columns": [],
            "rows": [],
            "items": answer.items,
        },
        "sources": answer.sources,
        "metadata": {},
        "_llm_calls": llm_calls,
    }


def _attach_tool_visualization(final: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    rows, columns, source_tools = _rows_from_tool_results(tool_results)
    requested_type = final.get("visualization", {}).get("type")
    if rows and requested_type == "table":
        final["visualization"] = {
            "type": "table",
            "columns": columns,
            "rows": rows,
            "items": [],
        }
        if final.get("status") == "completed":
            final["summary"] = final.get("summary") or f"{len(rows)} item(ns) encontrados."
    elif requested_type == "table":
        final["visualization"] = {"type": "answer", "columns": [], "rows": [], "items": []}
        if final.get("status") == "completed":
            final["status"] = "no_data"
    final["sources"] = sorted(set([*final.get("sources", []), *source_tools]))
    return final


def _rows_from_tool_results(tool_results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    for result in reversed(tool_results):
        if result.get("ok") is False:
            continue
        tool_name = str(result.get("tool_name") or "")
        payload = result.get("result")
        if not isinstance(payload, dict):
            continue
        rows, columns = _rows_for_tool(tool_name, payload)
        if rows:
            return rows, columns, [tool_name]
    return [], [], []


def _rows_for_tool(tool_name: str, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    _inventory_columns = [
        {"key": "sku", "label": "SKU"},
        {"key": "name", "label": "Produto"},
        {"key": "category", "label": "Categoria"},
        {"key": "current_stock", "label": "Estoque"},
        {"key": "minimum_stock", "label": "Minimo"},
        {"key": "ideal_stock", "label": "Ideal"},
        {"key": "criticality", "label": "Criticidade"},
        {"key": "expiration_date", "label": "Validade"},
        {"key": "status", "label": "Status"},
    ]
    if tool_name in ("fuzzy_search_inventory_tool", "search_inventory_items_tool"):
        return payload.get("items") or [], _inventory_columns
    if tool_name == "get_inventory_item_tool":
        product = payload.get("product")
        return ([product] if isinstance(product, dict) else []), [
            {"key": "sku", "label": "SKU"},
            {"key": "name", "label": "Produto"},
            {"key": "category", "label": "Categoria"},
            {"key": "current_stock", "label": "Estoque"},
            {"key": "minimum_stock", "label": "Minimo"},
            {"key": "ideal_stock", "label": "Ideal"},
            {"key": "supplier_id", "label": "Fornecedor ID"},
            {"key": "status", "label": "Status"},
        ]
    if tool_name == "list_suppliers_tool":
        return payload.get("suppliers") or [], [
            {"key": "name", "label": "Fornecedor"},
            {"key": "contact_name", "label": "Contato"},
            {"key": "email", "label": "Email"},
            {"key": "phone", "label": "Telefone"},
            {"key": "missing", "label": "Pendencias"},
            {"key": "default_lead_time_days", "label": "Prazo"},
        ]
    if tool_name == "list_stock_alerts_tool":
        return payload.get("alerts") or [], [
            {"key": "title", "label": "Alerta"},
            {"key": "alert_type", "label": "Tipo"},
            {"key": "severity", "label": "Severidade"},
            {"key": "product_id", "label": "Produto ID"},
            {"key": "description", "label": "Descricao"},
            {"key": "created_at", "label": "Criado em"},
        ]
    if tool_name == "list_saved_documents_tool":
        return payload.get("documents") or [], [
            {"key": "supplier_name", "label": "Fornecedor"},
            {"key": "amount", "label": "Valor"},
            {"key": "due_date", "label": "Vencimento"},
            {"key": "category", "label": "Categoria"},
            {"key": "description", "label": "Descricao"},
            {"key": "file_path", "label": "Arquivo"},
            {"key": "status", "label": "Status"},
        ]
    if tool_name == "run_stock_check_tool":
        return payload.get("alerts") or [], [
            {"key": "type", "label": "Tipo"},
            {"key": "severity", "label": "Severidade"},
            {"key": "product", "label": "Produto"},
            {"key": "description", "label": "Descricao"},
        ]
    return [], []


def _recover_partial_answer(partial: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Constroi uma resposta valida a partir de um JSON parcial do modelo, preenchendo campos ausentes com defaults."""
    status = partial.get("status") or "completed"
    if status not in ("completed", "no_data", "needs_clarification", "error"):
        status = "completed"
    title = str(partial.get("title") or "Resultado operacional")
    summary = str(partial.get("summary") or partial.get("answer") or "Consulta processada com sucesso.")
    answer = str(partial.get("answer") or partial.get("summary") or summary)
    viz_type = partial.get("visualization_type") or "answer"
    if viz_type not in ("answer", "table", "list"):
        viz_type = "answer"
    items_raw = partial.get("items")
    items = [str(i) for i in items_raw] if isinstance(items_raw, list) else []
    sources_raw = partial.get("sources")
    sources = [str(s) for s in sources_raw] if isinstance(sources_raw, list) else []
    return {
        "status": status,
        "mode": "operational_agent",
        "source": "lm_studio_tools",
        "title": title,
        "summary": summary,
        "answer": answer,
        "visualization": {
            "type": viz_type,
            "columns": [],
            "rows": [],
            "items": items,
        },
        "sources": sources,
        "metadata": metadata or {},
        "_llm_calls": 1,
    }


def _error_result(
    message: str,
    *,
    trace: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    llm_calls: int = 0,
) -> dict[str, Any]:
    return {
        "status": "error",
        "mode": "operational_agent",
        "source": "lm_studio_tools",
        "title": "Falha na entrega operacional",
        "summary": message,
        "answer": message,
        "visualization": {"type": "answer", "columns": [], "rows": [], "items": []},
        "sources": [],
        "trace": trace or [],
        "metadata": {"llm_calls": llm_calls, **(metadata or {})},
    }


def _requires_data_tool(prompt: str) -> bool:
    normalized = _normalize_for_lookup(prompt)
    markers = (
        "estoque",
        "produto",
        "produtos",
        "item",
        "itens",
        "sku",
        "fornecedor",
        "fornecedores",
        "validade",
        "venc",
        "alerta",
        "conta",
        "boleto",
        "documento",
        "banco",
        "comeca",
        "comecam",
    )
    return any(marker in normalized for marker in markers)


def _normalize_for_lookup(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    chars = []
    for char in text.lower():
        if unicodedata.combining(char):
            continue
        chars.append(char if char.isalnum() else " ")
    return " ".join("".join(chars).split())


def _parse_tool_args(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _compact_for_model(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is False:
        return result
    compact = {
        "ok": result.get("ok"),
        "tool_name": result.get("tool_name"),
        "result": result.get("result"),
    }
    payload = compact.get("result")
    if isinstance(payload, dict):
        payload = dict(payload)
        for key in ("items", "alerts", "suppliers", "documents", "products"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                payload[f"{key}_count"] = len(rows)
                payload[key] = rows[:20]
                payload["full_result_rendered_by_system"] = True
        compact["result"] = payload
    return compact


def _compact_for_trace(result: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= 2500:
        return result
    return {"truncated": True, "preview": text[:2500]}


def _tool_result_message(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        return str(result.get("error") or "Tool falhou.")
    payload = result.get("result")
    if isinstance(payload, dict) and "count" in payload:
        return f"Tool retornou {payload.get('count')} item(ns)."
    return "Tool executada."


def _visible_model_message(content: str | None, tool_calls: list[Any]) -> str:
    if tool_calls:
        names = ", ".join(tool_call.function.name for tool_call in tool_calls)
        return f"Modelo solicitou tools: {names}."
    if not content:
        return "Modelo respondeu sem chamada de tool."
    compact = " ".join(content.split())
    return compact[:240] + ("..." if len(compact) > 240 else "")


def _usage_from_response(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


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
