#!/usr/bin/env python3
import argparse
import json
import shlex
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from statistics import median
from textwrap import shorten
from typing import Any, Dict, Iterable, List, Optional, Tuple


AUTH_COOKIE_NAMES = {"WorkosCursorSessionToken"}
DEFAULT_COOKIE_ALLOWLIST = {"WorkosCursorSessionToken"}

BOLD = "\033[1m"
CYAN = "\033[36m"
DIM = "\033[2m"
RESET = "\033[0m"

DEBUG = False
USE_COLOR = True


def _debug(msg: str) -> None:
    if DEBUG:
        print(msg, file=sys.stderr)


def parse_curl_command(curl_command: str) -> Tuple[str, Dict[str, str], Optional[bytes]]:
    tokens = shlex.split(curl_command)
    if not tokens or tokens[0] != "curl":
        raise ValueError("Expected curl command starting with 'curl'.")

    headers: Dict[str, str] = {}
    data_bytes: Optional[bytes] = None
    url: Optional[str] = None

    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in {"-H", "--header"}:
            i += 1
            header = tokens[i]
            if ":" in header:
                name, value = header.split(":", 1)
                headers[name.strip()] = value.strip()
        elif token in {"-b", "--cookie"}:
            i += 1
            cookie_value = tokens[i]
            existing = headers.get("Cookie")
            headers["Cookie"] = f"{existing}; {cookie_value}" if existing else cookie_value
        elif token in {"--data-raw", "--data", "--data-binary"}:
            i += 1
            data_bytes = tokens[i].encode("utf-8")
        elif token.startswith("http"):
            url = token
        i += 1

    if not url:
        raise ValueError("Could not find URL in curl command.")

    return url, headers, data_bytes


