import sys
from typing import Optional

import typer
from sqlmodel import Session, select

from embeddr.db.session import get_engine
from embeddr.services import auth_service
from embeddr_core.models.api_key import ApiKey
from embeddr_core.models.user_account import UserAccount

app = typer.Typer(help="Destructive account and key management commands.")


def _require_confirmation(action: str) -> None:
    if not sys.stdin.isatty():
        typer.secho(
            "Confirmation required. Run this command in an interactive shell.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if not typer.confirm(
        f"DESTRUCTIVE: {action}\n\nThis will modify data in the database.",
        default=False,
    ):
        raise typer.Exit(code=1)


@app.command("reset-admin-key")
def reset_admin_key(
    username: Optional[str] = typer.Option(
        "local",
        help="Admin username to reset (default: local).",
    ),
    revoke_existing: bool = typer.Option(
        True,
        help="Revoke existing API keys for this user.",
    ),
):
    """DESTRUCTIVE: Create a new admin API key and optionally revoke old keys."""
    _require_confirmation("Reset admin API key")

    if not auth_service.is_auth_enabled():
        typer.secho(
            "Auth mode is open; API keys are not required. Continuing anyway.",
            fg=typer.colors.YELLOW,
        )

    with Session(get_engine()) as session:
        user = None
        if username:
            user = session.exec(
                select(UserAccount).where(UserAccount.username == username)
            ).first()

        if not user:
            user = session.exec(
                select(UserAccount).where(UserAccount.is_admin == True)
            ).first()

        if not user:
            typer.secho("No admin users found.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        if revoke_existing:
            keys = session.exec(
                select(ApiKey).where(ApiKey.user_id == user.id)
            ).all()
            for key in keys:
                key.is_active = False
            session.commit()

        api_key, raw_key = auth_service.create_api_key(
            session,
            user_id=user.id,
            operator_id=user.operator_id,
            name="reset-admin",
            scopes=["*"],
        )
        username_value = user.username

    typer.secho("🚨 DESTRUCTIVE: Admin key reset", fg=typer.colors.YELLOW)
    typer.secho(f"   Username: {username_value}",
                fg=typer.colors.BRIGHT_YELLOW)
    typer.secho(f"   Client Key: {raw_key}", fg=typer.colors.BRIGHT_YELLOW)
    typer.secho(
        "   Store it securely. The old keys were revoked." if revoke_existing else
        "   Store it securely. Existing keys were left active.",
        fg=typer.colors.BRIGHT_BLACK,
    )
