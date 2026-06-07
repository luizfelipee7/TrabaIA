import json
import random
from datetime import date, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import models
from app.services import normalize_name


def _days_from_now(days: int) -> date:
    return (date.today() + timedelta(days=days))


def reset_and_seed(db: Session) -> dict:
    random.seed(42)

    for table in (
        models.StockAlert,
        models.InventoryMovement,
        models.StockRule,
        models.Product,
        models.Supplier,
    ):
        db.execute(delete(table))
    db.commit()

    suppliers = [
        models.Supplier(
            name="MedSupply Distribuidora",
            contact_name="Carla Nogueira",
            email="carla@medsupply.example",
            phone="(11) 4002-1001",
            default_lead_time_days=4,
            notes="Fornecedor principal de descartáveis.",
        ),
        models.Supplier(
            name="Clin Farma Hospitalar",
            contact_name="Rafael Lima",
            email="rafael@clinfarm.example",
            phone="(21) 3003-2200",
            default_lead_time_days=6,
            notes="Medicamentos e soluções.",
        ),
        models.Supplier(
            name="Higieniza Pro",
            contact_name="Marina Costa",
            email="pedidos@higienizapro.example",
            phone="(31) 3555-0101",
            default_lead_time_days=5,
            notes="Produtos de limpeza e antissepsia.",
        ),
        models.Supplier(
            name="Office Care Suprimentos",
            contact_name="João Martins",
            email=None,
            phone="(41) 3222-9090",
            default_lead_time_days=8,
            notes="Contato incompleto proposital para simulação.",
        ),
        models.Supplier(
            name="DentalMed Equipamentos",
            contact_name="Patrícia Rocha",
            email="patricia@dentalmed.example",
            phone="(51) 3777-8080",
            default_lead_time_days=10,
            notes="Equipamentos pequenos e reposições.",
        ),
    ]
    db.add_all(suppliers)
    db.flush()

    product_rows = [
        ("LUVA-NIT-P", "Luva nitrílica P", "luvas", "caixa", 42, 30, 90, 32.90, 1, "medium", 240),
        ("LUVA-NIT-M", "Luva nitrílica M", "luvas", "caixa", 8, 30, 100, 34.50, 1, "high", 240),
        ("LUVA-NIT-G", "Luva nitrílica G", "luvas", "caixa", 55, 25, 80, 35.10, 1, "medium", 240),
        ("MASC-CIR-TRI", "Máscara cirúrgica tripla", "máscaras", "caixa", 120, 80, 220, 18.90, 1, "high", 180),
        ("MASC-N95", "Máscara N95", "máscaras", "unidade", 18, 20, 60, 4.80, 1, "high", 365),
        ("ALC-70-1L", "Álcool 70%", "limpeza", "frasco", 14, 20, 60, 9.40, 3, "high", 18),
        ("SER-5ML", "Seringa descartável 5ml", "descartáveis", "unidade", 300, 120, 500, 0.48, 1, "medium", 540),
        ("SER-10ML", "Seringa descartável 10ml", "descartáveis", "unidade", 90, 100, 350, 0.62, 1, "medium", 540),
        ("GAZE-EST", "Gaze estéril", "descartáveis", "pacote", 70, 45, 140, 6.70, 1, "medium", 90),
        ("ESPAR-10", "Esparadrapo", "descartáveis", "rolo", 22, 12, 35, 5.90, 1, "low", 420),
        ("PAP-GRAU", "Papel grau cirúrgico", "descartáveis", "rolo", 7, 15, 45, 49.90, 1, "high", 365),
        ("TOUCA-DESC", "Touca descartável", "descartáveis", "pacote", 88, 40, 130, 11.20, 1, "low", 365),
        ("LENCOL-DESC", "Lençol descartável", "descartáveis", "rolo", 16, 10, 30, 38.50, 1, "medium", 360),
        ("SORO-500", "Soro fisiológico 500ml", "medicamentos", "frasco", 5, 25, 80, 7.90, 2, "high", 12),
        ("DIPI-GOTAS", "Dipirona gotas", "medicamentos", "frasco", 9, 8, 20, 6.40, 2, "medium", 7),
        ("ATAD-CREPE", "Atadura crepe", "descartáveis", "unidade", 64, 25, 90, 2.80, 1, "medium", 730),
        ("COLET-PERF", "Coletor perfurocortante", "descartáveis", "unidade", 3, 8, 20, 12.70, 1, "high", 720),
        ("PAP-TOALHA", "Papel toalha", "limpeza", "fardo", 11, 10, 25, 21.30, 3, "low", None),
        ("SAB-ANTI", "Sabonete antisséptico", "limpeza", "frasco", 6, 12, 36, 14.80, 3, "medium", 120),
        ("ALG-ROLO", "Algodão rolo", "descartáveis", "rolo", 24, 15, 45, 8.20, 1, "medium", 365),
        ("ABAIX-LING", "Abaixador de língua", "descartáveis", "pacote", 45, 20, 60, 7.50, 1, "low", 365),
        ("TERM-DIG", "Termômetro digital", "equipamentos pequenos", "unidade", 4, 2, 6, 28.00, 5, "medium", None),
        ("PILHA-AA", "Pilha AA", "equipamentos pequenos", "cartela", 10, 6, 18, 13.20, 5, "low", 540),
        ("CANETA-AZ", "Caneta azul", "escritório", "unidade", 18, 10, 30, 1.90, 4, "low", None),
        ("ETIQ-ADES", "Etiqueta adesiva", "escritório", "rolo", 2, 5, 15, 15.40, 4, "low", None),
    ]

    products = []
    for sku, name, category, unit, current, minimum, ideal, cost, supplier_id, criticality, expires_in in product_rows:
        products.append(
            models.Product(
                sku=sku,
                name=name,
                normalized_name=normalize_name(name),
                category=category,
                unit=unit,
                current_stock=current,
                minimum_stock=minimum,
                ideal_stock=ideal,
                average_unit_cost=cost,
                supplier_id=supplier_id,
                criticality=criticality,
                expiration_date=_days_from_now(expires_in) if expires_in is not None else None,
                active=True,
            )
        )
    db.add_all(products)
    db.flush()

    product_by_sku = {product.sku: product for product in products}
    movements: list[models.InventoryMovement] = []
    today = datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
    reasons = ["atendimento", "reposição de sala", "procedimento", "uso administrativo"]
    responsible = ["Ana Paula", "Bruno Reis", "Camila Torres", "Diego Araujo"]

    for product in products:
        incoming_qty = max(product.ideal_stock - product.current_stock, 10)
        movements.append(
            models.InventoryMovement(
                product_id=product.id,
                movement_type="in",
                quantity=incoming_qty,
                reason="Compra mensal simulada",
                source="seed",
                responsible_name="Sistema Seed",
                occurred_at=today - timedelta(days=29, hours=random.randint(0, 6)),
            )
        )
        for day in range(28, 1, -random.choice([3, 4, 5])):
            qty = random.randint(1, 8)
            if product.category in {"luvas", "máscaras", "descartáveis"}:
                qty += random.randint(2, 8)
            movements.append(
                models.InventoryMovement(
                    product_id=product.id,
                    movement_type="out",
                    quantity=qty,
                    reason=random.choice(reasons),
                    source="consultório",
                    responsible_name=random.choice(responsible),
                    occurred_at=today - timedelta(days=day, hours=random.randint(0, 8)),
                )
            )

    mask = product_by_sku["MASC-CIR-TRI"]
    for day in range(29, 0, -1):
        movements.append(
            models.InventoryMovement(
                product_id=mask.id,
                movement_type="out",
                quantity=8 if day > 1 else 34,
                reason="uso em atendimentos",
                source="recepção",
                responsible_name="Ana Paula",
                occurred_at=today - timedelta(days=day),
            )
        )

    movements.append(
        models.InventoryMovement(
            product_id=product_by_sku["SER-10ML"].id,
            movement_type="adjustment",
            quantity=-80,
            reason="Ajuste manual de inventário divergente",
            source="contagem física",
            responsible_name="Bruno Reis",
            occurred_at=today - timedelta(days=3, hours=2),
        )
    )
    movements.append(
        models.InventoryMovement(
            product_id=product_by_sku["ALC-70-1L"].id,
            movement_type="loss",
            quantity=4,
            reason="Frascos com lacre rompido",
            source="almoxarifado",
            responsible_name="Camila Torres",
            occurred_at=today - timedelta(days=5, hours=1),
        )
    )
    db.add_all(movements)

    rules = [
        models.StockRule(
            name="low_stock",
            description="Produto ativo com estoque atual abaixo do estoque mínimo.",
            rule_type="stock_level",
            parameters_json=json.dumps({"field": "minimum_stock"}, ensure_ascii=False),
        ),
        models.StockRule(
            name="critical_low_stock",
            description="Produto crítico com estoque atual abaixo do estoque mínimo.",
            rule_type="stock_level",
            parameters_json=json.dumps({"criticality": "high"}, ensure_ascii=False),
        ),
        models.StockRule(
            name="near_expiration",
            description="Produto com vencimento em até 30 dias.",
            rule_type="expiration",
            parameters_json=json.dumps({"days": 30}, ensure_ascii=False),
        ),
        models.StockRule(
            name="abnormal_consumption",
            description="Saída diária acima de 2x a média dos últimos 30 dias.",
            rule_type="movement_pattern",
            parameters_json=json.dumps({"window_days": 30, "multiplier": 2}, ensure_ascii=False),
        ),
        models.StockRule(
            name="missing_supplier_contact",
            description="Fornecedor sem e-mail ou telefone cadastrado.",
            rule_type="supplier_quality",
            parameters_json=json.dumps({"required": ["email", "phone"]}, ensure_ascii=False),
        ),
    ]
    db.add_all(rules)
    db.commit()

    return {
        "suppliers": len(suppliers),
        "products": len(products),
        "movements": len(movements),
        "rules": len(rules),
    }
