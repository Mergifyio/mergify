### BASE ###
FROM python:3.9-slim-buster as base-image
ENV DEBIAN_FRONTEND=noninteractive
RUN apt update -y && apt upgrade -y && apt install -y git && apt autoremove --purge -y

### BUILDER JS ###
FROM node:16-buster-slim as js-builder
# Real install that can't be cached
ADD installer /installer
WORKDIR /installer
RUN npm ci
RUN npm run build
RUN rm -rf node_modules

### BUILDER PYTHON ###
FROM base-image as python-builder
RUN apt install -y gcc

RUN python3 -m venv /venv
ENV VIRTUAL_ENV=/venv
ENV PATH="/venv/bin:${PATH}"

RUN python3 -m pip install wheel

ADD . /app
# Just to be able cache a layer with all deps
ADD requirements.txt /

# NOTE(sileht): We don't use pipenv
# nosemgrep: generic.ci.security.use-frozen-lockfile.use-frozen-lockfile-pip
RUN python3 -m pip install --no-cache-dir -r /requirements.txt

WORKDIR /app
# NOTE(sileht): We don't use pipenv
# nosemgrep: generic.ci.security.use-frozen-lockfile.use-frozen-lockfile-pip
RUN python3 -m pip install --no-cache-dir -c ./requirements.txt -e .

### RUNNER ###
FROM base-image as runner
ARG MERGIFYENGINE_VERSION=dev
LABEL mergify-engine.version="$MERGIFYENGINE_VERSION"
RUN apt clean -y && rm -rf /var/lib/apt/lists/*
COPY --from=python-builder /app /app
COPY --from=python-builder /venv /venv
COPY --from=js-builder /installer/build /app/installer/build
WORKDIR /app
ENV VIRTUAL_ENV=/venv
ENV PYTHONUNBUFFERED=1
ENV DD_DOGSTATSD_DISABLE=1
ENV DD_TRACE_ENABLED=0
ENV PATH="/venv/bin:${PATH}"
ENV MERGIFYENGINE_VERSION=$MERGIFYENGINE_VERSION
ENTRYPOINT ["/app/entrypoint.sh"]
