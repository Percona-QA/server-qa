#!/bin/bash

#############################################################################
# Created By Manish Chawla, Percona LLC                                     #
# Modified By Mohit Joshi, Percona LLC
# This script tests backup with a load tool as pquery/pstress/sysbench      #
# Assumption: PS and PXB are already installed as tarballs                  #
# Usage:                                                                    #
# 1. Compile pquery/pstress with mysql                                      #
# 2. Set variables in this script:                                          #
#    xtrabackup_dir, mysqldir, datadir, backup_dir, qascripts, logdir,      #
#    load_tool, tool_dir, num_tables, table_size, kmip, kms configuration   #
# 3. For usage run the script as: ./inc_backup_load_tests.sh                #
# 4. Logs are available in: logdir                                          #
#############################################################################

# Set script variables
export xtrabackup_dir="$HOME/pxb-9.1/bld_9.1/install/bin"
export mysqldir="$HOME/mysql-9.1/bld_9.1/install"
export datadir="${mysqldir}/data"
export backup_dir="$HOME/dbbackup_$(date +"%d_%m_%Y")"
export PATH="$PATH:$xtrabackup_dir"
source "$(dirname "${BASH_SOURCE[0]}")/pxb_helper.sh"
init_pxb_version
export qascripts="$HOME/percona-qa"
export logdir="$HOME/backuplogs"
export mysql_start_timeout=60
declare -A KMIP_CONFIGS=(
    ["hashicorp"]="addr=127.0.0.1,port=5696,name=kmip_hashicorp,setup_script=hashicorp-kmip-setup.py"

    # Fortanix Setup Configuration
    ["fortanix"]="addr=216.180.120.88,port=5696,name=kmip_fortanix,setup_script=fortanix_kmip_setup.py"

    # PyKMIP Docker Configuration
    #["pykmip"]="addr=127.0.0.1,image=satyapercona/kmip:latest,port=5696,name=kmip_pykmip"

    # API Configuration
    # ["ciphertrust"]="addr=127.0.0.1,port=5696,name=kmip_ciphertrust,setup_script=setup_kmip_api.py"
)

# Set tool variables
load_tool="pstress" # Set value as pquery/pstress/sysbench
num_tables=10 # Used for Sysbench
table_size=1000 # Used for Sysbench
tool_dir="$HOME/pstress_9.1/src" # pstress dir

if ${mysqldir}/bin/mysqld --version | grep "MySQL Community Server" > /dev/null 2>&1 ; then
  MS=1
else
  MS=0
fi

initialize_db() {
local keyring_type="$1"
local kmip_type="$2"
# This function initializes and starts mysql database
if [ ! -d "${logdir}" ]; then
    mkdir "${logdir}"
fi

if [[ "${MYSQLD_OPTIONS}" != *"keyring"* ]]; then
  if [ "$keyring_type" = "keyring_kmip" ]; then
    echo "Keyring type is KMIP. Taking KMIP-specific action..."

    echo '{
      "components": "file://component_keyring_kmip"
    }' > "$mysqldir/bin/mysqld.my"

    start_kmip_server "$kmip_type"
    [ -f "${HOME}/${kimp_config[cert_dir]}/component_keyring_kmip.cnf" ] && cp "${HOME}/${kimp_config[cert_dir]}/component_keyring_kmip.cnf" "$mysqldir/lib/plugin/"

  elif [ "$keyring_type" = "keyring_file" ]; then
    echo "Keyring type is file. Taking file-based action..."

    echo '{
      "components": "file://component_keyring_file"
    }' > "$mysqldir/bin/mysqld.my"

    cat > "$mysqldir/lib/plugin/component_keyring_file.cnf" <<-EOFL
    {
       "component_keyring_file_data": "${mysqldir}/keyring",
       "read_only": false
    }
EOFL
  fi
fi
  echo "=>Creating data directory"
  $mysqldir/bin/mysqld --no-defaults --datadir=$datadir --initialize-insecure > $mysqldir/mysql_install_db.log 2>&1
  echo "..Data directory created"

  start_server
  $mysqldir/bin/mysql -uroot -S$mysqldir/socket.sock -e "DROP DATABASE IF EXISTS test"
  $mysqldir/bin/mysql -uroot -S$mysqldir/socket.sock -e "CREATE DATABASE IF NOT EXISTS test"

  # Create data using sysbench
  if [[ "${load_tool}" = "sysbench" ]]; then
    if [[ "${MYSQLD_OPTIONS}" != *"keyring"* ]]; then
      sysbench /usr/share/sysbench/oltp_insert.lua --tables=${num_tables} --table-size=${table_size} --mysql-db=test --mysql-user=root --threads=100 --db-driver=mysql --mysql-socket="${mysqldir}"/socket.sock prepare >"${logdir}"/sysbench.log
    else
      # Encryption enabled
      for ((i=1; i<=num_tables; i++)); do
        echo "Creating the table sbtest$i..."
        "${mysqldir}"/bin/mysql -uroot -S"${mysqldir}"/socket.sock -e "CREATE TABLE test.sbtest$i (id int(11) NOT NULL AUTO_INCREMENT, k int(11) NOT NULL DEFAULT '0', c char(120) NOT NULL DEFAULT '', pad char(60) NOT NULL DEFAULT '', PRIMARY KEY (id), KEY k_1 (k)) ENGINE=InnoDB DEFAULT CHARSET=latin1 ENCRYPTION='Y';"
      done
    fi
  fi
}

