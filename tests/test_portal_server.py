import json
from pathlib import Path

import pytest

from robotenon.infra.trace import TraceDaemon
from robotenon.web.server import DEFAULT_GRAFANA_URL, HTML as HTTP_HTML, PortalData
from robotenon.web.page import render_page, portal_assets

HTML = render_page(inline=True)


def test_default_grafana_url_keeps_editor_controls_visible():
    assert "kiosk=1" not in DEFAULT_GRAFANA_URL
    assert "refresh=5s" in DEFAULT_GRAFANA_URL


def test_http_shell_uses_versioned_same_origin_assets():
    assert len(HTTP_HTML.encode()) < 40000
    assert '<script defer src="/assets/' in HTTP_HTML
    assert '<link rel="stylesheet" href="/assets/' in HTTP_HTML
    for url, (payload, _) in portal_assets().items():
        from hashlib import sha256
        assert sha256(payload).hexdigest()[:16] in url
        assert url in HTTP_HTML


def test_metrics_tab_is_grafana_only():
    metrics_section = HTML.split('<section id="detail-monitoring"', 1)[1].split('<section id="detail-traces"', 1)[0]
    assert 'id="grafana"' in metrics_section
    assert "全部指标" not in metrics_section
    assert "Prometheus 文本" not in metrics_section
    assert ".grafana-only{height:calc(100vh - 126px);" in HTML
    assert "overflow:auto" in HTML
    assert "min-width:1100px" in HTML
    assert '<details class="metrics-log-card"' in HTML
    assert 'id="metrics-log-search"' in HTML
    for field in ("metrics-filter-name", "metrics-filter-kind", "metrics-filter-scope", "metrics-filter-environment", "metrics-filter-group", "metrics-filter-type", "metrics-filter-label"):
        assert f'id="{field}"' in HTML
    assert "parseLabelFilters" in HTML
    assert "getJson('/api/metrics?limit=300')" in HTML
    assert "'/api/traces')+'?limit=100&offset='" in HTML
    # Refreshing is single-flight and scheduled adaptively with setTimeout;
    # a fixed interval could stack slow requests over the SSH tunnel.
    assert "setInterval(refresh,2000)" not in HTML
    assert "refreshTimer=document.hidden?null:setTimeout(refresh" in HTML
    assert ".grafana-expand" in HTML


def test_experiment_config_submission_tab_is_present():
    assert 'id="experiments-view"' in HTML
    assert 'id="create-experiment"' in HTML
    assert 'id="experiment-config"' in HTML
    assert 'id="experiment-submit"' in HTML
    assert 'id="base-config-json"' in HTML
    assert 'id="custom-config-json"' in HTML
    assert 'id="merged-config-json"' in HTML
    assert 'id="detail-parameters"' in HTML
    assert 'id="graph-scroll"' in HTML
    assert 'id="graph-edge-tooltip"' in HTML
    assert "cursor:help" not in HTML
    assert "data-condition" in HTML
    assert ".graph-edge-label{display:none" in HTML
    assert ".graph-edge-group:hover .graph-edge-label" in HTML
    assert "primary_graph" in HTML
    assert "getJson('/api/experiments'" in HTML
    assert "getJson('/api/configs')" in HTML
    assert "getJson('/api/configs/'+encodeURIComponent" in HTML


