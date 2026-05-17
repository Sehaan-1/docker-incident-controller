from __future__ import annotations

from agent.models.incident import IncidentCandidate, IncidentType
from agent.observer.observer import ObservationsBundle


NGINX_EMERG_PATTERNS = (
    "nginx: [emerg]",
    "[emerg]",
    "unknown directive",
    "directive is not allowed here",
)


def detect(observations_bundle: ObservationsBundle) -> list[IncidentCandidate]:
    candidates: list[IncidentCandidate] = []
    nginx_candidate = detect_nginx_config_error(observations_bundle)
    if nginx_candidate is not None:
        candidates.append(nginx_candidate)

    app_candidate = detect_app_crash_loop(observations_bundle)
    if app_candidate is not None:
        candidates.append(app_candidate)

    return candidates


def detect_nginx_config_error(bundle: ObservationsBundle) -> IncidentCandidate | None:
    bad_state_evidence = nginx_bad_state_evidence(bundle)
    if bad_state_evidence is None:
        return None

    nginx_logs = bundle.by_source_kind("nginx", "log")
    for observation in nginx_logs:
        text = str(observation.payload.get("text") or "")
        if any(pattern in text for pattern in NGINX_EMERG_PATTERNS):
            return IncidentCandidate(
                type=IncidentType.NGINX_CONFIG_ERROR,
                summary="Nginx failed to load managed conf.d site configuration.",
                evidence=[
                    {
                        "source": "nginx",
                        "kind": "log",
                        "matched_patterns": [
                            pattern for pattern in NGINX_EMERG_PATTERNS if pattern in text
                        ],
                    }
                ],
            )

    return IncidentCandidate(
        type=IncidentType.NGINX_CONFIG_ERROR,
        summary="Nginx is restarting after a configuration load failure.",
        evidence=[bad_state_evidence],
    )


def nginx_bad_state_evidence(bundle: ObservationsBundle) -> dict[str, object] | None:
    for observation in bundle.by_source_kind("nginx", "container"):
        payload = observation.payload
        state = payload.get("state", {})
        restart_count = int(payload.get("restart_count") or 0)
        is_bad = restart_count > 0 and (
            state.get("restarting") is True
            or (state.get("running") is False and state.get("status") != "running")
        )
        if is_bad:
            return {
                "source": "nginx",
                "kind": "container",
                "restart_count": restart_count,
                "state": state,
            }
    return None


def detect_app_crash_loop(
    bundle: ObservationsBundle,
    *,
    restart_threshold: int = 3,
) -> IncidentCandidate | None:
    for observation in bundle.by_source_kind("app", "container"):
        payload = observation.payload
        state = payload.get("state", {})
        restart_count = int(payload.get("restart_count") or 0)
        if state.get("restarting") is True and restart_count >= restart_threshold:
            return IncidentCandidate(
                type=IncidentType.APP_CRASH_LOOP,
                summary=(
                    f"App container is crash-looping ({restart_count} restarts observed by Docker)."
                ),
                evidence=[
                    {
                        "source": "app",
                        "kind": "container",
                        "restart_count": restart_count,
                        "state": state,
                    }
                ],
            )

    return None
