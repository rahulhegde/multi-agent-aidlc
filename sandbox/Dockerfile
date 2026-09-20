FROM python:3.14.7-slim-trixie@sha256:810da6270e43d30a1f3e0e1eabbeb6fbd9d78ad9dd2e754d5297a3d6cb42df46

ADD --checksum=sha256:fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6 https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz /tmp/node.tar.xz
RUN python -c "import tarfile; tarfile.open('/tmp/node.tar.xz').extractall('/opt', filter='data')" \
    && ln -s /opt/node-v24.21.0-linux-x64/bin/node /usr/local/bin/node \
    && ln -s /opt/node-v24.21.0-linux-x64/bin/npm /usr/local/bin/npm \
    && rm /tmp/node.tar.xz
COPY react/ /opt/aidlc/react/
RUN cd /opt/aidlc/react && npm ci --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

COPY requirements.txt /opt/aidlc/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
    -r /opt/aidlc/requirements.txt
COPY worker.py /opt/aidlc/worker.py
ENV LANG=C.UTF-8 RAYON_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "/opt/aidlc/worker.py"]
