# THE DEPLOYED APP. The build context is `app/` and nothing else, because
# `resources/app.yml` hands Databricks Apps `../app` as its `source_code_path`
# and nothing above it travels.
#
# This image exists because of a specific failure. Deleting `app/shared/` as
# apparent duplication left all 296 tests green and broke the deployed app:
# pytest has the repo root on its `sys.path` and a Databricks App does not.
# `tests/deploy/test_app_is_self_contained.py` reads the imports statically,
# which is fast and catches the same class of thing. This actually runs it.
ARG BASE=dbx-test-base
FROM ${BASE}

WORKDIR /app
COPY . /app

# Where Databricks Apps looks, and the only dependency list the app gets.
RUN pip install --no-cache-dir -r /app/requirements.txt

# `server.main:app` — the command in app.yaml and resources/app.yml, verbatim.
ENV DBX_FRONTEND_DIST=dist