run_crash_tests_pstress() {
    # This function crashes the server during load and then runs backup
    local test_type="$1"
    local kmip_type="$2"

    if [[ "${test_type}" == *keyring* ]]; then
      echo "Running crash tests with ${load_tool} and mysql running with encryption"
      if ${mysqldir}/bin/mysqld --version | grep "MySQL Community Server" >/dev/null 2>&1 ; then
          # Server is MS 8.0
          MYSQLD_OPTIONS="--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --binlog-rotate-encryption-master-key-at-startup --table-encryption-privilege-check=ON --max-connections=5000 --binlog-encryption"
          load_options="--tables 10 --records 200 --threads 10 --seconds 50 --undo-tbs-sql 0 --no-column-compression" # MS does not support column compression
      else
          # Server is PS 8.0
          MYSQLD_OPTIONS="--innodb-undo-log-encrypt --innodb-redo-log-encrypt --default-table-encryption=ON --innodb_encrypt_online_alter_logs=ON --innodb_temp_tablespace_encrypt=ON --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --encrypt-tmp-files --table-encryption-privilege-check=ON --max-connections=5000"
          load_options="--tables 10 --records 200 --threads 10 --seconds 50 --undo-tbs-sql 0" # Used for pstress
      fi
      BACKUP_PARAMS="--xtrabackup-plugin-dir=${xtrabackup_dir}/../lib/plugin --core-file"
      if [ "$test_type" = "keyring_kmip" ]; then
        keyring_filename="${mysqldir}/lib/plugin/component_keyring_kmip.cnf"
      elif [ "$test_type" = "keyring_file" ]; then
        keyring_filename="${mysqldir}/lib/plugin/component_keyring_file.cnf"
      fi
      PREPARE_PARAMS="${BACKUP_PARAMS} --component-keyring-config=$keyring_filename"
      RESTORE_PARAMS="${BACKUP_PARAMS}"
    elif [[ "${test_type}" = "rocksdb" ]]; then
      echo "Running crash tests with ${load_tool} for rocksdb"
      MYSQLD_OPTIONS="--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
      BACKUP_PARAMS="--core-file --lock-ddl"
      PREPARE_PARAMS="--core-file"
      RESTORE_PARAMS=""
      load_options="--tables 10 --records 1000 --threads 10 --seconds 150 --no-encryption --engine=rocksdb"
    else
      echo "Running crash tests with ${load_tool}"
      MYSQLD_OPTIONS="--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
      BACKUP_PARAMS="--core-file --lock-ddl"
      PREPARE_PARAMS="--core-file"
      RESTORE_PARAMS=""
      if [ $MS -eq 1 ]; then
        load_options="--tables 10 --records 200 --threads 5 --no-encryption --undo-tbs-sql 0 --no-column-compression"
      else
        load_options="--tables 10 --records 200 --threads 5 --no-encryption --undo-tbs-sql 0"
      fi
    fi

    if [ -d "${backup_dir}" ]; then
        rm -r "${backup_dir}"
    fi
    mkdir "${backup_dir}"
    log_date=$(date +"%d_%m_%Y_%M")

    cleanup
    initialize_db $test_type $kmip_type
    if [ "$test_type" = "rocksdb" ]; then
      $mysqldir/bin/ps-admin --enable-rocksdb -uroot -S${mysqldir}/socket.sock >/dev/null 2>&1
    fi

    if [[ "$3" = "pagetracking" ]]; then
        echo "Running test with page tracking enabled"
        BACKUP_PARAMS="${BACKUP_PARAMS} --page-tracking"
        "${mysqldir}"/bin/mysql -uroot -S"${mysqldir}"/socket.sock -e "INSTALL COMPONENT 'file://component_mysqlbackup';"
    fi

    echo "=>Run pstress to prepare metadata: ${load_options}"
    pushd "$tool_dir" >/dev/null 2>&1 || exit
    ./pstress-ps ${load_options} --prepare --exact-initial-records --logdir=${logdir}/pstress --socket ${mysqldir}/socket.sock >"${logdir}"/pstress/pstress_prepare.log
    popd >/dev/null 2>&1 || exit
    echo "..Metadata created"
    run_load "${load_options} --step 2"
    echo "=>Taking full backup"
    rr "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --password='' --backup --target-dir="${backup_dir}"/full -S "${mysqldir}"/socket.sock --datadir="${datadir}" ${BACKUP_PARAMS} 2>"${logdir}"/full_backup_"${log_date}"_log
    if [ "$?" -ne 0 ]; then
        echo "ERR: Full Backup failed. Please check the log at: ${logdir}/full_backup_${log_date}_log"
        exit 1
    else
        echo "..Full backup was successfully created at: ${backup_dir}/full. Logs available at: ${logdir}/full_backup_${log_date}_log"
    fi

    # Save the full backup dir
    cp -pr ${backup_dir}/full ${backup_dir}/full_save

    sleep 1

    if [ -d ${mysqldir}/data_crash_save1 ]; then
            rm -r ${mysqldir}/data_crash_save1
    fi

    echo "Crash the mysql server"
    {  kill -15 $MPID && wait $MPID; } 2>/dev/null
    cp -pr ${mysqldir}/data ${mysqldir}/data_crash_save1

    start_server
    run_load "${load_options} --step 3"

    for inc_num in $(seq 1 4); do
      echo "Taking incremental backup: $inc_num"
      if [ ${inc_num} -eq 1 ]; then
        rr ${xtrabackup_dir}/xtrabackup --no-defaults --user=root --password='' --backup --target-dir=${backup_dir}/inc${inc_num} --incremental-basedir=${backup_dir}/full -S ${mysqldir}/socket.sock --datadir=${datadir} ${BACKUP_PARAMS} 2>${logdir}/inc${inc_num}_backup_${log_date}_log
      else
        rr ${xtrabackup_dir}/xtrabackup --no-defaults --user=root --password='' --backup --target-dir=${backup_dir}/inc${inc_num} --incremental-basedir=${backup_dir}/inc$((inc_num - 1)) -S ${mysqldir}/socket.sock --datadir=${datadir} ${BACKUP_PARAMS} 2>${logdir}/inc${inc_num}_backup_${log_date}_log
      fi
      if [ "$?" -ne 0 ]; then
        echo "ERR: Incremental Backup failed. Please check the log at: ${logdir}/inc${inc_num}_backup_${log_date}_log"
        exit 1
      else
        echo "Inc backup was successfully created at: ${backup_dir}/inc${inc_num} Logs available at: ${logdir}/inc${inc_num}_backup_${log_date}_log"
      fi

      # Save the incremental backup dir
      cp -pr ${backup_dir}/inc${inc_num} ${backup_dir}/inc${inc_num}_save
    done

    echo "Crash the mysql server"
    {  kill -15 $MPID && wait $MPID; } 2>/dev/null
    cp -pr ${mysqldir}/data ${mysqldir}/data_crash_save2
    start_server
    run_load "${load_options} --step 4"

    for ((inc_num=5;inc_num<9;inc_num++)); do
      echo "Taking incremental backup: $inc_num"
      if [ ${inc_num} -eq 1 ]; then
        rr ${xtrabackup_dir}/xtrabackup --no-defaults --user=root --password='' --backup --target-dir=${backup_dir}/inc${inc_num} --incremental-basedir=${backup_dir}/full -S ${mysqldir}/socket.sock --datadir=${datadir} ${BACKUP_PARAMS} 2>${logdir}/inc${inc_num}_${i}_backup_${log_date}_log
      else
        rr ${xtrabackup_dir}/xtrabackup --no-defaults --user=root --password='' --backup --target-dir=${backup_dir}/inc${inc_num} --incremental-basedir=${backup_dir}/inc$((inc_num - 1)) -S ${mysqldir}/socket.sock --datadir=${datadir} ${BACKUP_PARAMS} 2>${logdir}/inc${inc_num}_backup_${log_date}_log
      fi
      if [ "$?" -ne 0 ]; then
        echo "ERR: Incremental Backup failed. Please check the log at: ${logdir}/inc${inc_num}_backup_${log_date}_log"
        exit 1
      else
        echo "Inc backup was successfully created at: ${backup_dir}/inc${inc_num} Logs available at: ${logdir}/inc${inc_num}_backup_${log_date}_log"
      fi

      # Save the incremental backup dir
      cp -pr ${backup_dir}/inc${inc_num} ${backup_dir}/inc${inc_num}_save
    done

    echo "Preparing full backup"
    PREPARE_PARAMS=$(prepare_args_for_pxb_version "$PREPARE_PARAMS")
    rr ${xtrabackup_dir}/xtrabackup --no-defaults --prepare --apply-log-only --target_dir=${backup_dir}/full ${PREPARE_PARAMS} 2>${logdir}/prepare_full_backup_${log_date}_log
    if [ "$?" -ne 0 ]; then
        echo "ERR: Prepare of full backup failed. Please check the log at: ${logdir}/prepare_full_backup_${log_date}_log"
        exit 1
    else
        echo "Prepare of full backup was successful. Logs available at: ${logdir}/prepare_full_backup_${log_date}_log"
    fi

    for ((i=1; i<$inc_num; i++)); do
      echo "Preparing incremental backup: $i"
        if [[ "${i}" -eq "${inc_num}-1" ]]; then
          rr ${xtrabackup_dir}/xtrabackup --no-defaults --prepare --target_dir=${backup_dir}/full --incremental-dir=${backup_dir}/inc${i} ${PREPARE_PARAMS} 2>${logdir}/prepare_inc${i}_backup_${log_date}_log
        else
          rr ${xtrabackup_dir}/xtrabackup --no-defaults --prepare --apply-log-only --target_dir=${backup_dir}/full --incremental-dir=${backup_dir}/inc${i} ${PREPARE_PARAMS} 2>${logdir}/prepare_inc${i}_backup_${log_date}_log
        fi
      if [ "$?" -ne 0 ]; then
        echo "ERR: Prepare of incremental backup failed. Please check the log at: ${logdir}/prepare_inc${i}_backup_${log_date}_log"
        exit 1
      else
        echo "Prepare of incremental backup was successful. Logs available at: ${logdir}/prepare_inc${i}_backup_${log_date}_log"
      fi
    done

    echo "Stopping mysql server and moving data directory"
    "${mysqldir}"/bin/mysqladmin -uroot -S"${mysqldir}"/socket.sock shutdown
    if [ -d "${mysqldir}"/data_orig_"$(date +"%d_%m_%Y")" ]; then
        rm -r "${mysqldir}"/data_orig_"$(date +"%d_%m_%Y")"
    fi
    mv "${mysqldir}"/data "${mysqldir}"/data_orig_"$(date +"%d_%m_%Y")"

    echo "Restoring full backup"
    "${xtrabackup_dir}"/xtrabackup --no-defaults --copy-back --target-dir="${backup_dir}"/full --datadir="${datadir}" ${RESTORE_PARAMS} 2>"${logdir}"/res_backup_"${log_date}"_log
    if [ "$?" -ne 0 ]; then
        echo "ERR: Restore of full backup failed. Please check the log at: ${logdir}/res_backup_${log_date}_log"
        exit 1
    else
        echo "Restore of full backup was successful. Logs available at: ${logdir}/res_backup_${log_date}_log"
    fi

    start_server

    # Binlog can't be applied if binlog is encrypted or skipped
    if [[ "${MYSQLD_OPTIONS}" != *"binlog-encryption"* ]] && [[ "${MYSQLD_OPTIONS}" != *"--encrypt-binlog"* ]] && [[ "${MYSQLD_OPTIONS}" != *"skip-log-bin"* ]]; then
        echo "Check xtrabackup for binlog position"
        xb_binlog_file=$(cat "${backup_dir}"/full/xtrabackup_binlog_info|awk '{print $1}'|head -1)
        xb_binlog_pos=$(cat "${backup_dir}"/full/xtrabackup_binlog_info|awk '{print $2}'|head -1)
        echo "Xtrabackup binlog position: $xb_binlog_file, $xb_binlog_pos"

        echo "Applying binlog to restored data starting from $xb_binlog_file, $xb_binlog_pos"
        "${mysqldir}"/bin/mysqlbinlog "${mysqldir}"/data_orig_$(date +"%d_%m_%Y")/$xb_binlog_file --start-position=$xb_binlog_pos | "${mysqldir}"/bin/mysql -uroot -S"${mysqldir}"/socket.sock
        if [ "$?" -ne 0 ]; then
            echo "ERR: The binlog could not be applied to the restored data"
        fi
        sleep 5
    else
        echo "Binlog applying skipped, ignore differences between actual data and restored data"

    fi

}

