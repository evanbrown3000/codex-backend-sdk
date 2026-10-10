import pytest

from codex_backend_sdk.bridge_provider import _admission_application_intent


def test_admission_preserves_readback_from_downstream_operation():
    result = _admission_application_intent(
        {"objective": "publish the change"},
        {"operation": "install", "required_readback": {"revision": "main"}},
    )
    assert result == {
        "objective": "publish the change",
        "required_readback": {"revision": "main"},
    }


def test_admission_uses_downstream_operation_as_readback_contract():
    result = _admission_application_intent(
        {"objective": "publish the change"}, {"revision": "main", "service": "runtime"}
    )
    assert result["required_readback"] == {"revision": "main", "service": "runtime"}


@pytest.mark.parametrize(
    "intent,downstream,error",
    [
        ({"required_readback": {"revision": "main"}}, None, "application_intent_objective_required"),
        ({"objective": "publish"}, None, "application_intent_required_readback_required"),
        (None, {"revision": "main"}, "application_intent_objective_required"),
    ],
)
def test_incomplete_intent_is_rejected_at_unified_admission(intent, downstream, error):
    with pytest.raises(ValueError, match=error):
        _admission_application_intent(intent, downstream)
