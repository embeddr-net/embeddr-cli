"""add_artifact_core_tables

Revision ID: 5a1f6b7c8d9e
Revises: 344367c39e1c
Create Date: 2026-01-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5a1f6b7c8d9e"
down_revision: Union[str, Sequence[str], None] = "344367c39e1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "artifacttype",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("parent_name", sa.String(), nullable=True),
        sa.Column("default_capabilities", sa.JSON(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["parent_name"], ["artifacttype.name"]),
        sa.PrimaryKeyConstraint("name"),
    )
    with op.batch_alter_table("artifacttype", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_artifacttype_name"), [
                              "name"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_artifacttype_parent_name"), ["parent_name"], unique=False
        )

    op.create_table(
        "artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("uri", sa.String(), nullable=True),
        sa.Column("type_name", sa.String(), nullable=False),
        sa.Column("base_type_name", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("override_capabilities", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["type_name"], ["artifacttype.name"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("artifact", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_artifact_uri"), [
                              "uri"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_artifact_type_name"), ["type_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_artifact_base_type_name"),
            ["base_type_name"],
            unique=False,
        )

    op.create_table(
        "transformation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("plugin_name", sa.String(), nullable=True),
        sa.Column("task_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False,
                  server_default="completed"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("transformation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_transformation_plugin_name"), ["plugin_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_transformation_task_name"), ["task_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_transformation_status"), ["status"], unique=False
        )

    op.create_table(
        "artifactlineage",
        sa.Column("parent_id", sa.Uuid(), nullable=False),
        sa.Column("child_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("relationship_metadata", sa.JSON(), nullable=False),
        sa.Column("transformation_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["child_id"], ["artifact.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["artifact.id"]),
        sa.ForeignKeyConstraint(["transformation_id"], ["transformation.id"]),
        sa.PrimaryKeyConstraint("parent_id", "child_id"),
    )
    with op.batch_alter_table("artifactlineage", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_artifactlineage_transformation_id"),
            ["transformation_id"],
            unique=False,
        )

    op.create_table(
        "artifactrelation",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.String(), nullable=False),
        sa.Column(
            "source_namespace",
            sa.String(),
            nullable=False,
            server_default="user",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["artifact.id"]),
        sa.ForeignKeyConstraint(["target_id"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("source_id", "target_id"),
    )
    with op.batch_alter_table("artifactrelation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_artifactrelation_relation_type"),
            ["relation_type"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_artifactrelation_source_namespace"),
            ["source_namespace"],
            unique=False,
        )

    op.create_table(
        "tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "source", sa.String(), nullable=False, server_default="user"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    with op.batch_alter_table("tag", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tag_name"),
                              ["name"], unique=False)
        batch_op.create_index(batch_op.f("ix_tag_source"), [
                              "source"], unique=False)

    op.create_table(
        "artifacttaglink",
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"]),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"]),
        sa.PrimaryKeyConstraint("tag_id", "artifact_id"),
    )

    op.create_table(
        "artifactembedding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column("plugin_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("vector_dim", sa.Integer(), nullable=False),
        sa.Column("vector_json", sa.JSON(), nullable=False),
        sa.Column(
            "space", sa.String(), nullable=False, server_default="default"
        ),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("artifactembedding", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_artifactembedding_artifact_id"),
            ["artifact_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_artifactembedding_model_name"),
            ["model_name"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_artifactembedding_plugin_name"),
            ["plugin_name"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_artifactembedding_space"),
            ["space"],
            unique=False,
        )

    op.create_table(
        "artifactannotation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("annotation_type", sa.String(), nullable=False),
        sa.Column("plugin_name", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("artifactannotation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_artifactannotation_artifact_id"),
            ["artifact_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_artifactannotation_annotation_type"),
            ["annotation_type"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_artifactannotation_plugin_name"),
            ["plugin_name"],
            unique=False,
        )

    op.create_table(
        "pluginregistry",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False,
                  server_default="active"),
        sa.Column(
            "plugin_type", sa.String(), nullable=False, server_default="adapter"
        ),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("contributed_types", sa.JSON(), nullable=False),
        sa.Column("contributed_capabilities", sa.JSON(), nullable=False),
        sa.Column("installed_at", sa.DateTime(), nullable=False),
        sa.Column("plugin_metadata", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    with op.batch_alter_table("pluginregistry", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pluginregistry_name"), ["name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_pluginregistry_status"), ["status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_pluginregistry_plugin_type"),
            ["plugin_type"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("pluginregistry", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pluginregistry_plugin_type"))
        batch_op.drop_index(batch_op.f("ix_pluginregistry_status"))
        batch_op.drop_index(batch_op.f("ix_pluginregistry_name"))

    op.drop_table("pluginregistry")

    with op.batch_alter_table("artifactannotation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifactannotation_plugin_name"))
        batch_op.drop_index(batch_op.f(
            "ix_artifactannotation_annotation_type"))
        batch_op.drop_index(batch_op.f("ix_artifactannotation_artifact_id"))

    op.drop_table("artifactannotation")

    with op.batch_alter_table("artifactembedding", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifactembedding_space"))
        batch_op.drop_index(batch_op.f("ix_artifactembedding_plugin_name"))
        batch_op.drop_index(batch_op.f("ix_artifactembedding_model_name"))
        batch_op.drop_index(batch_op.f("ix_artifactembedding_artifact_id"))

    op.drop_table("artifactembedding")

    op.drop_table("artifacttaglink")

    with op.batch_alter_table("tag", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tag_source"))
        batch_op.drop_index(batch_op.f("ix_tag_name"))

    op.drop_table("tag")

    with op.batch_alter_table("artifactrelation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifactrelation_source_namespace"))
        batch_op.drop_index(batch_op.f("ix_artifactrelation_relation_type"))

    op.drop_table("artifactrelation")

    with op.batch_alter_table("artifactlineage", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifactlineage_transformation_id"))

    op.drop_table("artifactlineage")

    with op.batch_alter_table("transformation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transformation_status"))
        batch_op.drop_index(batch_op.f("ix_transformation_task_name"))
        batch_op.drop_index(batch_op.f("ix_transformation_plugin_name"))

    op.drop_table("transformation")

    with op.batch_alter_table("artifact", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifact_base_type_name"))
        batch_op.drop_index(batch_op.f("ix_artifact_type_name"))
        batch_op.drop_index(batch_op.f("ix_artifact_uri"))

    op.drop_table("artifact")

    with op.batch_alter_table("artifacttype", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifacttype_parent_name"))
        batch_op.drop_index(batch_op.f("ix_artifacttype_name"))

    op.drop_table("artifacttype")
