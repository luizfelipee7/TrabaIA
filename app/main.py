from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.ai.routes import router as ai_router
from app.database import create_db_and_tables
from app.database import BASE_DIR
from app.ops.routes import router as operational_router
from app.routes import router


app = FastAPI(
    title="Banco Simulado de Estoque",
    description="API local para simular o estoque operacional de um pequeno consultório.",
    version="0.1.0",
)


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "Banco Simulado de Estoque"}


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(router)
app.include_router(ai_router)
app.include_router(operational_router)
