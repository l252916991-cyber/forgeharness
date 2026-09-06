# ADR 004: Host API processes with containerized dependencies

Status: accepted for local demonstration.

OMLX is intentionally bound to localhost on macOS. FastAPI and ARQ therefore run as host processes, while PostgreSQL, Redis, Qdrant and Nginx run in Docker Compose. Nginx reaches two API ports through `host.docker.internal`.

This topology demonstrates dependency separation and process failover without exposing OMLX to the Docker network. It is not regional high availability: one laptop, one disk and one OMLX server remain shared failure domains.
