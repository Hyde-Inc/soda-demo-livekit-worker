# Local Development

```bash
# Run migrations
source .venv/bin/activate && alembic upgrade head

# Start FastAPI
uvicorn server:app --reload

# Start Celery worker + beat
celery -A celery_app worker --beat --loglevel=info

# Start LiveKit agent
python main.py start

# purge celery queue
celery -A celery_app purge -f
```

# Docker

```bash
# Copy env.example to .env and fill in values
cp env.example .env

# Build and start all services
docker compose up --build -d

# View logs
docker compose logs -f

# Stop all services
docker compose down

# Reset (remove volumes)
docker compose down -v
```