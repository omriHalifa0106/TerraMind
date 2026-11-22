# PostgreSQL 17 + pgvector (official image, Debian bookworm) + PostGIS from the PGDG apt repo.
# Note: postgis/postgis:17-3.5 is built on Debian bullseye, whose PGDG repo has been retired
# (apt returns 404), so installing pgvector on top of it no longer works.
FROM pgvector/pgvector:pg17-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-postgis-3 postgresql-17-postgis-3-scripts \
 && rm -rf /var/lib/apt/lists/*
