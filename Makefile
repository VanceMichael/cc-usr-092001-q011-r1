.PHONY: test migrate run smoke
test:
	python -m unittest discover -s tests
migrate:
	python -m scripts.migrate
run:
	python -m src.app
smoke:
	python scripts/smoke.py $${BASE_URL:-http://127.0.0.1:8080}