run_load() {
  # This function runs a load using pquery/sysbench
  local tool_options="$1"
  if [[ "${load_tool}" = "pstress" ]]; then
    echo "Run pstress with options: ${tool_options}"
    pushd "$tool_dir" >/dev/null 2>&1 || exit
    ./pstress-ps ${tool_options} --logdir=${logdir}/pstress --socket ${mysqldir}/socket.sock  > $logdir/pstress/pstress.log &
    popd >/dev/null 2>&1 || exit
    sleep 2
  else
    echo "Run sysbench"
    sysbench /usr/share/sysbench/oltp_insert.lua --tables=${num_tables} --mysql-db=test --mysql-user=root --threads=100 --db-driver=mysql --mysql-socket="${mysqldir}"/socket.sock --time=200 run >>"${logdir}"/sysbench.log &
  fi
}

take_backup() {
  # This function takes the incremental backup
  if [ -d "${backup_dir}" ]; then
    rm -r "${backup_dir}"
  fi
  mkdir "${backup_dir}"
  log_date=$(date +"%d_%m_%Y_%M")

  echo "=>Taking full backup"
  ${xtrabackup_dir}/xtrabackup --no-defaults --user=root --password='' --backup --target-dir=${backup_dir}/full -S ${mysqldir}/socket.sock --datadir=${datadir} ${BACKUP_PARAMS} 2>${logdir}/full_backup_${log_date}_log
  if [ "$?" -ne 0 ]; then
    echo "ERR: Full Backup failed. Please check the log at: ${logdir}/full_backup_${log_date}_log"
    exit 1
  else
    echo "..Full backup was successfully created at: ${backup_dir}/full. Logs available at: ${logdir}/full_backup_${log_date}_log"
  fi

  sleep 1
  inc_num=1
  while [[ $(pgrep ${load_tool}) ]]; do
    echo "=>Taking incremental backup: $inc_num"
    if [[ "${inc_num}" -eq 1 ]]; then
      "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --password='' --backup --target-dir="${backup_dir}"/inc${inc_num} --incremental-basedir="${backup_dir}"/full -S "${mysqldir}"/socket.sock --datadir="${datadir}" ${BACKUP_PARAMS} 2>"${logdir}"/inc${inc_num}_backup_"${log_date}"_log
    else
      "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --password='' --backup --target-dir="${backup_dir}"/inc${inc_num} --incremental-basedir="${backup_dir}"/inc$((inc_num - 1)) -S "${mysqldir}"/socket.sock --datadir="${datadir}" ${BACKUP_PARAMS} 2>"${logdir}"/inc${inc_num}_backup_"${log_date}"_log
    fi
    if [ "$?" -ne 0 ]; then
      grep -e "PXB will not be able to make a consistent backup" -e "PXB will not be able to take a consistent backup" "${logdir}"/inc${inc_num}_backup_"${log_date}"_log
      if [ "$?" -eq 0 ]; then
        echo "Retrying incremental backup with --lock-ddl option"
        rm -r "${backup_dir}"/inc${inc_num}

        if [[ "${inc_num}" -eq 1 ]]; then
          "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --password='' --backup --target-dir="${backup_dir}"/inc${inc_num} --incremental-basedir="${backup_dir}"/full -S "${mysqldir}"/socket.sock --datadir="${datadir}" ${BACKUP_PARAMS} --lock-ddl 2>"${logdir}"/inc${inc_num}_backup_"${log_date}"_log
        else
          "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --password='' --backup --target-dir="${backup_dir}"/inc${inc_num} --incremental-basedir="${backup_dir}"/inc$((inc_num - 1)) -S "${mysqldir}"/socket.sock --datadir="${datadir}" ${BACKUP_PARAMS} --lock-ddl 2>>"${logdir}"/inc${inc_num}_backup_"${log_date}"_log
          if [ "$?" -ne 0 ]; then
            echo "ERR: Incremental Backup failed. Please check the log at: ${logdir}/inc${inc_num}_backup_${log_date}_log"
            exit 1
          fi
        fi
      else
        echo "ERR: Incremental Backup failed. Please check the log at: ${logdir}/inc${inc_num}_backup_${log_date}_log"
        exit 1
      fi
    else
      echo "..Inc backup was successfully created at: ${backup_dir}/inc${inc_num}. Logs available at: ${logdir}/inc${inc_num}_backup_${log_date}_log"
    fi
    let inc_num++
    sleep 2
  done

  echo "=>Preparing full backup"
  PREPARE_PARAMS=$(prepare_args_for_pxb_version "$PREPARE_PARAMS")
  "${xtrabackup_dir}"/xtrabackup --no-defaults --prepare --apply-log-only --target_dir="${backup_dir}"/full ${PREPARE_PARAMS} 2>"${logdir}"/prepare_full_backup_"${log_date}"_log
  if [ "$?" -ne 0 ]; then
    echo "ERR: Prepare of full backup failed. Please check the log at: ${logdir}/prepare_full_backup_${log_date}_log"
    exit 1
  else
    echo "..Prepare of full backup was successful. Logs available at: ${logdir}/prepare_full_backup_${log_date}_log"
  fi

  for ((i=1; i<inc_num; i++)); do
    echo "=>Preparing incremental backup: $i"
    if [[ "${i}" -eq "${inc_num}-1" ]]; then
      "${xtrabackup_dir}"/xtrabackup --no-defaults --prepare --target_dir="${backup_dir}"/full --incremental-dir="${backup_dir}"/inc"${i}" ${PREPARE_PARAMS} 2>"${logdir}"/prepare_inc"${i}"_backup_"${log_date}"_log
    else
      "${xtrabackup_dir}"/xtrabackup --no-defaults --prepare --apply-log-only --target_dir="${backup_dir}"/full --incremental-dir="${backup_dir}"/inc"${i}" ${PREPARE_PARAMS} 2>"${logdir}"/prepare_inc"${i}"_backup_"${log_date}"_log
    fi
    if [ "$?" -ne 0 ]; then
      echo "ERR: Prepare of incremental backup failed. Please check the log at: ${logdir}/prepare_inc${i}_backup_${log_date}_log"
      exit 1
    else
      echo "..Prepare of incremental backup was successful. Logs available at: ${logdir}/prepare_inc${i}_backup_${log_date}_log"
    fi
  done

  echo "Collecting existing table count"
  pushd "$mysqldir" >/dev/null 2>&1 || exit
  pt-table-checksum S=${PWD}/socket.sock,u=root -d test --recursion-method hosts --no-check-binlog-format| awk '{print $4,$9}' >file1
  popd >/dev/null 2>&1 || exit
  sleep 2

  echo "Stopping mysql server and moving data directory"
  ${mysqldir}/bin/mysqladmin -uroot -S${mysqldir}/socket.sock shutdown
  if [ -d "${mysqldir}"/data_orig_"$(date +"%d_%m_%Y")" ]; then
    rm -r "${mysqldir}"/data_orig_"$(date +"%d_%m_%Y")"
  fi
  mv "${mysqldir}"/data "${mysqldir}"/data_orig_"$(date +"%d_%m_%Y")"

  echo "=>Restoring full backup"
  "${xtrabackup_dir}"/xtrabackup --no-defaults --copy-back --target-dir="${backup_dir}"/full --datadir="${datadir}" ${RESTORE_PARAMS} 2>"${logdir}"/res_backup_"${log_date}"_log
  if [ "$?" -ne 0 ]; then
    echo "ERR: Restore of full backup failed. Please check the log at: ${logdir}/res_backup_${log_date}_log"
    exit 1
  else
    echo "..Restore of full backup was successful. Logs available at: ${logdir}/res_backup_${log_date}_log"
  fi

  start_server

  # Binlog can't be applied if binlog is encrypted or skipped
  if [[ "${MYSQLD_OPTIONS}" != *"binlog-encryption" ]] && [[ "${MYSQLD_OPTIONS}" != *"--encrypt-binlog"* ]] && [[ "${MYSQLD_OPTIONS}" != *"skip-log-bin"* ]]; then
    echo "Check xtrabackup for binlog position"
    xb_binlog_file=$(cat "${backup_dir}"/full/xtrabackup_binlog_info|awk '{print $1}'|head -1)
    xb_binlog_pos=$(cat "${backup_dir}"/full/xtrabackup_binlog_info|awk '{print $2}'|head -1)
    echo "Xtrabackup binlog position: $xb_binlog_file, $xb_binlog_pos"

    echo "Applying binlog to restored data starting from $xb_binlog_file, $xb_binlog_pos"
    "${mysqldir}"/bin/mysqlbinlog "${mysqldir}"/data_orig_$(date +"%d_%m_%Y")/$xb_binlog_file --start-position=$xb_binlog_pos | "${mysqldir}"/bin/mysql -uroot -S"${mysqldir}"/socket.sock
    if [ "$?" -ne 0 ]; then
      echo "ERR: The binlog could not be applied to the restored data"
    fi
    sleep 3
  fi
}