def test_experiment_detail_has_run_trajectory_monitoring_and_trace_views():
    for view_id in ("detail-run", "detail-trajectory", "detail-monitoring", "detail-traces"):
        assert f'id="{view_id}"' in HTML
    assert "/episodes" in HTML
    assert 'id="trajectory-task-metrics"' not in HTML
    assert "getJson('/api/metrics?limit=1000')" not in HTML
    assert 'id="trajectory-media-mode"' in HTML
    assert 'id="trajectory-node-info"' in HTML
    assert "subtask 详情（原始 JSON）" in HTML
    assert "trajectorySelectedSequenceOrder" in HTML
    assert 'id="language-toggle"' in HTML
    assert 'id="theme-toggle"' in HTML
    assert 'id="detail-terminal"' in HTML
    assert 'id="detail-terminal-status"' in HTML
    assert 'id="detail-terminal-follow"' in HTML
    assert 'id="episode-since"' not in HTML
    assert 'id="episode-until"' not in HTML
    assert 'id="episode-page-prev"' not in HTML
    assert 'id="episode-page-next"' not in HTML
    assert "limit=500&offset=" in HTML
    assert 'id="detail-failure-reason"' in HTML
    assert "'/terminal?limit=65536'" in HTML
    assert "terminalBufferLimit=512*1024" in HTML
    assert 'data-i18n="experimentsTitle"' in HTML
    assert "portalText" in HTML
    assert "data-theme=dark" in HTML
    assert 'id="trace-query-preset"' not in HTML
    assert 'id="trace-schema-list"' not in HTML
    assert 'id="sql"' in HTML
    assert 'id="run-sql"' in HTML
    assert 'id="trajectory-gantt-fullscreen"' in HTML
    # A vis-timeline instance starts with an inline visibility:hidden while it
    # waits for its first redraw.  The portal must force a successful initial
    # render visible and fall back to the DOM timeline when native rendering
    # fails, otherwise the first trajectory view appears empty.
    assert "visibility:visible!important" in HTML
    assert "trajectoryGanttNativeFailedSignature=signature" in HTML
    assert "调用时间线加载失败" in HTML
    assert "trajectoryGanttToggleSection" in HTML
    assert "trajectoryGanttApplyCollapsedNativeGroups" in HTML
    assert ".trajectory-gantt-block-body.is-collapsed" in HTML
    assert "trajectoryGanttVisBindLayoutObserver" in HTML
    assert "ResizeObserver" in HTML
    assert ".trajectory-gantt-vis-section-toggle" in HTML
    assert "mergeTrajectoryProgressEpisodes" in HTML
    assert "trajectoryProgressEpisodes" in HTML
    assert "trajectoryMediaGroupSignature" in HTML
    assert "trajectoryPlaybackCursor" in HTML
    assert "Promise.all(requests.map" in HTML
    assert "trajectoryUserScrollGeneration" in HTML
    assert "detailRefreshInterval=5000" in HTML
    assert "trajectoryRefreshInterval=1500" in HTML
    assert "format=jpeg" in HTML


def test_trajectory_task_controls_and_adapter_back_navigation_are_present():
    for control_id in (
        "trajectory-back-adapters",
        "trajectory-stop",
        "trajectory-rerun",
        "trajectory-shutdown",
    ):
        assert f'id="{control_id}"' in HTML
    assert "/api/system/shutdown" in HTML
    assert "/stop" in HTML
    assert "applyExperimentControl('rerun',true)" in HTML
    # The adapter heading/count belongs to the adapter list, not the task
    # browser header; the browser should show the task list directly.
    assert 'id="trajectory-adapter-title"' not in HTML
    assert 'id="trajectory-adapter-summary"' not in HTML


def test_experiment_list_has_graph_json_preview_and_fork_action():
    assert 'id="graph-json-button"' in HTML
    assert 'id="graph-json-input"' in HTML
    assert 'id="graph-json-svg"' in HTML
    assert "drawGraph(graph,'graph-json-svg','graph-json-edge-tooltip')" in HTML
    assert "fork-experiment" in HTML
    assert "/fork" in HTML


def test_grafana_dashboard_has_windowed_average_panels():
    dashboard = json.loads((Path(__file__).parents[1] / "docker/grafana/dashboards/robotenon-overview.json").read_text())
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}
    assert "avg_window" not in variables
    expressions = [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]
    assert any("rate(robotenon_actions_total" in expr and "[5m]" in expr for expr in expressions)
    assert any("robotenon_action_latency_ms_sum" in expr and "[5m]" in expr for expr in expressions)
    assert any("histogram_quantile(0.95" in expr and "robotenon_action_latency_ms_bucket" in expr for expr in expressions)
    assert any("histogram_quantile(0.90" in expr and "robotenon_action_latency_ms_bucket" in expr for expr in expressions)
    assert any("histogram_quantile(0.95" in expr and "robotenon_feedback_age_ms_bucket" in expr for expr in expressions)
    assert any("histogram_quantile(0.90" in expr and "robotenon_feedback_age_ms_bucket" in expr for expr in expressions)
    assert sum("last_over_time(robotenon_action_latency_ms_bucket" in expr for expr in expressions) == 2
    assert sum("last_over_time(robotenon_feedback_age_ms_bucket" in expr for expr in expressions) == 2
    assert all("robotenon_action_latency_ms_count" in expr for expr in expressions if "last_over_time(robotenon_action_latency_ms_bucket" in expr)
    assert not any("robotenon_action_latency_p95_ms" in expr or "robotenon_feedback_age_p95_ms" in expr for expr in expressions)
    assert any("Panel → Edit → Query" in panel.get("description", "") for panel in dashboard["panels"])


