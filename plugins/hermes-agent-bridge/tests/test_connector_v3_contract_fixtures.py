import json
import os
from pathlib import Path


FIXTURES = Path(
    os.environ.get(
        "RELAY_CONNECTOR_FIXTURE_DIR",
        str(Path(__file__).resolve().parents[3] / "contracts" / "fixtures"),
    )
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_python_validates_shared_connector_v3_fixtures():
    inventory = fixture("connector-v3-inventory-request.json")
    response = fixture("connector-v3-inventory-response.json")
    connect = fixture("connector-v3-connect-directive.json")
    request = fixture("connector-v3-provision-request.json")
    result = fixture("connector-v3-provision-result.json")

    assert inventory["protocolVersion"] == "relay-connector.v3"
    assert inventory["agents"][0]["documents"] == []
    assert response["discoveries"][0]["directive"] == "metadata_only"
    assert response["discoveries"][0]["documentSync"] is False
    assert connect["directive"] == "connect"
    assert connect["documentConsentVersion"] == 1
    for key in (
        "commandId",
        "jobId",
        "workspaceId",
        "runtimeHostId",
        "runtimeType",
        "idempotencyKey",
    ):
        assert result[key] == request[key]
    assert result["externalAgentId"] == request["payload"]["slug"]
