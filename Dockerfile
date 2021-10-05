### BASE ###
FROM python:3.9-slim-buster as base-image
ENV DEBIAN_FRONTEND=noninteractive
RUN apt update -y && apt upgrade -y && apt install -y git && apt autoremove --purge -y

### BUILDER ###
FROM base-image as builder
RUN apt install -y gcc nodejs yarnpkg
RUN ln -s /usr/bin/yarnpkg /usr/bin/yarn

RUN python3 -m venv /venv
ENV VIRTUAL_ENV=/venv
ENV PATH="/venv/bin:${PATH}"

RUN python3 -m pip install wheel

# Just to be able cache a layer with all deps
ADD requirements.txt /
RUN python3 -m pip install --no-cache-dir -r /requirements.txt

# Real install that can't be cached
ADD . /app
WORKDIR /app/installer
RUN yarn
RUN yarn build
RUN rm -rf node_modules
WORKDIR /app
RUN python3 -m pip install --no-cache-dir -c ./requirements.txt -e .

### RUNNER ###
FROM base-image as runner
ARG MERGIFYENGINE_VERSION=dev
LABEL mergify-engine.version="$MERGIFYENGINE_VERSION"
RUN apt clean -y && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app /app
COPY --from=builder /venv /venv
WORKDIR /app
ENV VIRTUAL_ENV=/venv
ENV PYTHONUNBUFFERED=1
ENV DD_DOGSTATSD_DISABLE=1
ENV DD_TRACE_ENABLED=0
ENV PATH="/venv/bin:${PATH}"
ENV MERGIFYENGINE_VERSION=$MERGIFYENGINE_VERSION
ENTRYPOINT ["/app/entrypoint.sh"]
