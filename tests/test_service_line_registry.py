from app.services.service_line import (
    SERVICE_LINE_PAYMENT_SYSTEMS,
    SERVICE_LINES,
)


def test_payment_service_line_exposes_its_dedicated_cost_reviewer() -> None:
    config = SERVICE_LINES[SERVICE_LINE_PAYMENT_SYSTEMS]

    assert config["shows_cost_analyst"] is False
    assert config["shows_cost_reviewer"] is True
    assert "payment-specific Cost Reviewer" in config["description"]
