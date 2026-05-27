def test_replicates_table_across_nodes(gr_cluster):
    primary = gr_cluster.primary()
    docker = gr_cluster.docker

    gr_cluster.verify()

    gr_cluster.log("create database gr_test")
    docker.exec_mysql(primary, "CREATE DATABASE IF NOT EXISTS gr_test;")

    gr_cluster.log("create table gr_test.t")
    docker.exec_mysql(
        primary,
        "CREATE TABLE gr_test.t (id INT PRIMARY KEY, v VARCHAR(32));",
    )

    gr_cluster.log("insert 3 rows into gr_test.t")
    docker.exec_mysql(
        primary,
        "INSERT INTO gr_test.t VALUES (1,'a'),(2,'b'),(3,'c');",
    )

    gr_cluster.verify_checksums("gr_test")