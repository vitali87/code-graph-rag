#!/bin/bash
set -e

# Fix permission cho home directory cua memgraph (chi thay doi owner cua directory, KHONG recursive)
# Va tao san .memgraph directory voi dung permission
# Buoc nay bat buoc khi chay Docker rootless vi user namespace remapping
chown memgraph:memgraph /home/memgraph
mkdir -p /home/memgraph/.memgraph
chown -R memgraph:memgraph /home/memgraph/.memgraph

# Fix permission cho cac volume mount points
chown -R memgraph:memgraph /var/lib/memgraph
chown -R memgraph:memgraph /var/log/memgraph
chown -R memgraph:memgraph /etc/memgraph

# Switch sang user memgraph va chay Memgraph binary goc
# gosu thay the process hien tai (exec implicit) -> Memgraph tro thanh PID 1
# Dam bao SIGTERM, SIGINT duoc Memgraph xu ly truc tiep
exec gosu memgraph /usr/lib/memgraph/memgraph "$@"
