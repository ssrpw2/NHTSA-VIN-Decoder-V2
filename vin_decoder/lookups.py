"""Resolve element attribute IDs to human-readable values using lookup tables."""

import sqlite3


class LookupResolver:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._cache: dict[str, dict[str, str]] = {}
        self._element_tables: dict[int, str] = self._load_element_tables()

    def _load_element_tables(self) -> dict[int, str]:
        """Load element→lookup table mapping from the element table itself."""
        rows = self._conn.execute(
            "SELECT id, lookuptable FROM element WHERE lookuptable IS NOT NULL"
        ).fetchall()
        mapping = {}
        for r in rows:
            table_name = r["lookuptable"].lower()
            # Normalize: the element table stores names like 'BodyStyle' but
            # actual table names are lowercase 'bodystyle'
            mapping[r["id"]] = table_name
        return mapping

    def _load_table(self, table_name: str) -> dict[str, str]:
        if table_name not in self._cache:
            try:
                rows = self._conn.execute(
                    f"SELECT id, name FROM {table_name}"
                ).fetchall()
                self._cache[table_name] = {str(r["id"]): r["name"] for r in rows}
            except Exception:
                self._cache[table_name] = {}
        return self._cache[table_name]

    def resolve(self, element_id: int, attribute_id: str) -> str:
        table_name = self._element_tables.get(element_id)
        if table_name is None:
            return attribute_id
        lookup = self._load_table(table_name)
        return lookup.get(attribute_id, attribute_id)
