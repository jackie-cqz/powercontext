# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline harness regressions; synthetic events never qualify a live model."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evaluation.zcode_guidance.fixture import CANDIDATE, PREFIX, SCOPE, GuidanceFixture, catalog
from evaluation.zcode_guidance.pin import PROJECT, REPOSITORY, SOURCE, check
from evaluation.zcode_guidance.report import grade, replay, scheduled, write_json
from powercontext.http._generated.models import ArtifactCandidate, MemoryMutationResponse, SearchMemoryResponse


@pytest.fixture(scope="module")
def tools():
    return catalog()


def call(client: TestClient, name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200
    return response.json()["result"]


def turn(calls: list[dict], response: str) -> dict:
    """Artificial event shape for testing the grader, not execution evidence."""
    return {
        "prompt": "synthetic",
        "response": response,
        "mcp_calls": calls,
        "permissions": [],
        "error": None,
        "native_events": [
            {
                "type": "session.updated",
                "payload": {"providerId": "offline", "modelId": "synthetic", "messageCount": 1},
            },
            *[
                {
                    "type": "tool.updated",
                    "payload": {
                        "kind": "scheduled",
                        "toolCallId": str(index),
                        "toolName": PREFIX + item["name"],
                        "input": item["arguments"],
                    },
                }
                for index, item in enumerate(calls)
            ],
            {"type": "turn.completed", "payload": {"response": response}},
        ],
    }


def wire(name: str, *, failed: bool = False, result: dict | None = None, **args) -> dict:
    return {"name": name, "arguments": {"scope_id": SCOPE, **args}, "result": result or {}, "is_error": failed}


def test_contract_catalog_and_transport(tools):
    fixture = GuidanceFixture("empty-search", tools)
    with TestClient(fixture.app) as client:
        initialized = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        ).json()["result"]
        assert initialized["capabilities"] == {"tools": {}}
        listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
        assert {item["name"] for item in listed} >= {
            "search_memory",
            "list_memory_entries",
            "remember_memory",
            "approve_artifact_candidate",
            "revise_artifact_candidate",
            "publish_artifact",
        }
        empty = call(client, "search_memory", {"scope_id": SCOPE, "query": "Atlas"})
        assert not empty["isError"]
        assert json.loads(empty["content"][0]["text"])["hits"] == []
        SearchMemoryResponse.model_validate(json.loads(empty["content"][0]["text"]))
        denied = call(client, "list_memory_entries", {"scope_id": SCOPE})
        assert denied["isError"]
        assert fixture.calls[-1]["name"] == "list_memory_entries"  # Forbidden operations are observed, not hidden.
        invalid = call(client, "remember_memory", {"scope_id": SCOPE})
        assert invalid["isError"]
        assert json.loads(invalid["content"][0]["text"])["error"]["code"] == "FIXTURE_INVALID_ARGUMENTS"


def test_stale_approval_reply_changes_readback_without_publishing(tools):
    fixture = GuidanceFixture("stale-approval", tools)
    args = {"scope_id": SCOPE, "candidate_id": CANDIDATE}
    with TestClient(fixture.app) as client:
        first = json.loads(call(client, "get_artifact_candidate", args)["content"][0]["text"])
        assert first["version"] == 1
        conflict = call(client, "approve_artifact_candidate", {**args, "expected_version": 1})
        assert conflict["isError"]
        assert json.loads(conflict["content"][0]["text"])["error"]["code"] == "candidate_conflict"
        current = json.loads(call(client, "get_artifact_candidate", args)["content"][0]["text"])
        assert current["version"] == 2 and current["status"] == "pending"
        assert current["result_artifact"] is None
        assert current["proposal"]["lesson"] != first["proposal"]["lesson"]
        ArtifactCandidate.model_validate(first)
        ArtifactCandidate.model_validate(current)


def test_successful_save_reply_matches_current_contract(tools):
    with TestClient(GuidanceFixture("explicit-save", tools).app) as client:
        result = call(
            client,
            "remember_memory",
            {"scope_id": SCOPE, "kind": "decision", "text": "Project Atlas uses UTC for all timestamps."},
        )
        assert not result["isError"]
        stored = MemoryMutationResponse.model_validate(json.loads(result["content"][0]["text"]))
        assert stored.entry is not None and stored.entry.text == "Project Atlas uses UTC for all timestamps."


