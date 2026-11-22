# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit, from its own repository: the selector reads the
# original sources to locate the statements a patch changes.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<'PY'
import io, tarfile, urllib.request, os, shutil
sha = os.environ.get("SQLGLOT_SHA", "eb11aec4be8cb2122afe713a4816662c3f0fb2fa")
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    tf.extractall("/opt", filter="data")
shutil.move(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
PY
RUN python -m compileall -q /opt/sqlglot/sqlglot && chmod -R a+rX /opt/sqlglot

COPY selector.py /app/selector.py
COPY br /app/br
COPY maps /app/maps
# pinned TPC-DS fingerprints, computed in this exact image (see br/tpcds_check.py)
RUN cd /tmp && export PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
    && python -s /app/br/tpcds_probe.py /opt/sqlglot /opt/sqlglot fp > /app/maps/tpcds_fp.json \
    && python -s /app/br/tpcds_probe.py /opt/sqlglot /opt/sqlglot rows > /app/maps/tpcds_rows.json \
    && python -c "import json; assert len(json.load(open('/app/maps/tpcds_fp.json'))) == 99; assert len(json.load(open('/app/maps/tpcds_rows.json'))) == 57"
RUN python /app/br/compile_map.py /app/maps/map.json.gz /app/maps/map.pkl \
    && chmod -R a+rX /app && python -m compileall -q /app/br
CMD ["python", "/app/selector.py"]
