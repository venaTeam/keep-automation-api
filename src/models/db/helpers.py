from enum import Enum

from sqlalchemy import Column
from sqlalchemy import Enum as SAEnum


def enum_column(enum_cls: type[Enum], type_name: str, **kwargs) -> Column:
    """Native PG enum column storing the enum *values* (pinned in contracts)."""
    return Column(
        SAEnum(
            enum_cls,
            name=type_name,
            values_callable=lambda e: [m.value for m in e],
        ),
        **kwargs,
    )
