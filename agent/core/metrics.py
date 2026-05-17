from prometheus_client import Counter

INCIDENT_CREATED_COUNT = Counter(
    "incident_created_total", "Total number of incidents created", ["type"]
)

INCIDENT_RETRY_COUNT = Counter("incident_retry_total", "Total number of incident retries", ["type"])

INCIDENT_RESOLUTION_COUNT = Counter(
    "incident_resolution_total", "Total number of incident resolutions", ["type", "status"]
)

ACTION_EXECUTED_COUNT = Counter(
    "action_executed_total", "Total number of actions executed", ["tool_name", "status"]
)

WORKER_PASS_COUNT = Counter(
    "worker_pass_total", "Total number of worker polling passes", ["status"]
)
