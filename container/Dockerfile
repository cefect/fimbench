# syntax=docker/dockerfile:1.7

FROM condaforge/miniforge3:25.3.1-0 AS dev

ARG CONDA_NAME=fimbench
ARG PYTHON_VERSION=3.11

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONDA_NAME=${CONDA_NAME} \
    CONDA_DEFAULT_ENV=${CONDA_NAME} \
    PATH=/opt/conda/envs/${CONDA_NAME}/bin:/opt/conda/bin:${PATH}

WORKDIR /workspace

# Install basic development tools.
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a dedicated conda environment.
RUN --mount=type=cache,target=/opt/conda/pkgs \
    conda create -y -n "${CONDA_NAME}" "python=${PYTHON_VERSION}" pip \
    && conda clean -afy

# Install python dependencies first for efficient layer reuse.
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    conda run -n "${CONDA_NAME}" python -m pip install --upgrade pip \
    && conda run -n "${CONDA_NAME}" python -m pip install -r /tmp/requirements.txt \
    && conda run -n "${CONDA_NAME}" python -m pip install fimeval requests tqdm pytest

# Copy source after dependency install so edits do not invalidate dependency layers.
COPY . /workspace

# Build-time smoke tests for key runtime paths.
RUN conda run -n "${CONDA_NAME}" python -c "import fimeval, geopandas, requests, streamlit; import fetch_all; print('smoke imports ok')" \
    && conda run -n "${CONDA_NAME}" python fetch_all.py --help >/tmp/fetch_all_help.txt

CMD ["sleep", "infinity"]
