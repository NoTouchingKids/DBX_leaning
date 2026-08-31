# The base every test image starts from. Built once, so the three role images
# differ only in what they are allowed to see.
#
# Nothing about this repo is in here. Its only job is to make pip work behind
# a TLS-intercepting proxy, which is an artefact of the machine the tests run
# on and not of anything being tested. On a machine with no such proxy the
# build context has no ca-bundle.crt and the COPY below is skipped by the
# runner (tests/container/harness.py) using a different Dockerfile line.
ARG PYTHON_IMAGE=mirror.gcr.io/library/python:3.11-slim
FROM ${PYTHON_IMAGE}

# An early layer, per /root/.ccr/README.md: processes inside a container do
# not trust the proxy's CA and cannot reach it on 127.0.0.1.
COPY ca-bundle.crt /usr/local/share/ca-certificates/proxy-ca.crt
RUN cat /usr/local/share/ca-certificates/proxy-ca.crt >> \
      /usr/local/lib/python3.11/site-packages/pip/_vendor/certifi/cacert.pem 2>/dev/null || true
ENV PIP_CERT=/usr/local/share/ca-certificates/proxy-ca.crt \
    SSL_CERT_FILE=/usr/local/share/ca-certificates/proxy-ca.crt \
    REQUESTS_CA_BUNDLE=/usr/local/share/ca-certificates/proxy-ca.crt \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1
