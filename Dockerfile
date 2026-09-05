# The container serves the toolkit shell, the three tool pages, the CSVs and
# the /api endpoints from a single origin. analyze_server.py is already a
# static file server (SimpleHTTPRequestHandler), so this needs no nginx in
# front, and the pages keep their hardcoded "/api/..." paths without any CORS
# headers or API base URL configuration.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so an edit to a page or a CSV reuses this layer.
# pandas, lxml and the grpc wheels firebase-admin pulls in all ship manylinux
# builds, so this stays a wheel-only install with no compiler in the image.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN chmod +x docker-entrypoint.sh

# PORT is what Fly injects. ANALYZE_ARGS carries the server's own flags so
# they can be changed from fly.toml without rebuilding the image.
ENV PORT=8080 \
    ANALYZE_ARGS="--local-technicals"

EXPOSE 8080

# Runs as a non-root user: nothing in the served tree needs write access, and
# the Firestore key is written to /tmp, which stays writable.
RUN useradd --create-home --uid 10001 toolkit \
    && chown -R toolkit:toolkit /app
USER toolkit

ENTRYPOINT ["/app/docker-entrypoint.sh"]
