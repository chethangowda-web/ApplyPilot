from pgvector.sqlalchemy import Vector
from sqlalchemy import Float, JSON, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.types import TypeDecorator


JSONVariant = JSON().with_variant(JSONB(), "postgresql")
StringArrayVariant = JSON().with_variant(ARRAY(String()), "postgresql")


class EmbeddingVector(TypeDecorator):
    """Uses pgvector Vector(dim) on PostgreSQL and JSON array on SQLite/other dialects."""

    impl = JSON
    cache_ok = True

    class comparator_factory(TypeDecorator.Comparator):
        def l2_distance(self, other):
            return self.op("<->", return_type=Float)(other)

        def max_inner_product(self, other):
            return self.op("<#>", return_type=Float)(other)

        def cosine_distance(self, other):
            return self.op("<=>", return_type=Float)(other)

        def l1_distance(self, other):
            return self.op("<+>", return_type=Float)(other)

    def __init__(self, dimensions: int = 768) -> None:
        super().__init__()
        self.dimensions = dimensions

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dimensions))
        return dialect.type_descriptor(JSON())
