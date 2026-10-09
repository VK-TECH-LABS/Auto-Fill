# Tiny static server for the synthetic application fixtures.
# Build from tests/fixtures: docker build -f server/Dockerfile .
FROM python:3.12-slim-bookworm

WORKDIR /srv
COPY eight_page_application.html sample_application.html multi_step_application.html ./
COPY ats ./ats
COPY server/serve.py ./server/serve.py

EXPOSE 8090
CMD ["python", "server/serve.py", "--host", "0.0.0.0", "--port", "8090"]
