"""Tests for _extract_parent_execution_id — parses *_job_id:<uuid> tags from event metadata."""

from uuid import uuid4

from embeddr.services.job_runtime import _extract_parent_execution_id
from embeddr_core.plugin_interface import EmbeddrEvent


def _make_event(tags=None):
    meta = {"tags": tags} if tags is not None else {}
    return EmbeddrEvent(event_type="artifact.created", source="test", payload={"metadata_json": meta})


class TestExtractParentExecutionId:
    def test_with_sys_job_id(self):
        job_id = uuid4()
        assert _extract_parent_execution_id(_make_event([f"sys_job_id:{job_id}"])) == job_id

    def test_with_custom_prefix(self):
        job_id = uuid4()
        assert _extract_parent_execution_id(_make_event(["other", f"custom_job_id:{job_id}"])) == job_id

    def test_missing_no_matching_tag(self):
        assert _extract_parent_execution_id(_make_event(["unrelated", "foo:bar"])) is None

    def test_missing_empty_tags(self):
        assert _extract_parent_execution_id(_make_event([])) is None

    def test_missing_no_metadata(self):
        event = EmbeddrEvent(event_type="artifact.created", source="test", payload={})
        assert _extract_parent_execution_id(event) is None

    def test_invalid_uuid(self):
        assert _extract_parent_execution_id(_make_event(["sys_job_id:not-valid"])) is None

    def test_comma_separated_string(self):
        job_id = uuid4()
        assert _extract_parent_execution_id(_make_event(f"tag1,sys_job_id:{job_id}")) == job_id

    def test_first_match_wins(self):
        first, second = uuid4(), uuid4()
        result = _extract_parent_execution_id(_make_event([f"a_job_id:{first}", f"b_job_id:{second}"]))
        assert result == first
