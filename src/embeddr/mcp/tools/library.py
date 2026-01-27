from sqlmodel import select
from embeddr.mcp.utils import get_db_session
from embeddr_core.models.library import LibraryPath


def list_libraries() -> str:
    """List all image libraries available in the system."""
    with get_db_session() as session:
        libraries = session.exec(select(LibraryPath)).all()
        if not libraries:
            return "No libraries found."
        return "\n".join(
            [f"ID: {lib.id} | Name: {lib.name} | Path: {lib.path}" for lib in libraries]
        )


def register_library_resources(mcp) -> None:
    mcp.resource("libraries://list")(list_libraries)


# @mcp.resource("data://list")
# def list_collections() -> dict:
#     with get_db_session() as session:
#         collections = session.exec(select(Collection)).all()
#         return {
#             "collections": [
#                 {"id": col.id, "name": col.name}
#                 for col in collections
#             ]
#         }