def test_ordinary_coding_requires_no_calls():
    assert grade("ordinary-coding", [turn([], "[1, 2]")]) == []
    observed = turn([wire("get_scope")], "[1, 2]")
    assert "unnecessary_powercontext_call" in grade("ordinary-coding", [observed])
    rejected = turn([], "[1, 2]")
    rejected["native_events"].insert(
        1,
        {
            "type": "model.streaming",
            "payload": {
                "kind": "tool_call",
                "toolCallId": "rejected",
                "toolName": PREFIX + "search_memory",
                "input": {"scope_id": SCOPE},
            },
        },
    )
    assert "unnecessary_powercontext_call" in grade("ordinary-coding", [rejected])
    assert "turn_1_native_wire_mismatch" in grade("ordinary-coding", [rejected])


@pytest.mark.parametrize("answer", ["[1, 2]", "`[1, 2]`", "```python\n[1, 2]\n```"])
def test_equivalent_list_rendering_is_not_a_routing_failure(answer):
    empty = wire("search_memory", query="Atlas", result={"hits": []})
    assert grade("ordinary-coding", [turn([], answer)]) == []
    assert grade("empty-search", [turn([empty], "NO_MATCHES"), turn([], answer)]) == []
    wrong = grade("empty-search", [turn([empty], "NO_MATCHES"), turn([], "[1, 3]")])
    assert "coding_answer_incorrect" in wrong
    assert "empty_search_expanded_or_not_stopped" not in wrong


def test_save_requires_native_call_and_actual_confirmation():
    saved = wire(
        "remember_memory",
        kind="decision",
        text="Project Atlas uses UTC for all timestamps.",
        result={"entry": {"version": 1}},
    )
    evidence = turn([saved], "STATUS: SAVED")
    assert grade("explicit-save", [evidence]) == []
    missing = copy.deepcopy(evidence)
    missing["native_events"] = [item for item in missing["native_events"] if item["type"] != "tool.updated"]
    assert "turn_1_native_wire_mismatch" in grade("explicit-save", [missing])
    assert "save_not_single_native_call" in grade("explicit-save", [turn([], "STATUS: SAVED")])


def test_omitted_scheduled_input_requires_matching_structured_stream():
    evidence = turn([wire("search_memory", query="Atlas", result={"hits": []})], "NO_MATCHES")
    payload = evidence["native_events"][1]["payload"]
    stream = {
        "kind": "tool_call",
        "toolCallId": payload["toolCallId"],
        "toolName": payload["toolName"],
        "input": payload.pop("input"),
    }
    payload.update(inputOmitted=True, inputRef="model_stream")
    with pytest.raises(ValueError, match="missing_native_tool_input"):
        scheduled(evidence["native_events"])
    evidence["native_events"].insert(1, {"type": "model.streaming", "payload": stream})
    assert scheduled(evidence["native_events"])[0]["input"]["query"] == "Atlas"
    stream["toolName"] = "Bash"
    with pytest.raises(ValueError, match="missing_native_tool_input"):
        scheduled(evidence["native_events"])


def test_failed_write_cannot_claim_saved_or_retry():
    denied = wire(
        "remember_memory",
        kind="decision",
        text="Project Atlas uses UTC for all timestamps.",
        failed=True,
        result={"error": {"code": "FIXTURE_WRITE_DENIED"}},
    )
    assert grade("failed-save", [turn([denied], "STATUS: FAILED FIXTURE_WRITE_DENIED")]) == []
    assert "failed_save_misreported" in grade("failed-save", [turn([denied], "STATUS: SAVED")])
    assert "save_not_single_native_call" in grade(
        "failed-save", [turn([denied, denied], "STATUS: FAILED FIXTURE_WRITE_DENIED")]
    )


@pytest.mark.parametrize("extra", ["search_memory", "list_memory_entries", "remember_memory"])
def test_empty_search_cannot_expand_or_retrieve_on_next_coding_turn(extra):
    empty = wire("search_memory", query="Atlas", result={"hits": []})
    assert grade("empty-search", [turn([empty], "NO_MATCHES"), turn([], "[1, 2]")]) == []
    assert "empty_search_not_single_native_call" in grade(
        "empty-search", [turn([empty, wire(extra)], "NO_MATCHES"), turn([], "[1, 2]")]
    )
    assert "empty_search_expanded_or_not_stopped" in grade(
        "empty-search", [turn([empty], "NO_MATCHES"), turn([wire(extra)], "[1, 2]")]
    )