start_server() {
  # This function starts the server
  echo "=>Starting MySQL server"
  rr $mysqldir/bin/mysqld --no-defaults --basedir=$mysqldir --datadir=$datadir $MYSQLD_OPTIONS --port=21000 --socket=$mysqldir/socket.sock --plugin-dir=$mysqldir/lib/plugin --max-connections=1024 --log-error=$mysqldir/error.log  --general-log --log-error-verbosity=3 --core-file > /dev/null 2>&1 &
  MPID="$!"

  for X in $(seq 0 ${mysql_start_timeout}); do
    sleep 1
    if ${mysqldir}/bin/mysqladmin -uroot -S${mysqldir}/socket.sock ping > /dev/null 2>&1; then
      echo "..Server started successfully"
      break
    fi
    if [ $X -eq ${mysql_start_timeout} ]; then
      echo "ERR: Database could not be started. Please check error logs: ${mysqldir}/data/error.log"
      exit 1
    fi
  done
}

run_load_tests() {
    # This function runs the load backup tests with normal options
    MYSQLD_OPTIONS="--log-bin=binlog --log-slave-updates --gtid-mode=ON --enforce-gtid-consistency --binlog-format=row --master_verify_checksum=ON --binlog_checksum=CRC32 --max-connections=5000"
    BACKUP_PARAMS="--core-file --lock-ddl"
    PREPARE_PARAMS="--core-file"
    RESTORE_PARAMS=""

    original_tool="${load_tool}"

    # Pstress options
    if [[ "$1" = "rocksdb" ]]; then
        echo "Test: Incremental Backup and Restore for rocksdb with ${load_tool}"
        tool_options="--tables 10 --records 200 --threads 10 --seconds 150 --no-encryption --engine=rocksdb"
    elif [[ "$1" = "memory_estimation" ]]; then
        if "${mysqldir}"/bin/mysqld --version | grep "5.7" >/dev/null 2>&1 ; then
            echo "Memory estimation is not supported in PXB 2.4 version, skipping tests"
            return
        fi

        # This test can only be run using sysbench, since all ddl are blocked with lock-ddl
        load_tool="sysbench"

        echo "Test: Incremental Backup and Restore with ${load_tool} and using memory estimation"
        BACKUP_PARAMS="--core-file --lock-ddl"
        PREPARE_PARAMS="--core-file --use-free-memory-pct=20"
    else
        echo "Test: Incremental Backup and Restore with ${load_tool}"
        tool_options="--tables 10 --records 200 --threads 10 --seconds 30 --no-encryption --undo-tbs-sql 0"
	if [ $MS -eq 1 ]; then
	  tool_options="$tool_options --no-column-compression"
	fi
    fi

    cleanup
    initialize_db
    if [ "$1" = "rocksdb" ]; then
      ${mysqldir}/bin/ps-admin --enable-rocksdb -uroot -S${mysqldir}/socket.sock >/dev/null 2>&1
    fi

    if [[ "$1" = "pagetracking" ]]; then
        echo "Running test with page tracking enabled"
        BACKUP_PARAMS="--core-file --lock-ddl --page-tracking"
        "${mysqldir}"/bin/mysql -uroot -S"${mysqldir}"/socket.sock -e "INSTALL COMPONENT 'file://component_mysqlbackup';"
    fi

    run_load "${tool_options}"
    take_backup
    load_tool="${original_tool}"
}