def test_portal_data_and_read_only_trace_sql(tmp_path):
    trace = TraceDaemon(tmp_path / "trace.jsonl")
    trace.ingest({"name": "runner.act", "status": "ok"})
    trace.close()
    data = PortalData(tmp_path / "metrics.jsonl", tmp_path / "trace.jsonl", run_root=tmp_path)
    assert data.traces.sql("SELECT name FROM spans")[0]["name"] == "runner.act"
    with pytest.raises(ValueError):
        data.traces.sql("WITH x AS (SELECT 1) DELETE FROM spans")
    data.traces.close()


def test_observation_preview_uses_small_cached_jpeg(tmp_path):
    numpy = pytest.importorskip("numpy")
    path = tmp_path / "front.npy"
    numpy.save(path, numpy.zeros((32, 32, 3), dtype=numpy.uint8))

    first = PortalData.image_from_path(path, encoding="npy", output_format="jpeg")
    second = PortalData.image_from_path(path, encoding="npy", output_format="jpeg")

    assert first is not None
    assert first[1] == "image/jpeg"
    assert first[0].startswith(b"\xff\xd8")
    assert second is first


def test_media_http_images_and_video_ranges(tmp_path):
    import http.client
    import threading
    from types import SimpleNamespace
    from robotenon.web.server import build_server

    video = tmp_path / "video.mp4"
    video.write_bytes(bytes(range(256)) * 8)
    calls = []

    def image(uri, *, output_format):
        calls.append((uri, output_format))
        return b"jpeg-preview", "image/jpeg"

    server = build_server(
        "127.0.0.1", 0,
        SimpleNamespace(observation_image=image, traces=None),
        experiment_manager=SimpleNamespace(episode_video_path=lambda *args, **kwargs: video),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        client.request("GET", "/api/observations/image?uri=cache%3A%2F%2Ftest&format=jpeg")
        response = client.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "image/jpeg"
        assert response.read() == b"jpeg-preview"
        assert calls == [("cache://test", "jpeg")]
        client.close()
        for url, (payload, content_type) in portal_assets().items():
            import gzip
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            client.request("GET", url, headers={"Accept-Encoding": "gzip"})
            response = client.getresponse()
            assert response.status == 200
            assert "immutable" in response.getheader("Cache-Control")
            assert response.getheader("Content-Type") == content_type
            assert gzip.decompress(response.read()) == payload
            client.close()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        client.request("GET", "/assets/../../server.py")
        response = client.getresponse()
        assert response.status == 404
        response.read()
        client.close()
        for header, expected in [("bytes=0-63", video.read_bytes()[:64]), ("bytes=-32", video.read_bytes()[-32:])]:
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            client.request("GET", "/api/experiments/job/media?adapter_id=test&global_index=0", headers={"Range": header})
            response = client.getresponse()
            assert response.status == 206
            assert response.getheader("Content-Type") == "video/mp4"
            assert response.getheader("Accept-Ranges") == "bytes"
            assert response.read() == expected
            client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_terminal_http_endpoint_forwards_incremental_cursor():
    import http.client
    import threading
    from types import SimpleNamespace

    from robotenon.web.server import build_server

    calls = []

    def terminal_output(job_id, *, offset, limit):
        calls.append((job_id, offset, limit))
        return {
            "job_id": job_id,
            "text": "failure detail\n",
            "offset": offset,
            "next_offset": 19,
            "size": 19,
            "complete": True,
        }

    manager = SimpleNamespace(terminal_output=terminal_output)
    server = build_server(
        "127.0.0.1",
        0,
        SimpleNamespace(traces=None),
        experiment_manager=manager,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        client.request("GET", "/api/experiments/exp-1/terminal?offset=4&limit=12")
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["text"] == "failure detail\n"
        assert calls == [("exp-1", 4, 12)]
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
