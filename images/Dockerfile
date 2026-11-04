ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG DEBIAN_SNAPSHOT
ARG KUBERNETES
# Frozen Debian repositories; runtime binaries do not drift with apt's latest index.
RUN rm /etc/apt/sources.list.d/debian.sources \
 && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/%s bookworm main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/%s bookworm-security main\n' "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list \
 && apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git jq procps iproute2 dnsutils \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/v${KUBERNETES}/bin/linux/amd64/kubectl" \
 && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/v${KUBERNETES}/bin/linux/amd64/kubectl.sha256" \
 && cd /usr/local/bin && echo "$(cat /tmp/kubectl.sha256)  kubectl" | sha256sum -c - && chmod 755 kubectl
COPY images/agent.lock images/build.lock /tmp/locks/
RUN pip install --no-cache-dir -r /tmp/locks/build.lock \
 && pip install --no-cache-dir --no-build-isolation -r /tmp/locks/agent.lock
COPY sdk /tmp/sdk
RUN pip wheel --no-build-isolation --no-deps /tmp/sdk -w /tmp/wheels \
 && pip install --no-deps /tmp/wheels/*.whl && rm -rf /tmp/sdk /tmp/wheels
COPY runtime /opt/sregym/runtime
ARG INTEGRATION_REVISION
ENV HOLMES_SREGYM_REVISION=${INTEGRATION_REVISION}
ENV PYTHONPATH=/opt/sregym
RUN mkdir /logs
WORKDIR /logs
ENTRYPOINT ["/bin/bash", "-c"]