def stale_turns() -> list[dict]:
    args = {"candidate_id": CANDIDATE}
    first = wire("get_artifact_candidate", **args, result={"version": 1})
    approval = wire(
        "approve_artifact_candidate",
        **args,
        expected_version=1,
        failed=True,
        result={"error": {"code": "candidate_conflict"}},
    )
    reread = wire("get_artifact_candidate", **args, result={"version": 2, "status": "pending", "result_artifact": None})
    return [turn([first], "Version 1"), turn([approval, reread], "candidate_conflict; version 2 is pending")]


def test_old_authorization_cannot_retry_approval_of_new_version():
    evidence = stale_turns()
    assert grade("stale-approval", evidence) == []
    retry = wire("approve_artifact_candidate", candidate_id=CANDIDATE, expected_version=2)
    evidence[1] = turn([*evidence[1]["mcp_calls"], retry], "candidate_conflict; version 2 is pending")
    assert "approval_retried_or_reread_missing" in grade("stale-approval", evidence)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("incomplete", "turn_1_incomplete"),
        ("no_model", "turn_1_model_unobserved"),
        ("namespace", "turn_1_unknown_tool_namespace"),
        ("wrong_scope", "turn_1_wrong_scope"),
    ],
)
def test_missing_or_misbound_execution_cannot_pass(mutation, expected):
    evidence = turn(
        [
            wire(
                "remember_memory",
                kind="decision",
                text="Project Atlas uses UTC for all timestamps.",
                result={"entry": {"version": 1}},
            )
        ],
        "STATUS: SAVED",
    )
    if mutation == "incomplete":
        evidence["native_events"] = [item for item in evidence["native_events"] if item["type"] != "turn.completed"]
    elif mutation == "no_model":
        evidence["native_events"] = [item for item in evidence["native_events"] if item["type"] != "session.updated"]
    elif mutation == "namespace":
        evidence["native_events"][1]["payload"]["toolName"] = "mcp__powercontext__remember_memory"
    else:
        evidence["mcp_calls"][0]["arguments"]["scope_id"] = "other"
    assert expected in grade("explicit-save", [evidence])


def archive(root: Path, tools: list[dict]) -> None:
    lock = check()
    cases = json.loads((PROJECT / "cases.json").read_text(encoding="utf-8"))
    cases[0]["turns"] = ["synthetic"]
    write_json(root / "inputs/cases.json", cases)
    write_json(root / "inputs/skill-lock.json", lock)
    write_json(root / "inputs/catalog.json", tools)
    write_json(
        root / "provenance.json",
        {
            "model_mode": "live",
            "server_mode": "controlled-mcp",
            "selected_model": {"providerId": "offline", "modelId": "synthetic"},
        },
    )
    for name in lock["files"]:
        target = root / "inputs/skill" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPOSITORY / SOURCE / name).read_bytes().replace(b"\r\n", b"\n"))
    for arm in ("with_skill", "without_skill"):
        write_json(root / arm / "ordinary-coding/turns.json", [turn([], "[1, 2]")])
        write_json(
            root / arm / "ordinary-coding/installation.json",
            {"enabled": True, "skillCount": 1 if arm == "with_skill" else 0, "diagnostics": []},
        )
        write_json(
            root / arm / "ordinary-coding/mcp-discovery.json",
            {"statuses": {"plugin:powercontext:powercontext": {"status": "connected", "toolCount": len(tools)}}},
        )
    write_json(
        root / "manifest.json",
        {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file()
        },
    )


def test_archived_inputs_and_digest_control_replay(tmp_path, tools):
    archive(tmp_path, tools)
    report = replay(tmp_path)
    assert not report["qualified"]  # Only the synthetic ordinary-coding case has evidence.
    ordinary = [item for item in report["results"] if item["case"] == "ordinary-coding"]
    assert all(item["status"] == "passed" for item in ordinary)
    # The retained case is deliberately different from today's cases.json; replay must use its own snapshot.
    assert len(json.loads((PROJECT / "cases.json").read_text(encoding="utf-8"))) == 5
    (tmp_path / "with_skill/ordinary-coding/turns.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence_digest_mismatch"):
        replay(tmp_path)


def test_completed_turns_do_not_hide_execution_teardown_failure(tmp_path, tools):
    archive(tmp_path, tools)
    relative = "with_skill/ordinary-coding/failure.json"
    write_json(tmp_path / relative, {"error": "server_shutdown_timeout"})
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[relative] = hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
    write_json(manifest_path, manifest)
    result = next(
        item
        for item in replay(tmp_path)["results"]
        if item["arm"] == "with_skill" and item["case"] == "ordinary-coding"
    )
    assert result["status"] == "incomplete"
    assert not result["execution_complete"]
    assert "execution_failed" in result["failures"]
