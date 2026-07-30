.PHONY: install run test backup-db

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000

test:
	.venv/bin/pytest tests/ -q

backup-db:
	cp data.db "data.db.bak.$$(date +%Y%m%d-%H%M%S)"
