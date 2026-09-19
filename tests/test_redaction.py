from autocab.framework.components import SensitiveDataRedactor
from autocab.framework.config import PipelineConfig


def test_redaction_masks_sensitive_patterns():
    redactor = SensitiveDataRedactor(PipelineConfig().redact_deny_terms)
    report = redactor.redact_text("Send to analyst@example.org with MRN 123456 and DOB 2012-06-01.")

    assert "[REDACTED_EMAIL]" in report.redacted_text
    assert "[REDACTED_MRN]" in report.redacted_text
    assert "[REDACTED_DOB]" in report.redacted_text
    assert set(report.findings) >= {"email", "mrn", "dob"}
