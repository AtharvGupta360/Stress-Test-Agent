# Judge sandbox. One container per submission, kept warm for the whole run.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        python3 \
        python3-minimal \
        openjdk-17-jdk-headless \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged. Combined with --network none, --read-only and a tmpfs /work,
# a hostile submission has nothing to reach for.
#
# The uid is pinned rather than auto-assigned because the runner passes it to
# `--tmpfs /work:...,uid=1000`; the tmpfs is created by the daemon and would
# otherwise come up root-owned and unwritable. Keep the two in sync.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin judge \
    && mkdir -p /work && chown judge:judge /work

COPY driver.py /opt/driver.py
RUN chmod 0555 /opt/driver.py

USER judge
WORKDIR /work

# Idle by default: the worker copies sources in, then `docker exec`s the
# compile and the stress loop. Keeping the container alive across all rounds is
# the entire point -- see the note at the top of driver.py.
CMD ["sleep", "infinity"]
