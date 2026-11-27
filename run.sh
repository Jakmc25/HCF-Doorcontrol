#!/bin/bash
f=$1; W=/tmp/qa/run_$f; mkdir -p $W/door-control/data; cd $W/door-control
echo x>.env; echo '{"web":1}'>config.json
python3 -c "import sqlite3;sqlite3.connect('data/doorcontrol.db').execute('create table t(a)')"
sed "s#/srv/fair-apps#$W#g; s#sleep 5#:#" /tmp/qa/sb/block.sh > $W/block.sh
case $f in still) export SVC_STILL_ACTIVE=1 FAIL=;; none) export SVC_STILL_ACTIVE=0 FAIL=;; *) export SVC_STILL_ACTIVE=0 FAIL=$f;; esac
SB=$W LOG=$W/log PATH=/tmp/qa/sb/bin:$PATH bash $W/block.sh >$W/out 2>&1; echo "== $f exit $?"
grep -E "FAILED|restored|Stop" $W/out; grep -E "systemctl (enable|stop|disable)|compose stop|checkout" $W/log | tr '\n' ';'; echo
