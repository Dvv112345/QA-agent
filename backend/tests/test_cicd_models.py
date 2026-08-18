"""Schema-level pins for the CI/CD export tables."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from backend.models.database import (
    CicdConfig,
    CicdExport,
    CicdExportItem,
    CicdProvider,
    Repo,
    Sprint,
    TestCase,
)
from backend.models.types import CicdConfigResponse


def _sprint(db_session) -> Sprint:
    repo = Repo(github_link="https://github.com/o/r", name="r")
    db_session.add(repo)
    db_session.commit()
    sprint = Sprint(name="S1", repo_id=repo.id, directory="dir-1")
    db_session.add(sprint)
    db_session.commit()
    return sprint


def test_cicd_config_round_trips(db_session):
    sprint = _sprint(db_session)
    config = CicdConfig(
        sprint_id=sprint.id,
        provider=CicdProvider.GITHUB_ACTIONS,
        access_token="encrypted",
        ci_environment_hint="self-hosted runner",
    )
    db_session.add(config)
    db_session.commit()

    stored = db_session.exec(select(CicdConfig)).one()
    assert stored.provider == CicdProvider.GITHUB_ACTIONS
    assert stored.ci_environment_hint == "self-hosted runner"
    assert stored.verified_at is not None
    assert stored.sprint.id == sprint.id
    assert sprint.cicd_config.id == config.id


def test_cicd_config_is_one_per_sprint(db_session):
    sprint = _sprint(db_session)
    db_session.add(CicdConfig(sprint_id=sprint.id, provider=CicdProvider.JENKINS, access_token="a"))
    db_session.commit()

    db_session.add(
        CicdConfig(sprint_id=sprint.id, provider=CicdProvider.GITHUB_ACTIONS, access_token="b")
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_config_response_cannot_carry_the_token(db_session):
    sprint = _sprint(db_session)
    config = CicdConfig(
        sprint_id=sprint.id, provider=CicdProvider.GITHUB_ACTIONS, access_token="secret-token"
    )
    db_session.add(config)
    db_session.commit()

    payload = CicdConfigResponse.model_validate(config, from_attributes=True).model_dump()
    assert "access_token" not in payload
    assert "secret-token" not in str(payload)


def test_export_items_cascade_delete_with_their_export(db_session):
    sprint = _sprint(db_session)
    case = TestCase(
        position=1,
        title="Case",
        steps="a",
        expected_result="b",
        case_type="functional",
        priority="high",
    )
    db_session.add(case)
    db_session.commit()

    export = CicdExport(sprint_id=sprint.id, provider=CicdProvider.GITHUB_ACTIONS)
    export.items = [
        CicdExportItem(
            test_case_id=case.id,
            case_title="Case",
            requirement_name="Req",
            committed_path="qa-agent-tests/req_1/case_1.py",
        )
    ]
    db_session.add(export)
    db_session.commit()
    assert export.case_count == 1

    db_session.delete(export)
    db_session.commit()

    # SQLite recycles rowids, so assert absence by a stable field.
    remaining = db_session.exec(select(CicdExportItem)).all()
    assert remaining == []


def test_export_json_columns_decode_through_properties(db_session):
    sprint = _sprint(db_session)
    export = CicdExport(
        sprint_id=sprint.id,
        provider=CicdProvider.JENKINS,
        ci_file_paths_json='["Jenkinsfile"]',
        dropped_paths_json='["../etc/passwd"]',
        variable_names_json='["BASE_URL"]',
        secret_names_json='["QA_PASSWORD"]',
    )
    db_session.add(export)
    db_session.commit()

    assert export.ci_file_paths == ["Jenkinsfile"]
    assert export.dropped_paths == ["../etc/passwd"]
    assert export.variable_names == ["BASE_URL"]
    assert export.secret_names == ["QA_PASSWORD"]


def test_export_json_properties_default_to_empty_lists(db_session):
    sprint = _sprint(db_session)
    export = CicdExport(sprint_id=sprint.id, provider=CicdProvider.GITHUB_ACTIONS)
    db_session.add(export)
    db_session.commit()

    assert export.ci_file_paths == []
    assert export.dropped_paths == []
    assert export.variable_names == []
    assert export.secret_names == []
    assert export.case_count == 0


def test_test_case_script_revisions_default_to_none(db_session):
    case = TestCase(
        position=1,
        title="Case",
        steps="a",
        expected_result="b",
        case_type="functional",
        priority="high",
    )
    db_session.add(case)
    db_session.commit()

    assert case.script_requirement_revision is None
    assert case.script_plan_revision is None
    assert case.script_env_revision is None
