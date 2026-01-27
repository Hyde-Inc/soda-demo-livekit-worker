# Setup

## Foundry Authentication

Set these environment variables to authenticate with Foundry repositories:

```bash
export FOUNDRY_TOKEN=token
export UV_INDEX_FOUNDRY_MAIN_USERNAME=user
export UV_INDEX_FOUNDRY_MAIN_PASSWORD=$FOUNDRY_TOKEN
export UV_INDEX_FOUNDRY_SDK_BUNDLE_USERNAME=user
export UV_INDEX_FOUNDRY_SDK_BUNDLE_PASSWORD=$FOUNDRY_TOKEN
```

Then install dependencies:
```bash
uv sync
```


uvicorn server:app --reload

uv run main.py start

celery -A celery_app worker --beat --loglevel=info
