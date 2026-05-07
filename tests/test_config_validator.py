"""Tests for markbot.config.validator — cross-field configuration validation."""

from markbot.config.schema import Config
from markbot.config.validator import (
    Severity,
    ValidationResult,
    validate_config,
)


class TestValidationResult:
    def test_empty_is_valid(self):
        result = ValidationResult()
        assert result.is_valid
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    def test_add_error(self):
        result = ValidationResult()
        result.add("field", "error message")
        assert not result.is_valid
        assert len(result.errors) == 1

    def test_add_warning(self):
        result = ValidationResult()
        result.add("field", "warning", severity=Severity.WARNING)
        assert result.is_valid
        assert len(result.warnings) == 1

    def test_merge(self):
        r1 = ValidationResult()
        r2 = ValidationResult()
        r1.add("a", "error")
        r2.add("b", "warning", severity=Severity.WARNING)
        r1.merge(r2)
        assert len(r1.issues) == 2


class TestValidateConfig:
    def test_default_config_valid(self):
        config = Config()
        result = validate_config(config)
        assert isinstance(result, ValidationResult)

    def test_empty_model_chain_warns(self):
        config = Config()
        config.agents.defaults.model_chain = []
        result = validate_config(config)
        warnings = [i for i in result.issues if "model_chain" in i.field]
        assert len(warnings) > 0

    def test_invalid_model_ref(self):
        config = Config()
        config.agents.defaults.model_chain = ["invalid-no-slash"]
        result = validate_config(config)
        errors = [i for i in result.errors if "model_chain" in i.field]
        assert len(errors) > 0

    def test_budget_warning_threshold(self):
        config = Config()
        config.budget.enabled = True
        config.budget.max_budget_usd = 1.0
        config.budget.warn_threshold_usd = 5.0
        result = validate_config(config)
        warnings = [i for i in result.warnings if "warn_threshold" in i.field]
        assert len(warnings) > 0
