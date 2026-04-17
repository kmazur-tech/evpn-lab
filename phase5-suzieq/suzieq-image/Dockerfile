# Phase 5 Part B: thin child image that adds the `vjunos-switch`
# devtype to the upstream SuzieQ service catalog.
#
# Why this image exists:
#   The lab runs vJunos-switch (vrnetlab juniper_vjunos-switch
#   23.2R1.14) which needs an unusual mix of upstream service
#   templates: junos-mx-shape `device` service (single-routing-engine
#   JSON) but junos-qfx-shape `lldp` service (detail view, has
#   port id). No built-in SuzieQ devtype matches this combination.
#   See add-vjunos-switch.py for the full rationale.
#
# Build mode:
#   BUILD-TIME ONLY. The patcher script runs as a `RUN` step at
#   `docker compose build` time. The result is a new image layer
#   with the patches baked in. Container start does NOTHING extra -
#   the entrypoint is unchanged from upstream. There is no runtime
#   patching, no entrypoint wrapper, no per-start re-application.
#
# Upgrade procedure:
#   1. Bump the FROM digest below to the new upstream sha256
#   2. `docker compose build` (re-runs the patcher against the new
#      upstream content; if upstream restructured the yaml shape
#      in a way the script cannot handle, the build FAILS LOUDLY)
#   3. `docker compose up -d` (recreates poller / coalescer /
#      rest-server containers from the new image)
#
# Pinned to the same digest as docker-compose.yml's other
# references for consistency. When you bump one, bump both.

FROM netenglabs/suzieq@sha256:6e4e955ae0ca214af975d0278e16f7d4e193685369fdefb92c563a60fc3d5472

# Run as root only for the patch step; the upstream image already
# defines a non-root `suzieq` user that the entrypoint switches to.
USER root

COPY add-vjunos-switch.py /tmp/add-vjunos-switch.py
# Two args: the config dir (service yamls) and the package root
# (for patching known_devtypes() in shared/utils.py). Both must be
# patched - service yamls define HOW to poll the device, the
# allowlist in shared/utils.py decides WHETHER the devtype name
# is even recognized at node-init time.
RUN python3 /tmp/add-vjunos-switch.py \
        /usr/local/lib/python3.9/site-packages/suzieq/config/ \
        /usr/local/lib/python3.9/site-packages/suzieq/ \
    && rm /tmp/add-vjunos-switch.py

# Restore upstream user. The entrypoint inherited from the base
# image is unchanged - the patcher only modifies static yamls and
# leaves binaries / Python source / volumes alone.
USER suzieq
