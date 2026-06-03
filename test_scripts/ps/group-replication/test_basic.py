"""Basic Group Replication sanity test.

Verifies that a freshly bootstrapped cluster has the expected GR configuration and
membership, then creates a database/table and inserts rows on the primary and confirms
the data replicates identically (matching checksums) to all nodes.
"""


def test_replicates_table_across_nodes(gr_cluster):
    gr_cluster.verify()

    gr_cluster.log("create database gr_test")
    gr_cluster.exec_sql("CREATE DATABASE IF NOT EXISTS gr_test;")

    gr_cluster.log("create table gr_test.t")
    gr_cluster.exec_sql("CREATE TABLE gr_test.t (id INT PRIMARY KEY, v VARCHAR(32));")

    gr_cluster.log("insert 3 rows into gr_test.t")
    gr_cluster.exec_sql("INSERT INTO gr_test.t VALUES (1,'a'),(2,'b'),(3,'c');")

    gr_cluster.verify_checksums("gr_test")