run_pagetracking_encryption_tests() {
    local test_type="$1"
    local feature="$2"

    if [[ "${test_type}" == "encryption" ]]; then
      echo "Testing keyring_file..."
      for X in $(seq 1 10); do
        run_crash_tests_pstress "keyring_file" "" "$feature"
      done

      if ! source ./kmip_helper.sh; then
        echo "ERROR: Failed to load KMIP helper library"
        exit 1
      fi
      init_kmip_configs
      echo "Testing keyring_kmip with vault types..."
      for vault_type in "${!KMIP_CONFIGS[@]}"; do
        echo "Testing with $vault_type..."
        for X in $(seq 1 10); do
            run_crash_tests_pstress "keyring_kmip" "$vault_type" "$feature"
        done
      done
    fi
}

#############################################################################
# Page tracking read-efficiency regression tests (PXB-3853 / PXB-3862)
#
# Neither bug corrupts data, so the crash/load tests above never catch
# them -- both only make a --page-tracking incremental read far more of
# the tablespace than it needs to. These use a small, deterministic
# dataset instead of pstress/sysbench so read volume and read-request
# counts are assertable.
#
#   PXB-3853  range_get_next_page() left its iterator on the *next*
#             changed page instead of the last page of the current
#             contiguous block, so a single gap between two changed-page
#             blocks made the read batch span the whole gap -- reading
#             every unmodified page in between. Fixed in 8.4.0-7 / 9.7.1-2.
#             https://perconadev.atlassian.net/browse/PXB-3853
#
#   PXB-3862  scattered (but individually correct) changed pages produced
#             one tiny pread() per page instead of a few larger sequential
#             reads, which can make a page-tracking incremental slower
#             than a full scan. Fixed in 8.4.0-7 / 9.7.1-2 by
#             --page-tracking-merge-gap (default: auto).
#             https://perconadev.atlassian.net/browse/PXB-3862
#############################################################################

