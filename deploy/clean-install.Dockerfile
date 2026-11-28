# Official Ubuntu 24.04 multi-architecture index, resolved from registry-1.docker.io.
# Build this image natively on amd64 and arm64. No emulation is counted as DGX proof.
FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates python3.12 python3.12-venv libstdc++6 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 omni

USER 10001:10001
WORKDIR /repo
ENV CC=clang CXX=clang++ LANG=C.UTF-8
# Mount the checkout read-only at /repo and an empty writable directory at /work.
# The offline phase must be started in a second container with --network=none.
ENTRYPOINT ["bash", "/repo/scripts/test-clean-onboarding.sh"]

FROM runtime AS build
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       cmake make clang g++ gperf libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
