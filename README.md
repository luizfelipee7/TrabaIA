# Banco Simulado de Estoque

Aplicação local em FastAPI para operar um banco SQLite de estoque e testar uma IA local via LM Studio.

## Como abrir

```powershell
cd C:\Users\tzdie\Documents\Codex\Banco_Simulado
.\run_api.bat
```

Interface principal:

```text
http://127.0.0.1:8000/assistente
```

API docs:

```text
http://127.0.0.1:8000/docs
```

## Modelos

A aplicação não carrega modelos pelo endpoint nativo do LM Studio e não chama `/api/v1/models/load`.

Mantenha o modelo desejado carregado no LM Studio. O backend apenas envia o nome do modelo na chamada OpenAI-compatible.

Política padrão:

- `AI_WORKER_MODEL=nvidia/nemotron-3-nano-4b`: pedidos operacionais, banco, tools e saída estruturada.
- `AI_QUALITY_MODEL=google/gemma-4-e4b`: OCR, resumo e geração que exige mais qualidade.
- `AI_BALANCED_MODEL=mistralai/ministral-3-3b`: modelo intermediário para testes.

Se o modelo esperado para a tarefa não aparecer em `/v1/models`, a aplicação bloqueia a chamada e mostra erro claro em vez de cair silenciosamente em outro modelo.

## IA Operacional

A aba `Entrega` usa um agente operacional simples com native tool calling.

Fluxo:

1. Recebe o pedido do usuário.
2. Valida o modelo esperado pela política.
3. Chama a IA trabalhadora com tools operacionais.
4. Executa as tools no backend enquanto a IA pedir novas consultas úteis.
5. Interrompe apenas quando a IA parar de pedir tools ou repetir exatamente uma chamada sem progresso.
6. Valida a resposta final curta da IA com Pydantic.
7. Renderiza tabelas diretamente do resultado real da tool, sem pedir para o modelo reescrever todas as linhas em JSON.
8. Mostra o Agent Trace com eventos observáveis.

Tools operacionais:

- `search_inventory_items_tool`
- `get_inventory_item_tool`
- `list_suppliers_tool`
- `list_stock_alerts_tool`
- `list_saved_documents_tool`
- `run_stock_check_tool`

## Revisão Diária

A revisão diária de estoque continua separada do fluxo operacional rápido.

Dashboard técnico:

```text
http://127.0.0.1:8000/ai/dashboard
```

Esse painel é para QA, batch, trace e exportação de runs. Ele não representa a experiência principal do usuário operacional.

## STT

O STT principal é o reconhecimento nativo do navegador quando disponível.

Uploads de áudio retornam status claro caso não exista backend STT externo configurado. O projeto principal não exige iniciar servidor Whisper separado.

Configurações aceitas:

- `STT_ENGINE=browser`
- `STT_ENGINE=proxy`
- `STT_ENGINE=embedded`
- `STT_ENGINE=disabled`

## Banco

O SQLite fica em:

```text
clinic_inventory.db
```

Endpoints úteis:

- `GET /products`
- `GET /suppliers`
- `GET /alerts`
- `POST /seed/reset`
- `POST /simulation/run-stock-check`
- `POST /ops/agent/request`
- `POST /ops/agent/request/stream`
- `POST /ops/search`

## Ambiente

Se aparecer `ModuleNotFoundError`, rode:

```powershell
.\run_api.bat
```

O inicializador valida a `.venv`, recria quando ela está corrompida e instala as dependências de `requirements.txt`.