PT_READ_EFF_TABLE_ROWS=200000
PT_READ_EFF_PAGE_SIZE=16384

pt_mysql() {
  "${mysqldir}"/bin/mysql --no-defaults -uroot -S"${mysqldir}/socket.sock" "$@"
}

# Does this xtrabackup build expose --page-tracking-merge-gap (>= the
# PXB-3862 fix)? Probing --help is more robust than comparing version
# strings across major/minor branches.
pt_read_eff_supports_merge_gap() {
  "${xtrabackup_dir}"/xtrabackup --help 2>&1 | grep -q -- "--page-tracking-merge-gap"
}

# Sum the bytes actually read from $2 (a path substring, e.g. "t1.ibd")
# out of an strace -y -e trace=pread64 log at $1.
pt_read_eff_bytes_read_for_file() {
  local strace_log="$1" file_pattern="$2"
  grep "$file_pattern" "$strace_log" \
    | sed -E 's/.*, ([0-9]+)\) = ([0-9]+)$/\2/' \
    | awk '{s+=$1} END{print s+0}'
}

# Extract one field from xtrabackup's PXB-3862 grouping log line:
#   pagetracking: t1.ibd: <N> changed pages in <R> ranges (avg gap <G>
#   pages); merge-gap=<M> [(auto)] combined them into <K> reads: ...
#   issued <B> read batches
pt_read_eff_grouping_field() { # $1=log $2=sed-expr
  grep -m1 "pagetracking: .*t1.ibd: .* changed pages" "$1" | sed -E "$2"
}

pt_read_eff_issued_batches() { # $1=log
  pt_read_eff_grouping_field "$1" 's/.*issued ([0-9]+) read batches.*/\1/'
}

# Page tracking records a page as "changed" only when it is FLUSHED from
# the buffer pool, not when the UPDATE commits. Without this, a backup
# taken right after an UPDATE can see none (or only some) of the pages
# we just touched, making the assertions below meaningless. Force a full
# drain before every backup call.
pt_read_eff_flush_dirty_pages() {
  local dirty=-1 i
  pt_mysql -e "SET GLOBAL innodb_max_dirty_pages_pct_lwm = 0;" > /dev/null
  pt_mysql -e "SET GLOBAL innodb_max_dirty_pages_pct = 0;" > /dev/null
  for i in $(seq 1 120); do
    dirty=$(pt_mysql -Ns -e "SELECT VARIABLE_VALUE FROM performance_schema.global_status \
      WHERE VARIABLE_NAME = 'Innodb_buffer_pool_pages_dirty';")
    [ "${dirty}" -eq 0 ] && break
    sleep 1
  done
  pt_mysql -e "SET GLOBAL innodb_max_dirty_pages_pct = DEFAULT;" > /dev/null
  pt_mysql -e "SET GLOBAL innodb_max_dirty_pages_pct_lwm = DEFAULT;" > /dev/null
  [ "${dirty}" -eq 0 ] || { echo "ERR: buffer pool did not drain, ${dirty} dirty pages remain"; exit 1; }
}

# Fresh datadir, a 200k-row/~70MB single-table dataset (enough pages to
# make sparse/scattered changes meaningful), page tracking installed.
# Reuses this script's own cleanup/start_server so datadir/socket/port
# handling stays identical to the crash-test suite above.
pt_read_eff_init_dataset() {
  echo "################## Initializing read-efficiency dataset ##################"
  cleanup
  # cleanup()'s "kill -15 $MPID && wait $MPID" is unreliable here: $MPID
  # is an unquoted array (only its first element is used) and "wait"
  # only works on a job of this shell, not an arbitrary PID found via ps
  # -- so cleanup() can return with the old mysqld still alive, and the
  # next mysqld started against the same datadir corrupts InnoDB's redo
  # log (observed as "Unable to open './#innodb_redo/#ib_redoNN'").
  # Force it and confirm it is actually gone before proceeding.
  pkill -9 -f "mysqld.*--datadir=${datadir}" 2>/dev/null
  for i in $(seq 1 20); do
    pgrep -f "mysqld.*--datadir=${datadir}" > /dev/null 2>&1 || break
    sleep 1
  done
  if pgrep -f "mysqld.*--datadir=${datadir}" > /dev/null 2>&1; then
    echo "ERR: a previous mysqld against ${datadir} would not die"
    exit 1
  fi
  rm -f "${mysqldir}/socket.sock"
  MYSQLD_OPTIONS="--innodb_buffer_pool_size=256M --innodb_file_per_table=ON"
  mkdir -p "${datadir}"
  "${mysqldir}"/bin/mysqld --no-defaults --basedir="${mysqldir}" --datadir="${datadir}" \
    --initialize-insecure > "${logdir}/pt_read_eff_install_db.log" 2>&1
  start_server

  pt_mysql -e "INSTALL COMPONENT 'file://component_mysqlbackup';"
  pt_mysql -e "CREATE DATABASE IF NOT EXISTS test_pt;"
  pt_mysql -e "DROP TABLE IF EXISTS test_pt.t1;"
  pt_mysql -e "CREATE TABLE test_pt.t1 (id INT PRIMARY KEY AUTO_INCREMENT, pad CHAR(255)) ENGINE=InnoDB;"
  pt_mysql -e "
    SET SESSION cte_max_recursion_depth = ${PT_READ_EFF_TABLE_ROWS} + 10;
    INSERT INTO test_pt.t1 (pad)
    SELECT REPEAT('a',255) FROM (
      WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < ${PT_READ_EFF_TABLE_ROWS})
      SELECT n FROM seq
    ) x;
  "
  local rows
  rows=$(pt_mysql -Ns -e "SELECT COUNT(*) FROM test_pt.t1;")
  [ "$rows" -eq "${PT_READ_EFF_TABLE_ROWS}" ] || { echo "ERR: dataset load failed, got $rows rows"; exit 1; }
}

