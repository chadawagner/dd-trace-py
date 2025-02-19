import pytest

from ddtrace.appsec._constants import IAST
from ddtrace.appsec._iast._taint_tracking import OriginType
from ddtrace.appsec._iast._taint_tracking import is_pyobject_tainted
from ddtrace.appsec._iast._taint_tracking import taint_pyobject
from ddtrace.appsec._iast.constants import VULN_SQL_INJECTION
from ddtrace.internal import core
from tests.appsec.iast.aspects.conftest import _iast_patched_module
from tests.appsec.iast.iast_utils import get_line_and_hash
from tests.utils import override_env


FIXTURES_PATH = "tests/appsec/iast/fixtures/taint_sinks/sql_injection.py"


def test_sql_injection(iast_span_defaults):
    mod = _iast_patched_module("tests.appsec.iast.fixtures.taint_sinks.sql_injection")
    table = taint_pyobject(
        pyobject="students",
        source_name="test_ossystem",
        source_value="students",
        source_origin=OriginType.PARAMETER,
    )
    assert is_pyobject_tainted(table)

    mod.sqli_simple(table)
    span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
    assert span_report

    assert len(span_report.vulnerabilities) == 1
    vulnerability = list(span_report.vulnerabilities)[0]
    source = span_report.sources[0]
    assert vulnerability.type == VULN_SQL_INJECTION
    assert vulnerability.evidence.valueParts == [{"value": "SELECT 1 FROM "}, {"source": 0, "value": "students"}]
    assert vulnerability.evidence.value is None
    assert vulnerability.evidence.pattern is None
    assert vulnerability.evidence.redacted is None
    assert source.name == "test_ossystem"
    assert source.origin == OriginType.PARAMETER
    assert source.value == "students"

    line, hash_value = get_line_and_hash("test_sql_injection", VULN_SQL_INJECTION, filename=FIXTURES_PATH)
    assert vulnerability.location.line == line
    assert vulnerability.location.path == FIXTURES_PATH
    assert vulnerability.hash == hash_value


@pytest.mark.parametrize("num_vuln_expected", [1, 0, 0])
def test_sql_injection_deduplication(num_vuln_expected, iast_span_defaults):
    mod = _iast_patched_module("tests.appsec.iast.fixtures.taint_sinks.sql_injection")
    with override_env(dict(_DD_APPSEC_DEDUPLICATION_ENABLED="true")):
        table = taint_pyobject(
            pyobject="students",
            source_name="test_ossystem",
            source_value="students",
            source_origin=OriginType.PARAMETER,
        )
        assert is_pyobject_tainted(table)
        for _ in range(0, 5):
            mod.sqli_simple(table)

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)

        if num_vuln_expected == 0:
            assert span_report is None
        else:
            assert span_report

            assert len(span_report.vulnerabilities) == num_vuln_expected
