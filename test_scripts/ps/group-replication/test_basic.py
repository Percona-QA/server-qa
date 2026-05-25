import time


def _count_with_retry(docker, node, table_fqn, expected, timeout=30):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = docker.exec_mysql(node, f"SELECT COUNT(*) FROM {table_fqn};", check=False)
        last = result.stdout.strip()
        if result.ok and last == str(expected):
            return last
        time.sleep(1)
    return last


def test_replicates_table_across_nodes(gr_cluster):
    primary = gr_cluster.primary()
    docker = gr_cluster.docker

    gr_cluster.verify()

    docker.exec_mysql(primary, "CREATE DATABASE IF NOT EXISTS gr_test;")
    docker.exec_mysql(
        primary,
        "CREATE TABLE gr_test.t (id INT PRIMARY KEY, v VARCHAR(32));",
    )
    docker.exec_mysql(
        primary,
        "INSERT INTO gr_test.t VALUES (1,'a'),(2,'b'),(3,'c');",
    )