# PXB-3853: two changed pages, far apart, must not drag in everything
# between them.
test_pxb3853_sparse_read_amplification() {
  echo "################## PXB-3853: sparse changed-page read amplification ##################"

  if ! command -v strace > /dev/null 2>&1; then
    echo "SKIP: strace not available"
    return
  fi
  if [ "${PXB_VERSION:-0}" -lt "$(normalize_xtrabackup_version "8.4.0-7")" ]; then
    echo "SKIP: PXB-3853 requires PXB >= 8.4.0-7 (found ${PXB_VER:-unknown})"
    return
  fi

  pt_read_eff_init_dataset
  rm -rf "${backup_dir}"
  mkdir -p "${backup_dir}"

  pt_read_eff_flush_dirty_pages
  echo "=>Taking full backup with --page-tracking"
  if ! "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --socket="${mysqldir}/socket.sock" \
    --backup --target-dir="${backup_dir}/full" --datadir="${datadir}" --page-tracking \
    2> "${logdir}/pxb3853_full.log"; then
    echo "FAIL: PXB-3853 full backup failed. Log: ${logdir}/pxb3853_full.log"
    return 1
  fi

  echo "=>Updating first and last row only (2 changed pages, tens of MB apart)"
  pt_mysql -e "UPDATE test_pt.t1 SET pad=REPEAT('b',255) WHERE id=1;"
  pt_mysql -e "UPDATE test_pt.t1 SET pad=REPEAT('b',255) WHERE id=${PT_READ_EFF_TABLE_ROWS};"
  pt_read_eff_flush_dirty_pages

  # Use the strictest, most deterministic mode when available so the
  # PXB-3862 auto-merge heuristic (a legitimate, separate trade-off)
  # cannot muddy this assertion.
  local extra_args=""
  pt_read_eff_supports_merge_gap && extra_args="--page-tracking-merge-gap=0"

  echo "=>Taking incremental backup with --page-tracking, tracing reads on t1.ibd"
  if ! strace -f -y -e trace=pread64 -o "${logdir}/pxb3853_strace.log" \
    "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --socket="${mysqldir}/socket.sock" \
    --backup --target-dir="${backup_dir}/inc" --incremental-basedir="${backup_dir}/full" \
    --datadir="${datadir}" --page-tracking ${extra_args} \
    2> "${logdir}/pxb3853_inc.log"; then
    echo "FAIL: PXB-3853 incremental backup failed. Log: ${logdir}/pxb3853_inc.log"
    return 1
  fi

  local bytes_read ibd_size max_allowed
  bytes_read=$(pt_read_eff_bytes_read_for_file "${logdir}/pxb3853_strace.log" "t1.ibd")
  ibd_size=$(stat -c %s "${datadir}/test_pt/t1.ibd")
  # 2 changed pages plus a generous slack (8x + fixed overhead for the
  # small header/first-page reads xtrabackup always does) -- wide enough
  # to never false-fail on legitimate overhead, but two orders of
  # magnitude below what the unfixed bug reads (nearly the whole file).
  max_allowed=$(( 2 * PT_READ_EFF_PAGE_SIZE * 8 + 200000 ))

  echo "t1.ibd size: ${ibd_size} bytes; bytes read for incremental: ${bytes_read} bytes; max allowed: ${max_allowed} bytes"
  if [ "${bytes_read}" -gt "${max_allowed}" ]; then
    echo "FAIL: PXB-3853 regression -- read ${bytes_read} bytes for 2 changed pages" \
         "(tablespace is ${ibd_size} bytes). Expected <= ${max_allowed} bytes."
    return 1
  fi
  echo "PASS: read ${bytes_read} bytes for 2 changed pages (<= ${max_allowed})"
}

