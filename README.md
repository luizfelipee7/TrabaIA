# Banco Simulado de Estoque

API local em Python para simular o banco operacional de estoque de um pequeno consultório.

O projeto combina um banco SQLite de estoque simulado com uma camada local e controlada de IA via LM Studio. A IA usa apenas tools permitidas pelo codigo e nao altera dados criticos diretamente.

## Stack

- Python 3.11+
- FastAPI
- SQLite
- SQLAlchemy 2.x
- Pydantic
- Uvicorn

## Como rodar

No PowerShell, entre na pasta do projeto:

```powershell
cd Banco_Simulado
```

Crie e ative um ambiente virtual:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Instale as dependências:

```powershell
pip install -r requirements.txt
```

Inicie a API:

```powershell
uvicorn app.main:app --reload
```

A documentação interativa fica em:

```text
http://127.0.0.1:8000/docs
```

O banco SQLite será criado automaticamente em:

```text
clinic_inventory.db
```

## Fluxo principal

Resetar e popular o banco:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/seed/reset
```

Listar produtos:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/products
```

Listar movimentações:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/movements
```

Executar a análise determinística de estoque:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/simulation/run-stock-check
```

Consultar alertas abertos:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/alerts/open
```

## Cenários simulados no seed

- 5 fornecedores
- 25 produtos
- Movimentações nos últimos 30 dias
- Produtos com estoque normal
- Produtos abaixo do mínimo
- Produtos críticos abaixo do mínimo
- Produtos próximos do vencimento
- Fornecedor com contato incompleto
- Produto com consumo anormal recente
- Ajuste de estoque suspeito

## Endpoints

Produtos:

- `GET /products`
- `GET /products/{product_id}`
- `POST /products`
- `PATCH /products/{product_id}`

Fornecedores:

- `GET /suppliers`
- `GET /suppliers/{supplier_id}`
- `POST /suppliers`

Movimentações:

- `GET /movements`
- `GET /products/{product_id}/movements`
- `POST /movements`

Regras:

- `GET /rules`

Alertas:

- `GET /alerts`
- `GET /alerts/open`
- `PATCH /alerts/{alert_id}/resolve`
- `PATCH /alerts/{alert_id}/ignore`

Simulação:

- `POST /seed/reset`
- `POST /simulation/run-stock-check`

## Consistência de estoque

Ao registrar uma movimentação por `POST /movements`:

- `in` aumenta o estoque atual
- `out` diminui o estoque atual
- `loss` diminui o estoque atual
- `adjustment` aplica a quantidade informada, positiva ou negativa
- estoque negativo é bloqueado, exceto quando `allow_negative=true`
- `updated_at` do produto é atualizado
- a movimentação é salva no histórico

## IA local com LM Studio

Esta etapa adiciona uma IA operacional simples para revisar o estoque usando apenas tools controladas pelo codigo. Ela nao acessa SQL diretamente, nao altera estoque, nao aprova compras, nao resolve alertas e nao envia mensagens externas.

Dependencias adicionais:

```powershell
pip install -r requirements.txt
```

No LM Studio:

1. Abra o LM Studio.
2. Carregue um modelo de chat.
3. Inicie o servidor local da API.
4. Use a URL padrao `http://localhost:1234/v1`.

A aplicacao usa o SDK oficial da OpenAI apontando para o LM Studio:

- `LM_STUDIO_BASE_URL`: padrao `http://localhost:1234/v1`
- `LM_STUDIO_API_KEY`: padrao `lm-studio`
- `LM_STUDIO_MODEL`: padrao `local-model`

Interface web da IA:

```text
http://127.0.0.1:8000/ai/dashboard
```

Endpoints da IA:

- `GET /ai/models`: lista modelos disponiveis no LM Studio via `/v1/models`
- `POST /ai/models/select`: seleciona o modelo usado nas proximas chamadas
- `POST /ai/daily-inventory-review`: executa a revisao diaria com tools controladas
- `POST /ai/daily-inventory-review/batch`: executa varias revisoes em sequencia para QA
- `GET /ai/logs`: mostra logs recentes da IA
- `GET /ai/reports`: mostra relatorios salvos
- `GET /ai/qa/runs`: lista execucoes de QA exportadas
- `GET /ai/qa/runs/{run_id}`: abre uma execucao completa com timeline e metricas
- `GET /ai/qa/runs/{run_id}/export`: baixa o JSON de uma execucao
- `GET /ai/qa/batches`: lista batches exportados
- `GET /ai/qa/batches/{batch_id}/export?format=json`: baixa resumo JSON do batch
- `GET /ai/qa/batches/{batch_id}/export?format=csv`: baixa comparativo CSV do batch

Exemplo para selecionar modelo:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/ai/models/select `
  -ContentType "application/json" `
  -Body '{"model_name":"nome-do-modelo"}'
```

Executar a revisao pela API:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/ai/daily-inventory-review `
  -ContentType "application/json" `
  -Body '{}'
```

Executar batch para QA:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/ai/daily-inventory-review/batch `
  -ContentType "application/json" `
  -Body '{"count":10}'
```

Executar por script:

```powershell
python scripts/run_ai_inventory_review.py --list-models
```

Ou com modelo especifico:

```powershell
python scripts/run_ai_inventory_review.py --model "nome-do-modelo"
```

Arquivos gerados:

- Relatorios: `reports/*.json`
- Logs: `logs/ai_actions.jsonl`
- Runs de QA: `qa_runs/runs/*.json`
- Batches de QA: `qa_runs/batches/*.json` e `qa_runs/batches/*.csv`

Observabilidade para QA:

A interface `/ai/dashboard` mostra metricas, timeline da execucao, chamadas de tools, resultados, resposta final, alertas e comparacao entre execucoes. Ela nao exibe raciocinio oculto da IA; mostra apenas mensagens e eventos observaveis retornados pelo modelo e pelo runtime.

Sobre carregamento de modelos:

A selecao de modelo na interface define o identificador enviado para o LM Studio nas chamadas de chat. O OpenAI-compatible endpoint do LM Studio usa `/v1/models` para listar modelos e `/v1/chat/completions` para conversar. Quando disponivel, a interface tambem pode tentar carregar um modelo pelo endpoint nativo `/api/v1/models/load`; se isso falhar, carregue o modelo manualmente no LM Studio e mantenha a selecao como referencia para as proximas chamadas.

Limitacoes desta etapa:

- Sem LangGraph, CrewAI, AutoGen, MCP ou framework agentic.
- Sem RAG, embeddings ou memoria vetorial.
- Sem multiplos agentes.
- Sem compras reais.
- Sem e-mail real.
- Sem escrita de estoque pela IA.
