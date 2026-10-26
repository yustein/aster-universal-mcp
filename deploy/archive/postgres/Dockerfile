FROM postgres:18.6-bookworm AS vector-build
ARG PGVECTOR_COMMIT=f37c13f68b57d2c3472b2214fbcff699d6d34876
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential ca-certificates git postgresql-server-dev-18 \
    && git init /tmp/pgvector \
    && git -C /tmp/pgvector remote add origin https://github.com/pgvector/pgvector.git \
    && git -C /tmp/pgvector fetch --depth 1 origin "${PGVECTOR_COMMIT}" \
    && git -C /tmp/pgvector checkout --detach FETCH_HEAD \
    && test "$(git -C /tmp/pgvector rev-parse HEAD)" = "${PGVECTOR_COMMIT}" \
    && make -C /tmp/pgvector OPTFLAGS='' PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config \
    && make -C /tmp/pgvector OPTFLAGS='' PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config DESTDIR=/vector install

FROM postgres:18.6-bookworm
# Keep PostgreSQL 18's existing PGDATA=/var/lib/postgresql/18/docker and volume
# root unchanged. This image adds only the pinned extension and fresh-init SQL.
COPY --from=vector-build /vector/usr/lib/postgresql/18/ /usr/lib/postgresql/18/
COPY --from=vector-build /vector/usr/share/postgresql/18/ /usr/share/postgresql/18/
COPY --from=vector-build /tmp/pgvector/LICENSE /usr/share/doc/pgvector/LICENSE
COPY --chmod=644 deploy/archive/postgres/20-vector.sql /docker-entrypoint-initdb.d/20-vector.sql
