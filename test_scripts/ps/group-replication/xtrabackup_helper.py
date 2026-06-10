from collections.abc import Callable

from docker_helper import DockerHelper


class XtraBackup:
    """Percona XtraBackup driver for full + incremental backup and restore.

    Each operation runs as a one-off (--rm) container as root (the user Percona
    documents for container backups), mounting the relevant data/backup volumes. The
    backup volume is mounted at /backup, holding the full backup at /backup/full and the
    incremental at /backup/inc1.
    """

    def __init__(
        self,
        docker: DockerHelper,
        network: str,
        backup_volume: str,
        image: str = "percona/percona-xtrabackup:8.4",
        platform: str | None = None,
        root_password: str = "rootpass",
        name_prefix: str = "xtrabackup",
        log: Callable[[str], None] | None = None,
    ):
        self.docker = docker
        self.network = network
        self.backup_volume = backup_volume
        self.image = image
        self.platform = platform
        self.root_password = root_password
        self.name_prefix = name_prefix
        self.log = log or (lambda msg: None)
        self.backup_mount = "/backup"
        self.full_dir = "/backup/full"
        self.inc_dir = "/backup/inc1"

    def _run(self, suffix: str, volumes: list[str], command: str, network: str | None = None):
        """Run a single xtrabackup step in an ephemeral root container."""
        return self.docker.run(
            self.image,
            name=f"{self.name_prefix}_{suffix}",
            networks=[network] if network else [],
            entrypoint="bash",
            command=["-c", command],
            volumes=volumes,
            user="root",
            platform=self.platform,
            remove=True,
            check=True,
        )

    def full_backup(self, source_node: str, source_volume: str):
        """Take a full backup of source_node, reading its data via the shared volume."""
        self.log(f"xtrabackup full backup of {source_node} -> {self.full_dir}")
        command = (
            f"xtrabackup --backup --datadir=/var/lib/mysql --target-dir={self.full_dir} "
            f"--host={source_node} --port=3306 --user=root --password={self.root_password}"
        )
        return self._run(
            "full",
            volumes=[f"{source_volume}:/var/lib/mysql", f"{self.backup_volume}:{self.backup_mount}"],
            command=command,
            network=self.network,
        )

    def incremental_backup(self, source_node: str, source_volume: str):
        """Take an incremental backup of source_node based on the existing full backup."""
        self.log(f"xtrabackup incremental backup of {source_node} -> {self.inc_dir}")
        command = (
            f"xtrabackup --backup --datadir=/var/lib/mysql --target-dir={self.inc_dir} "
            f"--incremental-basedir={self.full_dir} "
            f"--host={source_node} --port=3306 --user=root --password={self.root_password}"
        )
        return self._run(
            "inc",
            volumes=[f"{source_volume}:/var/lib/mysql", f"{self.backup_volume}:{self.backup_mount}"],
            command=command,
            network=self.network,
        )

    def prepare(self):
        """Prepare the full backup and merge the incremental into it, ready to restore.

        The base is prepared with --apply-log-only (no rollback yet); the single/last
        incremental is then merged without --apply-log-only so the final rollback runs
        and the backup becomes consistent.
        """
        self.log("xtrabackup prepare (full + incremental)")
        command = (
            f"xtrabackup --prepare --apply-log-only --target-dir={self.full_dir} && "
            f"xtrabackup --prepare --target-dir={self.full_dir} --incremental-dir={self.inc_dir}"
        )
        return self._run(
            "prepare",
            volumes=[f"{self.backup_volume}:{self.backup_mount}"],
            command=command,
        )

    def copy_back(self, target_volume: str):
        """Restore the prepared backup into an empty target volume and fix ownership.

        chown to mysql:mysql works because the XtraBackup and Percona Server images
        share the same mysql user, so the restored datadir is readable by the server.
        """
        self.log(f"xtrabackup copy-back {self.full_dir} -> {target_volume}")
        command = (
            f"xtrabackup --copy-back --target-dir={self.full_dir} --datadir=/var/lib/mysql && "
            "chown -R mysql:mysql /var/lib/mysql"
        )
        return self._run(
            "restore",
            volumes=[f"{target_volume}:/var/lib/mysql", f"{self.backup_volume}:{self.backup_mount}"],
            command=command,
        )

    def cleanup(self):
        """Remove the backup volume."""
        self.docker.volume_remove(self.backup_volume)
