"""Generates the KnowPilot Grafana dashboard.

Written as code rather than exported from the interface: an exported JSON
carries the id of the machine it came from and hundreds of lines of defaults
nobody reads. This is the same dashboard, in a form a reviewer can check.
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "knowpilot-prometheus"}
panels: list[dict] = []
_next = [0]
_ref = [0]


def new_id() -> int:
    _next[0] += 1
    return _next[0]


def target(expr: str, legend: str = "", instant: bool = False) -> dict:
    _ref[0] += 1
    t: dict = {"datasource": DS, "expr": expr, "refId": chr(64 + (_ref[0] - 1) % 26 + 1)}
    if legend:
        t["legendFormat"] = legend
    if instant:
        t["instant"] = True
    return t


def row(title: str, y: int) -> None:
    panels.append(
        {
            "type": "row",
            "title": title,
            "id": new_id(),
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "collapsed": False,
            "panels": [],
        }
    )


def stat(
    title: str,
    expr: str,
    x: int,
    y: int,
    w: int = 6,
    unit: str = "short",
    description: str = "",
    mappings: list | None = None,
) -> None:
    panels.append(
        {
            "type": "stat",
            "title": title,
            "id": new_id(),
            "datasource": DS,
            "description": description,
            "gridPos": {"h": 4, "w": w, "x": x, "y": y},
            "targets": [target(expr, instant=True)],
            "fieldConfig": {
                "defaults": {"unit": unit, "mappings": mappings or []},
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "textMode": "auto",
                "colorMode": "value",
            },
        }
    )


def series(
    title: str,
    targets: list,
    x: int,
    y: int,
    unit: str = "short",
    description: str = "",
    stack: bool = False,
) -> None:
    panels.append(
        {
            "type": "timeseries",
            "title": title,
            "id": new_id(),
            "datasource": DS,
            "description": description,
            "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
            "targets": targets,
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "custom": {
                        "fillOpacity": 30 if stack else 8,
                        "lineWidth": 1,
                        "showPoints": "never",
                        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                    },
                },
                "overrides": [],
            },
            "options": {
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": "multi", "sort": "desc"},
            },
        }
    )


UP = [
    {
        "type": "value",
        "options": {
            "0": {"text": "DOWN", "color": "red", "index": 0},
            "1": {"text": "up", "color": "green", "index": 1},
        },
    }
]

# ----------------------------------------------------------- is it alive? --
row("Is it alive?", 0)
stat(
    "API",
    'up{job="api"}',
    0,
    1,
    w=4,
    description="A scrape that fails means nobody can log in or ask a question.",
    mappings=UP,
)
stat(
    "Worker",
    'up{job="worker"}',
    4,
    1,
    w=4,
    description=(
        "The API can answer every request while every upload rots in processing. "
        "This is the panel that says so."
    ),
    mappings=UP,
)
stat("Questions, last hour", "sum(increase(knowpilot_questions_total[1h]))", 8, 1, w=4)
stat(
    "Documents indexed, last 24h",
    'sum(increase(knowpilot_ingestions_total{outcome="indexed"}[24h]))',
    12,
    1,
    w=4,
)
stat(
    "Share refused, last hour",
    'sum(increase(knowpilot_questions_total{outcome=~"refused|nothing_found"}[1h]))'
    " / clamp_min(sum(increase(knowpilot_questions_total[1h])), 1)",
    16,
    1,
    w=4,
    unit="percentunit",
    description=(
        "Refusing is a valid answer, not a failure - but nearly always refusing "
        "means retrieval stopped finding anything."
    ),
)
stat(
    "Tokens, last 24h",
    "sum(increase(knowpilot_llm_tokens_total[24h]))",
    20,
    1,
    w=4,
    description=(
        "The free tier has a daily budget. Running out of it is an outage that "
        "looks like success until 4 pm."
    ),
)

# ---------------------------------------------------------------- answers --
row("Answers", 5)
series(
    "Questions by outcome",
    [target("sum by (outcome) (rate(knowpilot_questions_total[5m]))", "{{outcome}}")],
    0,
    6,
    unit="reqps",
    stack=True,
    description=(
        "answered, refused, nothing_found, busy, unavailable. A rise in refusals "
        "after a deployment is a regression no error rate would ever show."
    ),
)
series(
    "Passages admitted per question",
    [
        target(
            "histogram_quantile(0.5, sum by (le) (rate(knowpilot_retrieved_passages_bucket[15m])))",
            "p50",
        ),
        target(
            "histogram_quantile(0.95, sum by (le) "
            "(rate(knowpilot_retrieved_passages_bucket[15m])))",
            "p95",
        ),
    ],
    12,
    6,
    description="Zero passages means the question never reached the model.",
)
series(
    "Time to generate an answer (p95)",
    [
        target(
            "histogram_quantile(0.95, sum by (le, route) "
            "(rate(knowpilot_generation_duration_seconds_bucket[15m])))",
            "{{route}}",
        )
    ],
    0,
    14,
    unit="s",
    description="From the call to the model to the final verdict: the provider, not retrieval.",
)
series(
    "Answers resting on something the documents never name",
    [target("sum(rate(knowpilot_answers_with_unnamed_subject_total[15m]))", "unnamed subject")],
    12,
    14,
    unit="reqps",
    description=(
        "ADR-0018. Its share of answers measures how often people ask about things "
        "their documents only cover by category."
    ),
)

# ------------------------------------------------------------------- http --
row("HTTP", 22)
series(
    "Requests by route",
    [target("sum by (route) (rate(knowpilot_http_requests_total[5m]))", "{{route}}")],
    0,
    23,
    unit="reqps",
    stack=True,
)
series(
    "Failures by status",
    [
        target(
            'sum by (status) (rate(knowpilot_http_requests_total{status=~"4..|5.."}[5m]))',
            "{{status}}",
        )
    ],
    12,
    23,
    unit="reqps",
    stack=True,
    description="429 is our own limit working. 5xx is ours to fix.",
)
series(
    "Time to the start of the response (p95)",
    [
        target(
            "histogram_quantile(0.95, sum by (le, route) "
            "(rate(knowpilot_http_request_duration_seconds_bucket[15m])))",
            "{{route}}",
        )
    ],
    0,
    31,
    unit="s",
    description=(
        "To the response headers: a streamed answer stays open for seconds by "
        "design, and counting that would bury every slow endpoint under the chat."
    ),
)
series(
    "Refused by our own limits",
    [target("sum by (limit) (rate(knowpilot_requests_limited_total[15m]))", "{{limit}}")],
    12,
    31,
    unit="reqps",
    description="chat_rate, upload_rate, user_quota, service_quota.",
)

# -------------------------------------------------------------- ingestion --
row("Ingestion (the worker)", 39)
series(
    "Documents by outcome",
    [target("sum by (outcome) (rate(knowpilot_ingestions_total[30m]))", "{{outcome}}")],
    0,
    40,
    unit="reqps",
    stack=True,
    description=(
        "unsupported_format and no_text_found are people uploading scans - the "
        "product working. processing_error is ours."
    ),
)
series(
    "Time to index a document",
    [
        target(
            "histogram_quantile(0.5, sum by (le) "
            "(rate(knowpilot_ingestion_duration_seconds_bucket[30m])))",
            "p50",
        ),
        target(
            "histogram_quantile(0.95, sum by (le) "
            "(rate(knowpilot_ingestion_duration_seconds_bucket[30m])))",
            "p95",
        ),
    ],
    12,
    40,
    unit="s",
    description=(
        "The job times out at 600 s. A p95 climbing towards it means documents "
        "are being killed, not indexed."
    ),
)
series(
    "Passages produced per document",
    [
        target(
            "histogram_quantile(0.5, sum by (le) (rate(knowpilot_ingestion_chunks_bucket[1h])))",
            "p50",
        ),
        target(
            "histogram_quantile(0.95, sum by (le) (rate(knowpilot_ingestion_chunks_bucket[1h])))",
            "p95",
        ),
    ],
    0,
    48,
)
series(
    "Tokens per minute, by model and direction",
    [
        target(
            "sum by (model, direction) (rate(knowpilot_llm_tokens_total[15m]) * 60)",
            "{{model}} {{direction}}",
        )
    ],
    12,
    48,
    description="The budget that runs out is a daily one; this is the rate of burn.",
)

dashboard = {
    "uid": "knowpilot",
    "title": "KnowPilot",
    "description": (
        "What the service does, in numbers (ADR-0019, ADR-0021). Generated by "
        "infra/grafana/dashboard.py - change that, not this file."
    ),
    "tags": ["knowpilot"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "editable": True,
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
}

out = Path(__file__).resolve().parent / "dashboards" / "knowpilot.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8", newline="\n")
print(
    "panels:",
    len([p for p in panels if p["type"] != "row"]),
    "rows:",
    len([p for p in panels if p["type"] == "row"]),
)
