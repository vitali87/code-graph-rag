# Fix Memgraph Permission Denied trong Docker Rootless Mode

Lien ket GitHub Issue: [#1](https://github.com/NguyenThanhHungDev140503/code-graph-rag-OpenAI-compatible/issues/1)

## Boi canh

Khi chay Docker Rootless, user namespace remapping khien user `memgraph` (UID 101) trong container mat quyen ghi vao `/home/memgraph/` (thuoc `root:root`, quyen `0750`). Memgraph can thu muc nay de tao `.memgraph/config` khi khoi dong.

## Proposed Changes

### Component: Docker deployment

---

#### [NEW] [Dockerfile.memgraph](file:///home/nguyen-thanh-hung/Documents/Code/code-graph-rag/Dockerfile.memgraph)

Custom Dockerfile extend tu `memgraph/memgraph-mage`:

```dockerfile
FROM memgraph/memgraph-mage

USER root

# Cai gosu de switch user an toan (signal handling dung voi PID 1)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/* && \
    gosu nobody true

# Copy entrypoint script
COPY memgraph-entrypoint.sh /usr/local/bin/memgraph-entrypoint.sh
RUN chmod +x /usr/local/bin/memgraph-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/memgraph-entrypoint.sh"]
CMD [""]
```

**Chi tiet**:
- `FROM memgraph/memgraph-mage`: Ke thua toan bo image goc (Memgraph + MAGE modules)
- `USER root`: Chuyen sang root de cai gosu va set entrypoint
- `gosu nobody true`: Verify gosu hoat dong dung sau khi cai
- ENTRYPOINT moi tro toi script custom, CMD giu nguyen rong nhu image goc

---

#### [NEW] [memgraph-entrypoint.sh](file:///home/nguyen-thanh-hung/Documents/Code/code-graph-rag/memgraph-entrypoint.sh)

Script entrypoint thay the:

```bash
#!/bin/bash
set -e

# Fix permission cho home directory cua memgraph
# Day la buoc bat buoc khi chay Docker rootless vi user namespace remapping
# lam cho /home/memgraph (thuoc root:root trong image) khong ghi duoc boi user memgraph
chown -R memgraph:memgraph /home/memgraph

# Fix permission cho cac volume mount points
chown -R memgraph:memgraph /var/lib/memgraph
chown -R memgraph:memgraph /var/log/memgraph
chown -R memgraph:memgraph /etc/memgraph

# Switch sang user memgraph va chay Memgraph binary goc
# gosu thay the process hien tai (exec implicit) -> Memgraph tro thanh PID 1
# Dam bao SIGTERM, SIGINT duoc Memgraph xu ly truc tiep
exec gosu memgraph /usr/lib/memgraph/memgraph "$@"
```

**Chi tiet luong thuc thi**:
```
Container start (root, UID 0)
    |
    v
memgraph-entrypoint.sh (PID 1, root)
    |-- chown /home/memgraph -> memgraph:memgraph
    |-- chown /var/lib/memgraph -> memgraph:memgraph
    |-- chown /var/log/memgraph -> memgraph:memgraph
    |-- chown /etc/memgraph -> memgraph:memgraph
    |
    v
exec gosu memgraph /usr/lib/memgraph/memgraph
    |
    v
Memgraph binary (PID 1, memgraph UID 101)
    |-- Tao /home/memgraph/.memgraph/config -> OK (co quyen ghi)
    |-- Khoi dong database -> OK
```

**Tai sao dung `gosu` thay vi `su`**:

| | `gosu` | `su` |
|---|---|---|
| PID | Thay the PID 1 (exec) | Tao subprocess (PID khac) |
| Signal | SIGTERM toi Memgraph truc tiep | SIGTERM toi su, khong forward |
| Zombie | Khong | Co the tao zombie process |
| Docker best practice | Co | Khong |

---

#### [MODIFY] [docker-compose.yaml](file:///home/nguyen-thanh-hung/Documents/Code/code-graph-rag/docker-compose.yaml)

Thay doi service `memgraph` tu dung image truc tiep sang build tu Dockerfile:

```diff
 services:
   memgraph:
-    image: memgraph/memgraph-mage
+    build:
+      context: .
+      dockerfile: Dockerfile.memgraph
     ports:
       - "${MEMGRAPH_PORT:-7687}:7687"
       - "${MEMGRAPH_HTTP_PORT:-7444}:7444"
```

**Chi tiet**:
- `build.context: .` - Build context la thu muc hien tai (chua Dockerfile va entrypoint script)
- `build.dockerfile: Dockerfile.memgraph` - Ten file cu the de khong conflict voi Dockerfile khac (neu co)

---

#### [MODIFY] [.gitignore](file:///home/nguyen-thanh-hung/Documents/Code/code-graph-rag/.gitignore)

Khong can thay doi gi - cac file moi (`Dockerfile.memgraph`, `memgraph-entrypoint.sh`) can duoc track boi git.

---

## Verification Plan

### Automated Tests

1. **Build image**:
```bash
cd /home/nguyen-thanh-hung/Documents/Code/code-graph-rag
docker compose build memgraph
```
Ky vong: Build thanh cong, khong loi.

2. **Khoi dong service**:
```bash
docker compose down -v
docker compose up -d memgraph lab
```
Ky vong: Ca 2 container deu `Up`.

3. **Kiem tra log Memgraph** (doi 5 giay):
```bash
sleep 5
docker logs code-graph-rag-memgraph-1 --tail 20
```
Ky vong: Thay `You are running Memgraph v3.8.1`, KHONG co `Permission denied`.

4. **Kiem tra process user trong container**:
```bash
docker exec code-graph-rag-memgraph-1 ps aux
```
Ky vong: Memgraph process chay boi user `memgraph`, KHONG phai `root`.

5. **Kiem tra ket noi Memgraph qua port 7687**:
```bash
docker exec code-graph-rag-memgraph-1 mgconsole --host 127.0.0.1 --port 7687 <<< "RETURN 1 AS test;"
```
Ky vong: Tra ve ket qua `1`.

6. **Kiem tra Memgraph Lab truy cap duoc**:
Mo browser tai `http://localhost:3000`, ket noi toi Memgraph.

### Manual Verification

7. **Reboot test** (khong bat buoc):
Khoi dong lai may, kiem tra Docker rootless tu dong start va Memgraph chay dung.
