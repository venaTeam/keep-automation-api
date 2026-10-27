"""Shared test helpers for lifecycle / cascade tests."""
from sqlalchemy import text

VALID = {
    "name": "restart-payments",
    "namespace": "payments-wallet",
    "script": "def handle(alert):\n    return {}\n",
    "triggers": [
        {"field": "severity", "value": "critical"},
        {"field": "application", "value": "payments"},
    ],
}

DIGEST = "sha256:" + "a" * 64


def mark_built(engine, automation_id: str, digest: str = DIGEST, **extra) -> None:
    """Simulate a finished first build (D15 is not landed): idle + active_digest."""
    assignments = {"build_state": "idle", "active_digest": digest, **extra}
    set_clause = ", ".join(f"{column} = :{column}" for column in assignments)
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE automations SET {set_clause} WHERE id = :id"),
            {**assignments, "id": automation_id},
        )


def row(engine, automation_id: str):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM automations WHERE id = :id"), {"id": automation_id}
        ).one()


def revisions(engine, automation_id: str) -> list:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT action, actor, git_sha, resulting_digest "
                "FROM automation_revisions WHERE automation_id = :id "
                "ORDER BY created_at, action"
            ),
            {"id": automation_id},
        ).all()


def create_built(client, engine, payload: dict | None = None, **extra) -> str:
    created = client.post("/automations", json=payload or VALID)
    assert created.status_code == 201, created.text
    automation_id = created.json()["id"]
    mark_built(engine, automation_id, **extra)
    return automation_id