# PXB-3862: scattered changed pages must not cost one read request per
# page once --page-tracking-merge-gap is available; auto must never
# issue more read requests than the strict (merge-gap=0) baseline.
test_pxb3862_merge_gap() {
  echo "################## PXB-3862: scattered changed-page read batching ##################"

  if ! pt_read_eff_supports_merge_gap; then
    echo "SKIP: this xtrabackup build has no --page-tracking-merge-gap (pre-8.4.0-7/9.7.1-2)"
    return
  fi

  pt_read_eff_init_dataset
  rm -rf "${backup_dir}"
  mkdir -p "${backup_dir}"

  pt_read_eff_flush_dirty_pages
  echo "=>Taking full backup with --page-tracking"
  if ! "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --socket="${mysqldir}/socket.sock" \
    --backup --target-dir="${backup_dir}/full" --datadir="${datadir}" --page-tracking \
    2> "${logdir}/pxb3862_full.log"; then
    echo "FAIL: PXB-3862 full backup failed. Log: ${logdir}/pxb3862_full.log"
    return 1
  fi

  echo "=>Scattering changes evenly across the table (~1 in 180 rows, ~3 page gap)"
  pt_mysql -e "UPDATE test_pt.t1 SET pad=REPEAT('c',255) WHERE MOD(id,180)=1;"
  pt_read_eff_flush_dirty_pages

  echo "=>Incremental with --page-tracking-merge-gap=0 (strict, one read per range)"
  if ! "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --socket="${mysqldir}/socket.sock" \
    --backup --target-dir="${backup_dir}/inc_strict" --incremental-basedir="${backup_dir}/full" \
    --datadir="${datadir}" --page-tracking --page-tracking-merge-gap=0 \
    2> "${logdir}/pxb3862_strict.log"; then
    echo "FAIL: PXB-3862 strict incremental failed. Log: ${logdir}/pxb3862_strict.log"
    return 1
  fi
  local batches_strict
  batches_strict=$(pt_read_eff_issued_batches "${logdir}/pxb3862_strict.log")

  echo "=>Scattering a second, disjoint batch of changes the same way"
  pt_mysql -e "UPDATE test_pt.t1 SET pad=REPEAT('d',255) WHERE MOD(id,180)=2;"
  pt_read_eff_flush_dirty_pages

  echo "=>Incremental with default (auto) merge-gap"
  if ! "${xtrabackup_dir}"/xtrabackup --no-defaults --user=root --socket="${mysqldir}/socket.sock" \
    --backup --target-dir="${backup_dir}/inc_auto" --incremental-basedir="${backup_dir}/inc_strict" \
    --datadir="${datadir}" --page-tracking \
    2> "${logdir}/pxb3862_auto.log"; then
    echo "FAIL: PXB-3862 auto incremental failed. Log: ${logdir}/pxb3862_auto.log"
    return 1
  fi
  local batches_auto
  batches_auto=$(pt_read_eff_issued_batches "${logdir}/pxb3862_auto.log")

  if [ -z "${batches_strict}" ] || [ -z "${batches_auto}" ]; then
    echo "FAIL: could not find the pagetracking grouping log line" \
         "(need >= 1000 changed pages for it to print -- check the UPDATE stride)"
    return 1
  fi

  echo "issued read batches: strict(merge-gap=0)=${batches_strict}  auto=${batches_auto}"
  if [ "${batches_auto}" -gt "${batches_strict}" ]; then
    echo "FAIL: PXB-3862 regression -- auto issued more read requests than strict:" \
         "${batches_auto} > ${batches_strict}"
    return 1
  fi
  echo "PASS: auto issued ${batches_auto} read batches, strict issued ${batches_strict}" \
       "(auto never worse than strict)"
}

run_pagetracking_read_efficiency_tests() {
  local failures=0
  test_pxb3853_sparse_read_amplification || failures=$((failures+1))
  test_pxb3862_merge_gap || failures=$((failures+1))
  return "${failures}"
}

cleanup() {
  echo "################################## CleanUp #######################################"
  echo "Killing any previously running mysqld process"
  MPID=( $(ps -ef | grep -e mysqld | grep error.log | grep -v grep | awk '{print $2}') )
  {  kill -15 $MPID && wait $MPID; } 2>/dev/null

  if [ -d $mysqldir/data ]; then
    echo "=>Found previously existing data directory"
    rm -rf $mysqldir/data
    echo "..Deleted"
  fi

  if [ -f $mysqldir/bin/mysqld.my ]; then
    echo "=>Found older manifest file in mysql bin directory"
    rm -rf $mysqldir/bin/mysqld.my
    echo "..Deleted"
  fi
  if [ -f $mysqldir/lib/plugin/component_keyring_file.cnf ]; then
    echo "=>Found older keyring_component config file in lib/plugin directory"
    rm -rf $mysqldir/lib/plugin/component_keyring_file.cnf
    echo "..Deleted"
  fi
  if [ -f $mysqldir/lib/plugin/component_keyring_file ]; then
    echo "=>Found older keyring_component keyfile in lib/plugin directory"
    rm -rf $mysqldir/lib/plugin/component_keyring_file
    echo "..Deleted"
  fi

  containers_found=false
  if [ ${#KMIP_CONTAINER_NAMES[@]} -gt 0 ] 2>/dev/null; then
   get_kmip_container_names
   echo "Checking for previously started containers..."
   for name in "${KMIP_CONTAINER_NAMES[@]}"; do
      if docker ps -aq --filter "name=$name" | grep -q .; then
        containers_found=true
        break
      fi
   done
  fi

  if [[ "$containers_found" == true ]]; then
    echo "Killing previously started containers if any..."
    for name in "${KMIP_CONTAINER_NAMES[@]}"; do
        cleanup_existing_container "$name"
    done
  fi

 # Only cleanup vault directory if it exists
  if [[ -d "$HOME/vault" && -n "$HOME" ]]; then
    echo "Cleaning up vault directory..."
    sudo rm -rf "$HOME/vault"
  fi
}
trap cleanup EXIT INT TERM

if [ "$#" -lt 1 ]; then
    echo "This script tests backup with a load tool as pquery/pstress/sysbench"
    echo "Assumption: PS and PXB are already installed as tarballs"
    echo "Usage: "
    echo "1. Compile pquery/pstress with mysql"
    echo "2. Set variables in this script:"
    echo "   xtrabackup_dir, mysqldir, datadir, backup_dir, qascripts, logdir,"
    echo "   load_tool, tool_dir, num_tables, table_size, kmip, kms configuration"
    echo "3. Run the script as: $0 <Test Suites>"
    echo "   Test Suites: "
    echo "   Page_Tracking_tests"
    echo "   Page_Tracking_Read_Efficiency_tests"
    echo " "
    echo "   Example:"
    echo "   $0 Page_Tracking_tests"
    echo "   $0 Page_Tracking_Read_Efficiency_tests"
    echo " "
    echo "4. Logs are available at: $logdir"
    exit 1
fi

if [ ! -d $logdir ]; then
  mkdir $logdir
fi
if [ ! -d $logdir/pstress ]; then
  mkdir $logdir/pstress
else
  rm -rf $logdir/pstress
  mkdir $logdir/pstress
fi

echo "################################## Running Tests ##################################"
for tsuitelist in $*; do
  case "${tsuitelist}" in
    Page_Tracking_tests)
      if "${mysqldir}"/bin/mysqld --version | grep "5.7" >/dev/null 2>&1 ; then
        echo "Page Tracking is not supported in MS/PS 5.7, skipping tests"
        continue
      fi
      run_pagetracking_encryption_tests "encryption" "pagetracking"
      ;;
    Page_Tracking_Read_Efficiency_tests)
      if "${mysqldir}"/bin/mysqld --version | grep "5.7" >/dev/null 2>&1 ; then
        echo "Page Tracking is not supported in MS/PS 5.7, skipping tests"
        continue
      fi
      run_pagetracking_read_efficiency_tests || exit 1
      ;;
  esac
done