def parse_cookie_header(cookie_header: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def filter_cookies(
    cookies: Dict[str, str],
    allowlist: Optional[Iterable[str]],
    keep_all: bool,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    if keep_all or not allowlist:
        return cookies, {}

    allowlist_set = {name.strip() for name in allowlist if name.strip()}
    kept = {k: v for k, v in cookies.items() if k in allowlist_set}
    omitted = {k: v for k, v in cookies.items() if k not in allowlist_set}

    if not kept:
        return cookies, {}

    return kept, omitted


def build_cookie_header(cookies: Dict[str, str]) -> Optional[str]:
    if not cookies:
        return None
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def clean_headers(headers: Dict[str, str], minimal: bool) -> Dict[str, str]:
    if not minimal:
        return headers

    drop_headers = {
        "accept-language",
        "priority",
        "sec-ch-ua",
        "sec-ch-ua-arch",
        "sec-ch-ua-bitness",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-ch-ua-platform-version",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-gpc",
    }

    filtered = {}
    for key, value in headers.items():
        if key.lower() in drop_headers:
            continue
        filtered[key] = value
    return filtered


def request_json(
    url: str,
    headers: Dict[str, str],
    data_bytes: Optional[bytes],
    method: str = "POST",
) -> Any:
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def get_first(obj: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return None


def find_events(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = [
        "events",
        "usageEvents",
        "items",
        "usageEventsDisplay",
    ]
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, list):
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in candidates:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def find_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidates = [
        "summary",
        "totals",
        "usageSummary",
    ]
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, dict):
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in candidates:
            value = data.get(key)
            if isinstance(value, dict):
                return value

    return {}


def to_money(value: Any, key_hint: str = "") -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str):
        try:
            amount = float(value)
        except ValueError:
            return None
    else:
        return None

    if "cent" in key_hint.lower():
        return amount / 100.0
    return amount


def parse_money_like(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in {"-", ""}:
            return None
        if stripped.startswith("$"):
            stripped = stripped[1:]
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def get_nested(obj: Dict[str, Any], path: Iterable[str]) -> Any:
    current: Any = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def pick_money(obj: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        value = obj.get(key)
        money = to_money(value, key)
        if money is not None:
            return money
    return None


def pick_int(obj: Dict[str, Any], keys: Iterable[str]) -> Optional[int]:
    value = get_first(obj, keys)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_money(amount: Optional[float]) -> str:
    if amount is None:
        return "n/a"
    return f"${amount:,.4f}".rstrip("0").rstrip(".")


def format_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        if value > 1e12:
            value = value / 1000.0
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(value, str):
        if value.isdigit():
            num = int(value)
            if num > 1e12:
                num = num / 1000.0
            dt = datetime.fromtimestamp(num, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            return value
    return "unknown"


def parse_timestamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        num = float(value)
        return num / 1000.0 if num > 1e12 else num
    if isinstance(value, str) and value.isdigit():
        num = float(value)
        return num / 1000.0 if num > 1e12 else num
    return None


def classify_event(event: Dict[str, Any]) -> str:
    usage_type = get_first(event, ["usageType", "pricingType", "requestType"])
    if isinstance(usage_type, str) and usage_type.lower() in {"on_demand", "ondemand"}:
        return "on-demand"

    if get_first(event, ["isOnDemand", "onDemand"]) is True:
        return "on-demand"

    on_demand_cost = pick_money(
        event,
        [
            "onDemandCostUsd",
            "onDemandCost",
            "onDemandCostCents",
            "onDemandUsageUsd",
            "onDemandUsageCents",
            "onDemandSpendUsd",
            "onDemandSpendCents",
        ],
    )
    if on_demand_cost is not None:
        return "on-demand"

    included_requests = pick_int(
        event,
        [
            "includedRequests",
            "includedRequestCount",
            "includedRequestsUsed",
            "included_request_count",
        ],
    )
    if included_requests:
        return "included"

    kind = get_first(event, ["kind"])
    if isinstance(kind, str):
        upper = kind.upper()
        if "USAGE_BASED" in upper or "ON_DEMAND" in upper:
            return "on-demand"
        if "INCLUDED" in upper:
            return "included"
        if "ERRORED" in upper:
            return "errored"

    return "unknown"


def is_token_based_call(event: Dict[str, Any]) -> bool:
    """Return True when the event is marked as a token-based call."""
    value = get_first(event, ["isTokenBasedCall", "is_token_based_call"])
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def summarize_events(events: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    def event_sort_key(item: Dict[str, Any]) -> float:
        ts = get_first(item, ["createdAt", "timestamp", "time"])
        parsed = parse_timestamp(ts)
        if parsed is not None:
            return parsed
        return 0.0

    events_sorted = sorted(events, key=event_sort_key, reverse=True)[:limit]
    summaries = []

    for event in events_sorted:
        model = get_first(
            event,
            ["model", "modelName", "model_name", "model_id"],
        )
        if not model and isinstance(event.get("metadata"), dict):
            model = event["metadata"].get("model")
        model = model or "unknown"

        usage_kind = classify_event(event)
        on_demand_cost = pick_money(
            event,
            [
                "onDemandCostUsd",
                "onDemandCost",
                "onDemandCostCents",
                "onDemandUsageUsd",
                "onDemandUsageCents",
                "onDemandSpendUsd",
                "onDemandSpendCents",
            ],
        )
        if on_demand_cost is None:
            on_demand_cost = parse_money_like(event.get("usageBasedCosts"))
        token_usage_cost = pick_money(
            {"tokenUsageTotalCents": get_nested(event, ["tokenUsage", "totalCents"])},
            ["tokenUsageTotalCents"],
        )

        included_requests = pick_int(
            event,
            [
                "includedRequests",
                "includedRequestCount",
                "includedRequestsUsed",
                "included_request_count",
                "requestsCosts",
            ],
        )
        included_cost = pick_money(
            event,
            [
                "includedRequestsCostUsd",
                "includedCostUsd",
                "includedCost",
                "includedRequestsCostCents",
            ],
        )
        if included_cost is None:
            included_cost = pick_money(
                {"tokenUsageTotalCents": get_nested(event, ["tokenUsage", "totalCents"])},
                ["tokenUsageTotalCents"],
            )
        event_cost = pick_money(
            event,
            [
                "costUsd",
                "cost",
                "costCents",
            ],
        )
        if event_cost is None:
            event_cost = pick_money(
                {"tokenUsageTotalCents": get_nested(event, ["tokenUsage", "totalCents"])},
                ["tokenUsageTotalCents"],
            )

        if usage_kind == "on-demand":
            usage_cost_display = format_money(on_demand_cost)
            token_cost_display = format_money(token_usage_cost)
            usage_display = f"on-demand {usage_cost_display} ({token_cost_display})"
        elif usage_kind == "included":
            reqs_display = str(included_requests) if included_requests is not None else "n/a"
            cost_display = format_money(included_cost or event_cost)
            usage_display = f"included {reqs_display} reqs ({cost_display})"
        elif usage_kind == "errored":
            usage_display = "errored (not charged)"
        else:
            cost_display = format_money(event_cost)
            usage_display = f"unknown ({cost_display})"

        timestamp = format_timestamp(get_first(event, ["createdAt", "timestamp", "time"]))
        tokens = extract_token_breakdown(event)
        summaries.append(
            {
                "time": timestamp,
                "model": str(model),
                "usage": usage_display,
                "input_tokens": tokens["input"],
                "output_tokens": tokens["output"],
                "cache_read": tokens["cache_read"],
                "cache_write": tokens["cache_write"],
                "is_token_based_call": is_token_based_call(event),
            }
        )

    return summaries


def build_summary(payload: Dict[str, Any], events: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[float]]:
    summary = find_summary(payload)

    requests_consumed = pick_int(
        summary,
        [
            "requestsConsumed",
            "requestsUsed",
            "requestCount",
            "totalRequests",
            "totalRequestCount",
            "includedRequestsUsed",
            "includedRequestCount",
        ],
    )

    on_demand_usage = pick_money(
        summary,
        [
            "onDemandUsageUsd",
            "onDemandUsage",
            "onDemandCostUsd",
            "onDemandCost",
            "onDemandSpendUsd",
            "onDemandSpend",
            "onDemandUsageCents",
            "onDemandCostCents",
            "onDemandSpendCents",
        ],
    )
    if on_demand_usage is None:
        nested_candidates = [
            ("onDemandUsage", "totalUsd"),
            ("onDemandUsage", "totalCents"),
            ("onDemandUsage", "total"),
            ("onDemandCost", "totalUsd"),
            ("onDemandCost", "totalCents"),
            ("onDemandCost", "total"),
            ("onDemandSpend", "totalUsd"),
            ("onDemandSpend", "totalCents"),
            ("onDemandSpend", "total"),
        ]
        for path in nested_candidates:
            nested_value = get_nested(summary, path)
            money = to_money(nested_value, path[-1])
            if money is not None:
                on_demand_usage = money
                break

    if on_demand_usage is None:
        costs = []
        for event in events:
            cost = pick_money(
                event,
                [
                    "onDemandCostUsd",
                    "onDemandCost",
                    "onDemandCostCents",
                    "onDemandUsageUsd",
                    "onDemandUsageCents",
                    "onDemandSpendUsd",
                    "onDemandSpendCents",
                ],
            )
            if cost is None:
                cost = parse_money_like(event.get("usageBasedCosts"))
            if cost is not None:
                costs.append(cost)
        if costs:
            on_demand_usage = sum(costs)

    if requests_consumed is None:
        counts = []
        for event in events:
            count = pick_int(
                event,
                [
                    "requestsConsumed",
                    "requestCount",
                    "requests",
                    "includedRequests",
                    "includedRequestCount",
                    "requestsCosts",
                ],
            )
            if count is not None:
                counts.append(count)
        if counts:
            requests_consumed = sum(counts)

    return requests_consumed, on_demand_usage


def classify_cookies(cookies: Dict[str, str]) -> Tuple[List[str], List[str]]:
    auth = []
    other = []
    for name in cookies.keys():
        if name in AUTH_COOKIE_NAMES or "session" in name.lower() or "token" in name.lower():
            auth.append(name)
        else:
            other.append(name)
    return auth, other


def event_model_label(row: Dict[str, Any]) -> str:
    model = str(row.get("model") or "unknown")
    if row.get("is_token_based_call"):
        return f"{model} [M]"
    return model


def format_table(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return []
    headers = ["Time (UTC)", "Model", "Usage"]
    model_values = [event_model_label(r) for r in rows]
    widths = [
        max(len(headers[0]), max(len(r["time"]) for r in rows)),
        max(len(headers[1]), max(len(v) for v in model_values)),
        max(len(headers[2]), max(len(r["usage"]) for r in rows)),
    ]
    total_width = sum(widths) + 10
    lines = [f"+{'-' * (total_width - 2)}+"]
    header_row = f"| {headers[0]:<{widths[0]}} | {headers[1]:<{widths[1]}} | {headers[2]:<{widths[2]}} |"
    lines.append(header_row)
    lines.append(f"+{'-' * (total_width - 2)}+")
    for row in rows:
        model = shorten(event_model_label(row), width=widths[1], placeholder="...")
        usage = shorten(row["usage"], width=widths[2], placeholder="...")
        lines.append(
            f"| {row['time']:<{widths[0]}} | {model:<{widths[1]}} | {usage:<{widths[2]}} |"
        )
    lines.append(f"+{'-' * (total_width - 2)}+")
    return lines


def format_models_table(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return []
    headers = ["Rank", "Model", "Requests", "Total spend"]
    rank_values = [str(i) for i in range(1, len(rows) + 1)]
    model_values = [shorten(str(r["model"]), width=40, placeholder="...") for r in rows]
    request_values = [str(r.get("requests", 0)) for r in rows]
    cost_values = [format_money(r.get("cost")) for r in rows]

    widths = [
        max(len(headers[0]), max(len(v) for v in rank_values)),
        max(len(headers[1]), max(len(v) for v in model_values)),
        max(len(headers[2]), max(len(v) for v in request_values)),
        max(len(headers[3]), max(len(v) for v in cost_values)),
    ]
    total_width = sum(widths) + 14
    lines = [f"+{'-' * (total_width - 2)}+"]
    header_row = (
        f"| {headers[0]:<{widths[0]}} | {headers[1]:<{widths[1]}} | "
        f"{headers[2]:<{widths[2]}} | {headers[3]:<{widths[3]}} |"
    )
    lines.append(header_row)
    lines.append(f"+{'-' * (total_width - 2)}+")
    for idx, row in enumerate(rows, start=1):
        model = shorten(str(row["model"]), width=widths[1], placeholder="...")
        requests = str(row.get("requests", 0))
        cost = format_money(row.get("cost"))
        lines.append(
            f"| {idx:<{widths[0]}} | {model:<{widths[1]}} | {requests:<{widths[2]}} | {cost:<{widths[3]}} |"
        )
    lines.append(f"+{'-' * (total_width - 2)}+")
    return lines


def format_expensive_table(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return []
    headers = ["Rank", "Model", "Tokens", "Cost", "Time (UTC)"]
    rank_values = [str(i) for i in range(1, len(rows) + 1)]
    model_values = [shorten(str(r["model"]), width=40, placeholder="...") for r in rows]
    token_values = [format_tokens(r.get("tokens")) for r in rows]
    cost_values = [format_money(r.get("cost")) for r in rows]
    time_values = [str(r.get("time") or "n/a") for r in rows]

    widths = [
        max(len(headers[0]), max(len(v) for v in rank_values)),
        max(len(headers[1]), max(len(v) for v in model_values)),
        max(len(headers[2]), max(len(v) for v in token_values)),
        max(len(headers[3]), max(len(v) for v in cost_values)),
        max(len(headers[4]), max(len(v) for v in time_values)),
    ]
    total_width = sum(widths) + 20
    lines = [f"+{'-' * (total_width - 2)}+"]
    header_row = (
        f"| {headers[0]:<{widths[0]}} | {headers[1]:<{widths[1]}} | "
        f"{headers[2]:<{widths[2]}} | {headers[3]:<{widths[3]}} | "
        f"{headers[4]:<{widths[4]}} |"
    )
    lines.append(header_row)
    lines.append(f"+{'-' * (total_width - 2)}+")
    for idx, row in enumerate(rows, start=1):
        model = shorten(str(row["model"]), width=widths[1], placeholder="...")
        tokens = format_tokens(row.get("tokens"))
        cost = format_money(row.get("cost"))
        time = str(row.get("time") or "n/a")
        lines.append(
            f"| {idx:<{widths[0]}} | {model:<{widths[1]}} | {tokens:<{widths[2]}} | "
            f"{cost:<{widths[3]}} | {time:<{widths[4]}} |"
        )
    lines.append(f"+{'-' * (total_width - 2)}+")
    return lines


def style(text: str, *codes: str) -> str:
    if not USE_COLOR or not codes:
        return text
    return f"{''.join(codes)}{text}{RESET}"


def event_cost(event: Dict[str, Any]) -> Optional[float]:
    on_demand_cost = pick_money(
        event,
        [
            "onDemandCostUsd",
            "onDemandCost",
            "onDemandCostCents",
            "onDemandUsageUsd",
            "onDemandUsageCents",
            "onDemandSpendUsd",
            "onDemandSpendCents",
        ],
    )
    if on_demand_cost is None:
        on_demand_cost = pick_money(
            {"tokenUsageTotalCents": get_nested(event, ["tokenUsage", "totalCents"])},
            ["tokenUsageTotalCents"],
        )
    if on_demand_cost is not None:
        return on_demand_cost

    included_cost = pick_money(
        event,
        [
            "includedRequestsCostUsd",
            "includedCostUsd",
            "includedCost",
            "includedRequestsCostCents",
        ],
    )
    if included_cost is None:
        included_cost = pick_money(
            {"tokenUsageTotalCents": get_nested(event, ["tokenUsage", "totalCents"])},
            ["tokenUsageTotalCents"],
        )
    return included_cost


def event_request_cost(event: Dict[str, Any]) -> Optional[int]:
    return pick_int(
        event,
        [
            "requestsCosts",
            "includedRequests",
            "includedRequestCount",
            "requestsConsumed",
        ],
    )


def latest_events(events: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    def event_sort_key(item: Dict[str, Any]) -> float:
        ts = get_first(item, ["createdAt", "timestamp", "time"])
        parsed = parse_timestamp(ts)
        return parsed if parsed is not None else 0.0

    return sorted(events, key=event_sort_key, reverse=True)[:limit]


def compute_stats(events: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
    recent = latest_events(events, limit)
    costs: List[float] = []
    requests = 0
    for event in recent:
        cost = event_cost(event)
        if cost is not None:
            costs.append(cost)
        reqs = event_request_cost(event)
        if reqs is not None:
            requests += reqs
    median_cost = median(costs) if costs else None
    total_cost = sum(costs) if costs else None
    return {
        "count": len(recent),
        "median_cost": median_cost,
        "total_cost": total_cost,
        "requests": requests,
    }


def total_tokens_used(event: Dict[str, Any]) -> Optional[int]:
    token_usage = event.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return None
    total = 0
    found = False
    for value in token_usage.values():
        if isinstance(value, (int, float)):
            total += int(value)
            found = True
    return total if found else None


def extract_token_breakdown(event: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Extract individual token counts from an event's tokenUsage dict."""
    token_usage = event.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return {"input": None, "output": None, "cache_read": None, "cache_write": None}

    def _pick_int(*keys: str) -> Optional[int]:
        for k in keys:
            v = token_usage.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return None

    return {
        "input": _pick_int("inputTokens", "input_tokens", "promptTokens", "prompt_tokens"),
        "output": _pick_int("outputTokens", "output_tokens", "completionTokens", "completion_tokens"),
        "cache_read": _pick_int("cacheReadTokens", "cache_read_tokens", "cacheReadInputTokens"),
        "cache_write": _pick_int("cacheCreationTokens", "cache_creation_tokens", "cacheCreationInputTokens", "cacheWriteTokens"),
    }


def most_expensive_request(events: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
    recent = latest_events(events, limit)
    best: Dict[str, Any] = {}
    best_cost: Optional[float] = None
    for event in recent:
        cost = event_cost(event)
        if cost is None:
            continue
        if best_cost is None or cost > best_cost:
            best_cost = cost
            best = event
    if not best:
        return {}
    return {
        "cost": best_cost,
        "model": get_first(best, ["model", "modelName", "model_name", "model_id"]) or "unknown",
        "tokens": total_tokens_used(best),
        "time": format_timestamp(get_first(best, ["createdAt", "timestamp", "time"])),
    }


def top_expensive_requests(events: List[Dict[str, Any]], limit: int, top_n: int) -> List[Dict[str, Any]]:
    recent = latest_events(events, limit)
    items: List[Dict[str, Any]] = []
    for event in recent:
        cost = event_cost(event)
        if cost is None:
            continue
        items.append(
            {
                "cost": cost,
                "model": get_first(event, ["model", "modelName", "model_name", "model_id"]) or "unknown",
                "tokens": total_tokens_used(event),
                "time": format_timestamp(get_first(event, ["createdAt", "timestamp", "time"])),
            }
        )
    items.sort(key=lambda item: item["cost"], reverse=True)
    return items[:top_n]


def format_tokens(value: Optional[int]) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def update_events_payload(data_bytes: Optional[bytes], lookback_days: int) -> Optional[bytes]:
    if not data_bytes:
        return data_bytes
    try:
        payload = json.loads(data_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        return data_bytes
    if not isinstance(payload, dict):
        return data_bytes

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    lookback_ms = max(0, lookback_days) * 24 * 60 * 60 * 1000
    start_ms = now_ms - lookback_ms

    if "startDate" in payload:
        payload["startDate"] = str(start_ms)
    if "endDate" in payload:
        payload["endDate"] = str(now_ms)
    if "pageSize" in payload:
        try:
            payload["pageSize"] = max(int(payload["pageSize"]), 100)
        except (TypeError, ValueError):
            payload["pageSize"] = 100
    return json.dumps(payload).encode("utf-8")


def month_window_utc() -> Tuple[int, int]:
    now = datetime.now(tz=timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    return start_ms, end_ms


def month_window_for(year: int, month: int) -> Tuple[int, int]:
    """Return (start_ms, end_ms) for a given year and month in UTC."""
    import calendar
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    _, last_day = calendar.monthrange(year, month)
    end = datetime(year, month, last_day, 23, 59, 59, 999000, tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    return start_ms, end_ms


def update_events_payload_for_month(
    data_bytes: Optional[bytes],
    page: int,
    page_size: int,
) -> Optional[bytes]:
    if not data_bytes:
        return data_bytes
    try:
        payload = json.loads(data_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        return data_bytes
    if not isinstance(payload, dict):
        return data_bytes

    start_ms, end_ms = month_window_utc()
    payload["startDate"] = str(start_ms)
    payload["endDate"] = str(end_ms)
    payload["page"] = page
    payload["pageSize"] = page_size
    return json.dumps(payload).encode("utf-8")


def update_events_payload_for_specific_month(
    data_bytes: Optional[bytes],
    year: int,
    month: int,
    page: int,
    page_size: int,
) -> Optional[bytes]:
    """Update payload with start/end dates for a specific year and month."""
    if not data_bytes:
        return data_bytes
    try:
        payload = json.loads(data_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        return data_bytes
    if not isinstance(payload, dict):
        return data_bytes

    start_ms, end_ms = month_window_for(year, month)
    payload["startDate"] = str(start_ms)
    payload["endDate"] = str(end_ms)
    payload["page"] = page
    payload["pageSize"] = page_size
    return json.dumps(payload).encode("utf-8")


def fetch_events_for_month(
    url: str,
    headers: Dict[str, str],
    data_bytes: Optional[bytes],
    year: int,
    month: int,
    page_size: int = 100,
) -> List[Dict[str, Any]]:
    """Fetch all usage events for a specific year and month."""
    all_events: List[Dict[str, Any]] = []
    page = 1
    while True:
        page_payload = update_events_payload_for_specific_month(
            data_bytes, year, month, page, page_size
        )
        payload = request_json(url, headers, page_payload, method="POST")
        events = find_events(payload)
        all_events.extend(events)
        if len(events) < page_size:
            break
        total_count = payload.get("totalUsageEventsCount")
        if isinstance(total_count, int) and len(all_events) >= total_count:
            break
        page += 1
    return all_events


def fetch_all_events(
    url: str,
    headers: Dict[str, str],
    data_bytes: Optional[bytes],
    page_size: int = 100,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    all_events: List[Dict[str, Any]] = []
    payload: Dict[str, Any] = {}
    page = 1
    while True:
        page_payload = update_events_payload_for_month(data_bytes, page, page_size)
        payload = request_json(url, headers, page_payload, method="POST")
        events = find_events(payload)
        all_events.extend(events)
        if len(events) < page_size:
            break
        total_count = payload.get("totalUsageEventsCount")
        if isinstance(total_count, int) and len(all_events) >= total_count:
            break
        page += 1
    return all_events, payload


def extract_user_from_cookie(cookies: Dict[str, str]) -> Optional[str]:
    token = cookies.get("WorkosCursorSessionToken")
    if not token:
        return None
    decoded = urllib.parse.unquote(token)
    if "::" in decoded:
        return decoded.split("::", 1)[0]
    return None


def extract_billing_summary(
    payload: Dict[str, Any],
) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    if not isinstance(payload, dict):
        return None, None, None

    start_of_month = payload.get("startOfMonth")
    summary = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload

    requests_consumed = pick_int(
        summary,
        [
            "requestsConsumed",
            "requestsUsed",
            "requestCount",
            "totalRequests",
            "includedRequestsUsed",
            "includedRequestCount",
        ],
    )
    total_cost = pick_money(
        summary,
        [
            "totalUsageUsd",
            "totalUsage",
            "totalUsageCents",
            "totalSpendUsd",
            "totalSpend",
            "totalSpendCents",
            "usageUsd",
            "usageCents",
        ],
    )
    if total_cost is None:
        total_cost = pick_money(
            {"tokenUsageTotalCents": get_nested(summary, ["tokenUsage", "totalCents"])},
            ["tokenUsageTotalCents"],
        )

    if requests_consumed is None:
        total = 0
        found = False
        for key, value in payload.items():
            if key == "startOfMonth" or not isinstance(value, dict):
                continue
            count = pick_int(value, ["numRequests", "numRequestsTotal"])
            if count is not None:
                total += count
                found = True
        if found:
            requests_consumed = total

    return requests_consumed, total_cost, start_of_month


def compute_model_breakdown(events: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    recent = latest_events(events, limit)
    aggregates: Dict[str, Dict[str, Any]] = {}
    for event in recent:
        model = get_first(event, ["model", "modelName", "model_name", "model_id"])
        if not model and isinstance(event.get("metadata"), dict):
            model = event["metadata"].get("model")
        model = str(model or "unknown")

        entry = aggregates.setdefault(model, {"model": model, "requests": 0, "cost": 0.0})
        reqs = event_request_cost(event)
        if reqs is not None:
            entry["requests"] += reqs
        cost = event_cost(event)
        if cost is not None:
            entry["cost"] += cost

    ranked = sorted(
        aggregates.values(),
        key=lambda item: (item["requests"], item["cost"]),
        reverse=True,
    )
    return ranked[:5]


def compute_monthly_stats(events: List[Dict[str, Any]]) -> Tuple[int, float]:
    """Compute total requests and total cost from events."""
    total_requests = 0
    total_cost = 0.0
    for event in events:
        reqs = event_request_cost(event)
        if reqs is not None:
            total_requests += reqs
        cost = event_cost(event)
        if cost is not None:
            total_cost += cost
    return total_requests, total_cost


def compute_monthly_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute full summary stats for a month's events.

    Returns:
        Dict with requests, total_cost, median_cost, event_count,
        model_breakdown (top 5), top_expensive (top 5).
    """
    stats = compute_stats(events, len(events) or 0)
    model_breakdown = compute_model_breakdown(events, len(events) or 0)
    top_expensive = top_expensive_requests(events, len(events) or 0, 5)
    return {
        "requests": stats.get("requests", 0),
        "total_cost": stats.get("total_cost"),
        "median_cost": stats.get("median_cost"),
        "event_count": stats.get("count", 0),
        "model_breakdown": model_breakdown,
        "top_expensive": top_expensive,
    }


def _build_month_entry(
    year: int,
    month: int,
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a month entry dict from a computed summary."""
    import calendar as cal_mod
    return {
        "month": month,
        "month_name": f"{cal_mod.month_name[month]} {year}",
        "requests": summary.get("requests", 0),
        "cost": summary.get("total_cost"),
        "median_cost": summary.get("median_cost"),
        "event_count": summary.get("event_count", 0),
        "model_breakdown": summary.get("model_breakdown", []),
        "top_expensive": summary.get("top_expensive", []),
    }


def fetch_monthly_breakdown(
    url: str,
    headers: Dict[str, str],
    data_bytes: Optional[bytes],
    year: int,
    current_month_events: Optional[List[Dict[str, Any]]] = None,
    cache: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Fetch usage for each month of the given year up to the current month.

    Args:
        url: API endpoint.
        headers: Request headers.
        data_bytes: Request body template.
        year: Year to fetch.
        current_month_events: Pre-fetched events for the current month.
            Avoids a redundant API call.
        cache: Dict mapping month number to a previously computed entry.
            Historical (non-current) months are served from cache.

    Returns:
        List of dicts with keys: month, month_name, requests, cost,
        median_cost, event_count, model_breakdown, top_expensive.
    """
    from concurrent.futures import ThreadPoolExecutor

    if cache is None:
        cache = {}

    now = datetime.now(tz=timezone.utc)
    current_month = now.month if year == now.year else None
    max_month = now.month if year == now.year else 12

    months_to_fetch: List[int] = []
    for month in range(1, max_month + 1):
        if month == current_month:
            continue
        if month in cache:
            continue
        months_to_fetch.append(month)

    def _fetch_one(month: int) -> Tuple[int, Dict[str, Any]]:
        events = fetch_events_for_month(
            url, headers, data_bytes, year, month, page_size=100
        )
        summary = compute_monthly_summary(events)
        return month, _build_month_entry(year, month, summary)

    if months_to_fetch:
        with ThreadPoolExecutor(max_workers=min(len(months_to_fetch), 4)) as pool:
            for month, entry in pool.map(
                _fetch_one, months_to_fetch
            ):
                cache[month] = entry

    if current_month is not None and current_month <= max_month:
        if current_month_events is not None:
            summary = compute_monthly_summary(current_month_events)
        else:
            events = fetch_events_for_month(
                url, headers, data_bytes, year, current_month, page_size=100
            )
            summary = compute_monthly_summary(events)
        cache[current_month] = _build_month_entry(
            year, current_month, summary
        )

    return [cache[m] for m in range(1, max_month + 1) if m in cache]


def prepare_curl(
    curl_command: str,
    keep_all_cookies: bool = False,
    cookie_allowlist: Optional[str] = None,
    minimal_headers: bool = False,
) -> Tuple[str, Dict[str, str], Optional[bytes], Dict[str, str]]:
    """Parse a curl command and return (url, headers, data_bytes, cookies)."""
    url, headers, data_bytes = parse_curl_command(curl_command)

    cookie_header = headers.get("Cookie")
    cookies = parse_cookie_header(cookie_header) if cookie_header else {}
    if cookie_allowlist is None:
        cookie_allowlist = ",".join(sorted(DEFAULT_COOKIE_ALLOWLIST))
    allowlist = [item.strip() for item in cookie_allowlist.split(",") if item.strip()]

    kept, _omitted = filter_cookies(cookies, allowlist, keep_all_cookies)

    new_cookie_header = build_cookie_header(kept)
    if new_cookie_header:
        headers["Cookie"] = new_cookie_header
    elif "Cookie" in headers:
        headers.pop("Cookie")

    headers = clean_headers(headers, minimal_headers)
    return url, headers, data_bytes, cookies


def fetch_dashboard_data(
    curl_command: str,
    limit: int = 20,
    keep_all_cookies: bool = False,
    cookie_allowlist: Optional[str] = None,
    minimal_headers: bool = False,
    usage_user: Optional[str] = None,
    monthly_cache: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fetch all data needed for the dashboard and return a structured dict.

    Args:
        monthly_cache: Mutable dict that caches historical month data
            across refreshes. Pass the same dict on every call to avoid
            re-fetching completed months.
    """
    url, headers, data_bytes, cookies = prepare_curl(
        curl_command,
        keep_all_cookies=keep_all_cookies,
        cookie_allowlist=cookie_allowlist,
        minimal_headers=minimal_headers,
    )

    events, payload = fetch_all_events(url, headers, data_bytes, page_size=100)
    requests_consumed, on_demand_usage = build_summary(payload, events)
    event_summaries = summarize_events(events, limit)
    stats = compute_stats(events, len(events) or 0)
    model_breakdown = compute_model_breakdown(events, len(events) or 0)
    top_expensive = top_expensive_requests(events, len(events) or 0, 5)

    resolved_user = usage_user or extract_user_from_cookie(cookies)
    billing_requests = None
    billing_total: Optional[float] = None
    billing_start = None
    if resolved_user:
        usage_url = f"https://cursor.com/api/usage?user={resolved_user}"
        usage_headers = dict(headers)
        usage_headers.pop("content-type", None)
        usage_payload = request_json(usage_url, usage_headers, None, method="GET")
        billing_requests, billing_total, billing_start = extract_billing_summary(usage_payload)

    now = datetime.now(tz=timezone.utc)
    monthly_breakdown = fetch_monthly_breakdown(
        url, headers, data_bytes, year=now.year,
        current_month_events=events,
        cache=monthly_cache,
    )

    return {
        "events": events,
        "event_summaries": event_summaries,
        "requests_consumed": requests_consumed,
        "on_demand_usage": on_demand_usage,
        "stats": stats,
        "model_breakdown": model_breakdown,
        "top_expensive": top_expensive,
        "billing_requests": billing_requests,
        "billing_total": billing_total,
        "billing_start": billing_start,
        "monthly_breakdown": monthly_breakdown,
        "limit": limit,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Display Cursor on-demand usage and latest events."
    )
    parser.add_argument(
        "--curl",
        help="Full curl command wrapped in quotes.",
    )
    parser.add_argument(
        "--curl-file",
        help="Path to a text file containing the curl command.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of latest events to display (default: 20).",
    )
    parser.add_argument(
        "--keep-all-cookies",
        action="store_true",
        help="Do not filter cookies from the curl command.",
    )
    parser.add_argument(
        "--cookie-allowlist",
        default=",".join(sorted(DEFAULT_COOKIE_ALLOWLIST)),
        help="Comma-separated cookie names to keep when filtering.",
    )
    parser.add_argument(
        "--minimal-headers",
        action="store_true",
        help="Drop browser-only headers.",
    )
    parser.add_argument(
        "--dump-response",
        help="Write the raw JSON response to a file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug information to stderr.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in the output.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="How many days back to request usage events (deprecated, month is used).",
    )
    parser.add_argument(
        "--usage-user",
        help="Override user id for billing usage endpoint.",
    )
    parser.add_argument(
        "--dump-usage-response",
        help="Write the raw billing usage response to a file.",
    )

    args = parser.parse_args()

    global DEBUG
    global USE_COLOR
    DEBUG = args.debug
    USE_COLOR = not args.no_color

    curl_command = args.curl
    if args.curl_file:
        with open(args.curl_file, "r", encoding="utf-8") as handle:
            curl_command = handle.read().strip()

    if not curl_command:
        raise SystemExit("Provide --curl or --curl-file with your curl command.")

    data = fetch_dashboard_data(
        curl_command,
        limit=args.limit,
        keep_all_cookies=args.keep_all_cookies,
        cookie_allowlist=args.cookie_allowlist,
        minimal_headers=args.minimal_headers,
        usage_user=args.usage_user,
    )

    events = data["events"]
    event_summaries = data["event_summaries"]
    on_demand_usage = data["on_demand_usage"]
    stats = data["stats"]
    model_breakdown = data["model_breakdown"]
    top_expensive = data["top_expensive"]
    billing_requests = data["billing_requests"]
    billing_total = data["billing_total"]
    billing_start = data["billing_start"]

    title = " Cursor Usage "
    line = "=" * (len(title) + 10)
    print(style(line, DIM))
    print(style(title.center(len(line)), BOLD, CYAN))
    print(style(line, DIM))
    print()
    print(style("Summary", BOLD, CYAN))
    if billing_requests is not None or billing_total is not None:
        period_label = "Billing period total"
        if billing_start:
            try:
                start_dt = datetime.fromisoformat(billing_start.replace("Z", "+00:00"))
                period_label = f"Billing period total (since {start_dt.date().isoformat()})"
            except ValueError:
                period_label = f"Billing period total (since {billing_start})"
        print(
            f"{period_label}: {billing_requests if billing_requests is not None else 'n/a'} reqs"
            f" — {format_money(billing_total)}"
        )
    print(f"On-demand usage: {format_money(on_demand_usage)}")
    print(
        f"This month: {stats['requests']} reqs"
        f" — {format_money(stats['total_cost'])} total"
        f" — {format_money(stats['median_cost'])} median"
    )
    print()
    print(style("Top models (this month)", BOLD, CYAN))
    if not model_breakdown:
        print("No model data available.")
    else:
        model_lines = format_models_table(model_breakdown)
        for idx, line in enumerate(model_lines):
            if idx in {0, 2, len(model_lines) - 1}:
                print(style(line, DIM))
            elif idx == 1:
                print(style(line, BOLD))
            else:
                print(line)
    print()
    print(style("Top expensive requests (this month)", BOLD, CYAN))
    if not top_expensive:
        print("No cost data available.")
    else:
        expensive_lines = format_expensive_table(top_expensive)
        for idx, line in enumerate(expensive_lines):
            if idx in {0, 2, len(expensive_lines) - 1}:
                print(style(line, DIM))
            elif idx == 1:
                print(style(line, BOLD))
            else:
                print(line)
    print()
    print(style(f"Latest events (showing {min(args.limit, len(events))})", BOLD, CYAN))
    if not event_summaries:
        print("No events found in the response.")
        top_keys = ", ".join(sorted(data.keys())) if isinstance(data, dict) else "n/a"
        print(f"Response keys: {top_keys}")
        return

    table_lines = format_table(event_summaries)
    for idx, line in enumerate(table_lines):
        if idx in {0, 2, len(table_lines) - 1}:
            print(style(line, DIM))
        elif idx == 1:
            print(style(line, BOLD))
        else:
            print(line)


if __name__ == "__main__":
    main()
