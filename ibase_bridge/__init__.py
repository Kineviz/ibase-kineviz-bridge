"""ibase_bridge — make an i2 iBase database on Microsoft SQL Server look like a
graph database to Kineviz.

Kineviz sends Cypher over HTTP; this package translates it into ordinary T-SQL
SELECTs with JOINs, runs them, and rebuilds the rows into nodes and edges.

It is a sibling of the PostgreSQL 19 bridge, and the difference between them shapes
the whole design: PostgreSQL 19 understands graph patterns natively, so that bridge
hands the pattern straight to the database. SQL Server does not, so here a mapping
file says which tables are dots and which are lines, and the backend writes the
joins itself.
"""

__version__ = "0.1.0"
