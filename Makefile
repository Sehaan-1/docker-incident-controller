.PHONY: api once worker test unit integration lint compose-up compose-down demo phase-d-demo health break-nginx break-app

api:
	uvicorn agent.api.main:app --reload --host 0.0.0.0 --port 8000

once:
	python -m agent.core.worker --once

worker:
	python -m agent.core.worker --loop

test unit:
	pytest tests/unit

integration:
	pytest tests/integration

lint:
	ruff check .

compose-up:
	docker compose up --build

compose-down:
	docker compose down --volumes

health:
	curl -fsS http://localhost:8080/health

break-nginx:
	sh fault_injection/break_nginx_config.sh

break-app:
	sh fault_injection/enable_app_crash.sh

demo phase-d-demo:
	docker compose down --volumes
	docker compose up -d --build
	@echo "Waiting for services to start..."
	@for i in $$(seq 1 10); do if curl -fsS http://localhost:8080/health; then break; fi; sleep 2; done
	sh fault_injection/break_nginx_config.sh
	@echo "Waiting for nginx incident to resolve..."
	@for i in $$(seq 1 60); do \
		status=$$(curl -fsS "http://localhost:8000/incidents" | python -c "import json,sys; items=json.load(sys.stdin); print(next((x['status'] for x in items if x['type']=='NGINX_CONFIG_ERROR'), ''))" || true); \
		if [ "$$status" = "RESOLVED" ]; then break; fi; \
		if [ "$$status" = "FAILED" ] || [ "$$status" = "NEEDS_HUMAN" ]; then echo "Incident terminal failure: $$status"; exit 1; fi; \
		sleep 1; \
	done
	@for i in $$(seq 1 10); do if curl -fsS http://localhost:8080/health; then break; fi; sleep 2; done
	curl -fsS "http://localhost:8000/incidents?status=RESOLVED"
