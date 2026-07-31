# MergePilot TestRunner production image (non-root, no runtime network).
# The base image is pinned at BUILD time via PYTHON_BASE (a digest-pinned image);
# the final image digest is recorded (docker inspect) and used as
# MERGEPILOT_TR_IMAGE=repository@sha256:<digest>. This Dockerfile does NOT bake in
# a fabricated digest.
ARG PYTHON_BASE=python:3.9.25-slim@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b
FROM ${PYTHON_BASE}

# non-root user; no sudo
RUN addgroup --system mp \
 && adduser --system --ingroup mp --disabled-password --gecos "" mp \
 && pip install --no-cache-dir pytest==8.4.2

USER mp
WORKDIR /work
