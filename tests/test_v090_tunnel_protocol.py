"""Portable regression for bounded Cloudflare connectivity precheck classification."""
from local_dev_mcp_bridge.tunnel_manager import TunnelManager


def _tail(*entries: str) -> str:
    return "\n".join(entries + ("precheck complete",))


def test_precheck_recommends_http2_only_when_both_udp_regions_fail_and_tcp_works():
    data = _tail(
        'precheck component="udp connectivity" target=region1.v2.argotunnel.com status=fail',
        'precheck component="udp connectivity" target=region2.v2.argotunnel.com status=fail',
        'precheck component="tcp connectivity" target=region1.v2.argotunnel.com status=pass',
    )
    assert TunnelManager._parse_cloudflare_protocol_hint(data) == "http2"


def test_precheck_does_not_assume_http2_on_one_udp_failure():
    data = _tail(
        'precheck component="udp connectivity" target=region1.v2.argotunnel.com status=fail',
        'precheck component="tcp connectivity" target=region1.v2.argotunnel.com status=pass',
    )
    assert TunnelManager._parse_cloudflare_protocol_hint(data) == ""


def test_precheck_does_not_assume_http2_if_udp_succeeded_or_tcp_failed():
    data = _tail(
        'precheck component="udp connectivity" target=region1.v2.argotunnel.com status=fail',
        'precheck component="udp connectivity" target=region2.v2.argotunnel.com status=fail',
        'precheck component="udp connectivity" target=region1.v2.argotunnel.com status=pass',
        'precheck component="tcp connectivity" target=region1.v2.argotunnel.com status=fail',
    )
    assert TunnelManager._parse_cloudflare_protocol_hint(data) == ""


def test_precheck_absent_completion_yields_no_inference():
    data = (
        'precheck component="udp connectivity" target=region1.v2.argotunnel.com status=fail\n'
        'precheck component="udp connectivity" target=region2.v2.argotunnel.com status=fail\n'
        'precheck component="tcp connectivity" target=region1.v2.argotunnel.com status=pass'
    )
    assert TunnelManager._parse_cloudflare_protocol_hint(data) == ""
