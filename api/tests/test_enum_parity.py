"""Every app/enums.py value must appear in its column's CHECK constraint (no drift)."""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint

from app.enums import ENUM_COLUMNS, enum_values
from app.models import Base


@pytest.mark.parametrize(("table", "column"), list(ENUM_COLUMNS))
def test_enum_values_covered_by_check(table: str, column: str) -> None:
    enum_cls = ENUM_COLUMNS[(table, column)]
    table_obj = Base.metadata.tables[table]
    constraint_name = f"ck_{table}_{column}"

    checks = [
        c
        for c in table_obj.constraints
        if isinstance(c, CheckConstraint) and c.name == constraint_name
    ]
    assert checks, f"missing CHECK constraint {constraint_name} on {table}"

    sqltext = str(checks[0].sqltext)
    for value in enum_values(enum_cls):
        assert f"'{value}'" in sqltext, f"{value} not in CHECK {constraint_name}: {sqltext}"


def test_registry_covers_every_check_constraint() -> None:
    # The reverse: no enum CHECK exists that the registry does not know about.
    registered = {f"ck_{t}_{c}" for (t, c) in ENUM_COLUMNS}
    found = {
        c.name
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint) and (c.name or "").startswith("ck_")
    }
    assert found == registered
