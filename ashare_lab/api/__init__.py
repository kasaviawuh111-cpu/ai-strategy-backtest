"""HTTP interface. Business logic belongs in application/domain modules."""

from .app import create_app

__all__ = ["create_app"]
